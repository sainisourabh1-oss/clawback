"""Local web app, served on 127.0.0.1 only.

Security model (review bug 9):

* Binds to 127.0.0.1, never 0.0.0.0.
* Every request's ``Host`` header must be ``127.0.0.1:<port>`` or
  ``localhost:<port>``; anything else gets 421. This defeats DNS-rebinding
  attacks, where a web page re-points its own hostname at 127.0.0.1 to read
  this API.
* Every state-changing request is a POST that must carry the per-launch
  random token in ``X-Clawback-CSRF`` and a JSON (or octet-stream upload)
  body; a cross-site form cannot set either. If an ``Origin`` header is
  present it must be this server's origin.
* No CORS headers are ever sent, so other origins cannot read responses.
* Strict CSP (no inline script, no third-party anything), no-store caching,
  frame-ancestors 'none'.
* Mailbox and outgoing actions go through ``actions.build_plan`` +
  ``actions.confirm``: the API exposes the same two separate steps and
  nothing else can trigger them.
"""
from __future__ import annotations

import json
import mimetypes
import re
import secrets
import shutil
import threading
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import (__version__, actions, card, combined, config, errors, insights, links, ms_oauth, oneclick, privacy,
               proof, providers, saved, savings, tracker)
from ..actions import PlanError
from ..money import format_money, totals_by_currency
from ..sources.base import Source, SourceError

MAX_JSON = 2_000_000
STATIC = {"app.js": "application/javascript", "icons.js": "application/javascript", "app.css": "text/css",
          "logo.svg": "image/svg+xml", "i18n/en.json": "application/json", "favicon.svg": "image/svg+xml",
          "favicon.ico": "image/x-icon",
          "fonts/inter-latin.woff2": "font/woff2", "fonts/inter-latin-ext.woff2": "font/woff2",
          "fonts/OFL.txt": "text/plain"}
_BINARY = ("svg", "woff2", "icon")   # no charset parameter for these


class ScanJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"status": "idle", "done": 0, "total": None, "account": None, "error": None, "result": None,
                      "started": None, "finished": None, "unit": "messages", "progress": None, "paused": False,
                      "preview_seq": 0, "kind": None, "full_read": False, "hint": None, "mode": None, "since": None,
                      "last": None}
        self.preview: dict | None = None      # partial results while running (kept off the 1 s poll)
        from ..progress import ScanControl
        self.control = ScanControl()
        self.thread: threading.Thread | None = None

    @property
    def cancel(self):
        return self.control

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def update(self, **kw):
        with self.lock:
            self.state.update(kw)


