"""Gmail API source. Every permission is asked for in ONE Google sign-in.

Each Google sign-in asks for the two scopes Clawback can use (SIGN_IN_SCOPES),
so cleaning up later never needs a second sign-in:

=====================  ======================  =======================================================
scope                  Google's class          what Clawback does with it
=====================  ======================  =======================================================
gmail.modify           Restricted              read mail (scan); move mail to Trash / Archive / Spam and
                                               back (Undo); send an unsubscribe email you confirmed.
                                               Cannot delete mail for good (Google: "does not allow
                                               immediate, permanent deletion"), unlike mail.google.com.
gmail.settings.basic   Restricted              create / delete a filter for a sender you chose to block
=====================  ======================  =======================================================

gmail.modify covers gmail.readonly and gmail.send (messages.list/get/send all
accept it; Gmail API reference, read 2026-10-29), so those two are never asked
for separately any more. A token saved by an older version (gmail.readonly
only) still scans; the first cleanup action says once, plainly, that Google
must be asked again, and that one sign-in asks for the full set.
Sources (read 2026-10-29): developers.google.com/workspace/gmail/api/auth/scopes
(updated 2026-09-10), .../reference/rest/v1/users.messages/send (2026-04-15),
.../users.messages/batchModify (2026-05-06).
"""
from __future__ import annotations

import base64
import random
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from .. import config
from ..extract import from_gmail_message
from ..prefilter import SUBJECT_SEARCH_WORDS, needs_body
from ..progress import checkpoint
from .base import AuthError, Source, SourceError
from .pacing import DEFAULT_QUOTA_PER_MINUTE, UNITS, Pacer

READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SEND = "https://www.googleapis.com/auth/gmail.send"
MODIFY = "https://www.googleapis.com/auth/gmail.modify"
SETTINGS = "https://www.googleapis.com/auth/gmail.settings.basic"
# IMAP/SMTP with OAuth (XOAUTH2) needs this one scope. Google's scope page
# (updated 2026-09-10) lists it as Restricted: "read, compose, send, and
# permanently delete all your email". Opt-in only; Clawback only reads with it.
FULL_MAIL = "https://mail.google.com/"
FULL_MAIL_WARNING = ("Full mailbox access: signing in this way gives Clawback Google's https://mail.google.com/ "
                     "permission, which can read, send and permanently delete all your Gmail. Clawback only reads "
                     "with it, but the read-only sign-in or an app password asks for less. Revoke it any time at "
                     "myaccount.google.com/permissions.")
CAPABILITY_SCOPES = {"mailto": MODIFY, "send": MODIFY, "trash": MODIFY, "archive": MODIFY, "spam": MODIFY, "block": SETTINGS}
# What every Google sign-in asks for, in one consent screen.
SIGN_IN_SCOPES = frozenset({MODIFY, SETTINGS})
# A granted scope that also allows another one (Gmail API reference).
IMPLIES = {MODIFY: {READONLY, SEND}, FULL_MAIL: {READONLY, SEND, MODIFY}}
SCOPE_WHY = {
    SEND: "send email (only the unsubscribe/deletion emails you confirm)",
    MODIFY: "move mail to Trash, Archive or Spam and send unsubscribe emails (only for the senders you confirm)",
    SETTINGS: "create a Gmail filter (only for the senders you confirm)",
}
# Shown when a token from an older version can only read (one sentence, plain).
REAUTH_WHY = ("Your Google sign-in only allows reading. To clean up (unsubscribe by email, move mail to Trash, "
              "block senders) Google must ask you once more; Clawback asks for everything it needs in that one "
              "sign-in, so you will not be asked again.")


def covered(granted, wanted) -> bool:
    """Every wanted scope is granted, directly or through a broader one."""
    have = set(granted or ())
    for g in list(have):
        have |= IMPLIES.get(g, set())
    return set(wanted) <= have
