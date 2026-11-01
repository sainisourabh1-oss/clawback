"""Savings estimates. Every number here is an ESTIMATE and is labelled so.

* Paid subscriptions marked cancelled: monthly cost x 12 = yearly, and
  "so far" = monthly cost x months since the cancellation date (30.44-day
  months). Totals are per currency; different currencies are never added.
  An optional combined figure uses only rates the user typed in (dated).
* Newsletters you unsubscribed from / blocked: the sender's past rate
  (emails per month, frozen at unsubscribe time) is "emails avoided per
  month"; "so far" subtracts any emails that still arrived afterwards.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import config, tracker
from .money import convert, format_money

DAYS_PER_MONTH = 30.44


def _months_since(stamp: str | None, now: datetime) -> float:
    d = tracker.parse_dt(stamp)
    if not d:
        return 0.0
    return max(0.0, (now - d).total_seconds() / 86400 / DAYS_PER_MONTH)


def compute(account: str | None = None, now: datetime | None = None, path=None, settings: dict | None = None,
            subscription_ids: set | None = None, sender_keys: set | None = None) -> dict:
    """Savings per currency (never added across currencies). The All
    accounts view passes ``subscription_ids`` / ``sender_keys`` (account,
    address) so a mailbox connected twice is counted once."""
    now = now or datetime.now(timezone.utc)
    settings = settings or config.load_settings()
    money: dict[str, dict] = {}
    items = []
    for s in tracker.list_subscriptions(path, account):
        if s["status"] != "cancelled" or s["monthly_cost"] is None:
            continue
        if subscription_ids is not None and s["id"] not in subscription_ids:
            continue
        months = _months_since(s["cancelled_at"], now)
        cur = s["currency"]
        m = money.setdefault(cur, {"currency": cur, "monthly": 0.0, "yearly": 0.0, "so_far": 0.0, "count": 0})
        m["monthly"] += s["monthly_cost"]; m["yearly"] += s["monthly_cost"] * 12
        m["so_far"] += s["monthly_cost"] * months; m["count"] += 1
        items.append({"id": s["id"], "vendor": s["vendor"], "currency": cur, "monthly": s["monthly_cost"],
                      "yearly": round(s["monthly_cost"] * 12, 2), "so_far": round(s["monthly_cost"] * months, 2),
                      "cancelled_at": s["cancelled_at"], "currency_certainty": s["currency_certainty"]})
    for m in money.values():
        for k in ("monthly", "yearly", "so_far"):
            m[k] = round(m[k], 2)
        m["display"] = {k: format_money(m[k], m["currency"]) for k in ("monthly", "yearly", "so_far")}
    home = settings.get("home_currency")
    order = sorted(money, key=lambda c: (c != home, c))
    totals = [money[c] for c in order]

    silenced = []
    per_month = 0.0
    so_far = 0.0
    for s in tracker.list_senders(account, path=path):
        if s["status"] not in {"unsubscribed", "blocked", "escalated"} or s["category"] == "paid":
            continue
        if sender_keys is not None and (s["account"], s["address"]) not in sender_keys:
            continue
        months = _months_since(s["unsubscribed_at"], now)
        after = sum(1 for m in tracker.sender_messages(s["account"], s["address"], path)
                    if m["received"] and m["received"] > (s["unsubscribed_at"] or ""))
        avoided = max(0.0, s["monthly_rate"] * months - after)
        per_month += s["monthly_rate"]
        so_far += avoided
        silenced.append({"address": s["address"], "per_month": s["monthly_rate"], "avoided_so_far": round(avoided)})

    converted = None
    if settings.get("show_converted_total") and settings.get("rates") and totals:
        res = convert({t["currency"]: t["yearly"] for t in totals}, settings["rates"], home)
        if res:
            value, missing = res
            converted = {"home": home, "yearly": value, "display": format_money(value, home), "missing": missing,
                         "rates_date": settings["rates"].get("date"),
                         "label": f"Estimate in {home} at the rates you entered (dated {settings['rates'].get('date')})"}
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "money": totals, "subscriptions": items, "converted": converted,
        "emails": {"senders_silenced": len(silenced), "per_month": round(per_month), "avoided_so_far": round(so_far),
                   "senders": silenced},
        "label": "Estimates from your email history. Check your bank or card statements for exact figures.",
    }
