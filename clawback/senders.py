"""Group messages by sender and classify each sender.

Categories (one per sender):
  * ``paid``       – at least one message looks like a recurring charge;
  * ``important``  – bank/card alerts and statements, government mail, broker
                     contract notes, security alerts, OTPs, receipts, work
                     notifications (see important.py); never suggested;
  * ``spam``       – spam-like signals reach the threshold (we never offer
                     unsubscribe links for these: clicking confirms your address);
  * ``newsletter`` – bulk/marketing mail, shown as "Newsletters & marketing"
                     (marketing from a company you have an account with is
                     marketing, not spam) (List-Unsubscribe, List-Id,
                     Precedence: bulk, or a Gmail Promotions/Social/Forums tab;
                     the Updates tab alone is not enough, see list_signal);
  * ``other``      – personal or transactional mail; never suggested.

Signal weights and the threshold are explained in docs/HEURISTICS.md.
"""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from urllib.parse import parse_qs, unquote, urlparse

from .detect import PROMO_SUBJECT, base_domain
from .extract import MessageInfo
from .important import is_important

SPAM_THRESHOLD = 3
STRONG, MEDIUM, WEAK = 3, 2, 1

# Email service providers (ESPs) that legitimately host unsubscribe links for
# other brands. A link on one of these is "handled by <provider>", not a
# mismatch. The list lives in data/email_providers.json, where every domain
# carries a source (provider docs, the provider's own site, or public DNS);
# unsourced domains are left out on purpose (rule 3 below, "signed this
# email", still covers a provider that DKIM-signs the mail).
def _load_providers() -> tuple[dict[str, str], list[tuple[re.Pattern, str]]]:
    raw = json.loads(resources.files("clawback").joinpath("data/email_providers.json").read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    patterns: list[tuple[re.Pattern, str]] = []
    for p in raw["providers"]:
        for d in p["domains"]:
            names[d] = p["name"]
        for pat in p.get("patterns", []):
            patterns.append((re.compile(pat), p["name"]))
    return names, patterns


ESP_NAMES, ESP_PATTERNS = _load_providers()
ESP_DOMAINS = set(ESP_NAMES)


def esp_name(host: str) -> str | None:
    """Provider name when ``host`` belongs to a known email service provider,
    else None."""
    base = base_domain(host)
    if base in ESP_NAMES:
        return ESP_NAMES[base]
    for pattern, name in ESP_PATTERNS:
        if pattern.match(base):
            return name
    return None


SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly", "cutt.ly", "shorturl.at", "rb.gy", "t.ly"}
FREEMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "aol.com", "icloud.com",
            "proton.me", "protonmail.com", "gmx.com", "mail.com", "yandex.com", "rediffmail.com", "zoho.com"}
SPAM_SUBJECT = re.compile(
    r"(?i)\b(?:you(?:'ve| have) won|winner|claim your (?:prize|reward)|lottery|congratulations!|act now|urgent action required|"
    r"verify your account (?:now|immediately)|account (?:suspended|will be closed)|100% free|risk[- ]free|crypto (?:giveaway|airdrop)|"
    r"final notice|limited time only|wire transfer|inheritance|bitcoin)\b"
)
# Brand names whose appearance in a display name from an unrelated domain is a
# spoofing signal. Generic words ("bank", "card") are deliberately absent: a
# real mailbox test showed "Bank of Baroda" mail from mybobcard.co.in being
# scored as spoofing because the display name contained "bank".
BRANDS = {"paypal", "apple", "amazon", "netflix", "microsoft", "google", "hdfc", "icici", "sbi", "fedex", "dhl", "irs", "hmrc"}


@dataclass
class SenderSummary:
    address: str
    domain: str
    display_name: str = ""
    message_count: int = 0
    unread_count: int = 0
    read_known: int = 0                 # messages whose read/unread state is known
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    list_unsubscribe: list[str] = field(default_factory=list)
    one_click: bool = False
    list_id: str = ""
    category: str = "other"
    spam_score: int = 0
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)      # informational, e.g. "handled by Brevo"
    signed: list[str] = field(default_factory=list)     # domains that passed DKIM/SPF on the latest email
    important: list[str] = field(default_factory=list)  # why it looks transactional (empty = not important)
    message_ids: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    bulk: bool = False
    body_unsub: str = ""                # unsubscribe link found in the newest email body read
    body_checked: bool = False          # a body was read for this sender (link or not)
    listed: bool = False                # some message had a mailing-list / marketing signal (list_signal)

    @property
    def unread_ratio(self) -> float | None:
        return round(self.unread_count / self.read_known, 2) if self.read_known else None

    def monthly_rate(self, now: datetime | None = None) -> float:
        return monthly_rate(self.message_count, self.first_seen, self.last_seen)


