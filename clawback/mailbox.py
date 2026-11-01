"""Scan an IMAP mailbox (read-only) using the shared extraction code.

Speed: one FETCH per CHUNK messages for headers only, then one FETCH per
chunk for the full bodies of billing candidates (see prefilter.py). IMAP has
no per-request quota like the Gmail API, so a header pass over thousands of
messages takes seconds; only round trips and bytes matter. Folders are opened
with EXAMINE (read-only) and every fetch uses BODY.PEEK, so nothing is ever
marked as read.
"""
from __future__ import annotations

import imaplib
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

from .detect import Hit, classify_message
from .extract import MessageInfo, from_bytes
from .prefilter import needs_body
from .progress import checkpoint

DEFAULT_FOLDERS = ["INBOX"]
# 200 UIDs per FETCH: a header fetch of 200 messages is ~0.5 MB, small enough
# to keep progress smooth (one update every fraction of a second) and large
# enough that round-trip latency stops mattering (~25 round trips per 5,000).
CHUNK = 200
HEADER_FIELDS = ("FROM SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST LIST-ID PRECEDENCE "
                 "AUTHENTICATION-RESULTS ARC-AUTHENTICATION-RESULTS REPLY-TO")
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_UID = re.compile(rb"\bUID (\d+)")
_FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")
_INTERNAL = re.compile(rb'INTERNALDATE "([^"]+)"')
_LABELS = re.compile(rb"X-GM-LABELS \(([^)]*)\)")


def imap_date(d: datetime) -> str:
    """IMAP SEARCH date (RFC 3501 date-text, month names are fixed English)."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


_MSGID = re.compile(rb"X-GM-MSGID (\d+)")
# One ID-map FETCH covers this many UIDs. "(UID X-GM-MSGID)" is ~45 bytes per
# message, so 5,000 is ~225 KB: one round trip for a typical year of mail.
ID_CHUNK = 5000


def _uidset(uids: list[bytes]) -> str:
    """Compact UID set: consecutive UIDs become ranges ("1:200,305,310:400"),
    keeping commands short however many messages are fetched."""
    nums = sorted({int(u) for u in uids})
    parts, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(str(nums[i]) if i == j else f"{nums[i]}:{nums[j]}")
        i = j + 1
    return ",".join(parts)


def gmail_raw_query(since: datetime | None) -> str:
    """X-GM-RAW search: the same syntax as the Gmail search box, to the
    second (epoch ``after:``), excluding what you sent and chats server-side."""
    q = "-in:sent -in:chats"
    if since is not None:
        q = f"after:{int(since.timestamp()) - 1} " + q
    return '"' + q + '"'


def xoauth2_string(user: str, token: str) -> bytes:
    """SASL XOAUTH2 initial response (Google and Microsoft IMAP)."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()


def _login(conn, user: str, password: str | None, oauth_token=None, host: str = "", provider: dict | None = None,
           users: list[str] | None = None) -> None:
    """Sign in. ``users``: user names to try in order (iCloud documents the
    part before the @ first, then the full address); only a refused sign-in
    moves on to the next one."""
    from .sources.base import AuthError
    candidates = users or [user]
    last = None
    for i, name in enumerate(candidates):
        try:
            if oauth_token is not None:
                token = oauth_token() if callable(oauth_token) else oauth_token
                conn.authenticate("XOAUTH2", lambda _challenge, n=name: xoauth2_string(n, token))
            else:
                conn.login(name, password)
            return
        except imaplib.IMAP4.error as exc:
            last = exc
            if i == len(candidates) - 1:
                break
    detail = last.args[0].decode(errors="replace") if last.args and isinstance(last.args[0], bytes) else str(last)
    raise AuthError(f"{login_hint(host, detail, oauth_token is not None, provider)} Server said: {detail}") from last


# Phrases servers use when the normal password is refused because the account
# needs an app password (Gmail: "[ALERT] Application-specific password
# required"; others say "app password" or ask for a web sign-in first).
_APP_PASSWORD_WORDS = ("application-specific password", "app password", "app-specific password", "web login required")


