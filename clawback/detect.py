"""Explainable heuristic detection of PAID subscription email.

Newsletters and marketing are handled by ``senders.py``; this module only
answers "does this message look like a recurring charge?". Every pattern uses
word boundaries (``industrial`` must not match ``trial``). Thresholds and
weights are explained in docs/HEURISTICS.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from .money import Money, find_amounts

SUBJECT_HINTS = [
    r"\breceipt\b", r"\binvoice\b", r"\bpayment (?:received|confirmation|confirmed|successful|processed)\b",
    r"\brenewal\b", r"\brenewed\b", r"\bwill renew\b", r"\byou'?ve been charged\b",
    r"\b(?:free )?trial (?:ends?|ending|expires?|expiring|is ending|will end|is over)\b",
    r"\bsubscription (?:renewed|renewal|confirmed|confirmation|activated|receipt|is active|started|charge)\b",
    r"\bbilling statement\b", r"\bpayment due\b", r"\bupi autopay\b", r"\be-?mandate\b",
    r"\bstanding instruction\b", r"\brecurring (?:payment|charge|debit)\b", r"\bpre-?debit\b",
    r"\bdirect debit\b", r"\bsepa\b", r"\bmonthly (?:statement|receipt)\b", r"\bannual (?:statement|receipt)\b",
]
BODY_HINTS = [
    r"\bbilling (?:period|cycle|date)\b", r"\bnext (?:billing|charge|payment|renewal) date\b",
    r"\bauto-?renew(?:al|s|ed|ing)?\b", r"\brenews? (?:on|automatically|every|at|monthly|annually|yearly)\b",
    r"\bwill (?:automatically )?renew\b", r"\bcharged every\b", r"\brecurring (?:payment|charge|billing|debit)\b",
    r"\btrial (?:ends?|period|expires?)\b", r"\bsubscription (?:fee|price|renewal|period|plan|id|number|started|confirmed|activated|renewed|details)\b",
    r"\byour (?:plan|subscription) (?:renews|will)\b", r"\bupi autopay\b", r"\be-?mandate\b",
    r"\bstanding instruction\b", r"\bauto-?debit\b", r"\bmembership (?:fee|renew\w*)\b",
    r"\bbilled (?:monthly|annually|yearly|weekly|quarterly)\b", r"\bpre-?debit\b", r"\bdirect debit\b",
    r"\bmandate (?:reference|id|registered|created)\b", r"\bnext payment\b",
]
# Footer/marketing phrases that mention "subscription" without any charge.
# They are removed before hint matching so a newsletter footer cannot create
# a paid-subscription hit (review bug 6).
NEWSLETTER_CUES = re.compile(
    r"(?i)\b(?:manage|update|change|edit)\s+(?:your\s+)?(?:email\s+)?(?:subscriptions?|preferences|membership|notification settings)\b"
    r"|\bunsubscribe\b|\bemail preferences\b|\byou(?:'re| are) receiving this\b|\bsubscribed to (?:our|this)\b"
    r"|\bnewsletter subscription\b|\bsubscription preferences\b|\bview (?:this email )?in (?:your )?browser\b"
    r"|\bstart (?:your|a) free trial\b|\btry (?:it )?free\b"
)
_TRIAL = re.compile(r"(?i)\b(?:free\s+)?trial\b")
_RENEW = re.compile(r"(?i)\b(?:renew(?:al|ed|s)?|auto-?renew\w*)\b")
_MANDATE = re.compile(r"(?i)\b(?:upi autopay|autopay|e-?mandate|standing instruction|direct debit|sepa|pre-?debit|mandate)\b")

NEUTRAL_PREFIXES = {"www", "mail", "email", "e-mail", "billing", "payments", "pay", "invoice", "receipts", "receipt",
                    "notify", "notification", "notifications", "no-reply", "noreply", "noreply-mail", "info", "team",
                    "hello", "support", "accounts", "account", "help", "news", "cs", "customerservice", "updates",
                    "messages", "alerts", "mailer", "e", "em", "m", "t", "mailing", "newsletter", "marketing"}
# Registry-level suffixes under which each organisation gets its own name
# (a curated subset of the Public Suffix List, publicsuffix.org). Without an
# entry, "alerts.sbi.bank.in" and "alerts.hdfcbank.bank.in" would both reduce
# to "bank.in" and be treated as the same organisation.
MULTI_LABEL_SUFFIXES = {
    # India (bank.in / fin.in: RBI-mandated domains for banks and other regulated finance)
    "co.in", "firm.in", "net.in", "org.in", "gen.in", "ind.in", "bank.in", "fin.in", "gov.in", "nic.in", "ac.in",
    "edu.in", "res.in", "mil.in",
    # UK
    "co.uk", "org.uk", "ac.uk", "gov.uk", "nhs.uk", "police.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk",
    # Asia-Pacific
    "com.au", "net.au", "org.au", "gov.au", "edu.au", "co.nz", "org.nz", "govt.nz", "co.jp", "ne.jp", "or.jp",
    "go.jp", "ac.jp", "com.sg", "gov.sg", "edu.sg", "org.sg", "com.cn", "gov.cn", "com.hk", "gov.hk", "org.hk",
    "co.kr", "go.kr", "com.tw", "gov.tw", "co.id", "go.id", "com.my", "gov.my", "com.ph", "gov.ph", "co.th",
    "go.th", "in.th", "com.vn", "gov.vn", "com.pk", "gov.pk", "com.bd", "gov.bd", "com.np", "gov.np", "com.lk",
    "gov.lk",
    # Americas
    "com.br", "gov.br", "com.mx", "gob.mx", "com.ar", "gob.ar", "com.co", "gov.co", "com.pe", "gob.pe", "gc.ca",
    # Middle East / Africa / Europe
    "co.za", "gov.za", "org.za", "com.tr", "gov.tr", "com.sa", "gov.sa", "gov.ae", "co.ae", "co.il", "gov.il",
    "com.eg", "gov.eg", "com.ng", "gov.ng", "co.ke", "go.ke", "gouv.fr", "com.pl", "gov.pl", "com.ua", "gov.ua",
}

# Billers whose subjects never name the product ("Your receipt from Apple.").
# For these the plan name must come from the body (review bug 2).
GENERIC_BILLERS = {"apple.com", "google.com", "stripe.com", "razorpay.com", "paddle.com", "paddle.net", "paypal.com",
                   "fastspring.com", "lemonsqueezy.com", "gumroad.com", "chargebee.com", "recurly.com", "2checkout.com",
                   "verifone.com", "cashfree.com", "payu.in", "instamojo.com", "amazon.com", "amazon.in"}
_KNOWN_PRODUCTS = re.compile(
    r"(?i)\b(iCloud\+?(?:\s+with\s+\d+\s*[GT]B(?:\s+of\s+storage)?|\s+\d+\s*[GT]B)?|Apple Music|Apple TV\+?|Apple One(?:\s+\w+)?|Apple Arcade|"
    r"Apple Fitness\+?|Apple News\+?|AppleCare\+?(?:\s+for\s+[\w ]+)?|YouTube Premium(?:\s+\w+)?|YouTube Music(?:\s+Premium)?|"
    r"Google One(?:\s+\d+\s*[GT]B)?|Google Workspace|Nest Aware|Fitbit Premium|Google AI (?:Pro|Ultra))"
)
_PLAN_LABEL = re.compile(r"(?im)^\s*(?:plan|product|subscription|item|description|service|package|membership|order item)(?:\s+name)?\s*[:\-–]\s*(.{2,80}?)\s*$")
# Store receipts laid out as a table: "30 GB (Google One) (by Google LLC) ₹59.00/month".
_BY_DEVELOPER = re.compile(r"(?im)^\s*(.{2,80}?)\s+\(by [^)\n]{2,60}\)")
# Promotional subjects: offers, credits with a purchase, discounts. A promo
# never contributes a price or evidence to a subscription, even when it
# mentions a plan and "renews monthly" (real case: a Google One promo
# advertising a higher plan joined the Google One subscription).
PROMO_SUBJECT = re.compile(
    r"(?i)\b(?:member benefits?|exclusive (?:offer|deal)s?|special offer|limited[- ]time|with (?:the )?purchase of"
    r"|get \S+ (?:off|credit|cashback)|\d+% off|save (?:up to )?\S+|upgrade (?:now|today|and save)|deal of the|"
    r"flash sale|black friday|cyber monday|coupon|promo code|offer ends|last chance)\b")
# A real charge or receipt in the subject: the price in such an email is what
# you paid, so its kind is "receipt" whatever the body footer says about trials.
RECEIPT_SUBJECT = re.compile(
    r"(?i)\b(?:receipt|invoice|order (?:receipt|confirmation)|payment (?:received|confirmation|confirmed|successful|processed)"
    r"|you'?ve been charged|your (?:payment|purchase|order) (?:of|from|for))\b")
# Without a price, a "will renew" notice is only a subscription when it also
# says money moves (Amazon "nomination will renew" notices do not).
BILLING_SIGNAL = re.compile(
    r"(?i)\b(?:charged?|charges|billed|billing|payment method|card ending|debit(?:ed)?|you will pay|amount due"
    r"|price|fee|auto-?pay|invoice|receipt)\b")
_TAX_NOTE = re.compile(r"(?i)\(?\b((?:includes|including|incl\.?|inclusive of)\s+(?:GST|VAT|IGST|CGST|SGST|sales tax|tax(?:es)?)"
                       r"(?:\s+of)?\s*(?:₹|Rs\.?|INR|\$|€|£)?\s?[\d.,]*)")


@dataclass
class Hit:
    vendor: str
    domain: str
    subject: str
    amount: str | None
    kind: str
    confidence: float
    evidence: str = ""
    cycle: str | None = None          # month | year | week | quarter | half-year | None
    plan: str = ""
    date: datetime | None = None
    recurring: bool = True
    money: Money | None = None
    renewal_date: date | None = None
    trial_end: date | None = None
    merchant: str = ""                # for bank/UPI mandate alerts: the merchant being paid
    region: str = ""
    source_id: str = ""
    sender: str = ""
    extra: dict = field(default_factory=dict)


def base_domain(domain: str) -> str:
    domain = (domain or "").lower().strip().rstrip(".")
    if not domain or domain == "unknown.invalid":
        return domain
    parts = domain.split(".")
    if len(parts) <= 2:
        return domain
    suffix2 = ".".join(parts[-2:])
    return ".".join(parts[-3:]) if suffix2 in MULTI_LABEL_SUFFIXES else suffix2


def vendor_from_domain(domain: str) -> str:
    base = base_domain(domain)
    label = base.split(".")[0] if base else domain
    parts = domain.lower().split(".")
    extra = parts[: len(parts) - len(base.split("."))]
    brand = [p for p in extra if p not in NEUTRAL_PREFIXES]
    # "aws.amazon.com" -> "Aws": a non-neutral sub-label names a separate product.
    pick = brand[-1] if brand else label
    return pick.replace("-", " ").replace("_", " ").title()


_vendor_from_domain = vendor_from_domain  # backwards-compatible alias


# ---------------------------------------------------------------- billing cycle
_CYCLE_NEAR = [
    ("half-year", r"\b(?:every|per|each)\s+(?:6|six)\s+months\b|\bhalf[- ]yearly\b|\bsemi[- ]?annual(?:ly)?\b|/\s*6\s*mo(?:nths)?\b"),
    ("quarter", r"\bquarterly\b|\b(?:every|per|each)\s+(?:3|three)\s+months\b|\bper\s+quarter\b|/\s*quarter\b"),
    ("year", r"(?:/\s*|\bper\s+|\ba\s+|\bevery\s+|\beach\s+)(?:year|yr|annum)\b|\b(?:annual|annually|yearly)\b|\b12[- ]months?\b|/\s*yr\b"),
    ("month", r"(?:/\s*|\bper\s+|\ba\s+|\bevery\s+|\beach\s+)(?:month|mo)\b|\bmonthly\b|\bmonth[- ]to[- ]month\b|/\s*mo\b|\b1[- ]month\b"),
    ("week", r"(?:/\s*|\bper\s+|\ba\s+|\bevery\s+|\beach\s+)(?:week|wk)\b|\bweekly\b"),
]
_CYCLE_GLOBAL = [
    ("half-year", r"\bbilled (?:every 6 months|half[- ]yearly|semi[- ]annually)\b"),
    ("quarter", r"\bbilled quarterly\b|\bquarterly (?:plan|subscription|membership|billing|fee)\b"),
    ("year", r"\bbilled (?:annually|yearly|every year)\b|\b(?:annual|yearly|1[- ]year|12[- ]month) (?:plan|subscription|membership|billing|fee|renewal|pass)\b|\brenews? (?:annually|yearly|every year)\b"),
    ("month", r"\bbilled (?:monthly|every month)\b|\b(?:monthly|1[- ]month) (?:plan|subscription|membership|billing|fee|renewal|pass)\b|\brenews? (?:monthly|every month)\b"),
    ("week", r"\bbilled weekly\b|\bweekly (?:plan|subscription|billing)\b"),
]
CYCLE_MONTHS = {"week": 12 / 52, "month": 1.0, "quarter": 3.0, "half-year": 6.0, "year": 12.0}


def cycle_near(text: str, start: int | None = None, end: int | None = None) -> str | None:
    """Billing cycle from words next to the price (±80 chars) or explicit billing phrases.

    A stray "month" or "Happy New Year" elsewhere in the email does NOT set a
    cycle (review bug 5); that returns ``None`` and the aggregator may infer the
    cycle from the spacing of repeated receipts instead.
    """
    low = (text or "").lower()
    if start is not None:
        lo, hi = max(0, start - 80), min(len(low), (end or start) + 80)
        window = low[lo:hi]
        anchor = start - lo
        best = None
        for name, pat in _CYCLE_NEAR:
            for m in re.finditer(pat, window):
                dist = abs(m.start() - anchor)
                if best is None or dist < best[0]:
                    best = (dist, name)
        if best:
            return best[1]
    for name, pat in _CYCLE_GLOBAL:
        if re.search(pat, low):
            return name
    return None


# ---------------------------------------------------------------- dates
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_PATTERNS = [
    re.compile(rf"(?i)\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON},?\s+(20\d\d)\b"),     # 12 Nov 2026
    re.compile(rf"(?i)\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d\d)\b"),     # Nov 12, 2026
    re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b"),                                      # 2026-11-12
]


def _parse_date_at(text: str) -> date | None:
    for i, pat in enumerate(_DATE_PATTERNS):
        m = pat.search(text)
        if not m:
            continue
        try:
            if i == 0:
                return date(int(m.group(3)), _MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
            if i == 1:
                return date(int(m.group(3)), _MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except (ValueError, KeyError):
            continue
    return None


def find_dated(text: str, cue: str) -> date | None:
    """First date within 60 chars after a cue such as 'renews on' / 'trial ends'."""
    for m in re.finditer(cue, text, re.I):
        found = _parse_date_at(text[m.end():m.end() + 60])
        if found:
            return found
    return None


_RENEW_CUE = r"\b(?:renews?|renewal date|will renew|next (?:billing|charge|payment) date|will be charged|next payment)\b(?:\s+on)?"
_TRIAL_CUE = r"\btrial (?:ends?|expires?|period ends?|will end)\b(?:\s+on)?"


# ---------------------------------------------------------------- plan names
def extract_plan(body: str) -> str:
    """Plan/product name, whitespace-normalised ("Starter Business Email
    Trial", not "Starter Business Email       Trial")."""
    for pat in (_PLAN_LABEL, _BY_DEVELOPER):
        m = pat.search(body or "")
        if m:
            value = re.sub(r"(?:₹|\$|€|£|\b[A-Z]{3}\b)\s*[\d.,]+.*$", "", m.group(1))
            value = " ".join(value.split()).strip(" -–:|")
            if re.search(r"[A-Za-z]", value) and value.lower() not in {"price", "item price", "item"}:
                return value[:60]
    m = _KNOWN_PRODUCTS.search(body or "")
    if m:
        return " ".join(m.group(1).split())[:60]
    return ""


# ---------------------------------------------------------------- classifier
def _hints(patterns: list[str], text: str) -> list[str]:
    return [p for p in patterns if re.search(p, text, re.I)]


def _plain(p: str) -> str:
    return re.sub(r"\\b|\(\?:|\)|\?|\\", "", p).split("|")[0]


def classify(subject: str, body: str, sender_domain: str, region: str | None = None,
             is_bulk: bool = False) -> Hit | None:
    """Return a Hit when the message looks like a recurring charge, else None.

    Rules (weights justified in docs/HEURISTICS.md):
      * an amount in billing context + (a billing subject OR a billing body phrase), or
      * a billing subject + a billing body phrase (e.g. trial-ending notices without a price).
    Newsletter footers are stripped first, and a bulk (List-Unsubscribe) message
    needs a billing SUBJECT, since marketing mail often quotes prices.
    """
    subject = str(subject or "")[:500]
    body = str(body or "")[:30000]
    clean_body = NEWSLETTER_CUES.sub(" ", body)
    clean_subject = NEWSLETTER_CUES.sub(" ", subject)
    subj_matches = _hints(SUBJECT_HINTS, clean_subject)
    body_matches = _hints(BODY_HINTS, clean_body)
    domain = str(sender_domain or "unknown.invalid").lower().strip().rstrip(".")
    text = f"{clean_subject}\n{clean_body}"
    # A zero amount ("$0.00", "Rs 0") is not a charge: it never makes a mail billing evidence.
    amounts = [(m, s) for m, s in find_amounts(text, domain, region) if s > 0 and m.amount > 0]
    money = max(amounts, key=lambda x: x[1])[0] if amounts else None
    receipt_subject = bool(RECEIPT_SUBJECT.search(clean_subject))
    if PROMO_SUBJECT.search(subject) and not receipt_subject:
        return None
    if money is not None and not (subj_matches or body_matches):
        return None
    if money is None and not (subj_matches and body_matches):
        return None
    if money is None and not BILLING_SIGNAL.search(text):
        return None
    if is_bulk and not subj_matches:
        return None
    cycle = None
    if money is not None:
        idx = text.find(money.raw)
        cycle = cycle_near(text, idx, idx + len(money.raw))
    else:
        cycle = cycle_near(text)
    if receipt_subject and money is not None:
        kind = "receipt"
    elif _MANDATE.search(clean_subject) or (_MANDATE.search(clean_body) and not subj_matches):
        kind = "mandate"
    elif _TRIAL.search(clean_subject) or re.search(r"(?i)\btrial (?:ends?|period|expires?)\b", clean_body):
        kind = "trial"
    elif _RENEW.search(clean_subject + " " + clean_body):
        kind = "renewal"
    else:
        kind = "receipt"
    recurring = bool(body_matches) or cycle is not None or kind in {"trial", "renewal", "mandate"}
    score = 0.2 + 0.25 * bool(subj_matches) + 0.1 * min(3, len(body_matches)) + 0.2 * bool(money) + 0.1 * bool(cycle)
    if money is None:
        score = min(score, 0.4)   # no price: "check this", never a confident subscription
    extra = {}
    tax = _TAX_NOTE.search(clean_body)
    if tax:
        extra["tax_note"] = " ".join(tax.group(1).split())
    evidence = "; ".join(_plain(p) for p in (subj_matches[:2] + body_matches[:3]))
    amount_text = None
    if money is not None:
        amount_text = money.raw + (f" {cycle}" if cycle else "")
    return Hit(
        vendor=vendor_from_domain(domain), domain=domain, subject=subject[:160], amount=amount_text,
        kind=kind, confidence=round(min(0.95, score), 2), evidence=evidence, cycle=cycle,
        plan=extract_plan(body), recurring=recurring, money=money,
        renewal_date=find_dated(clean_body, _RENEW_CUE), trial_end=find_dated(clean_body, _TRIAL_CUE), extra=extra,
    )


def classify_message(info, region: str | None = None) -> Hit | None:
    """Classify an ``extract.MessageInfo``; regional modules get the first chance."""
    from . import regions
    hit = regions.detect_mandate(info, region)
    if hit is None:
        hit = classify(info.subject, info.body, info.domain, region, is_bulk=info.is_bulk)
    if hit is not None:
        hit.date = info.date
        hit.source_id = info.source_id
        hit.sender = info.from_addr
    return hit


def _is_biller(domain: str) -> bool:
    return base_domain(domain) in GENERIC_BILLERS


def hit_to_json(h: Hit) -> str:
    import json
    from dataclasses import asdict
    d = asdict(h)
    for k in ("date", "renewal_date", "trial_end"):
        d[k] = d[k].isoformat() if d[k] else None
    return json.dumps(d, ensure_ascii=False, default=str)


def hit_from_json(text: str) -> Hit:
    import json
    d = json.loads(text)
    d["date"] = datetime.fromisoformat(d["date"]) if d.get("date") else None
    for k in ("renewal_date", "trial_end"):
        d[k] = date.fromisoformat(d[k][:10]) if d.get(k) else None
    if d.get("money"):
        d["money"] = Money(**d["money"])
    return Hit(**d)