def monthly_rate(count: int, first: datetime | None, last: datetime | None) -> float:
    """Emails per month. The observed span is floored at 30 days so a sender
    seen 3 times in one afternoon is estimated at 3/month, not 2,000/month."""
    if not count:
        return 0.0
    if not first or not last:
        return float(count)
    days = max(30.0, (last - first).total_seconds() / 86400)
    return round(count / days * 30.44, 1)


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def mailto_address(uri: str) -> str:
    parsed = urlparse(uri)
    return unquote(parsed.path).strip().lower()


def mailto_parts(uri: str) -> tuple[str, str, str]:
    parsed = urlparse(uri)
    q = parse_qs(parsed.query)
    return unquote(parsed.path).strip(), (q.get("subject") or ["unsubscribe"])[0], (q.get("body") or ["unsubscribe"])[0]


def suspicious_host(host: str) -> str | None:
    """Reason a link host must never be contacted automatically, or None."""
    if not host:
        return "no host"
    if host in SHORTENERS:
        return "URL shortener hides the destination"
    if "xn--" in host:
        return "punycode (look-alike) domain"
    try:
        ipaddress.ip_address(host.strip("[]"))
        return "raw IP address instead of a domain"
    except ValueError:
        pass
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost", ".lan")):
        return "local network host"
    return None


def _org_label(domain: str) -> str:
    base = base_domain(domain)
    return base.split(".")[0] if base else ""


def related_domains(a: str, b: str) -> bool:
    ba, bb = base_domain(a), base_domain(b)
    if ba == bb:
        return True
    la, lb = _org_label(a), _org_label(b)
    # Same organisation name under a different suffix: sbi.co.in vs sbi.bank.in,
    # amazon.com vs amazon.in. Three letters is the shortest real bank/brand
    # label we must accept (sbi, bob); two-letter labels are too collision-prone.
    if la == lb and len(la) >= 3:
        return True
    # brand.com vs brandmail.com / brand-email.com style sending domains
    return len(la) >= 4 and len(lb) >= 4 and (la in lb or lb in la)


def authenticated_domains(auth: str) -> set[str]:
    """Domains that passed DKIM (header.d / header.i) or SPF (smtp.mailfrom)
    in the receiving provider's Authentication-Results header."""
    out: set[str] = set()
    for clause in re.split(r";", auth or ""):
        c = clause.strip().lower()
        if re.match(r"(?:\S+\s+)?dkim=pass\b", c) or re.search(r"\bdkim=pass\b", c):
            for m in re.finditer(r"header\.(?:d|i)=@?([a-z0-9.-]+\.[a-z]{2,})", c):
                out.add(m.group(1).rsplit("@", 1)[-1])
        if re.search(r"\bspf=pass\b", c):
            for m in re.finditer(r"smtp\.mailfrom=(?:[^@\s]*@)?([a-z0-9.-]+\.[a-z]{2,})", c):
                out.add(m.group(1))
    return {d.strip(".") for d in out}


def check_link(sender_domain: str, uri: str, signed: set[str] | frozenset = frozenset()) -> tuple[str, str] | None:
    """Judge one unsubscribe URI. Returns (level, text) or None when it simply
    belongs to the sender. Levels: "unsafe" (never contacted), "warn" (doesn't
    match and isn't a known provider: possible phishing), "info" (handled by
    an email provider or a domain that signed the email)."""
    is_mailto = uri.lower().startswith("mailto:")
    if is_mailto:
        addr = mailto_address(uri)
        host = addr.rsplit("@", 1)[-1] if "@" in addr else ""
        if not host:
            return None
    else:
        host = host_of(uri)
        why = suspicious_host(host)
        if why:
            return "unsafe", f"unsafe link: {why} ({host})"
        if uri.lower().startswith("http:"):
            return "unsafe", f"unencrypted http link ({host})"
    if related_domains(host, sender_domain):
        return None
    provider = esp_name(host)
    if provider is not None:
        base = base_domain(host)
        label = f"{provider} ({base})" if provider and provider != base else base
        return "info", f"unsubscribe is handled by {label}, an email provider"
    if any(related_domains(host, d) for d in signed):
        return "info", f"unsubscribe is handled by {base_domain(host)}, which signed this email (DKIM/SPF pass)"
    # One sentence per domain: the same wording for a link and a mailto on
    # that domain, so the two collapse into one line and one spam signal.
    return "warn", (f"unsubscribe goes to {base_domain(host)}, which isn't the sender ({base_domain(sender_domain)}) "
                    f"or a known email provider")


def _verdicts(sender_domain: str, uris: list[str], signed) -> list[tuple[str, str]]:
    """check_link for every URI, one entry per distinct sentence: a link and
    a mailto on the same domain give the same sentence and appear once."""
    out: list[tuple[str, str]] = []
    for v in filter(None, (check_link(sender_domain, u, signed) for u in uris)):
        if v not in out:
            out.append(v)
    return out