class App:
    """Application state shared by all request threads."""

    def __init__(self, http_post=None, demo_now=None, allow_demo=True):
        self.csrf = secrets.token_urlsafe(32)
        self.sources: dict[str, object] = {}
        self.active: str | None = None
        self.job = ScanJob()
        self.http_post = http_post or oneclick.post
        self.demo_now = demo_now
        self.allow_demo = allow_demo
        self.lock = threading.Lock()
        self.port = 0
        self.auth: dict = {"status": "idle"}
        self.previous_active: str | None = None
        self.dropped: dict | None = None      # {"account", "error"} of a new account whose first scan failed
        # After a scan: look inside one recent email of each newsletter that
        # has no List-Unsubscribe header, for an unsubscribe link (read-only,
        # paced by the source like a scan; the links are never opened).
        self.topup: dict = {"status": "idle", "account": None, "done": 0, "total": 0, "found": 0, "error": None}
        self.topup_stop = threading.Event()
        self.body_lock = threading.Lock()     # one body read at a time per app (shared Gmail pacer)
        self.topup_thread: threading.Thread | None = None

    # ---------------------------------------------------------------- sources
    def source(self):
        if self.active == ALL:
            raise ApiError(409, "Choose one account for this (not All accounts)")
        if not self.active or self.active not in self.sources:
            raise ApiError(409, "Connect a mailbox or import a file first")
        return self.sources[self.active]

    def source_for(self, account: str):
        """The live source of ``account`` in this session, else one that can
        only act without the mailbox (HTTPS unsubscribe, opening pages)."""
        from ..sources.base import OfflineSource
        if account == ALL:
            raise ApiError(409, "Choose one account for this (not All accounts)")
        return self.sources.get(account) or OfflineSource(account)

    def add_source(self, src):
        with self.lock:
            self.dropped = None
            if self.active and self.active != src.account:
                self.previous_active = self.active
            self.sources[src.account] = src
            self.active = src.account
        return src

    def _drop_failed(self, account: str) -> None:
        """A new account whose first scan failed (for example the sign-in was
        refused) and that has no saved results never stays on screen in place
        of the account you were looking at: go back to that one."""
        if account in tracker.scanned_accounts():
            return
        with self.lock:
            self.dropped = {"account": account, "error": self.job.snapshot().get("error")}
            self.sources.pop(account, None)
            if self.active == account:
                prev = getattr(self, "previous_active", None)
                self.active = prev if prev in self.sources or prev == ALL else (next(iter(self.sources), None))

    def connect(self, body: dict):
        kind = body.get("kind")
        if kind == "demo":
            if not self.allow_demo:
                raise ApiError(403, "Demo disabled")
            from ..demo import DemoSource
            return self.add_source(DemoSource(now=self.demo_now))
        if kind == "mbox":
            from ..sources.mbox import MboxSource
            path = str(body.get("path") or "").strip().strip('"')
            if not path:
                raise ApiError(400, "Enter the path of your Takeout .zip/.tgz, .mbox file or .eml folder")
            return self.add_source(MboxSource(path))
        if kind == "imap":
            # Any IMAP provider: a preset (picked, or matched from the address)
            # fills in the servers; whatever the user typed overrides it.
            from .. import providers as prov
            from ..sources.imap import ImapSource
            user = str(body.get("user") or "").strip()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", user):
                raise ApiError(400, "Enter your email address")
            try:
                st = prov.imap_settings(str(body.get("provider") or "") or None, user,
                                        str(body.get("host") or "").strip() or None, body.get("port") or None,
                                        str(body.get("security") or "") or None)
            except prov.PresetError as exc:
                raise ApiError(400, str(exc)) from None
            password = str(body.get("password") or "")
            if (st["provider"] or {}).get("auth") != "bridge":
                password = password.strip()
            if not password:
                raise ApiError(400, "Enter the app password")
            folders = [f.strip() for f in str(body.get("folders") or "").split(",") if f.strip()] or None
            cafile = str(body.get("cafile") or "").strip().strip('"') or None
            if cafile and not Path(cafile).is_file():
                raise ApiError(400, "The certificate file was not found")
            src = ImapSource(st["host"], user, password, folders, account=saved.account_label(user),
                             allow_send=bool(body.get("allow_send")), port=st["port"],
                             security=st["security"], cafile=cafile, provider=st["provider"], users=st["users"])
            src.verify()      # a refused sign-in never replaces the account on screen
            return self.add_source(src)
        if kind == "gmail_imap":
            # Fast scan: Gmail over IMAP (no per-minute API quota). App password
            # by default; XOAUTH2 only when the user ticked the full-access warning.
            from ..sources.imap import ImapSource
            user = str(body.get("user") or "").strip()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", user):
                raise ApiError(400, "Enter your Gmail address")
            if body.get("xoauth2"):
                from ..sources.gmail import FULL_MAIL_WARNING, imap_oauth_token
                if not body.get("accept_full_access"):
                    raise ApiError(400, FULL_MAIL_WARNING)
                token = imap_oauth_token(user, on_auth_url=lambda url: self.auth.update(url=url))
                src = ImapSource("imap.gmail.com", user, None, account=saved.account_label(user), oauth_token=token)
                return self._consent_then_add(src, user, token)
            password = re.sub(r"\s+", "", str(body.get("password") or ""))
            if not password:
                raise ApiError(400, "Enter the 16-letter app password from myaccount.google.com/apppasswords")
            src = ImapSource("imap.gmail.com", user, password, account=saved.account_label(user),
                             allow_send=bool(body.get("allow_send")))
            src.verify()      # a refused sign-in never replaces the account on screen
            return self.add_source(src)
        if kind == "outlook":
            # EXPERIMENTAL: Microsoft Graph with the user's own app registration.
            from .. import ms_oauth
            from ..sources.outlook import OutlookSource
            user = str(body.get("user") or "").strip().lower()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", user):
                raise ApiError(400, "Enter your Microsoft email address")
            ms_saved = ms_oauth.load_client() or {}
            client_id = str(body.get("client_id") or "").strip() or ms_saved.get("client_id", "")
            tenant = str(body.get("tenant") or "").strip() or ms_saved.get("tenant") or "common"
            if not ms_oauth.valid_client_id(client_id):
                raise ApiError(400, "Enter the Application (client) ID of your Microsoft app registration "
                                    "(it looks like 00000000-0000-0000-0000-000000000000)")
            if tenant not in ms_oauth.TENANTS:
                raise ApiError(400, "Account type must be common, consumers or organizations")
            ms_oauth.save_client(client_id, tenant)
            src = OutlookSource(user, client_id, tenant, on_auth_url=lambda url: self.auth.update(url=url))
            return self._consent_then_add(src, user, src.connect)
        if kind == "gmail":
            from ..sources.gmail import GmailSource
            account = re.sub(r"[^\w@.+-]", "", str(body.get("account") or ""))
            if account:
                # a saved account: its own sign-in, no new consent unless Google revoked it
                src = GmailSource(account=account, query=str(body.get("query") or ""),
                                  on_auth_url=lambda url: self.auth.update(url=url))

                def reopen():
                    src.service()
                    try:                      # learn its address once, so it is shown by address
                        if not (tracker.identities().get(account) or {}).get("address"):
                            tracker.record_identity(account, "gmail", src.mailbox_address())
                    except Exception:  # noqa: BLE001 - the name is a convenience; the account works without it
                        pass
                return self._consent_then_add(src, account, reopen)
            # A new Google sign-in: the account is named by the address Google
            # reports, and an address this computer already knows reopens its
            # existing account (data, sign-in, history) instead of a new one.
            src = GmailSource(account="new-sign-in", query=str(body.get("query") or ""),
                              token_file=saved.pending_token_path(), on_auth_url=lambda url: self.auth.update(url=url))
            return self._consent_then_add(src, "", lambda: (src.service(), saved.adopt_gmail_signin(src)))
        raise ApiError(400, "Unknown source kind")

    def open_saved(self, account: str) -> dict:
        """Open an account this computer already knows (Connect screen and
        account picker): already connected -> switch to it; a saved sign-in
        token -> reconnect with it (no password, no new consent unless Google
        or Microsoft revoked it); results only -> open them read-only."""
        if account in self.sources:
            self.active = account
            return {"account": account, "active": account}
        row = next((r for r in saved.saved_accounts(self.allow_demo) if r["account"] == account), None)
        if row is None:
            raise ApiError(404, "No saved account with that name")
        if row["reconnect"]:
            src = self.connect(dict(row["reconnect"]))
            pending = row["reconnect"]["kind"] in {"gmail", "outlook"} or row["reconnect"].get("xoauth2")
            return {"account": src.account, "pending": bool(pending), "auth": dict(self.auth)}
        from ..sources.base import SavedSource
        self.add_source(SavedSource(account))
        return {"account": account, "active": account, "saved_only": True}

    def _consent_then_add(self, src, account: str, sign_in):
        """Run a Google sign-in (``sign_in()``) in the background; the UI polls
        /api/auth for the URL and the outcome. The source is added on success."""
        if self.auth.get("status") == "waiting":
            raise ApiError(409, "A sign-in is already waiting in your browser")
        self.auth = {"status": "waiting", "url": None, "error": None, "account": account}

        def consent():
            try:
                sign_in()  # runs the Google consent in the browser if needed
                self.add_source(src)
                self.auth.update(status="done", account=src.account)
            except SourceError as exc:
                self.auth.update(status="error", error=str(exc))
            except Exception as exc:  # shown to the user, never swallowed
                self.auth.update(status="error", error=f"{type(exc).__name__}: {exc}")
        threading.Thread(target=consent, daemon=True).start()
        return src

    # ---------------------------------------------------------------- body links
    TOPUP_BATCH = 20

    def body_link(self, account: str, address: str) -> dict:
        """Read the newest email of one sender for an unsubscribe link (when a
        row is opened or ticked). Cached: a sender is read once."""
        s = tracker.get_sender(account, address)
        if not s:
            raise ApiError(404, "Unknown sender")
        if s["unsubscribe"] or s.get("body_unsub") or s.get("body_checked"):
            return s
        src = self.sources.get(account)
        if src is None:
            raise ApiError(409, "Connect this account again to look inside its emails")
        mid = tracker.latest_message_id(account, address)
        if mid is None:
            raise ApiError(404, "No email from this sender was saved by the scan")
        with self.body_lock:
            found = src.fetch_body_links([mid])
        tracker.set_body_link(account, address, found.get(mid, ""))
        return tracker.get_sender(account, address)

    def start_topup(self, src) -> None:
        account = src.account
        candidates = tracker.body_link_candidates(account)
        if not candidates or type(src).fetch_body_links is Source.fetch_body_links:
            return
        self.topup_stop = stop = threading.Event()
        self.topup = {"status": "running", "account": account, "done": 0, "total": len(candidates), "found": 0,
                      "error": None}

        def run():
            try:
                for i in range(0, len(candidates), self.TOPUP_BATCH):
                    if stop.is_set():
                        self.topup.update(status="stopped")
                        return
                    batch = candidates[i:i + self.TOPUP_BATCH]
                    ids = {}
                    for a in batch:
                        mid = tracker.latest_message_id(account, a)
                        if mid:
                            ids[mid] = a
                    with self.body_lock:
                        got = src.fetch_body_links(list(ids), cancel=stop)
                    for mid, a in ids.items():
                        tracker.set_body_link(account, a, got.get(mid, ""))
                        self.topup["found"] += bool(got.get(mid))
                    self.topup["done"] = min(len(candidates), i + len(batch))
                self.topup.update(status="done")
            except SourceError as exc:
                self.topup.update(status="stopped" if stop.is_set() else "error", error=str(exc))
            except Exception as exc:  # shown in the UI, never swallowed
                self.topup.update(status="error", error=f"{type(exc).__name__}: {exc}")
                traceback.print_exc()
        self.topup_thread = threading.Thread(target=run, daemon=True)
        self.topup_thread.start()

    # ---------------------------------------------------------------- scan
    def start_scan(self, limit: int | None = None, since=None, incremental: bool = True, mode: str | None = None,
                   last: int | None = None):
        from ..progress import ScanControl
        snap = self.job.snapshot()
        if snap["status"] == "running":
            raise ApiError(409, "A scan is already running")
        if self.active == ALL:
            raise ApiError(409, "Choose one account to scan (the All accounts view combines finished scans)")
        src = self.source()
        self.topup_stop.set()                 # a new scan supersedes the body top-up
        self.dropped = None
        self.job.control = ScanControl()
        from ..scan import reads_everything, speed_hint, upgrade_reread
        kind = getattr(src, "kind", "")
        unit = "bytes" if kind == "mbox" else "messages"
        if kind == "mbox" and mode in {"quick", "full", "since"}:
            limit = None   # an export is read whole; a cap would need a second pass to find the newest
        full_read = reads_everything(kind, src.account, incremental)
        upgrade = full_read and incremental and upgrade_reread(kind, src.account)
        self.job.update(status="running", done=0, total=None, account=src.account, error=None, result=None,
                        started=tracker.now_iso(), finished=None, unit=unit, progress=None, paused=False,
                        preview_seq=0, kind=kind, full_read=full_read, mode=mode, last=last,
                        since=since.date().isoformat() if since else None,
                        hint=speed_hint(kind, "running", full_read, upgrade=upgrade))
        self.job.preview = None

        def on_preview(p):
            self.job.preview = p
            self.job.update(preview_seq=self.job.snapshot()["preview_seq"] + 1)

        def progress(snap):
            from ..progress import describe
            snap = dict(snap, line=describe(snap))
            self.job.update(done=snap["done"], total=snap["total"], progress=snap, paused=self.job.control.paused)

        def run():
            from ..scan import run_scan
            try:
                r = run_scan(src, limit=limit, progress=progress, cancel=self.job.control, since=since,
                             incremental=incremental, on_preview=on_preview, mode=mode, last=last)
                from ..progress import describe
                result = {"messages": r.messages, "senders": r.senders, "categories": r.categories,
                          "subscriptions": len(r.subscriptions), "skipped": r.skipped,
                          "partial": r.partial, "capped": r.capped, "reused": r.reused,
                          "bodies": r.bodies, "since": r.since, "mode": r.mode, "last": r.last}
                self.job.update(status="done", finished=tracker.now_iso(), progress=dict(r.progress, line=describe(r.progress)),
                                result=result, hint=speed_hint(kind, "done", full_read, result))
                self.start_topup(src)
            except SourceError as exc:
                self.job.update(status="error", error=str(exc), finished=tracker.now_iso(), hint=None)
                self._drop_failed(src.account)
            except Exception as exc:  # surfaced to the UI, never swallowed
                self.job.update(status="error", error=f"{type(exc).__name__}: {exc}", finished=tracker.now_iso(), hint=None)
                self._drop_failed(src.account)
                traceback.print_exc()
        self.job.thread = threading.Thread(target=run, daemon=True)
        self.job.thread.start()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _static(name: str) -> bytes:
    return resources.files("clawback.web").joinpath("static/" + name).read_bytes()


