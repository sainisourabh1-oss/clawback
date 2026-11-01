"""Fake Gmail API service (same call shape as googleapiclient) built from raw
synthetic messages. Records every mutating call so tests can assert exactly
what was (or was not) done."""
from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import itertools
import re
from datetime import datetime
from collections import deque
from email.utils import parsedate_to_datetime

from clawback.sources.mbox import _labels_of


def http_error(status: int, reason: str = "", retry_after: str | None = None):
    import json
    import httplib2
    from googleapiclient.errors import HttpError
    headers = {"status": status}
    if retry_after:
        headers["retry-after"] = retry_after
    content = json.dumps({"error": {"code": status, "errors": [{"reason": reason}] if reason else []}}).encode()
    return HttpError(httplib2.Response(headers), content, uri="https://gmail.googleapis.com/fake")


def _part(msg) -> dict:
    d = {"mimeType": msg.get_content_type(), "headers": [{"name": k, "value": str(v)} for k, v in msg.items()],
         "filename": msg.get_filename() or "", "body": {}}
    if msg.is_multipart():
        d["parts"] = [_part(p) for p in msg.get_payload()]
    else:
        payload = msg.get_payload(decode=True) or b""
        d["body"] = {"data": base64.urlsafe_b64encode(payload).decode().rstrip("="), "size": len(payload)}
    return d


def to_gmail(raw: bytes, mid: str) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    labels, _ = _labels_of(raw)
    try:
        internal = int(parsedate_to_datetime(msg["Date"]).timestamp() * 1000)
    except (TypeError, ValueError):
        internal = None
    from clawback.extract import body_from_email
    snippet = " ".join(body_from_email(msg).split())[:200]
    d = {"id": mid, "threadId": "t" + mid, "labelIds": sorted(labels), "snippet": snippet, "payload": _part(msg)}
    if internal:
        d["internalDate"] = str(internal)
    return d