RETRY_STATUSES = {429, 500, 502, 503, 504}
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
PERMISSION_REASONS = {"insufficientPermissions", "PERMISSION_DENIED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}
MAX_TRIES = 6
# Five minutes covers reading the consent screen and a 2-Step Verification
# prompt; after that the local listener closes instead of waiting forever.
CONSENT_TIMEOUT = 300
# Gmail batch guide (updated 2026-09-10): at most 100 calls per batch, and
# "we recommend sending batches of no more than 50 requests".
BATCH_SIZE = 50
# Smart order: one cheap list query (5 units per 500 ids) finds the mail most
# likely to matter (Gmail's Promotions/Updates/Social tabs and billing words),
# which is read first so subscriptions and newsletters show up in the partial
# results within the first minute; everything else follows. Braces = OR.
PRIORITY_QUERY = ("{category:promotions category:updates category:social unsubscribe receipt invoice "
                  "subscription renewal trial autopay mandate}")
ORDER_NOTE = "Likely subscriptions and newsletters first, then everything else"
# Share of messages that need a full body when nothing better is known (scans
# too small for the BODY_QUERY count): 10%, the receipts-and-billing share of
# the benchmark mailbox. Replaced by the measured share after MEASURED messages.
BODY_SHARE_PRIOR = 0.10
# Billing words in the subject: one cheap list query (5 units per 500 ids)
# counts the messages needs_body() will most likely pick, so the first time
# estimate knows how many 60-unit bodies are coming. A fixed share cannot: the
# benchmark mailbox needs bodies for 10% of its mail, the smart-order test
# mailbox for 0.2%; "10% of the priority mail" put the benchmark's first ETA
# 39% low, a flat 10% put the test mailbox's 27% high. With the count: -1%.
BODY_QUERY = "subject:{" + " ".join(SUBJECT_SEARCH_WORDS) + "}"
MEASURED = 100
# Largest single pacer.take(), as a fraction of the sliding-window budget. The
# pacer can only start a request whose units fit in what is left of the
# window, so a big request leaves part of every window unused: 50 full bodies
# (3,000 units) in a 5,700-unit window spent ~4,000 units a minute (16.1 vs
# 14.1 min on scripts/bench_scan.py). Measured on that bench, 3,000 messages,
# simulated min, 0 rate-limit errors in every row:
#   1/1 16.12  1/2 16.12  1/3 16.11  1/4 15.11  1/6 13.12  1/8 13.12
#   1/10 13.13  1/12 14.09  1/16 14.10  1/24 14.08  1/32 13.21
# Smaller is not always better: a scan ends on a window step, and 1/12-1/24
# waste just enough per window to need one more step (1/16 also did at 4,000
# messages). 1/6, 1/8 and 1/10 hit the lowest step at every size tried (500,
# 1,000, 2,000, 3,000, 4,000, 6,000); 1/8 is the middle of that band, so a
# different header/body mix does not push it to an edge. At the default budget
# that is 35 metadata or 11 full ids per request, fewer automatically while a
# 429 has halved the budget.
TAKE_FRACTION = 1 / 8
# Only the headers the classifiers read (format=metadata returns just these).
METADATA_HEADERS = ["From", "Subject", "Date", "List-Unsubscribe", "List-Unsubscribe-Post", "List-Id", "Precedence",
                    "Authentication-Results", "ARC-Authentication-Results", "Reply-To", "Message-ID"]


def _gmail_query(query: str, since: datetime | None = None) -> str:
    """Gmail search for the scan window. Gmail reads plain dates in after: as
    midnight Pacific time, so the window uses epoch seconds instead (exact in
    any time zone), one second early because the operator's inclusiveness is
    not documented; messages before ``since`` are then dropped client-side."""
    q = (query or "").strip()
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        q = f"{q} after:{int(since.timestamp()) - 1}".strip()
    if "in:spam" not in q and "in:trash" not in q:
        q = f"{q} -in:spam -in:trash".strip()
    return q


def _status_of(exc) -> int | None:
    return getattr(getattr(exc, "resp", None), "status", None)


def _reason_of(exc) -> str:
    try:
        import json
        data = json.loads(exc.content.decode("utf-8"))
        errors = data.get("error", {}).get("errors", [])
        return errors[0].get("reason", "") if errors else data.get("error", {}).get("status", "")
    except (AttributeError, ValueError, UnicodeError, IndexError):
        return ""


class GmailSource(Source):
    kind = "gmail"

    def __init__(self, account: str = "default", service=None, query: str = "",
                 sleep=None, token_file: Path | None = None, client_secret: Path | None = None,
                 granted: set[str] | None = None, flow_runner=None, clock=None, pacer: Pacer | None = None,
                 on_auth_url=None):
        super().__init__(account)
        self._clock = clock or time.monotonic
        self.pacer = pacer
        self.on_auth_url = on_auth_url
        self.capped = False
        self.unread_refresh: dict[str, bool] = {}
        self.last_auth_url: str | None = None
        self.query = query
        self._service = service
        self._sleep = sleep or time.sleep
        self.token_file = token_file or config.token_path(account)
        self.client_secret = client_secret or config.client_secret_path()
        self._granted = set(granted) if granted is not None else None
        self._flow_runner = flow_runner
        self.capabilities = {"one-click", "link", "mailto", "send", "trash", "archive", "spam", "block"}

    # ------------------------------------------------------------ auth
    def granted_scopes(self) -> set[str]:
        if self._granted is not None:
            return self._granted
        if not self.token_file.exists():
            return set()
        try:
            import json
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
            scopes = data.get("scopes") or []
            return set(scopes.split() if isinstance(scopes, str) else scopes)
        except (OSError, ValueError):
            return set()

    def missing_permission(self, capability: str) -> str | None:
        scope = CAPABILITY_SCOPES.get(capability)
        if not scope or covered(self.granted_scopes(), {scope}):
            return None
        return REAUTH_WHY

    def can_clean_up(self) -> bool:
        """The saved sign-in already allows every cleanup action."""
        return covered(self.granted_scopes(), SIGN_IN_SCOPES)

    def grant(self, capability: str | None = None) -> None:
        """Sign in again asking for the full set (one consent screen), whatever
        action asked: after this no action needs another sign-in."""
        if self.can_clean_up():
            return  # already allowed (e.g. a second label of the same mailbox): no second Google window
        self._service = None
        self._credentials(set(SIGN_IN_SCOPES), force_consent=True)

    def _credentials(self, scopes: set[str], force_consent: bool = False):
        try:
            from google.auth.exceptions import RefreshError
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise SourceError("Gmail support needs the optional extra: pip install \"clawback[gmail]\"") from exc
        if not self.client_secret.exists():
            raise AuthError(
                f"Missing {self.client_secret}. Create a Desktop OAuth client in Google Cloud Console and save its JSON "
                "there (README: 'Connect Gmail'). No Google Cloud project? Import a Google Takeout .mbox instead.")
        creds = None
        if self.token_file.exists() and not force_consent:
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_file))
            except (ValueError, OSError):
                creds = None  # corrupt token file: fall through to a fresh consent
        if creds and not covered(creds.scopes or [], scopes):
            creds = None
        if creds and not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError:
                    # Review bug 8: an expired/revoked grant needs a new consent,
                    # not IMAP advice. Remove the dead token and ask again.
                    try:
                        self.token_file.unlink()
                    except OSError:
                        pass
                    creds = None
            else:
                creds = None
        if not creds:
            # Any Google sign-in asks for the whole set at once, so the user
            # never meets a second consent screen for cleanup later.
            want = set(SIGN_IN_SCOPES) | {s for s in scopes if not covered(SIGN_IN_SCOPES, {s})}
            run = self._flow_runner or (lambda: self._consent(InstalledAppFlow, want))
            try:
                creds = run()
            except AuthError:
                raise
            except Exception as exc:  # the flow surfaces many library-specific errors
                if type(exc).__name__ == "WSGITimeoutError":
                    raise AuthError(f"Google sign-in timed out after {CONSENT_TIMEOUT // 60} minutes with no answer. "
                                    "Nothing was saved; start the Gmail connection again.") from exc
                raise AuthError("Google sign-in did not complete. Try again; if your Google Cloud app is in "
                                "'Testing', add your address as a test user. "
                                f"Technical details: {type(exc).__name__}: {exc}") from exc
            config.write_private(self.token_file, creds.to_json())
        self._granted = set(creds.scopes or scopes)
        return creds

    def _consent(self, flow_cls, scopes):
        """Loopback consent (PKCE, 127.0.0.1, random port). The sign-in URL is
        always handed to ``on_auth_url`` (the CLI prints it, the web app shows
        it as a link) in case the browser does not open by itself."""
        import os
        import webbrowser
        # Google adds scopes granted earlier (include_granted_scopes), e.g. an old
        # gmail.readonly. oauthlib would reject that reply as "Scope has changed"
        # and the approval would never be saved, so the user saw the consent
        # window again and again. Accept the wider reply; the scopes are checked below.
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        flow = flow_cls.from_client_secrets_file(str(self.client_secret), sorted(scopes))
        original = flow.authorization_url

        def authorization_url(**kw):
            url, state = original(**kw)
            self.last_auth_url = url
            if self.on_auth_url:
                self.on_auth_url(url)
            try:
                webbrowser.open(url, new=1, autoraise=True)
            except webbrowser.Error:
                pass  # the printed/shown link is the fallback
            return url, state

        flow.authorization_url = authorization_url
        creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=False, prompt="consent",
                                      include_granted_scopes="true", authorization_prompt_message="",
                                      timeout_seconds=CONSENT_TIMEOUT)
        granted = (getattr(getattr(flow, "oauth2session", None), "token", None) or {}).get("scope")             or getattr(creds, "scopes", None) or []
        granted = set(granted.split() if isinstance(granted, str) else granted)
        if granted and not covered(granted, scopes):
            raise AuthError("Google sign-in finished, but not every box was ticked on Google's page. "
                            "Start again and leave all the permission boxes ticked.")
        if granted and hasattr(creds, "_scopes"):
            creds._scopes = sorted(granted)
        return creds

    def service(self):
        if self._service is None:
            from googleapiclient.discovery import build
            creds = self._credentials({READONLY} | (self.granted_scopes() & set(CAPABILITY_SCOPES.values())))
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    # ------------------------------------------------------------ retries
    def _execute(self, request):
        """Execute with exponential backoff on 429/5xx and Gmail rate-limit 403s,
        honouring Retry-After. Auth errors become a clear AuthError."""
        for attempt in range(MAX_TRIES):
            try:
                return request.execute()
            except Exception as exc:
                status = _status_of(exc)
                reason = _reason_of(exc)
                if status == 401:
                    raise AuthError("Gmail rejected the saved sign-in (expired or revoked). Reconnect Gmail to sign in again.") from exc
                if status == 403 and reason in PERMISSION_REASONS:
                    raise AuthError("This action needs a Gmail permission you have not granted yet.") from exc
                retry = status in RETRY_STATUSES or (status == 403 and reason in RATE_REASONS)
                if not retry or attempt == MAX_TRIES - 1:
                    raise
                wait = min(32.0, 2 ** attempt + random.random())
                header = getattr(getattr(exc, "resp", None), "get", lambda *_: None)("retry-after")
                if header and str(header).isdigit():
                    wait = min(60.0, float(header))
                self._sleep(wait)
        raise SourceError("unreachable")

    # ------------------------------------------------------------ scan
    def _pacer(self) -> Pacer:
        if self.pacer is None:
            quota = config.load_settings().get("gmail_quota_per_minute") or DEFAULT_QUOTA_PER_MINUTE
            self.pacer = Pacer(float(quota), clock=self._clock, sleep=self._sleep)
        return self.pacer

    def _list_ids(self, svc, q: str, cancel, progress=None, cap: int | None = None) -> list[str]:
        ids: list[str] = []
        token = None
        while True:
            checkpoint(cancel)
            self._pacer().take(UNITS["list"])
            resp = self._execute(svc.users().messages().list(userId="me", q=q, maxResults=500, pageToken=token))
            self._pacer().settle()
            ids.extend(m["id"] for m in resp.get("messages", []) or [])
            if progress:
                progress(0, None, phase="counting", counted=len(ids))
            token = resp.get("nextPageToken")
            if not token or (cap is not None and len(ids) > cap):
                return ids

    def _per_request(self, fmt: str) -> int:
        """Ids per batch request so that one pacer.take() is at most
        TAKE_FRACTION of the current budget (1..BATCH_SIZE)."""
        cost = UNITS["full" if fmt == "full" else "metadata"]
        return max(1, min(BATCH_SIZE, int(self._pacer().budget * TAKE_FRACTION // cost)))

    def _batch_get(self, svc, ids: list[str], fmt: str, cancel) -> dict[str, dict]:
        """messages.get for any number of ids: batch requests of
        _per_request(fmt) ids, each paced to the quota. Rate-limited
        sub-requests go back to the front of the queue after a backoff and
        are re-split at the (now halved) budget, so a retry is never a bigger
        take than the budget allows; MAX_TRIES throttled rounds in a row end
        the scan with a clear message."""
        out: dict[str, dict] = {}
        pending = list(ids)
        throttled_rounds = 0
        while pending:
            n = self._per_request(fmt)   # re-read every time: a 429 shrinks the budget
            part, pending = pending[:n], pending[n:]
            retry, after = self._batch_request(svc, part, fmt, cancel, out)
            if retry:
                if throttled_rounds == MAX_TRIES - 1:
                    raise SourceError("Gmail kept rate-limiting this scan. Wait a few minutes and scan again; "
                                      "results so far are kept.")
                self._pacer().throttled()
                self._sleep(max(after) if after else min(32.0, 2 ** throttled_rounds + random.random()))
                throttled_rounds += 1
                pending = retry + pending
            else:
                throttled_rounds = 0
                self._pacer().success()
        return out

    def _batch_request(self, svc, ids: list[str], fmt: str, cancel, out: dict) -> tuple[list[str], list[float]]:
        """One paced HTTP batch of messages.get (at most BATCH_SIZE ids).
        Answers go into ``out``; returns the rate-limited ids and any
        Retry-After values. Other failures raise."""
        checkpoint(cancel)
        self._pacer().take(UNITS["full" if fmt == "full" else "metadata"] * len(ids))
        retry: list[str] = []
        fatal: list[Exception] = []
        after: list[float] = []

        def callback(request_id, response, exception):
            if exception is None:
                out[request_id] = response
                return
            status, reason = _status_of(exception), _reason_of(exception)
            if status == 404:
                return   # deleted between list and get
            if status in RETRY_STATUSES or (status == 403 and reason in RATE_REASONS):
                retry.append(request_id)
                header = getattr(getattr(exception, "resp", None), "get", lambda *_: None)("retry-after")
                if header and str(header).isdigit():
                    after.append(min(60.0, float(header)))
            else:
                fatal.append(exception)

        batch = svc.new_batch_http_request(callback=callback)
        for mid in ids:
            kw = {"userId": "me", "id": mid, "format": fmt}
            if fmt == "metadata":
                kw["metadataHeaders"] = METADATA_HEADERS
            batch.add(svc.users().messages().get(**kw), request_id=mid)
        try:
            self._execute(batch)
        except AuthError:
            raise
        except Exception as exc:  # the whole batch failed after _execute's own retries
            raise SourceError(f"Gmail did not answer. Try again in a few minutes. "
                              f"Technical details: {type(exc).__name__}: {exc}") from exc
        self._pacer().settle()
        if fatal:
            exc = fatal[0]
            if _status_of(exc) == 401:
                raise AuthError("Gmail rejected the saved sign-in (expired or revoked). Reconnect Gmail to sign in again.") from exc
            if _status_of(exc) == 403 and _reason_of(exc) in PERMISSION_REASONS:
                raise AuthError("This scan needs Gmail read permission. Reconnect Gmail.") from exc
            raise SourceError("Gmail refused a request. Try again in a few minutes; if it keeps happening, "
                              f"connect Gmail again. Technical details: {type(exc).__name__}: {exc}") from exc
        return retry, after

    def iter_messages(self, limit: int | None = None, progress=None, cancel=None, since: datetime | None = None,
                      skip_ids=None):
        """Newest first. Headers for every message (format=metadata, 20 quota
        units), full bodies only for billing candidates (60 units each).
        ``skip_ids``: messages already saved by an earlier scan; they are not
        fetched again (incremental rescan), only their read/unread state is
        refreshed from two cheap list queries."""
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        svc = self.service()
        q = _gmail_query(self.query, since)
        cap = limit
        ids = self._list_ids(svc, q, cancel, progress, cap)
        self.capped = cap is not None and len(ids) > cap
        if cap is not None:
            ids = ids[:cap]
        skip = set(skip_ids or ())
        known = [i for i in ids if i in skip]
        todo = [i for i in ids if i not in skip]
        self.listed, self.reused = len(ids), len(known)
        if known:
            unread = set(self._list_ids(svc, q + " is:unread", cancel))
            self.unread_refresh = {i: (i in unread) for i in known}
        self.order_note = ""
        n_first = 0
        hinted: set[str] | None = None       # ids whose subject has a billing word
        if len(todo) > 2 * BATCH_SIZE:
            try:
                first = set(self._list_ids(svc, q + " " + PRIORITY_QUERY, cancel))
                hinted = set(self._list_ids(svc, q + " " + BODY_QUERY, cancel)) & set(todo)
            except AuthError:
                raise
            except Exception:  # noqa: BLE001 - ordering and the estimate are optimisations, never a failure
                first = set()
            if first and len(first) < len(todo):
                todo = [i for i in todo if i in first] + [i for i in todo if i not in first]
                n_first = sum(1 for i in todo if i in first)
                self.order_note = ORDER_NOTE
        total = len(todo)
        pacer = self._pacer()
        self.bodies = 0
        hinted_bodies = [0]
        hinted_upto = [0]                    # hinted_upto[k] = hinted ids among todo[:k]
        for mid in todo:
            hinted_upto.append(hinted_upto[-1] + (1 if hinted and mid in hinted else 0))

        def eta(done: int) -> float:
            """Seconds left, from quota units left: the API path is quota-bound,
            so this is steadier than a rate estimate (6,000 units/min default);
            Pacer.seconds_for turns units into time window by window.

            Full bodies left: every message the BODY_QUERY count found is
            expected to need one, the others none, until MEASURED of each kind
            are read and their measured shares take over. Without that count
            (small scans, or the query failed) BODY_SHARE_PRIOR of everything,
            then the measured share."""
            if hinted is None:
                share = self.bodies / done if done >= MEASURED else BODY_SHARE_PRIOR
                bodies_left = (total - done) * share
            else:
                read_h = hinted_upto[done]
                read_o = done - read_h
                share_h = hinted_bodies[0] / read_h if read_h >= MEASURED else 1.0
                share_o = (self.bodies - hinted_bodies[0]) / read_o if read_o >= MEASURED else 0.0
                bodies_left = ((hinted_upto[total] - read_h) * share_h
                               + (total - hinted_upto[total] - read_o) * share_o)
            return pacer.seconds_for((total - done) * UNITS["metadata"] + bodies_left * UNITS["full"])
        if progress:
            progress(0, total, phase="reading", eta_hint=eta(0), note=self.order_note)
        done = 0
        start = 0
        while start < total:
            # one metadata request per step (its size follows the budget); the
            # chunk never straddles the end of the priority part
            end = min(total, start + self._per_request("metadata"))
            if start < n_first:
                end = min(end, n_first)
            chunk = todo[start:end]
            start = end
            meta = self._batch_get(svc, chunk, "metadata", cancel)
            infos = {mid: from_gmail_message(meta[mid]) for mid in chunk if mid in meta}
            wanted = [mid for mid, info in infos.items() if needs_body(info)]
            if wanted:
                full = self._batch_get(svc, wanted, "full", cancel)
                for mid in wanted:
                    if mid in full:
                        infos[mid] = from_gmail_message(full[mid])
                        self.bodies += 1
                        if hinted and mid in hinted:
                            hinted_bodies[0] += 1
            for mid in chunk:
                done += 1
                info = infos.get(mid)
                if info is None:
                    continue
                if since is not None and info.date is not None and info.date < since:
                    continue   # exact boundary: the query is widened by a second on purpose
                if progress:
                    # "Back to <date>" only means "everything newer is read" once
                    # the priority pass is over; until then no date is claimed.
                    # the whole chunk is already paid for, so units left are
                    # counted from its end, not from this message
                    progress(done, total, current_date=info.date if done > n_first else None,
                             eta_hint=eta(end), note=self.order_note if done <= n_first else "")
                yield info
            if progress:
                progress(done, total, eta_hint=eta(done), note=self.order_note if done <= n_first else "")

    def fetch_body_links(self, ids, cancel=None):
        """format=full for these messages, paced like a scan (60 units each)."""
        full = self._batch_get(self.service(), list(ids), "full", cancel)
        return {mid: from_gmail_message(m).body_unsub for mid, m in full.items()}

    def mailbox_address(self) -> str | None:
        """The signed-in Gmail address (users.getProfile, covered by
        gmail.readonly), so the API account lines up with the same mailbox
        connected over IMAP."""
        prof = self._execute(self.service().users().getProfile(userId="me"))
        return (prof or {}).get("emailAddress")

    # ------------------------------------------------------------ actions
    def _users(self):
        return self.service().users()

    def send_email(self, to: str, subject: str, body: str) -> dict:
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(bytes(msg)).decode()
        sent = self._execute(self._users().messages().send(userId="me", body={"raw": raw}))
        return {"message_id": sent.get("id")}

    def trash(self, ids: list[str]) -> dict:
        done = []
        for mid in ids:
            self._execute(self._users().messages().trash(userId="me", id=mid))
            done.append(mid)
        return {"op": "untrash", "ids": done}

    def untrash(self, ids: list[str]) -> None:
        for mid in ids:
            self._execute(self._users().messages().untrash(userId="me", id=mid))

    def _batch(self, ids, add, remove):
        for i in range(0, len(ids), 1000):   # batchModify accepts up to 1000 ids
            self._execute(self._users().messages().batchModify(
                userId="me", body={"ids": ids[i:i + 1000], "addLabelIds": add, "removeLabelIds": remove}))

    def archive(self, ids: list[str]) -> dict:
        self._batch(ids, [], ["INBOX"])
        return {"op": "unarchive", "ids": ids}

    def unarchive(self, ids: list[str]) -> None:
        self._batch(ids, ["INBOX"], [])

    def report_spam(self, ids: list[str]) -> dict:
        self._batch(ids, ["SPAM"], ["INBOX"])
        return {"op": "unreport_spam", "ids": ids}

    def unreport_spam(self, ids: list[str]) -> None:
        self._batch(ids, ["INBOX"], ["SPAM"])

    def create_filter(self, address: str, mode: str) -> dict:
        if mode not in {"archive", "trash"}:
            raise ValueError("filter mode must be archive or trash")
        action = {"removeLabelIds": ["INBOX"]} if mode == "archive" else {"addLabelIds": ["TRASH"]}
        created = self._execute(self._users().settings().filters().create(
            userId="me", body={"criteria": {"from": address}, "action": action}))
        return {"op": "delete_filter", "filter_id": created.get("id")}

    def delete_filter(self, filter_id: str) -> None:
        self._execute(self._users().settings().filters().delete(userId="me", id=filter_id))


def imap_oauth_token(account: str, on_auth_url=None, flow_runner=None, client_secret: Path | None = None,
                     token_file: Path | None = None):
    """Opt-in: a callable returning a fresh access token for Gmail IMAP/SMTP
    XOAUTH2 (scope FULL_MAIL, see FULL_MAIL_WARNING). The token is stored apart
    from the read-only API token, so disconnecting one never touches the other."""
    src = GmailSource(account, token_file=token_file or config.token_path(f"{account}.imap"),
                      client_secret=client_secret, flow_runner=flow_runner, on_auth_url=on_auth_url)

    def token() -> str:
        return src._credentials({FULL_MAIL}).token
    return token