def _subscriptions(account):
    links.load_user_db()
    out = []
    for r in tracker.list_subscriptions(account=account):
        d = dict(r)
        d["senders"] = json.loads(d.get("senders") or "[]")
        url = links.find_cancel_link(d["domain"], d["vendor"], d["merchant"])
        entry = links.entry_for_url(url) if url else None
        d["cancel_url"] = url
        d["cancel_how"] = entry["how"] if entry else ("your saved link" if url else None)
        d["history"] = [dict(h) for h in tracker.price_history(d["id"])]
        d["amount_display"] = format_money(d["amount"], d["currency"]) if d["amount"] is not None else "?"
        d["monthly_display"] = format_money(d["monthly_cost"], d["currency"]) if d["monthly_cost"] is not None else "?"
        out.append(d)
    return out


def _paying(subs):
    totals = totals_by_currency((s["monthly_cost"], s["currency"]) for s in subs if s["status"] in {"suspected", "confirmed"})
    return [{"currency": c, "monthly": v, "display": format_money(v, c)} for c, v in sorted(totals.items())]



# ------------------------------------------------------------------ all accounts
ALL = combined.ALL


def _all_state() -> dict:
    """The mailboxes behind the All accounts view: each group of account
    labels that are one mailbox, with the address shown for it."""
    gs = combined.groups()
    return {"available": len(tracker.accounts()) >= 2,
            "mailboxes": [{"label": combined.label(g), "accounts": g} for g in gs]}