def login_hint(host: str, detail: str, oauth: bool, provider: dict | None = None) -> str:
    """Plain-language reason for a refused IMAP sign-in, with the provider's
    own app-password page when one is known."""
    from . import providers as prov
    h = (host or "").lower()
    if oauth:
        if "gmail" in h or "googlemail" in h:
            return "Gmail refused the Google sign-in token. Connect again to sign in afresh."
        return f"{host or 'The server'} refused the sign-in token (expired or revoked). Connect again to sign in afresh."
    if prov.is_microsoft_host(h):
        return (prov.get("outlook")["blocked"] + " (Microsoft 365 work and school accounts: your organisation decides; "
                "'Sign in with Microsoft' is the supported route.)")
    if "gmail" in h or "googlemail" in h:
        return ("Gmail refused the sign-in. Use a 16-letter app password from https://myaccount.google.com/apppasswords "
                "(your normal password does not work; 2-Step Verification must be on).")
    provider = provider or {}
    name = provider.get("name") or host or "The server"
    ap = provider.get("app_password") or {}
    wants_app_pw = any(w in (detail or "").lower() for w in _APP_PASSWORD_WORDS)
    if provider.get("auth") == "bridge":
        return ("Proton Mail Bridge refused the sign-in. Use the password shown in Bridge under Mailbox details, "
                "not your Proton password.")
    where = f" Make one at {ap['url']}" if ap.get("url") else ""
    needs = f" (needs {ap['needs']})" if ap.get("needs") else ""
    if wants_app_pw:
        return f"{name} wants an app password here, not your normal password.{where}{needs}."
    if ap:
        return (f"{name} refused the sign-in: check the address and the password. Mail apps need an app password, "
                f"not your normal password.{where}{needs}.")
    return (f"{name} refused the sign-in: check the address and the password. Many providers need an app password "
            "for IMAP rather than your normal password.")


# ------------------------------------------------------------------ connecting
# 30 s covers a slow mobile link and a server that is busy at login; a host
# that does not answer at all should fail with a message, not hang the scan.
TIMEOUT = 30


def open_imap(host: str, port: int = 993, security: str = "ssl", cafile: str | None = None,
              timeout: float = TIMEOUT):
    """An IMAP connection with certificate checks always on. ``security``:
    "ssl" (implicit TLS, usually port 993) or "starttls" (plain connect, then
    STARTTLS before anything else is sent, e.g. Proton Mail Bridge on 1143).
    ``cafile``: trust this certificate (Bridge's exported one) instead of the
    system store; verification is never switched off."""
    import ssl
    ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    if security == "starttls":
        conn = imaplib.IMAP4(host, port, timeout=timeout)
        try:
            conn.starttls(ssl_context=ctx)
        except BaseException:
            try:
                conn.shutdown()
            except OSError:
                pass
            raise
        return conn
    if security != "ssl":
        raise ValueError("security must be ssl or starttls")
    return imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=timeout)


def connection_error(exc: BaseException, host: str, port: int | None = None, provider: dict | None = None):
    """A SourceError the user can act on, for a connection that failed before
    any sign-in (wrong host, nothing listening, TLS refused or not trusted)."""
    import socket
    import ssl
    from .sources.base import SourceError
    where = f"{host}:{port}" if port else host
    bridge = (provider or {}).get("auth") == "bridge"
    if isinstance(exc, ssl.SSLCertVerificationError):
        if bridge:
            msg = (f"The secure connection to Proton Mail Bridge ({where}) could not be checked. Export Bridge's "
                   "certificate (Settings > Advanced settings > Export TLS certificates) and choose that file.")
        else:
            msg = (f"The secure connection to {where} could not be checked: the server's certificate is not trusted "
                   f"({exc.verify_message or 'verification failed'}). Check the server name. Clawback never connects "
                   "without a valid certificate.")
    elif isinstance(exc, ssl.SSLError):
        msg = (f"A secure (TLS) connection to {where} failed ({getattr(exc, 'reason', None) or exc}). Check the port and the security "
               "setting: SSL/TLS is usually port 993, STARTTLS usually 143.")
    elif isinstance(exc, ConnectionRefusedError):
        msg = (f"Nothing answered at {where}. Is Proton Mail Bridge running and signed in? Check the port shown in "
               "Bridge under Mailbox details." if bridge else
               f"{where} refused the connection. Check the server name and port.")
    elif isinstance(exc, socket.gaierror):
        msg = f"The server name {host} was not found. Check it for typing mistakes."
    elif isinstance(exc, (TimeoutError, socket.timeout)):
        msg = f"{where} did not answer within {TIMEOUT} seconds. Check the server name, port and your connection."
    elif isinstance(exc, imaplib.IMAP4.error) and ("STARTTLS" in str(exc).upper() or "TLS NOT SUPPORTED" in str(exc).upper()):
        msg = f"{where} does not offer STARTTLS. Try the SSL/TLS setting (usually port 993)."
    else:
        msg = (f"Could not connect to {where}. Check the server name, the port and your internet connection, "
               f"then try again. Technical details: {type(exc).__name__}: {exc}")
    return SourceError(msg)


