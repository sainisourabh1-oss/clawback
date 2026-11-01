"""Group raw detections into subscriptions without conflating plans, currencies or cycles."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median

from .detect import CYCLE_MONTHS, GENERIC_BILLERS, Hit, base_domain, cycle_near
from .money import AMBIGUOUS_YEN, parse_money


@dataclass
class Subscription:
    vendor: str
    domain: str
    amount: float | None
    currency: str
    cycle: str
    monthly_cost: float | None
    identity_key: str = "default"
    kinds: set = field(default_factory=set)
    full_domains: set = field(default_factory=set)
    hits: int = 0
    best_confidence: float = 0.0
    last_subject: str = ""
    evidence: list[str] = field(default_factory=list)
    plan: str = ""
    cycle_source: str = "email text"     # email text | receipt spacing | unknown
    currency_certainty: str = "explicit"
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    renewal_date: date | None = None
    trial_end: date | None = None
    merchant: str = ""
    region: str = ""
    recurring: bool = True
    prices: list[tuple] = field(default_factory=list)   # (datetime|None, amount, currency)
    senders: set = field(default_factory=set)
    tax_note: str = ""                   # "Includes GST of ₹9.00" from the latest receipt


_MONTH_WORDS = r"january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
_STOP = r"receipt|invoice|payment|confirmation|confirmed|successful|your|for|of|the|from|to|at|with|and|a|an|on|in|is|has|been|monthly|annual|yearly|renewal|renewed|renews|subscription|statement|billing|charged|charge|plan|membership|tax|order|no|number|thanks|thank|you|purchase|new|e-receipt|bill"


# Reminder countdowns: one trial sends "ends in 7 days", "ends in 3 days",
# "ends tomorrow", "ends today"; these must be one identity (real case: a
# Hostinger trial was split into two rows).
_COUNTDOWN = (r"\b(?:(?:ends?|ending|expires?|expiring|renews?|renewing|is ending|will (?:end|expire|renew))\s+)?"
              r"(?:in\s+(?:a|one|two|three|few|\d+)?\s*(?:days?|hours?|weeks?)|tomorrow|today|tonight|soon|this week|"
              r"next week|now)\b|\b(?:reminder|last chance|final reminder|heads up|only|left|remaining)\b"
              r"|\b(?:ends?|ending|expires?|expiring)\b")


def _identity_words(subject: str) -> str:
    value = (subject or "").lower()
    value = re.sub(r"\S*\d\S*", " ", value)                       # dates, order numbers, prices
    value = re.sub(rf"\b(?:{_MONTH_WORDS})\b\.?", " ", value)      # review bug 1: Oct / Nov / Sept
    value = re.sub(r"(?:₹|\$|€|£|¥)", " ", value)
    value = re.sub(_COUNTDOWN, " ", value)                         # "ends tomorrow" vs "ends in 7 days"
    value = re.sub(rf"\b(?:{_STOP})\b", " ", value)
    return re.sub(r"[^a-z0-9+]+", " ", value).strip()


def _identity(subject: str) -> str:
    """Normalise changing invoice subjects while keeping plan/product labels."""
    return _identity_words(subject)[:80] or "default"


def _is_generic(identity: str, domain: str) -> bool:
    """True when the subject names nothing but the vendor ("Your receipt from Apple.")."""
    if identity == "default":
        return True
    brand = set(re.split(r"[^a-z0-9]+", base_domain(domain).split(".")[0]))
    brand |= {"google", "play", "apple", "store", "app", "itunes", "stripe", "razorpay", "paddle", "paypal", "amazon", "inc", "ltd", "llc", "pvt"}
    return all(w in brand for w in identity.split())


def _plan_key(plan: str) -> str:
    p = re.sub(r"\((?:monthly|annual|yearly)\)", " ", plan.lower())
    return re.sub(r"[^a-z0-9+]+", " ", p).strip()


def _infer_cycle(dates: list[datetime]) -> str | None:
    """Cycle from the spacing of repeated charges.

    Windows: week 6–8 days, month 26–35, quarter 85–96, half-year 175–190,
    year 355–375. They cover calendar-month length (28–31) plus a few days of
    billing retries/time-zone drift, and stay far apart so they cannot overlap.
    """
    ds = sorted({d.date() for d in dates if d})
    if len(ds) < 2:
        return None
    gap = median((b - a).days for a, b in zip(ds, ds[1:]))
    for name, lo, hi in (("week", 6, 8), ("month", 26, 35), ("quarter", 85, 96), ("half-year", 175, 190), ("year", 355, 375)):
        if lo <= gap <= hi:
            return name
    return None


def monthly_from(amount: float | None, cycle: str) -> float | None:
    if amount is None or cycle not in CYCLE_MONTHS:
        return None
    return round(amount / CYCLE_MONTHS[cycle], 2)


def _money_of(h: Hit):
    if h.money is not None:
        return h.money.amount, h.money.currency, h.money.certainty
    m = parse_money(h.amount or "", h.domain)
    if m is None:
        return None, "?", "unknown"
    return m.amount, m.currency, m.certainty


def aggregate(hits: list[Hit]) -> list[Subscription]:
    groups: dict[tuple, list[tuple[Hit, float | None, str, str, str]]] = {}
    for h in hits:
        amount, cur, certainty = _money_of(h)
        cycle = h.cycle or (cycle_near(h.amount, 0, len(h.amount)) if h.amount else None) or "unknown"
        domain = base_domain(h.domain)
        if h.merchant:
            # Bank/UPI mandate alerts: the subscription is the MERCHANT, not the bank.
            identity = "merchant:" + _plan_key(h.merchant)
        else:
            identity = _identity(h.subject)
            if _is_generic(identity, h.domain) or domain in GENERIC_BILLERS and h.plan:
                if h.plan:
                    identity = "plan:" + _plan_key(h.plan)
                else:
                    # No product name anywhere: separate by price rather than merging
                    # distinct products into one row (a price change may then split a
                    # plan into two candidates; that is the safer error).
                    identity = "generic-" + ("unknown" if amount is None else f"{amount:.2f}-{cur}")
        # One row per merchant across all bank alerts, even when the bank changes
        # its sending domain (Indian banks moved to *.bank.in in 2025-26).
        key_domain = "" if h.merchant else domain
        groups.setdefault((key_domain, cur, identity), []).append((h, amount, cur, certainty, cycle))
    # A merchant's alerts without an amount (e.g. the mandate registration, whose
    # amount is only a cap) join that merchant's priced row when there is one.
    for (d, cur, identity) in [k for k in groups if k[0] == "" and k[1] == "?"]:
        priced = [k for k in groups if k[0] == "" and k[2] == identity and k[1] != "?"]
        if len(priced) == 1:
            groups[priced[0]].extend(groups.pop((d, cur, identity)))

    subs: list[Subscription] = []
    for (domain, cur, identity), items in groups.items():
        items.sort(key=lambda x: x[0].date.timestamp() if x[0].date else 0)
        # A merchant row is shown with the bank that sent its latest alert.
        domain = domain or base_domain(items[-1][0].domain)
        cycles = [c for *_, c in items if c != "unknown"]
        # Cycle stated in text wins; split monthly vs yearly statements into separate rows.
        by_cycle: dict[str, list] = {}
        for it in items:
            c = it[4] if it[4] != "unknown" else (cycles[-1] if cycles else "unknown")
            by_cycle.setdefault(c, []).append(it)
        for cycle, part in by_cycle.items():
            subs.append(_build(domain, cur, identity, cycle, part))
    subs = [s for s in subs if s.recurring]
    return sorted(subs, key=lambda s: (s.currency, -(s.monthly_cost or 0), s.vendor.lower()))


def _build(domain, cur, identity, cycle, items) -> Subscription:
    first = items[0][0]
    cycle_source = "email text" if cycle != "unknown" else "unknown"
    dated = [h.date for h, *_ in items if h.date]
    if cycle == "unknown":
        # Only charges have a billing rhythm: trial countdown reminders ("ends
        # in 7 days", "ends tomorrow") are days apart but bill nothing.
        inferred = _infer_cycle([h.date for h, *_ in items if h.date and h.kind != "trial"])
        if inferred:
            cycle, cycle_source = inferred, "receipt spacing"
    latest = items[-1]
    priced = [it for it in items if it[1] is not None]
    # The most recent known price is the current one (a price change shows up in
    # history and in price alerts instead of silently taking the max).
    amount = priced[-1][1] if priced else None
    certainty = priced[-1][3] if priced else "unknown"
    s = Subscription(
        vendor=first.merchant or (first.plan if identity.startswith("plan:") and first.plan else first.vendor),
        domain=domain, amount=amount, currency=cur, cycle=cycle, monthly_cost=monthly_from(amount, cycle),
        identity_key=f"{identity}|{cycle}", plan=first.plan, cycle_source=cycle_source,
        currency_certainty=certainty, merchant=first.merchant, region=first.region,
    )
    if s.currency == AMBIGUOUS_YEN:
        s.currency_certainty = "ambiguous"
    for h, amt, c, _, _ in items:
        s.kinds.add(h.kind); s.full_domains.add(h.domain); s.hits += 1
        s.best_confidence = max(s.best_confidence, h.confidence)
        if h.sender:
            s.senders.add(h.sender)
        if h.subject and h.subject not in s.evidence:
            s.evidence.append(h.subject)
        if amt is not None:
            s.prices.append((h.date, amt, c))
        s.renewal_date = h.renewal_date or s.renewal_date
        s.trial_end = h.trial_end or s.trial_end
    s.last_subject = latest[0].subject
    notes = [h.extra.get("tax_note") for h, *_ in items if h.extra.get("tax_note")]
    s.tax_note = notes[-1] if notes else ""
    s.first_seen = min(dated) if dated else None
    s.last_seen = max(dated) if dated else None
    # One-off purchases (a single shop receipt) are not subscriptions unless the
    # email text says it recurs or repeated charges are evenly spaced.
    s.recurring = any(h.recurring for h, *_ in items) or cycle_source == "receipt spacing"
    if s.plan and s.vendor == s.plan and first.vendor:
        s.vendor = f"{first.vendor}: {s.plan}"
    return s