class FakeClock:
    """Virtual time: sleeping and simulated network latency advance it
    instantly, so a 30-minute scan can be measured in milliseconds."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


UNITS = {"list": 5, "get:metadata": 20, "get:minimal": 20, "get:full": 60, "get:raw": 60}


class _Req:
    def __init__(self, fn, svc, name, units_key=None):
        self.fn, self.svc, self.name = fn, svc, name
        self.units = UNITS.get(units_key or name, 5)

    def _run(self):
        self.svc.calls.append(self.name)
        queued = self.svc.failures.get(self.name)
        if queued:
            raise queued.pop(0)
        if not self.svc._spend(self.units):
            raise http_error(429, "rateLimitExceeded")
        return self.fn()

    def execute(self, num_retries=0):
        self.svc._round_trip(1)
        return self._run()


class _Batch:
    def __init__(self, svc, callback):
        self.svc, self.callback, self.items = svc, callback, []

    def add(self, request, callback=None, request_id=None):
        if len(self.items) >= 100:
            raise ValueError("Gmail batches are limited to 100 calls")
        self.items.append((request, callback or self.callback, request_id or str(len(self.items))))

    def execute(self, http=None):
        self.svc.calls.append("batch")
        self.svc.batches.append(len(self.items))
        self.svc._round_trip(len(self.items))
        for req, cb, rid in self.items:
            try:
                resp, exc = req._run(), None
            except Exception as e:  # noqa: BLE001 - mirrors googleapiclient: errors go to the callback
                resp, exc = None, e
            cb(rid, resp, exc)


class FakeGmail:
    """Optional realism: ``latency`` seconds per HTTP round trip (+ ``per_item``
    per request inside a batch), and ``quota_per_minute`` enforced over a
    rolling minute (429 rateLimitExceeded when exceeded), on a ``clock``."""

    def __init__(self, raws: list[bytes], clock: FakeClock | None = None, latency: float = 0.0, per_item: float = 0.0,
                 quota_per_minute: float | None = None, unread: set[str] | None = None):
        self.clock = clock or FakeClock()
        self.latency, self.per_item, self.quota = latency, per_item, quota_per_minute
        self._spent: deque[tuple[float, int]] = deque()
        self.units_spent = 0
        self.rejected = 0
        self.priority_lists = 0
        self.body_lists = 0
        self.batches: list[int] = []
        self.formats: list[str] = []
        self.store = {}
        for i, raw in enumerate(raws):
            m = to_gmail(raw, f"m{i:05d}")
            if not ({"SPAM", "TRASH"} & set(m["labelIds"])):
                self.store[m["id"]] = m
        self.filters: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.calls: list[str] = []
        self.failures: dict[str, list] = {}
        self._fid = itertools.count(1)

    def _round_trip(self, n: int) -> None:
        self.clock.sleep(self.latency + self.per_item * n)

    def _spend(self, units: int) -> bool:
        now = self.clock()
        while self._spent and now - self._spent[0][0] >= 60:
            self._spent.popleft()
        if self.quota is not None and sum(u for _, u in self._spent) + units > self.quota:
            self.rejected += 1
            return False
        self._spent.append((now, units))
        self.units_spent += units
        return True

    def new_batch_http_request(self, callback=None):
        return _Batch(self, callback)

    # googleapiclient shape: service.users().messages().list(...).execute()
    def users(self):
        return self

    def messages(self):
        return _Messages(self)

    email_address: str | None = None      # set to answer users.getProfile

    def getProfile(self, userId):
        svc = self

        class _Req:
            def execute(self):
                svc.calls.append("getProfile")
                return {"emailAddress": svc.email_address} if svc.email_address else {}
        return _Req()

    def settings(self):
        return _Settings(self)

    def mutating_calls(self) -> list[str]:
        return [c for c in self.calls if c not in {"list", "get", "batch", "getProfile"}]


class _Messages:
    def __init__(self, svc: FakeGmail):
        self.svc = svc

    def list(self, userId, q, maxResults, pageToken=None):
        ids = sorted(self.svc.store, key=lambda i: -int(self.svc.store[i].get("internalDate", 0)))
        after = re.search(r"\bafter:(\d{6,})\b", q or "")
        if after:  # Gmail: epoch seconds, messages strictly after
            ids = [i for i in ids if int(self.svc.store[i].get("internalDate", 0)) // 1000 > int(after.group(1))]
        if re.search(r"\bis:unread\b", q or ""):
            ids = [i for i in ids if "UNREAD" in self.svc.store[i]["labelIds"]]
        subj_or = re.search(r"\bsubject:\{([^}]*)\}", q or "")
        if subj_or:  # Gmail: subject contains any of the words (whole words, any case)
            words = [re.compile(rf"(?i)\b{re.escape(w)}\b") for w in subj_or.group(1).split()]

            def subject_hit(i):
                m = self.svc.store[i]
                subj = next((h["value"] for h in m["payload"].get("headers", []) if h["name"].lower() == "subject"), "")
                return any(w.search(subj) for w in words)
            ids = [i for i in ids if subject_hit(i)]
            self.svc.body_lists += 1
            q = q.replace(subj_or.group(0), "")
        pri = re.search(r"\{([^}]*)\}", q or "")
        if pri:  # Gmail OR group: category:x matches tab labels, words match subject/snippet
            terms = pri.group(1).split()
            cats = {"CATEGORY_" + t.split(":", 1)[1].upper() for t in terms if t.startswith("category:")}
            words = [t.lower() for t in terms if ":" not in t]

            def hit(i):
                m = self.svc.store[i]
                subj = next((h["value"] for h in m["payload"].get("headers", []) if h["name"].lower() == "subject"), "")
                text = (subj + " " + m.get("snippet", "")).lower()
                return bool(cats & set(m["labelIds"])) or any(w in text for w in words)
            ids = [i for i in ids if hit(i)]
            self.svc.priority_lists += 1
        start = int(pageToken or 0)
        page = ids[start:start + maxResults]
        nxt = start + maxResults
        return _Req(lambda: {"messages": [{"id": i} for i in page], **({"nextPageToken": str(nxt)} if nxt < len(ids) else {})}, self.svc, "list")

    def get(self, userId, id, format="full", metadataHeaders=None):
        def run():
            if id not in self.svc.store:
                raise http_error(404, "notFound")
            self.svc.formats.append(format)
            m = self.svc.store[id]
            if format == "metadata":
                wanted = {h.lower() for h in (metadataHeaders or [])}
                headers = [h for h in m["payload"]["headers"] if not wanted or h["name"].lower() in wanted]
                return {k: v for k, v in m.items() if k != "payload"} | {"payload": {"mimeType": m["payload"]["mimeType"],
                                                                                    "headers": headers}}
            return m
        return _Req(run, self.svc, "get", f"get:{format}")

    def send(self, userId, body):
        def run():
            raw = base64.urlsafe_b64decode(body["raw"] + "==")
            m = email.message_from_bytes(raw)
            self.svc.sent.append({"to": m["To"], "subject": m["Subject"]})
            return {"id": f"s{len(self.svc.sent)}"}
        return _Req(run, self.svc, "send")

    def trash(self, userId, id):
        def run():
            self.svc.store[id]["labelIds"] = sorted(set(self.svc.store[id]["labelIds"]) - {"INBOX"} | {"TRASH"})
            return {}
        return _Req(run, self.svc, "trash")

    def untrash(self, userId, id):
        def run():
            self.svc.store[id]["labelIds"] = sorted(set(self.svc.store[id]["labelIds"]) - {"TRASH"} | {"INBOX"})
            return {}
        return _Req(run, self.svc, "untrash")

    def batchModify(self, userId, body):
        def run():
            for mid in body["ids"]:
                labels = set(self.svc.store[mid]["labelIds"])
                labels |= set(body.get("addLabelIds", []))
                labels -= set(body.get("removeLabelIds", []))
                self.svc.store[mid]["labelIds"] = sorted(labels)
            return {}
        return _Req(run, self.svc, "batchModify")


class _Settings:
    def __init__(self, svc):
        self.svc = svc

    def filters(self):
        return self

    def create(self, userId, body):
        def run():
            fid = f"f{next(self.svc._fid)}"
            self.svc.filters[fid] = body
            return {"id": fid, **body}
        return _Req(run, self.svc, "filters.create")

    def delete(self, userId, id):
        return _Req(lambda: self.svc.filters.pop(id) and {}, self.svc, "filters.delete")


class FakeIMAP:
    """imaplib.IMAP4_SSL stand-in: UID SEARCH ALL / SINCE, UID FETCH of a
    UID set with FLAGS, INTERNALDATE, X-GM-LABELS, BODY.PEEK[HEADER.FIELDS
    (...)] or BODY.PEEK[]. Optional ``latency`` per command and a byte rate,
    on a virtual ``clock``. Refuses anything that could mark mail as read."""

    def __init__(self, messages: list[bytes], clock: FakeClock | None = None, latency: float = 0.0,
                 bytes_per_second: float | None = None, gmail: bool = False, seen: set[int] | None = None,
                 sent: set[int] | None = None, oauth_token: str | None = None, raw_search: bool = True,
                 password: str | None = None, users: set[str] | None = None,
                 refusal: bytes = b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)",
                 listing: list | None = None, boxes: dict[str, list[bytes]] | None = None):
        """``password``/``users``: accept only these (None: accept anything),
        refusing with ``refusal``. ``listing``: raw LIST response lines
        (None: the server has no LIST, like the old fake). ``boxes``: folder
        name -> its own messages (selecting a folder switches to them)."""
        self.password, self.users, self.refusal = password, users, refusal
        self.listing, self.boxes = listing, boxes
        self.logins: list[str] = []
        self.selected: list[str] = []
        self.messages = messages
        self.clock = clock or FakeClock()
        self.latency, self.bps, self.gmail = latency, bytes_per_second, gmail
        self.seen = seen if seen is not None else set(range(1, len(messages) + 1))
        self.selected_readonly = None
        self.commands: list[str] = []
        self.full_fetches = 0
        self.sent = sent or set()               # UIDs carrying Gmail's Sent label
        self.oauth_token = oauth_token          # accept XOAUTH2 with this token
        self.raw_search = raw_search            # False: X-GM-RAW answers NO (fallback path)
        self.fetched_headers: list[int] = []
        self.auth_strings: list[bytes] = []
        self.searches: list[tuple] = []
        self.dates = []
        for raw in messages:
            msg = email.message_from_bytes(raw, policy=email.policy.compat32)
            try:
                self.dates.append(parsedate_to_datetime(msg["Date"]))
            except (TypeError, ValueError):
                self.dates.append(None)

    def __call__(self, host):
        return self

    def _tick(self, nbytes: int = 0):
        self.clock.sleep(self.latency + (nbytes / self.bps if self.bps else 0.0))

    def login(self, user, password):
        self._tick()
        self.logins.append(user)
        if (self.password is not None and password != self.password) or (self.users is not None and user not in self.users):
            raise imaplib.IMAP4.error(self.refusal)
        return "OK", [b"logged in"]

    def list(self):
        if self.listing is None:
            raise imaplib.IMAP4.error("command LIST illegal in state AUTH")
        return "OK", list(self.listing)

    def authenticate(self, mechanism, authobject):
        self._tick()
        assert mechanism == "XOAUTH2"
        sasl = authobject(b"")
        self.auth_strings.append(sasl)
        if self.oauth_token is None or f"auth=Bearer {self.oauth_token}".encode() not in sasl:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        return "OK", [b"authenticated"]

    @staticmethod
    def msgid(uid: int) -> int:
        return 0x1800000000000000 + uid * 7919

    def capability(self):
        return "OK", [b"IMAP4rev1 X-GM-EXT-1" if self.gmail else b"IMAP4rev1"]

    def select(self, folder, readonly=False):
        self._tick()
        self.selected_readonly = readonly
        assert readonly, "scan must open folders read-only (EXAMINE)"
        name = folder[1:-1].replace('\\"', '"') if folder.startswith('"') else folder
        self.selected.append(name)
        if self.boxes is not None:
            if name not in self.boxes:
                return "NO", [b"Mailbox does not exist"]
            self.messages = self.boxes[name]
            self.dates = [parsedate_to_datetime(email.message_from_bytes(r, policy=email.policy.compat32)["Date"])
                          for r in self.messages]
            self.seen = set(range(1, len(self.messages) + 1))
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        self.commands.append(command)
        if command == "SEARCH":
            self._tick()
            crit = [a for a in args if a is not None]
            self.searches.append(tuple(crit))
            uids = list(range(1, len(self.messages) + 1))
            if crit and crit[0] == "X-GM-RAW":
                if not (self.gmail and self.raw_search):
                    return "NO", [b"unsupported"]
                q = crit[1].strip('"').split()
                for term in q:
                    if term.startswith("after:"):
                        cut = int(term[6:])
                        uids = [u for u in uids if self.dates[u - 1] is None or self.dates[u - 1].timestamp() > cut]
                    elif term == "-in:sent":
                        uids = [u for u in uids if u not in self.sent]
            elif crit and crit[0] == "X-GM-MSGID":
                assert self.gmail
                uids = [u for u in uids if self.msgid(u) == int(crit[1])]
            elif crit and crit[0] == "SINCE":
                since = datetime.strptime(crit[1], "%d-%b-%Y").date()
                uids = [u for u in uids if self.dates[u - 1] is None or self.dates[u - 1].date() >= since]
            return "OK", [b" ".join(str(u).encode() for u in uids)]
        if command == "FETCH":
            uidset, items = args
            uids = []
            for part in uidset.split(","):
                if ":" in part:
                    a, b = part.split(":")
                    uids.extend(range(int(a), int(b) + 1))
                else:
                    uids.append(int(part))
            out, nbytes = [], 0
            if items == "(UID X-GM-MSGID)":
                assert self.gmail
                for u in uids:
                    line = f"{u} (UID {u} X-GM-MSGID {self.msgid(u)})".encode()
                    out.append(line)
                    nbytes += len(line)
                self._tick(nbytes)
                return "OK", out
            if items == "(UID INTERNALDATE)":
                for u in uids:
                    d = self.dates[u - 1]
                    internal = d.strftime("%d-%b-%Y %H:%M:%S %z") if d else "01-Jan-2000 00:00:00 +0000"
                    line = f'{u} (UID {u} INTERNALDATE "{internal}")'.encode()
                    out.append(line)
                    nbytes += len(line)
                self._tick(nbytes)
                return "OK", out
            assert "PEEK" in items, "scan must not mark mail as read"
            full = "BODY.PEEK[]" in items
            if not full:
                self.fetched_headers.extend(uids)
            if full:
                self.full_fetches += len(uids)
            for u in uids:
                raw = self.messages[u - 1]
                body = raw if full else raw.split(b"\n\n", 1)[0] + b"\n\n"
                nbytes += len(body)
                flags = "(\\Seen)" if u in self.seen else "()"
                d = self.dates[u - 1]
                internal = d.strftime("%d-%b-%Y %H:%M:%S %z") if d else "01-Jan-2000 00:00:00 +0000"
                label = "\\\\Sent" if u in self.sent else "\\\\Inbox"
                labels = f' X-GM-LABELS ("{label}")' if self.gmail and "X-GM-LABELS" in items else ""
                meta = f'{u} (UID {u} FLAGS {flags} INTERNALDATE "{internal}"{labels} BODY[] {{{len(body)}}}'
                out.extend([(meta.encode(), body), b")"])
            self._tick(nbytes)
            return "OK", out
        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self):
        return "BYE", []