def _all_senders(q):
    rows = combined.senders(q.get("category"))
    proofs = {}
    for g in combined.groups():
        for a in g:
            for p in proof.refresh(a):
                proofs.setdefault((a, p["address"]), p)
    for r in rows:
        p = next((proofs[(a, r["address"])] for a in r["accounts"] if (a, r["address"]) in proofs), None)
        r["proof"] = {k: p[k] for k in ("verdict", "after_grace", "grace_end")} if p else None
    return {"senders": rows}


def _all_subscriptions(q):
    links.load_user_db()
    subs = []
    for d in combined.subscriptions():
        d["senders"] = json.loads(d.get("senders") or "[]")
        url = links.find_cancel_link(d["domain"], d["vendor"], d["merchant"])
        entry = links.entry_for_url(url) if url else None
        d["cancel_url"] = url
        d["cancel_how"] = entry["how"] if entry else ("your saved link" if url else None)
        d["history"] = [dict(h) for h in tracker.price_history(d["id"])]
        d["amount_display"] = format_money(d["amount"], d["currency"]) if d["amount"] is not None else "?"
        d["monthly_display"] = format_money(d["monthly_cost"], d["currency"]) if d["monthly_cost"] is not None else "?"
        subs.append(d)
    return {"subscriptions": subs, "paying": _paying(subs), "revoke_guides": _guides()}


