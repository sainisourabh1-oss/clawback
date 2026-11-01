"""Outlook.com / Microsoft 365 through Microsoft Graph. EXPERIMENTAL.

Microsoft switched password (and app-password) sign-in off for Outlook.com on
16 September 2024, so this is the way to scan those mailboxes. One sign-in asks
for every permission below at once (ms_oauth.SIGN_IN_SCOPES), so cleanup never
stops to ask again; a token saved by an older version (Mail.Read only) asks
once more. What each permission is used for:

=================  ==========================  =====================================
capability         Graph permission            call
=================  ==========================  =====================================
mailto, send       Mail.Send                   POST /me/sendMail
trash/archive/spam Mail.ReadWrite              POST /me/messages/{id}/move
block              MailboxSettings.ReadWrite   POST /me/mailFolders/inbox/messageRules
=================  ==========================  =====================================

Sources (read 2026-10-25): learn.microsoft.com/en-us/graph/api/user-list-messages,
/message-move, /user-sendmail, /mailfolder-post-messagerules, /resources/mailfolder
(well-known folder names), /graph/outlook-immutable-id (Prefer:
IdType="ImmutableId": an id that stays the same when the message moves, so
undo finds it again), /graph/throttling (429 + Retry-After).

Message ids are immutable ids, so a moved message keeps its id; an "Undo"
moves it back to the Inbox.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .. import ms_oauth
from ..extract import from_bytes
from ..prefilter import needs_body
from ..progress import checkpoint
from .base import AuthError, Source, SourceError

CAPABILITY_SCOPES = {"mailto": ms_oauth.SEND, "send": ms_oauth.SEND, "trash": ms_oauth.READWRITE,
                     "archive": ms_oauth.READWRITE, "spam": ms_oauth.READWRITE, "block": ms_oauth.RULES}
SCOPE_WHY = {
    ms_oauth.SEND: "send email (only the unsubscribe/deletion emails you confirm)",
    ms_oauth.READWRITE: "move mail to Deleted Items/Archive/Junk (only the senders you confirm)",
    ms_oauth.RULES: "create an Outlook rule (only for the senders you confirm)",
}
REAUTH_WHY = ("Your Microsoft sign-in only allows reading. To clean up (unsubscribe by email, move mail to Deleted "
              "Items, block senders) Microsoft must ask you once more; Clawback asks for everything it needs in that "
              "one sign-in, so you will not be asked again.")
# Folders whose mail is never counted: your own sent mail, drafts, the
# outbox, deleted mail and junk (spam is shown, not counted, like Gmail).
SKIP_FOLDERS = ("sentitems", "drafts", "outbox", "deleteditems", "junkemail")
SELECT = ("id,internetMessageId,subject,from,sender,receivedDateTime,isRead,parentFolderId,conversationId,"
          "internetMessageHeaders")
PAGE = 100          # $top: Graph allows up to 1000; 100 keeps one page under a few hundred KB of headers
MAX_TRIES = 6
MAX_WAIT = 60       # cap on a single Retry-After wait


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OutlookSource(Source):
    kind = "outlook"

    def __init__(self, account: str, client_id: str | None = None, tenant: str = "common", signin=None,
                 graph: str = ms_oauth.GRAPH, on_auth_url=None, sleep=time.sleep):
        """``account``: the Microsoft address (the label everything is filed
        under; the sign-in must match it). ``client_id``: the user's own app
        registration; saved for next time."""
        client = ms_oauth.load_client() or {}
        cid = client_id or client.get("client_id")
        if not cid and signin is None:
            raise SourceError("Add your Application (client) ID first (README: Outlook.com).")
        self.signin = signin or ms_oauth.MicrosoftSignIn(cid, account, tenant or client.get("tenant", "common"),
                                                         on_auth_url=on_auth_url)
        super().__init__(self.signin.account)
        self.graph, self.sleep = graph.rstrip("/"), sleep
        self.capabilities = {"one-click", "link", "mailto", "send", "trash", "archive", "spam", "block"}
        self.bodies = 0
        self.reused = 0
        self.capped = False
        self._folder_ids: dict[str, str] = {}

    # ------------------------------------------------------------ permissions
    def connect(self) -> None:
        """Sign in (read permission only) if there is no usable saved token."""
        self.signin.access_token()

    def missing_permission(self, capability: str) -> str | None:
        scope = CAPABILITY_SCOPES.get(capability)
        if not scope or self.signin.has(scope):
            return None
        return REAUTH_WHY

    def grant(self, capability: str | None = None) -> None:
        """Sign in again with every permission at once (one consent screen)."""
        self.signin.access_token(set(ms_oauth.SIGN_IN_SCOPES))

    def _scope_for(self, capability: str | None) -> set[str]:
        return {CAPABILITY_SCOPES[capability]} if capability else set()

    # ------------------------------------------------------------ http
    def _call(self, method: str, path: str, body=None, capability: str | None = None, raw: bool = False,
              expect=(200,)):
        url = path if path.startswith("http") else self.graph + path
        refreshed = force = False
        for attempt in range(MAX_TRIES):
            token = self.signin.access_token(self._scope_for(capability), force_refresh=force)
            force = False
            headers = {"Authorization": "Bearer " + token, "Prefer": 'IdType="ImmutableId"', "Accept": "application/json"}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    payload = r.read()
                    if r.status not in expect:
                        raise SourceError(f"Outlook answered {r.status} for {method} {path.split('?')[0]}")
                    if raw:
                        return payload
                    return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    err = json.loads(exc.read() or b"{}").get("error", {})
                    detail = f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
                except ValueError:
                    pass
                if exc.code == 401 and not refreshed:
                    refreshed = force = True  # expired or revoked: refresh once, then give up
                    continue
                if exc.code == 401:
                    raise AuthError("Microsoft no longer accepts the sign-in for this mailbox (revoked or expired). "
                                    "Sign in with Microsoft again.") from None
                if exc.code == 403:
                    if capability and CAPABILITY_SCOPES.get(capability):
                        raise AuthError(f"Outlook refused: Clawback needs permission to {SCOPE_WHY[CAPABILITY_SCOPES[capability]]}. "
                                        f"({detail})") from None
                    raise AuthError(f"Outlook refused access to this mailbox ({detail}). Work and school accounts: "
                                    "your organisation may block apps it has not approved.") from None
                if exc.code in {429, 500, 502, 503, 504} and attempt < MAX_TRIES - 1:
                    try:
                        wait = float(exc.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        wait = 0
                    self.sleep(min(MAX_WAIT, wait or 2 ** attempt))
                    continue
                if exc.code == 404:
                    raise SourceError(f"Outlook could not find that item ({detail or 'not found'}). It may have been "
                                      "moved or deleted in another app.") from None
                raise SourceError(f"Outlook error {exc.code} ({detail})") from None
            except (urllib.error.URLError, OSError) as exc:
                raise SourceError(f"Could not reach Outlook: {getattr(exc, 'reason', exc)}") from None
        raise SourceError("Outlook kept asking Clawback to slow down; try again in a few minutes.")

    def _folder_id(self, well_known: str) -> str | None:
        if well_known not in self._folder_ids:
            try:
                self._folder_ids[well_known] = self._call("GET", f"/me/mailFolders/{well_known}?$select=id")["id"]
            except SourceError as exc:
                if isinstance(exc, AuthError):
                    raise
                self._folder_ids[well_known] = ""   # this mailbox has no such folder (e.g. no Archive yet)
        return self._folder_ids[well_known] or None

    # ------------------------------------------------------------ reading
    @staticmethod
    def _headers_bytes(m: dict) -> bytes:
        lines = []
        for hd in m.get("internetMessageHeaders") or []:
            name, value = (hd.get("name") or "").strip(), (hd.get("value") or "").replace("\r", " ").replace("\n", " ")
            if name and ":" not in name:
                lines.append(f"{name}: {value}")
        names = {ln.split(":", 1)[0].lower() for ln in lines}
        # Mail without internet headers (sent inside the same organisation):
        # build the basics from Graph's own fields.
        sender = (m.get("from") or m.get("sender") or {}).get("emailAddress") or {}
        if "from" not in names and sender.get("address"):
            lines.append(f'From: "{(sender.get("name") or "").replace(chr(34), "")}" <{sender["address"]}>')
        if "subject" not in names:
            lines.append(f"Subject: {(m.get('subject') or '').replace(chr(10), ' ')}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8", errors="replace")

    def _info(self, m: dict, raw: bytes | None = None):
        received = None
        if m.get("receivedDateTime"):
            try:
                received = datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00"))
            except ValueError:
                received = None
        info = from_bytes(raw or self._headers_bytes(m), m["id"], unread=not m.get("isRead", True),
                          folder=m.get("parentFolderId", ""), labels=set())
        info.thread_id = m.get("conversationId", "") or info.thread_id
        if received:
            info.date = received   # the server's receive time, like Gmail's internal date
        return info

    def iter_messages(self, limit: int | None = None, progress=None, cancel=None, since: datetime | None = None,
                      skip_ids=None):
        """Newest first across the whole mailbox except Sent, Drafts, Outbox,
        Deleted Items and Junk. Headers come with the list (one request per
        100 messages); the full message only for possible receipts."""
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        skip = set(skip_ids or ())
        skip_folders = {fid for fid in (self._folder_id(n) for n in SKIP_FOLDERS) if fid}
        params = {"$select": SELECT, "$top": str(PAGE), "$orderby": "receivedDateTime desc"}
        if since is not None:
            params["$filter"] = f"receivedDateTime ge {_iso(since)}"
        url = "/me/messages?" + urllib.parse.urlencode(params, safe="$,: ")
        url = url.replace(" ", "%20")
        done = yielded = 0
        self.bodies = self.reused = 0
        self.capped = False
        if progress:
            progress(0, None, phase="reading")
        while url:
            checkpoint(cancel)
            page = self._call("GET", url)
            for m in page.get("value", []):
                if m.get("parentFolderId") in skip_folders:
                    continue
                # the limit counts the newest emails, those an earlier scan already
                # read included: "the newest N" means the same N on every scan
                if limit is not None and done >= limit:
                    self.capped = True
                    return
                done += 1
                if m["id"] in skip:
                    self.reused += 1
                    continue
                if "internetMessageHeaders" not in m:
                    m = {**m, **self._call("GET", f"/me/messages/{urllib.parse.quote(m['id'])}?$select={SELECT}")}
                info = self._info(m)
                if needs_body(info):
                    raw = self._call("GET", f"/me/messages/{urllib.parse.quote(m['id'])}/$value", raw=True)
                    info = self._info(m, raw)
                    self.bodies += 1
                if since is not None and info.date is not None and info.date < since:
                    continue
                yielded += 1
                if progress:
                    progress(done, None, current_date=info.date)
                yield info
            url = page.get("@odata.nextLink")

    def fetch_body_links(self, ids, cancel=None):
        from ..extract import from_bytes
        out = {}
        for mid in ids:
            checkpoint(cancel)
            try:
                raw = self._call("GET", f"/me/messages/{urllib.parse.quote(mid)}/$value", raw=True)
            except AuthError:
                raise
            except SourceError:
                continue   # deleted since the scan
            out[mid] = from_bytes(raw, mid).body_unsub
        return out

    # ------------------------------------------------------------ actions
    def send_email(self, to: str, subject: str, body: str) -> dict:
        self._call("POST", "/me/sendMail", {"message": {"subject": subject, "body": {"contentType": "Text", "content": body},
                                                        "toRecipients": [{"emailAddress": {"address": to}}]},
                                            "saveToSentItems": True}, capability="send", expect=(202,))
        return {"message_id": None}

    def _move(self, ids: list[str], destination: str, capability: str) -> list[str]:
        moved = []
        for mid in ids:
            res = self._call("POST", f"/me/messages/{urllib.parse.quote(mid)}/move", {"destinationId": destination},
                             capability=capability, expect=(200, 201))
            moved.append(res.get("id") or mid)
        return moved

    def trash(self, ids: list[str]) -> dict:
        return {"op": "untrash", "ids": self._move(ids, "deleteditems", "trash")}

    def untrash(self, ids: list[str]) -> None:
        self._move(ids, "inbox", "trash")

    def archive(self, ids: list[str]) -> dict:
        if not self._folder_id("archive"):
            raise SourceError("This mailbox has no Archive folder yet. Archive one email in Outlook once, then try again.")
        return {"op": "unarchive", "ids": self._move(ids, "archive", "archive")}

    def unarchive(self, ids: list[str]) -> None:
        self._move(ids, "inbox", "archive")

    def report_spam(self, ids: list[str]) -> dict:
        return {"op": "unreport_spam", "ids": self._move(ids, "junkemail", "spam")}

    def unreport_spam(self, ids: list[str]) -> None:
        self._move(ids, "inbox", "spam")

    def create_filter(self, address: str, mode: str) -> dict:
        if mode not in {"archive", "trash"}:
            raise ValueError("filter mode must be archive or trash")
        self.signin.access_token({ms_oauth.RULES})
        target = self._folder_id("archive" if mode == "archive" else "deleteditems")
        if not target:
            raise SourceError("This mailbox has no Archive folder yet. Archive one email in Outlook once, then try again.")
        rule = self._call("POST", "/me/mailFolders/inbox/messageRules",
                          {"displayName": f"Clawback: {mode} mail from {address}"[:255], "sequence": 1, "isEnabled": True,
                           "conditions": {"fromAddresses": [{"emailAddress": {"address": address}}]},
                           "actions": {"moveToFolder": target, "stopProcessingRules": True}},
                          capability="block", expect=(200, 201))
        return {"op": "delete_filter", "filter_id": rule.get("id")}

    def delete_filter(self, filter_id: str) -> None:
        self._call("DELETE", f"/me/mailFolders/inbox/messageRules/{urllib.parse.quote(filter_id)}",
                   capability="block", expect=(204, 200))

    def describe(self) -> dict:
        d = super().describe()
        d.update(provider="outlook", auth="microsoft", label="experimental",
                 granted=sorted(self.signin.granted() - {"openid", "email", "offline_access", "profile"}))
        return d