def link_findings(sender_domain: str, uris: list[str], signed=frozenset()) -> list[str]:
    """Warnings only (unsafe + mismatch); provider notes come from link_notes."""
    return [t for lvl, t in _verdicts(sender_domain, uris, signed) if lvl != "info"]


def link_notes(sender_domain: str, uris: list[str], signed=frozenset()) -> list[str]:
    return [t for lvl, t in _verdicts(sender_domain, uris, signed) if lvl == "info"]


def _auth_failures(auth: str) -> list[str]:
    out = []
    for mech in ("dmarc", "dkim", "spf"):
        m = re.search(rf"\b{mech}=(\w+)", auth or "", re.I)
        if m and m.group(1).lower() in {"fail", "softfail", "permerror"}:
            out.append(f"{mech.upper()} {m.group(1).lower()}")
    return out


def spam_signals(info: MessageInfo) -> list[tuple[int, str]]:
    sig: list[tuple[int, str]] = []
    if info.is_spam_folder:
        sig.append((STRONG, "your mail provider filed it as spam"))
    fails = _auth_failures(info.auth_results)
    if any(f.startswith("DMARC") for f in fails):
        sig.append((STRONG, "fails DMARC (sender identity not verified)"))
    elif fails:
        sig.append((MEDIUM, ", ".join(fails)))
    for f in link_findings(info.domain, info.list_unsubscribe, authenticated_domains(info.auth_results)):
        sig.append((STRONG if f.startswith("unsafe") else MEDIUM, f))
    name = (info.from_name or "").lower()
    if "@" in name:
        m = re.search(r"[\w.+-]+@([\w.-]+)", name)
        if m and not related_domains(m.group(1), info.domain):
            sig.append((STRONG, f"display name shows a different address ({m.group(0)})"))
    brand_hits = [b for b in BRANDS if re.search(rf"\b{b}\b", name)]
    if brand_hits and not any(b in info.domain for b in brand_hits):
        sig.append((MEDIUM, f"name mentions {brand_hits[0].title()} but mail is from {base_domain(info.domain)}"))
    if info.reply_to and "@" in info.reply_to and not related_domains(info.reply_to.split("@")[1], info.domain):
        sig.append((WEAK, "replies go to a different domain"))
    if SPAM_SUBJECT.search(info.subject or ""):
        sig.append((WEAK, "spam-style subject wording"))
    if info.domain in FREEMAIL and info.is_bulk:
        sig.append((WEAK, "bulk mail from a free webmail address"))
    if info.domain == "unknown.invalid":
        sig.append((MEDIUM, "no valid sender address"))
    return sig


_IDENTITY_PREFIXES = ("your mail provider filed it as spam", "fails DMARC", "unsafe link", "display name shows",
                      "name mentions")


def is_identity_signal(text: str) -> bool:
    """Signals that the sender is not who it claims to be (as opposed to
    configuration noise such as an SPF softfail or an unusual link host)."""
    return text.startswith(_IDENTITY_PREFIXES)