def _all_insights(q):
    out = {"upcoming": [], "price_increases": [], "not_opened": [], "inactive_months": 3}
    seen = set()
    for g in combined.groups():
        for a in g:
            ins = insights.all_insights(a)
            out["inactive_months"] = ins["inactive_months"]
            for key in ("upcoming", "not_opened"):
                for item in ins[key]:
                    k = (key, tuple(g), item.get("address") or item.get("vendor"), item.get("date"))
                    if k not in seen:
                        seen.add(k)
                        out[key].append(dict(item, account=a))
            if not out["price_increases"]:
                out["price_increases"] = ins["price_increases"]   # already across every account
    out["upcoming"].sort(key=lambda u: u.get("date") or "")
    return out


def _all_proof(q):
    rows, seen = [], set()
    for g in combined.groups():
        for a in g:
            for p in proof.refresh(a):
                if (tuple(g), p["address"]) not in seen:
                    seen.add((tuple(g), p["address"]))
                    rows.append(dict(p, account=a))
    return {"senders": rows}


def _all_audit(q):
    entries = [e for a in tracker.accounts() for e in tracker.audit_entries(a)]
    entries.sort(key=lambda e: e["id"], reverse=True)
    for e in entries:
        op = e["undo"].get("op") if isinstance(e.get("undo"), dict) else None
        e["reversible"] = op in actions.REVERSIBLE_OPS
    return {"entries": entries}


def _all_privacy(q):
    return {"footprint": privacy.footprint(None), "laws": privacy.laws(), "requests": privacy.requests(None),
            "disclaimer": privacy.DISCLAIMER}