# ------------------------------------------------------------------ folders
# RFC 6154 special-use attributes. Folder NAMES are chosen by each provider and
# translated into the account's language ("[Gmail]/Alle Nachrichten", "Bulk",
# "Junk-E-Mail", "&BCEEPwQwBDw-" = "Спам"), so Clawback picks folders by these
# attributes only and never by name.
SPECIAL_USE = ("\\All", "\\Archive", "\\Junk", "\\Trash", "\\Sent", "\\Drafts", "\\Flagged", "\\Important")
_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$', re.I)


def _unquote(raw: bytes) -> str:
    raw = raw.strip()
    if raw.startswith(b'"') and raw.endswith(b'"'):
        raw = raw[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    return raw.decode("utf-8", errors="replace")


_CANON = {f.lower(): f for f in SPECIAL_USE}


def _flags(raw: bytes) -> set[str]:
    """Attributes as sent; special-use ones in their RFC spelling (servers
    differ in case: "\\JUNK", "\\junk")."""
    out = set()
    for f in raw.split():
        text = f.decode(errors="replace")
        out.add(_CANON.get(text.lower(), text))
    return out


def list_folders(conn) -> list[tuple[str, set[str]]]:
    """[(raw folder name, {attributes})] from LIST. Names sent as literals
    (imaplib returns those as tuples) are handled too."""
    try:
        typ, data = conn.list()
    except (AttributeError, imaplib.IMAP4.error):
        return []
    if typ != "OK":
        return []
    out = []
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):
            head, name = item[0], item[1]
            m = re.match(rb'^\((?P<flags>[^)]*)\)', head)
            out.append((name.decode("utf-8", errors="replace"), _flags(m.group("flags") if m else b"")))
            continue
        m = _LIST_LINE.match(item)
        if m:
            out.append((_unquote(m.group("name")), _flags(m.group("flags"))))
    return out


def decode_folder(name: str) -> str:
    """Modified UTF-7 (RFC 3501 5.1.3) folder name as readable text, for
    display only (the raw name is what gets selected)."""
    import base64
    out, i = [], 0
    while i < len(name):
        if name[i] == "&":
            j = name.find("-", i)
            if j == -1:
                out.append(name[i:])
                break
            if j == i + 1:
                out.append("&")
            else:
                b64 = name[i + 1:j].replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except (ValueError, UnicodeDecodeError):
                    out.append(name[i:j + 1])
            i = j + 1
        else:
            out.append(name[i])
            i += 1
    return "".join(out)


def choose_folders(listed: list[tuple[str, set[str]]], host: str = "") -> tuple[list[str], dict]:
    """Folders to scan, from the server's own special-use attributes.

    * A folder marked \\All (Gmail's "All Mail" in any language) holds every
      message except Spam and Trash: scan only that, like the Gmail API.
    * Otherwise INBOX plus every \\Archive folder (archived newsletters still
      count as mail from that sender).
    * \\Junk, \\Trash, \\Sent and \\Drafts are never scanned.

    Returns (folders, roles); roles maps each special use found to its folder
    names, shown to the user ("Spam folder: Bulk")."""
    roles: dict[str, list[str]] = {}
    for name, flags in listed:
        for f in flags:
            if f in SPECIAL_USE:
                roles.setdefault(f.strip("\\").lower(), []).append(name)
    if roles.get("all"):
        return roles["all"][:1], roles
    if not listed and host.lower().strip() in {"imap.gmail.com", "imap.googlemail.com"}:
        return ["[Gmail]/All Mail"], roles      # LIST failed: Gmail's English default
    inbox = next((n for n, _ in listed if n.upper() == "INBOX"), "INBOX")
    skip = {n for k in ("junk", "trash", "sent", "drafts") for n in roles.get(k, [])}
    archive = [n for n in roles.get("archive", []) if n not in skip and n.upper() != "INBOX"]
    return [inbox, *archive], roles


