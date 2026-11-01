"""Shared message extraction for every mail source (Gmail API, IMAP, mbox/.eml).

All sources produce the same ``MessageInfo`` so detection behaves identically no
matter where the mail came from. Bodies are bounded (``MAX_BODY_CHARS``) and only
kept in memory during a scan; the local database stores metadata, not bodies.
"""
from __future__ import annotations

import base64
import email
import email.policy
import html as htmlmod
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

MAX_BODY_CHARS = 30_000
# Messages larger than this are truncated before MIME parsing. Receipts and
# newsletters are far smaller; the bytes past this point are almost always
# attachments, which we never need.
MAX_MESSAGE_BYTES = 1_000_000


@dataclass
class MessageInfo:
    source_id: str
    from_addr: str
    from_name: str
    domain: str
    subject: str
    date: datetime | None
    body: str = ""
    list_unsubscribe: list[str] = field(default_factory=list)
    one_click: bool = False
    list_id: str = ""
    precedence: str = ""
    auth_results: str = ""
    reply_to: str = ""
    labels: set[str] = field(default_factory=set)
    unread: bool | None = None
    thread_id: str = ""
    folder: str = ""
    # Unsubscribe link found in the body (mail with no List-Unsubscribe
    # header); "" when none. body_seen: a body was read for this message.
    body_unsub: str = ""
    body_seen: bool = False

    @property
    def is_bulk(self) -> bool:
        return bool(self.list_unsubscribe or self.list_id or self.precedence in {"bulk", "list", "junk"}
                    or self.labels & {"CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS", "CATEGORY_SOCIAL"})

    @property
    def is_spam_folder(self) -> bool:
        return "SPAM" in self.labels


def decode_mime_header(value) -> str:
    """Decode RFC 2047 encoded words (``=?UTF-8?B?...?=``); never raises."""
    if value is None:
        return ""
    value = str(value)
    if "=?" not in value:
        return " ".join(value.split())
    try:
        return " ".join(str(make_header(decode_header(value))).split())
    except (UnicodeError, LookupError, ValueError, email.errors.HeaderParseError):
        parts = []
        for chunk, charset in decode_header(value):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(charset or "utf-8", errors="replace") if _known_charset(charset) else chunk.decode("utf-8", errors="replace"))
            else:
                parts.append(chunk)
        return " ".join("".join(parts).split())


def _known_charset(charset: str | None) -> bool:
    import codecs
    try:
        codecs.lookup(charset or "utf-8")
        return True
    except LookupError:
        return False


_BLOCK_TAGS = re.compile(r"(?i)<\s*(br|/p|/div|/tr|/li|/h[1-6]|/table|p|div|tr|li|h[1-6])\b[^>]*>")
_HIDDEN = re.compile(r"(?is)<(script|style|noscript|head|title)\b[^>]*>.*?</\1\s*>")
_HIDDEN_STYLE = re.compile(r"(?is)<(\w+)\b[^>]*style\s*=\s*['\"][^'\"]*(display\s*:\s*none|visibility\s*:\s*hidden)[^'\"]*['\"][^>]*>.*?</\1\s*>")
_CELL = re.compile(r"(?i)<\s*/?\s*(td|th)\b[^>]*>")


def html_to_text(markup: str) -> str:
    """Visible text from HTML, with line breaks at block elements.

    Script/style/head and inline ``display:none`` blocks are removed first so
    hidden "preheader" or tracking text cannot trigger a detection.
    """
    text = _HIDDEN.sub(" ", markup)
    text = _HIDDEN_STYLE.sub(" ", text)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _CELL.sub("  ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmlmod.unescape(text).replace(" ", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _combine(plain: list[str], html: list[str]) -> str:
    # Prefer text/plain when it has real content; many senders ship a stub
    # ("View this email in your browser") as text/plain and the receipt as HTML.
    plain_text = "\n".join(p for p in plain if p.strip())
    html_text = "\n".join(html_to_text(h) for h in html) if html else ""
    if html_text and len(plain_text.strip()) < 40 and len(html_text) > len(plain_text):
        return html_text[:MAX_BODY_CHARS]
    return (plain_text or html_text)[:MAX_BODY_CHARS]


def parts_from_email(msg: Message) -> tuple[list[str], list[str]]:
    """Decoded text/plain and text/html parts (attachments skipped)."""
    plain, html = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except (ValueError, TypeError, AssertionError):
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset if _known_charset(charset) else "utf-8", errors="replace")
        (plain if ctype == "text/plain" else html).append(text)
    return plain, html