ALL_GET = {"/api/senders": _all_senders, "/api/subscriptions": _all_subscriptions,
           "/api/savings": lambda q: combined.savings(), "/api/insights": _all_insights, "/api/proof": _all_proof,
           "/api/audit": _all_audit, "/api/privacy": _all_privacy}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Clawback"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # ------------------------------------------------------------ plumbing
        def log_message(self, *a):
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            return host in {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return origin is None or origin in {f"http://127.0.0.1:{app.port}", f"http://localhost:{app.port}"}

        def _headers(self, status, ctype, length, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
                             "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _send(self, status, body: bytes, ctype="application/json; charset=utf-8", extra=None):
            self._headers(status, ctype, len(body), extra)
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status, obj, extra=None):
            # every error string goes out with its plain reason and its technical part apart
            self._send(status, json.dumps(errors.annotate(obj), ensure_ascii=False, default=str).encode("utf-8"),
                       extra=extra)

        def _error(self, status, message):
            self._json(status, {"error": message})

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_JSON:
                raise ApiError(413, "Request too large")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except ValueError as exc:
                raise ApiError(400, "Invalid JSON") from exc
            if not isinstance(data, dict):
                raise ApiError(400, "JSON object expected")
            return data

        # ------------------------------------------------------------ GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._host_ok():
                self._error(421, "Unexpected Host header"); return
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in {"/", "/index.html"}:
                    page = _static("index.html").decode("utf-8").replace("__CSRF__", app.csrf).replace("__VERSION__", __version__)
                    self._send(200, page.encode("utf-8"), "text/html; charset=utf-8"); return
                if url.path.startswith("/static/"):
                    name = url.path[len("/static/"):]
                    if name not in STATIC:
                        self._error(404, "Not found"); return
                    self._send(200, _static(name), STATIC[name] + ("" if STATIC[name].endswith(_BINARY) else "; charset=utf-8")); return
                self._json(200, self.api_get(url.path, q))
            except _Sent:
                return
            except ApiError as exc:
                self._error(exc.status, str(exc))
            except (ValueError, SourceError) as exc:
                self._error(400, str(exc))

        def api_get(self, path, q):
            acct = q.get("account") or app.active
            if acct == ALL and path in ALL_GET:
                return ALL_GET[path](q)
            if path == "/api/state":
                saved_list = saved.saved_accounts(app.allow_demo)
                known = sorted(set(tracker.scanned_accounts()) | set(app.sources))
                # what people see for each account: its email address where known
                names = saved.display_names(set(known) | {r["account"] for r in saved_list})
                for r in saved_list:
                    r["name"] = names.get(r["account"], r["account"])
                    main = saved.label_for_address(r["address"]) if r["address"] else None
                    r["copy_of"] = names.get(main) if main and main != r["account"] else None
                    r["main"] = main if r["copy_of"] else None
                # a second copy of a mailbox is listed right under its main account
                order = {r["account"]: i for i, r in enumerate(saved_list)}
                saved_list.sort(key=lambda r: (order.get(r["main"], order[r["account"]]), r["main"] is not None))
                return {"version": __version__, "active": app.active, "names": names,
                        "sources": {k: v.describe() for k, v in app.sources.items()},
                        # every account you can pick: saved results plus this session's connections
                        "accounts": known,
                        "scanned": tracker.scanned_accounts(), "dropped": app.dropped,
                        "scan": app.job.snapshot(), "settings": config.load_settings(),
                        "last_scan": None if app.active == ALL else tracker.last_scan(app.active),
                        "last_failure": None if app.active == ALL else tracker.last_failure(app.active),
                        "gmail_ready": config.client_secret_path().exists(), "demo": app.allow_demo,
                        "data_folder": str(config.home()), "all": _all_state(),
                        "connectors": providers.public_view(),
                        "microsoft_client": bool(ms_oauth.load_client()), "topup": dict(app.topup),
                        "saved": saved_list}
            if path == "/api/scan":
                return app.job.snapshot()
            if path == "/api/scan/estimate":
                # how long the chosen scan should take (from saved data; reads nothing)
                from ..scan import estimate
                src = app.source()
                try:
                    return estimate(getattr(src, "kind", ""), src.account, q.get("mode") or "quick", q.get("since"),
                                    q.get("last"))
                except ValueError as exc:
                    raise ApiError(400, str(exc)) from None
            if path == "/api/scan/preview":
                return app.job.preview or {"partial": True, "messages": 0, "senders": [], "subscriptions": [],
                                           "categories": {}, "scanned_back_to": None}
            if path == "/api/auth":
                return dict(app.auth)
            if path == "/api/senders":
                rows = tracker.list_senders(acct, q.get("category"))
                proofs = {p["address"]: p for p in proof.refresh(acct)} if acct else {}
                for r in rows:
                    p = proofs.get(r["address"])
                    r["proof"] = {k: p[k] for k in ("verdict", "after_grace", "grace_end")} if p else None
                return {"senders": rows}
            if path == "/api/sender":
                s = tracker.get_sender(acct, q.get("address", ""))
                if not s:
                    raise ApiError(404, "Unknown sender")
                s["proof"] = proof.evaluate(s, acct) if s.get("unsubscribed_at") else None
                s["messages"] = tracker.sender_messages(acct, s["address"])[-200:]
                return s
            if path == "/api/subscriptions":
                subs = _subscriptions(acct)
                return {"subscriptions": subs, "paying": _paying(subs), "revoke_guides": _guides()}
            if path == "/api/proof":
                return {"senders": proof.refresh(acct) if acct else []}
            if path == "/api/savings":
                return savings.compute(acct)
            if path == "/api/card.svg":
                svg = card.svg(savings.compute(acct))
                self._send(200, svg.encode("utf-8"), "image/svg+xml", {"Content-Disposition": "inline; filename=clawback-savings.svg"})
                raise _Sent()
            if path == "/api/audit":
                entries = tracker.audit_entries(acct)
                for e in entries:
                    op = e["undo"].get("op") if isinstance(e.get("undo"), dict) else None
                    e["reversible"] = op in actions.REVERSIBLE_OPS
                return {"entries": entries}
            if path == "/api/plan":
                return actions.get_plan(q.get("id", ""))
            if path == "/api/privacy":
                return {"footprint": privacy.footprint(acct), "laws": privacy.laws(), "requests": privacy.requests(acct),
                        "disclaimer": privacy.DISCLAIMER}
            if path == "/api/insights":
                return insights.all_insights(acct)
            if path == "/api/keep":
                return {"patterns": tracker.keep_patterns()}
            if path == "/api/export":
                data = json.dumps(tracker.export_json(), ensure_ascii=False, indent=1, default=str).encode("utf-8")
                self._send(200, data, "application/json; charset=utf-8",
                           {"Content-Disposition": f"attachment; filename=clawback-export-{datetime.now():%Y%m%d}.json"})
                raise _Sent()
            raise ApiError(404, "Not found")

        # ------------------------------------------------------------ POST
        def do_POST(self):
            if not self._host_ok():
                self._error(421, "Unexpected Host header"); return
            if not self._origin_ok():
                self._error(403, "Cross-origin request refused"); return
            token = self.headers.get("X-Clawback-CSRF", "")
            if not secrets.compare_digest(token, app.csrf):
                self._error(403, "Missing or wrong CSRF token"); return
            url = urlparse(self.path)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            try:
                if url.path == "/api/upload":
                    if ctype != "application/octet-stream":
                        raise ApiError(415, "Upload must be application/octet-stream")
                    self._json(200, self.upload(url)); return
                if ctype != "application/json":
                    raise ApiError(415, "JSON body required")
                self._json(200, self.api_post(url.path, self._body()))
            except _Sent:
                return
            except ApiError as exc:
                self._error(exc.status, str(exc))
            except PlanError as exc:
                self._error(409, str(exc))
            except (ValueError, SourceError, OSError) as exc:
                self._error(400, str(exc))

        def upload(self, url):
            """Stream an uploaded export to the private data folder (never into memory)."""
            name = re.sub(r"[^\w.-]", "_", (parse_qs(url.query).get("name") or ["upload.mbox"])[0])[:120]
            if not name.lower().endswith((".mbox", ".zip", ".tgz", ".gz", ".tar", ".eml")):
                raise ApiError(400, "Choose a .zip, .tgz, .mbox or .eml file")
            remaining = int(self.headers.get("Content-Length") or 0)
            dest_dir = config.home() / "imports"
            dest_dir.mkdir(exist_ok=True)
            dest = dest_dir / f"{int(time.time())}-{name}"
            with open(dest, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            config._private(dest)
            from ..sources.mbox import MboxSource
            src = app.add_source(MboxSource(dest))
            return {"account": src.account, "path": str(dest)}

        def api_post(self, path, body):
            if path == "/api/connect":
                src = app.connect(body)
                if body.get("kind") in {"gmail", "outlook"} or (body.get("kind") == "gmail_imap" and body.get("xoauth2")):
                    return {"account": src.account, "pending": True, "auth": dict(app.auth)}
                return {"account": src.account, "source": src.describe()}
            if path == "/api/open":                       # a saved account, in one click
                return app.open_saved(str(body.get("account") or ""))
            if path == "/api/use":
                if body.get("account") == ALL:
                    if len(tracker.accounts()) < 2:
                        raise ApiError(409, "All accounts needs two or more scanned accounts")
                    app.active = ALL
                    return {"active": app.active}
                if body.get("account") not in app.sources:
                    raise ApiError(404, "Unknown account in this session")
                app.active = body["account"]
                return {"active": app.active}
            if path == "/api/scan" and body.get("mode"):
                # the scan options: quick update / fresh full scan / since a date / the newest N
                from ..scan import scan_request
                try:
                    req = scan_request(str(body["mode"]), body.get("since"), body.get("last"))
                except ValueError as exc:
                    raise ApiError(400, str(exc)) from None
                app.start_scan(req["limit"], req["since"], incremental=req["incremental"], mode=req["mode"],
                               last=req["last"])
                return app.job.snapshot()
            if path == "/api/scan":
                from ..scan import parse_since
                limit = body.get("limit")
                if limit not in (None, ""):
                    limit = int(limit)
                    if not 1 <= limit <= 1_000_000:
                        raise ApiError(400, "limit must be between 1 and 1,000,000")
                else:
                    limit = None
                try:
                    since = parse_since(body.get("since", "1y"))
                except ValueError as exc:
                    raise ApiError(400, str(exc)) from None
                app.start_scan(limit, since, incremental=not body.get("full"))
                return app.job.snapshot()
            if path == "/api/scan/cancel":
                app.job.control.cancel()
                return {"ok": True}
            if path == "/api/scan/pause":
                app.job.control.pause()
                app.job.update(paused=True)
                return {"ok": True, "paused": True}
            if path == "/api/scan/resume":
                app.job.control.resume()
                app.job.update(paused=False)
                return {"ok": True, "paused": False}
            if path == "/api/plan":                       # STEP 1 of 2: describe only
                action, targets = str(body.get("action")), list(body.get("targets") or [])
                options, dry = dict(body.get("options") or {}), bool(body.get("dry_run"))
                if app.active == ALL and not body.get("account"):
                    return actions.build_group_plan(app.sources, action, targets, options, dry_run=dry)
                src = app.source_for(str(body["account"])) if body.get("account") else app.source()
                return actions.build_plan(src, action, targets, options, dry_run=dry).to_dict()
            if path == "/api/confirm":                    # STEP 2 of 2: the confirm click quotes the count it showed + digest
                pid, count, digest = str(body.get("plan_id")), body.get("count"), str(body.get("digest") or "")
                if actions.is_group(pid):
                    return actions.confirm_group(app.sources, pid, count, digest, http_post=app.http_post,
                                                 opener=lambda u: None)
                plan_account = actions.get_plan(pid)["account"]
                src = app.source_for(plan_account) if app.active == ALL else app.source()
                return actions.confirm(src, pid, count, digest, http_post=app.http_post, opener=lambda u: None)
            if path == "/api/plan/cancel":
                actions.cancel_plan(str(body.get("plan_id")))
                return {"ok": True}
            if path == "/api/grant":
                src = app.source_for(str(body["account"])) if body.get("account") else app.source()
                src.grant(str(body.get("capability")))
                return {"ok": True, "source": src.describe()}
            if path == "/api/subscription/status":
                tracker.set_status(int(body["id"]), str(body["status"]))
                return {"ok": True}
            if path == "/api/subscription/dates":
                tracker.update_dates(int(body["id"]), body.get("trial_end") or None, body.get("renewal_date") or None, body.get("notes"))
                return {"ok": True}
            if path == "/api/keep":
                if body.get("remove"):
                    tracker.keep_remove(str(body.get("pattern")))
                else:
                    tracker.keep_add(str(body.get("pattern")), str(body.get("note") or ""))
                return {"patterns": tracker.keep_patterns()}
            if path == "/api/settings":
                return config.save_settings(dict(body))
            if path == "/api/privacy/draft":
                return privacy.draft(app.source().account, str(body.get("domain")), str(body.get("law")),
                                     str(body.get("name") or ""), str(body.get("email") or ""), str(body.get("to") or ""))
            if path == "/api/privacy/recipient":
                return privacy.update_recipient(int(body["id"]), str(body.get("to") or ""))
            if path == "/api/sender/body-link":       # read-only: looks in one email, opens nothing
                account = str(body.get("account") or app.active or "")
                return app.body_link(account, str(body.get("address") or ""))
            if path == "/api/link":
                links.save_link(str(body.get("domain")), str(body.get("url")))
                return {"ok": True}
            if path == "/api/import":
                return {"imported": tracker.import_json(body)}
            raise ApiError(404, "Not found")

    return Handler


class _Sent(Exception):
    """Response already written (file downloads)."""


def _guides():
    from .. import regions
    out = []
    for r in regions.all_regions():
        for g in r.revoke_guides:
            out.append({"region": r.code, **asdict(g)})
    return out


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup
        # that can stall startup for 30+ s on macOS. The name is never used.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return  # the browser closed the connection; nothing to report
        super().handle_error(request, client_address)


def make_server(app: App | None = None, port: int = 0):
    app = app or App()
    # A scan of an earlier session that was closed or killed mid-way gets its
    # reason recorded now, so the page can show it (last_failure).
    tracker.close_abandoned()
    httpd = _Server(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]
    return httpd, app


def warm_up() -> None:
    """Create/migrate the database and read the settings once before the
    browser opens, so the first screen does not pay for it (measured on
    Windows: /api/state on a new data folder ~150 ms, afterwards ~10 ms)."""
    try:
        tracker.accounts()
        config.load_settings()
    except Exception:  # noqa: BLE001 - the first API call reports any real problem
        pass


def serve(port: int = 8765, open_browser: bool = True, app: App | None = None):
    import webbrowser
    try:
        httpd, app = make_server(app, port)
    except OSError:
        httpd, app = make_server(app, 0)  # port busy: pick a free one
    warm_up()
    url = f"http://127.0.0.1:{app.port}/"
    print(f"Clawback is running at {url}  (only this computer can reach it). Press Ctrl+C to stop.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