def _parse_fetch(data) -> dict[str, tuple[bytes, bytes]]:
    """{uid: (meta, literal)} from an imaplib FETCH response. Servers may put
    UID/FLAGS before or after the literal, so both halves are searched."""
    out: dict[str, tuple[bytes, bytes]] = {}
    items = list(data or [])
    for i, item in enumerate(items):
        if not isinstance(item, tuple):
            continue
        meta = item[0] if isinstance(item[0], bytes) else str(item[0]).encode()
        tail = items[i + 1] if i + 1 < len(items) and isinstance(items[i + 1], bytes) else b""
        both = meta + b" " + tail
        m = _UID.search(both)
        if m:
            out[m.group(1).decode()] = (both, item[1])
    return out


def _internal_date(meta: bytes) -> datetime | None:
    m = _INTERNAL.search(meta)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).decode(), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


def _is_sent(meta: bytes) -> bool:
    gm = _LABELS.search(meta)
    return bool(gm and b"\\Sent" in gm.group(1))


def _info(meta: bytes, raw: bytes, folder: str, uid: str, ident: str | None = None) -> MessageInfo:
    flags = _FLAGS.search(meta)
    unread = None if flags is None else (b"\\Seen" not in flags.group(1))
    labels = set()
    gm = _LABELS.search(meta)
    if gm and b"\\Important" in gm.group(1):
        labels.add("IMPORTANT")
    info = from_bytes(raw, ident or f"{folder}:{uid}", unread=unread, folder=folder, labels=labels)
    if info.date is None:
        info.date = _internal_date(meta)
    return info