class SenderAccumulator:
    """Incremental per-sender aggregation so huge mailboxes never need every
    message in memory. The most recent message's List-Unsubscribe header is
    used, since older links may have expired."""

    def __init__(self):
        self.by: dict[str, SenderSummary] = {}
        self.latest: dict[str, MessageInfo] = {}
        self.sigs: dict[str, dict[str, int]] = {}
        self.important: dict[str, list[str]] = {}
        # newest body read per sender: (date, link) - only mail without a
        # List-Unsubscribe header is looked at (extract._with_body_link)
        self.body: dict[str, tuple[datetime | None, str]] = {}
        self.dirty: set[str] = set()       # changed since the last finish()/refresh()
        self._paid: set[str] = set()

    def add(self, info: MessageInfo) -> None:
        key = info.from_addr or f"unknown@{info.domain}"
        self.dirty.add(key)
        s = self.by.get(key)
        if s is None:
            s = self.by[key] = SenderSummary(address=key, domain=info.domain, display_name=info.from_name)
        s.message_count += 1
        if info.unread is not None:
            s.read_known += 1
            s.unread_count += bool(info.unread)
        if info.date:
            if not s.first_seen or info.date < s.first_seen:
                s.first_seen = info.date
            if not s.last_seen or info.date >= s.last_seen:
                s.last_seen = info.date
                self.latest[key] = _light(info)
        elif key not in self.latest:
            self.latest[key] = _light(info)
        s.bulk = s.bulk or info.is_bulk
        s.listed = s.listed or (info.is_bulk and list_signal(info))
        if info.body_seen and not info.list_unsubscribe:
            have = self.body.get(key)
            newer = have is None or have[0] is None or (info.date is not None and info.date >= have[0])
            # a newer body without a link never erases a link found in an older one
            if have is None or (info.body_unsub and (newer or not have[1])) or (newer and not have[1]):
                self.body[key] = (info.date, info.body_unsub)
        if len(s.message_ids) < 5000:
            s.message_ids.append(info.source_id)
        if len(s.subjects) < 5 and info.subject:
            s.subjects.append(info.subject)
        d = self.sigs.setdefault(key, {})
        for weight, text in spam_signals(info):
            d[text] = max(d.get(text, 0), weight)
        hit, reasons = is_important(info)
        if hit:
            have = self.important.setdefault(key, [])
            for r in reasons:
                if r not in have and len(have) < 6:
                    have.append(r)

    def finish(self, paid_senders: set[str] | None = None) -> list[SenderSummary]:
        paid_senders = set(paid_senders or ())
        for key in self.by:
            self._finish_one(key, paid_senders)
        self.dirty.clear()
        self._paid = paid_senders
        return sorted(self.by.values(), key=lambda s: (-s.message_count, s.address))

    def refresh(self, paid_senders: set[str] | None = None) -> list[SenderSummary]:
        """Like finish(), but recomputes only senders that changed since the
        last call (new mail, or paid status flipped): cheap enough to run
        every few seconds for progressive results."""
        paid_senders = set(paid_senders or ())
        for key in self.dirty | (paid_senders ^ self._paid):
            if key in self.by:
                self._finish_one(key, paid_senders)
        self.dirty.clear()
        self._paid = paid_senders
        return list(self.by.values())

    def _finish_one(self, key: str, paid_senders: set[str]) -> None:
        s = self.by[key]
        s.important = self.important.get(key, [])
        info = self.latest[key]
        s.display_name = info.from_name or s.display_name
        s.list_unsubscribe = info.list_unsubscribe
        s.one_click = info.one_click
        s.list_id = info.list_id
        s.signed = sorted(authenticated_domains(info.auth_results))
        s.notes = link_notes(s.domain, s.list_unsubscribe, set(s.signed))
        if key in self.body:
            s.body_checked, s.body_unsub = True, self.body[key][1]
        found = self.sigs.get(key, {})
        s.flags = sorted(found, key=lambda t: -found[t])
        s.spam_score = sum(found.values())
        spoofed = any(is_identity_signal(t) for t in found)
        if key in paid_senders:
            s.category = "paid"
        elif s.important and not spoofed:
            # Transactional mail outranks weak/medium spam signals (an SPF
            # softfail on a bank relay is not spam), but never outranks
            # identity spoofing: a look-alike "security alert" stays spam.
            s.category = "important"
        elif s.spam_score >= SPAM_THRESHOLD:
            s.category = "spam"
        elif s.bulk and s.listed:
            s.category = "newsletter"
        else:
            s.category = "other"


# Gmail's Updates tab holds "confirmations, receipts, bills and statements"
# (Google's own description of the tab), so that label alone made order,
# payment-failure and account mail "newsletter" in a real mailbox. A sender is
# a newsletter only with a mailing-list signal (List-Unsubscribe, List-Id,
# Precedence: bulk/list/junk), a Promotions/Social/Forums label, or a
# promotional subject. Since 2024 Gmail and Yahoo require bulk senders of
# marketing mail to send List-Unsubscribe (transactional mail is exempt), so
# Updates-only mail with none of these is account mail: category "other",
# which is never suggested.
_LIST_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}


def list_signal(info: MessageInfo) -> bool:
    return bool(info.list_unsubscribe or info.list_id or info.precedence in {"bulk", "list", "junk"}
                or (info.labels or set()) & _LIST_LABELS or PROMO_SUBJECT.search(info.subject or ""))


def _light(info: MessageInfo) -> MessageInfo:
    from dataclasses import replace
    return replace(info, body="")


def summarise(messages, paid_senders: set[str] | None = None) -> list[SenderSummary]:
    acc = SenderAccumulator()
    for info in messages:
        acc.add(info)
    return acc.finish(paid_senders)


def best_method(s) -> tuple[str, str] | None:
    """Preferred unsubscribe method: RFC 8058 one-click > mailto > web page >
    a link found in the email body ("body-link": only when the emails have no
    List-Unsubscribe header at all; the user opens it after confirming)."""
    uris = s.list_unsubscribe if isinstance(s.list_unsubscribe, list) else []
    https = [u for u in uris if u.lower().startswith("https:")]
    mailtos = [u for u in uris if u.lower().startswith("mailto:")]
    if s.one_click and https:
        return "one-click", https[0]
    if mailtos:
        return "mailto", mailtos[0]
    if https:
        return "link", https[0]
    body = getattr(s, "body_unsub", "") or ""
    if not uris and body.lower().startswith(("https:", "http:")):
        return "body-link", body
    return None