def body_from_email(msg: Message) -> str:
    return _combine(*parts_from_email(msg))


def parts_from_gmail_payload(payload: dict) -> tuple[list[str], list[str]]:
    plain, html = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime in {"text/plain", "text/html"} and not (part.get("filename") or ""):
            try:
                raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
                charset = "utf-8"
                for h in part.get("headers", []) or []:
                    if h.get("name", "").lower() == "content-type":
                        m = re.search(r'charset="?([\w-]+)', h.get("value", ""), re.I)
                        if m and _known_charset(m.group(1)):
                            charset = m.group(1)
                (plain if mime == "text/plain" else html).append(raw.decode(charset, errors="replace"))
            except (ValueError, UnicodeError):
                pass
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload or {})
    return plain, html


def body_from_gmail_payload(payload: dict) -> str:
    return _combine(*parts_from_gmail_payload(payload))


# ---------------------------------------------------------------- unsubscribe link in the body
# Words that label an unsubscribe link, in the languages Clawback's users
# mail in most. Kept to phrases that mean "stop these emails"; generic words
# ("click here", "settings") are deliberately absent.
UNSUB_WORDS = re.compile(
    r"(?i)unsubscribe|\bunsub\b|\bopt[\s-]?out\b|"
    r"\b(?:manage|update|change|edit|configure)\s+(?:your\s+|my\s+)?(?:e-?mail\s+|subscription\s+|communication\s+|notification\s+|newsletter\s+)?"
    r"(?:preferences|subscriptions?)\b|\be-?mail\s+(?:preferences|settings)\b|\bsubscription\s+preferences\b|"
    r"\bstop\s+(?:receiving\s+|getting\s+)?(?:these\s+|our\s+|all\s+)?(?:e-?mails|messages|newsletters)\b|"
    r"\bno\s+longer\s+(?:wish|want)\s+to\s+(?:receive|get)\b|"
    r"darse\s+de\s+baja|dar(?:te|se)?\s+de\s+baja|cancelar\s+(?:la\s+)?suscripci[oó]n|desuscrib|"
    r"d[ée]sabonn|d[ée]sinscri|abmelden|abbestellen|"
    r"descadastr|cancelar\s+(?:a\s+)?inscri[cç][aã]o|disiscriv|annulla\s+l'iscrizione|cancella\s+iscrizione|"
    r"afmelden|uitschrijven|wypisz|отпис|"
    r"सदस्यता\s*समाप्त|सदस्यता\s*रद्द|अनसब्सक्राइब|"
    r"配信停止|購読停止|配信解除|退订|取消订阅|退訂|取消訂閱|수신\s*거부|구독\s*취소|abonelikten\s+[çc][ıi]k|berhenti\s+berlangganan")
# Path words a sender puts in the URL of its unsubscribe page.
UNSUB_PATH = re.compile(r"(?i)unsub|opt-?out|/preferences|email-?pref|manage-?subscription|subscription-?center|"
                        r"abmelden|desinscri|desabonn|/baja")