def iter_messages(host: str, user: str, password: str | None, folders: list[str] | None = None,
                  limit: int | None = None, conn_factory=imaplib.IMAP4_SSL, progress=None, cancel=None,
                  since: datetime | None = None, skip_ids=None, stats: dict | None = None,
                  oauth_token=None, provider: dict | None = None, users: list[str] | None = None,
                  port: int | None = None) -> Iterator[MessageInfo]:
    """Yield MessageInfo newest first across ``folders``, back to ``since``.

    On Gmail (X-GM-EXT-1) the date window is an X-GM-RAW search to the second
    that also drops sent mail and chats on the server, and each message is
    identified by X-GM-MSGID in hex, which is the same ID the Gmail API uses:
    a rescan skips known mail before fetching any headers, and switching
    between the API and IMAP never counts a message twice.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    skip = set(skip_ids or ())
    stats = stats if stats is not None else {}
    stats.update(bodies=0, reused=0, capped=False, gmail_ext=False, round_trips=0)
    import socket
    import ssl
    try:
        conn = conn_factory(host)
    except (OSError, ssl.SSLError, socket.timeout, imaplib.IMAP4.error) as exc:
        raise connection_error(exc, host, port, provider) from exc

    def call(*args):
        stats["round_trips"] += 1
        return conn.uid(*args)

    try:
        _login(conn, user, password, oauth_token, host.lower(), provider, users)
        if not folders:
            # Pick folders by what the server says they are, never by name.
            folders, roles = choose_folders(list_folders(conn), host)
            stats["roles"] = roles
        stats["folders"] = list(folders)
        gmail_ext = False
        try:
            typ, caps = conn.capability()
            gmail_ext = typ == "OK" and b"X-GM-EXT-1" in b" ".join(c for c in caps if isinstance(c, bytes))
        except (AttributeError, imaplib.IMAP4.error):
            pass
        stats["gmail_ext"] = gmail_ext
        plan: list = []
        for folder in folders:
            checkpoint(cancel)
            status, _ = conn.select(_quote(folder), readonly=True)
            if status != "OK":
                continue
            status, data = ("NO", None)
            if gmail_ext:
                status, data = call("SEARCH", "X-GM-RAW", gmail_raw_query(since))
            if status != "OK":
                if since is not None:
                    # SEARCH SINCE compares dates in the server's time zone; ask
                    # one day earlier and drop older messages by their own date.
                    status, data = call("SEARCH", None, "SINCE", imap_date(since - timedelta(days=1)))
                else:
                    status, data = call("SEARCH", None, "ALL")
            if status != "OK" or not data or not data[0]:
                continue
            uids = list(reversed(data[0].split()))    # UIDs ascend with arrival: newest first
            if limit is not None and len(uids) > limit:
                # "the newest N": only this folder's newest N can be among them
                stats["capped"] = True
                uids = uids[:limit]
            ids: dict[str, str] = {}
            if gmail_ext:
                for start in range(0, len(uids), ID_CHUNK):
                    checkpoint(cancel)
                    status, got = call("FETCH", _uidset(uids[start:start + ID_CHUNK]), "(UID X-GM-MSGID)")
                    if status != "OK":
                        continue
                    for item in got or []:
                        meta = item[0] if isinstance(item, tuple) else item
                        if not isinstance(meta, bytes):
                            continue
                        u, g = _UID.search(meta), _MSGID.search(meta)
                        if u and g:
                            ids[u.group(1).decode()] = format(int(g.group(1)), "x")
            dates: dict[bytes, datetime | None] = {}
            if limit is not None and len(folders) > 1 and uids:
                # UIDs only order mail within one folder: the arrival dates
                # decide which folder's mail is newest overall (a short FETCH)
                for start in range(0, len(uids), ID_CHUNK):
                    checkpoint(cancel)
                    status, got = call("FETCH", _uidset(uids[start:start + ID_CHUNK]), "(UID INTERNALDATE)")
                    for item in (got or []) if status == "OK" else []:
                        meta = item[0] if isinstance(item, tuple) else item
                        u = _UID.search(meta) if isinstance(meta, bytes) else None
                        if u:
                            dates[u.group(1)] = _internal_date(meta)
            plan.append((folder, uids, ids, dates))
        if limit is not None and sum(len(p[1]) for p in plan) > limit:
            stats["capped"] = True
            floor = datetime.min.replace(tzinfo=timezone.utc)
            ranked = sorted(((p[3].get(u) or floor, -i, -n, u) for i, p in enumerate(plan) for n, u in enumerate(p[1])),
                            reverse=True)
            newest = {(-i, u) for _, i, _, u in ranked[:limit]}
            plan = [(f, [u for u in us if (i, u) in newest], ids, d) for i, (f, us, ids, d) in enumerate(plan)]
        # the limit counted the newest emails, those read by an earlier scan
        # included; only the ones not read yet are fetched
        fresh_plan = []
        for folder, uids, ids, _ in plan:
            key = lambda u, ids=ids, folder=folder: ids.get(u.decode()) or f"{folder}:{u.decode()}"  # noqa: E731
            fresh = [u for u in uids if key(u) not in skip]
            stats["reused"] += len(uids) - len(fresh)
            fresh_plan.append((folder, fresh, ids))
        plan = fresh_plan
        total = sum(len(u) for _, u, _ in plan)
        if progress:
            progress(0, total, phase="reading")
        done = 0
        items = "(UID FLAGS INTERNALDATE" + (" X-GM-LABELS" if gmail_ext else "") + f" BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
        for folder, uids, ids in plan:
            if not uids:
                continue
            checkpoint(cancel)
            conn.select(_quote(folder), readonly=True)
            for start in range(0, len(uids), CHUNK):
                checkpoint(cancel)
                chunk = uids[start:start + CHUNK]
                status, data = call("FETCH", _uidset(chunk), items)
                if status != "OK":
                    continue
                heads = _parse_fetch(data)
                # Gmail's "All Mail" also holds what you sent; that is not mail to clean up.
                heads = {u: v for u, v in heads.items() if not _is_sent(v[0])}
                infos = {uid: _info(meta, raw, folder, uid, ids.get(uid)) for uid, (meta, raw) in heads.items()}
                wanted = [uid for uid, info in infos.items() if needs_body(info)]
                if wanted:
                    status, data = call("FETCH", _uidset(wanted), "(UID FLAGS INTERNALDATE BODY.PEEK[])")
                    if status == "OK":
                        for uid, (meta, raw) in _parse_fetch(data).items():
                            if uid in infos:
                                infos[uid] = _info(heads[uid][0], raw, folder, uid, ids.get(uid))
                                stats["bodies"] += 1
                for u in chunk:
                    done += 1
                    info = infos.get(u.decode() if isinstance(u, bytes) else str(u))
                    if info is None:
                        continue
                    if since is not None and info.date is not None and info.date < since:
                        continue
                    if progress:
                        progress(done, total, current_date=info.date)
                    yield info
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def check_login(host: str, user: str, password: str | None, conn_factory=imaplib.IMAP4_SSL, oauth_token=None,
                provider: dict | None = None, users: list[str] | None = None, port: int | None = None) -> None:
    """Sign in and out again, reading nothing: raises AuthError (refused) or
    SourceError (server unreachable) with the same plain-language advice as
    a scan. Used before a new account replaces the one on screen."""
    import socket
    import ssl
    try:
        conn = conn_factory(host)
    except (OSError, ssl.SSLError, socket.timeout, imaplib.IMAP4.error) as exc:
        raise connection_error(exc, host, port, provider) from exc
    try:
        _login(conn, user, password, oauth_token, host.lower(), provider, users)
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def fetch_raw(host: str, user: str, password: str | None, idents: list[str], folders: list[str],
              conn_factory=imaplib.IMAP4_SSL, oauth_token=None, provider: dict | None = None,
              users: list[str] | None = None, port: int | None = None, cancel=None) -> dict[str, bytes]:
    """Full messages for the given scan ids, read-only (EXAMINE + BODY.PEEK).
    Ids are "folder:uid", or a Gmail X-GM-MSGID in hex (looked up in
    ``folders``). Ids that are gone are left out of the result."""
    import socket
    import ssl
    try:
        conn = conn_factory(host)
    except (OSError, ssl.SSLError, socket.timeout, imaplib.IMAP4.error) as exc:
        raise connection_error(exc, host, port, provider) from exc
    out: dict[str, bytes] = {}
    try:
        _login(conn, user, password, oauth_token, host.lower(), provider, users)
        by_folder: dict[str, list[tuple[str, str | None]]] = {}
        for ident in idents:
            if ":" in ident:
                folder, uid = ident.rsplit(":", 1)
                by_folder.setdefault(folder, []).append((ident, uid))
            else:
                for folder in folders[:1]:
                    by_folder.setdefault(folder, []).append((ident, None))
        for folder, wanted in by_folder.items():
            checkpoint(cancel)
            status, _ = conn.select(_quote(folder), readonly=True)
            if status != "OK":
                continue
            for ident, uid in wanted:
                checkpoint(cancel)
                if uid is None:
                    try:
                        status, data = conn.uid("SEARCH", "X-GM-MSGID", str(int(ident, 16)))
                    except (ValueError, imaplib.IMAP4.error):
                        continue
                    if status != "OK" or not data or not data[0]:
                        continue
                    uid = data[0].split()[0].decode()
                status, data = conn.uid("FETCH", uid, "(UID BODY.PEEK[])")
                if status != "OK":
                    continue
                for _u, (_meta, raw) in _parse_fetch(data).items():
                    out[ident] = raw
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
    return out


def _quote(folder: str) -> str:
    return folder if re.fullmatch(r"[A-Za-z0-9_./-]+", folder) else '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def scan(host: str, user: str, password: str, folders: list[str] | None = None,
         limit: int | None = 500, conn_factory=imaplib.IMAP4_SSL) -> Iterator[Hit]:
    """Yield subscription hits from the given IMAP account, newest messages first."""
    for info in iter_messages(host, user, password, folders, limit, conn_factory):
        hit = classify_message(info)
        if hit:
            yield hit


def utc(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