NEAR = 400            # characters between the words and the link (HTML source, or text before a URL)
_ANCHOR = re.compile(r"(?is)<a\b([^>]*)>(.*?)</a\s*>")
_HREF = re.compile(r"""(?is)\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_ALT = re.compile(r"""(?is)\b(?:alt|title|aria-label)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_URL = re.compile(r"""https?://[^\s<>"'\])]+""", re.I)


def _blank(m: re.Match) -> str:
    return " " * (m.end() - m.start())   # keeps every other position unchanged


def _link_in_html(markup: str) -> tuple[float, str] | None:
    # Hidden parts (style, script, head, display:none preheaders) can't label a link.
    src = _HIDDEN.sub(_blank, markup)
    src = _HIDDEN_STYLE.sub(_blank, src)
    src = re.sub(r"(?s)<!--.*?-->", _blank, src)
    anchors = []
    for m in _ANCHOR.finditer(src):
        h = _HREF.search(m.group(1))
        if not h:
            continue
        href = htmlmod.unescape(next(g for g in h.groups() if g is not None)).strip()
        if not re.match(r"(?i)https?://", href) or len(href) > 2000:
            continue
        label = re.sub(r"<[^>]+>", " ", m.group(2)) + " " + " ".join(
            next((g for g in a.groups() if g is not None), "") for a in _ALT.finditer(m.group(0)))
        anchors.append((m.start(), m.end(), href, htmlmod.unescape(label)))
    if not anchors:
        return None
    inside = [(a, b) for a, b, _, _ in anchors]
    words = [w for w in UNSUB_WORDS.finditer(src) if not any(a <= w.start() < b for a, b in inside)]
    best: tuple[float, int, str] | None = None
    for start, end, href, label in anchors:
        if UNSUB_WORDS.search(label):
            score = 0.0
        else:
            gaps = [start - w.end() if w.end() <= start else w.start() - end for w in words]
            gap = min((g for g in gaps if g >= 0), default=None)
            if gap is not None and gap <= NEAR:
                score = 1 + gap / (NEAR + 1)       # nearest to the words wins
            elif UNSUB_PATH.search(href):
                score = 2.0
            else:
                continue
        if best is None or (score, -start) < (best[0], -best[1]):   # ties: the later one (footers)
            best = (score, start, href)
    return (best[0], best[2]) if best else None


def _link_in_text(text: str) -> tuple[float, str] | None:
    best: tuple[float, int, str] | None = None
    for m in _URL.finditer(text):
        url = m.group(0).rstrip(".,;:!?")
        before = text[max(0, m.start() - NEAR):m.start()]
        after = text[m.end():m.end() + 80].split("\n", 1)[0]
        hits = list(UNSUB_WORDS.finditer(before))
        if hits:
            score = 1 + (len(before) - hits[-1].end()) / (NEAR + 1)
        elif UNSUB_WORDS.search(after):
            score = 1.5
        elif UNSUB_PATH.search(url):
            score = 2.0
        else:
            continue
        if best is None or (score, -m.start()) < (best[0], -best[1]):
            best = (score, m.start(), url)
    return (best[0], best[2]) if best else None


def find_unsubscribe_link(plain: list[str], html: list[str]) -> str:
    """The body link most likely to be the sender's unsubscribe page: an
    anchor labelled with one of UNSUB_WORDS first, then the link nearest
    those words, then a link whose path says unsubscribe. HTTP(S) only; it is
    never fetched here (the user opens it after confirming)."""
    found = [r for r in [*(_link_in_html(h) for h in html), *(_link_in_text(p) for p in plain)] if r]
    return min(found, key=lambda r: r[0])[1] if found else ""


def domain_of(address: str) -> str:
    if "@" not in (address or ""):
        return "unknown.invalid"
    return address.rsplit("@", 1)[1].strip().lower().rstrip(".>")


def parse_list_unsubscribe(value: str) -> list[str]:
    """RFC 2369 List-Unsubscribe: comma-separated <URI> list. Only https/http/mailto kept."""
    value = decode_mime_header(value)
    uris = re.findall(r"<\s*([^>]+?)\s*>", value)
    if not uris and value.strip():
        uris = [u.strip() for u in value.split(",")]
    out = []
    for uri in uris:
        uri = re.sub(r"\s+", "", uri)
        if re.match(r"(?i)^(https?|mailto):", uri) and uri not in out:
            out.append(uri)
    return out


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _from_headers(get, source_id: str, body: str, labels: set[str], unread: bool | None,
                  internal_date: datetime | None = None, folder: str = "", thread_id: str = "") -> MessageInfo:
    name, addr = parseaddr(decode_mime_header(get("from")))
    addr = addr.strip().lower()
    unsub = parse_list_unsubscribe(get("list-unsubscribe") or "")
    post = (get("list-unsubscribe-post") or "").replace(" ", "").lower()
    reply = getaddresses([decode_mime_header(get("reply-to"))])
    return MessageInfo(
        source_id=source_id,
        from_addr=addr,
        from_name=name.strip(),
        domain=domain_of(addr),
        subject=decode_mime_header(get("subject"))[:500],
        date=_parse_date(get("date")) or internal_date,
        body=body,
        list_unsubscribe=unsub,
        # RFC 8058 §3.1: the POST header must be exactly this key/value pair.
        one_click="list-unsubscribe=one-click" in post and any(u.lower().startswith("https:") for u in unsub),
        list_id=decode_mime_header(get("list-id"))[:200],
        precedence=(get("precedence") or "").strip().lower(),
        # Only the topmost Authentication-Results is trusted: it is added by the
        # receiving provider, while lower ones can be written by anyone.
        # ARC-Authentication-Results (i=1) is the fallback when no A-R exists.
        auth_results=" ".join((get("authentication-results", first=True)
                               or get("arc-authentication-results", first=True) or "").split())[:2000],
        reply_to=(reply[0][1].lower() if reply and reply[0][1] else ""),
        labels=labels,
        unread=unread,
        folder=folder,
        thread_id=thread_id,
    )


def from_email_message(msg: Message, source_id: str, unread: bool | None = None,
                       labels: set[str] | None = None, folder: str = "") -> MessageInfo:
    def get(name: str, first: bool = False) -> str:
        try:
            values = msg.get_all(name) or []
        except (TypeError, ValueError):
            return ""
        if first:
            return str(values[0]) if values else ""
        return ", ".join(str(v) for v in values)
    plain, html = parts_from_email(msg)
    info = _from_headers(get, source_id, _combine(plain, html), labels or set(), unread, folder=folder)
    _with_body_link(info, plain, html)
    return info


def _with_body_link(info: MessageInfo, plain: list[str], html: list[str]) -> None:
    info.body_seen = any(p.strip() for p in plain + html)
    if info.body_seen and not info.list_unsubscribe:
        info.body_unsub = find_unsubscribe_link(plain, html)


def from_bytes(raw: bytes, source_id: str, **kw) -> MessageInfo:
    msg = email.message_from_bytes(raw[:MAX_MESSAGE_BYTES], policy=email.policy.compat32)
    return from_email_message(msg, source_id, **kw)


def from_gmail_message(m: dict) -> MessageInfo:
    payload = m.get("payload", {}) or {}
    headers: dict[str, list[str]] = {}
    for h in payload.get("headers", []) or []:
        headers.setdefault(h.get("name", "").lower(), []).append(h.get("value", ""))

    def get(name: str, first: bool = False) -> str:
        values = headers.get(name, [])
        return (values[0] if values else "") if first else ", ".join(values)
    labels = set(m.get("labelIds", []) or [])
    internal = None
    if m.get("internalDate"):
        try:
            internal = datetime.fromtimestamp(int(m["internalDate"]) / 1000, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            internal = None
    plain, html = parts_from_gmail_payload(payload)
    body = _combine(plain, html) or htmlmod.unescape(m.get("snippet", "") or "")
    info = _from_headers(get, m.get("id", ""), body, labels, "UNREAD" in labels, internal, thread_id=m.get("threadId", ""))
    _with_body_link(info, plain, html)
    if internal:  # Gmail's internal date is the receive time; more reliable than the Date header.
        info.date = internal
    return info
