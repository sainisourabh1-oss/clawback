"""Suggestions that add value beyond a single scan (goal E).

* Upcoming renewals / trial ends (next 14 days): dates you entered win;
  dates read from email text are shown as "detected, check it".
* Price increases: latest recorded price above the previous one.
* Not opened in N months (default 3): an active newsletter sender with at
  least 3 messages in that window and NONE of them opened. Three is the
  smallest count that shows a habit rather than one missed email; it needs
  read-state data (Gmail, IMAP flags or Takeout labels).
* Senders on your keep-list are never suggested.
* Nor is any sender with an account signal (important.account_mail_reason:
  a security, payment, order or statement subject in ANY stored email, or an
  account mailbox name), even when it is categorised "newsletter": the
  "Select senders not opened" button bulk-selects this list for unsubscribing.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from . import config, tracker
from .important import account_mail_reason

MIN_UNOPENED = 3
RENEWAL_DAYS = 14


def upcoming(account, today: date | None = None, days: int = RENEWAL_DAYS) -> list[dict]:
    today = today or date.today()
    out = []
    for r in tracker.list_subscriptions(account=account):
        if r["status"] not in {"suspected", "confirmed"}:
            continue
        for kind, manual, detected in (("trial ends", r["trial_end"], r["detected_trial_end"]),
                                       ("renews", r["renewal_date"], r["detected_renewal"])):
            value = manual or detected
            if not value:
                continue
            d = date.fromisoformat(value[:10])
            if today <= d <= today + timedelta(days=days):
                out.append({"id": r["id"], "vendor": r["vendor"], "kind": kind, "date": d.isoformat(),
                            "days": (d - today).days, "source": "you entered it" if manual else "detected in email, check it",
                            "amount": r["amount"], "currency": r["currency"]})
    return sorted(out, key=lambda x: x["date"])


def not_opened(account, months: int | None = None, now: datetime | None = None) -> list[dict]:
    months = months or int(config.load_settings().get("inactive_months", 3))
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=30.44 * months)).isoformat()
    out = []
    for s in tracker.list_senders(account, "newsletter"):
        if s["status"] != "active" or s["kept"] or s.get("important"):
            continue
        messages = tracker.sender_messages(s["account"], s["address"])
        if account_mail_reason(s["address"], [m["subject"] for m in messages] + list(s.get("subjects") or [])):
            continue
        recent = [m for m in messages if (m["received"] or "") >= since]
        known = [m for m in recent if m["unread"] is not None]
        if len(known) >= MIN_UNOPENED and all(m["unread"] == 1 for m in known):
            out.append({"address": s["address"], "display_name": s["display_name"], "count": len(known), "months": months})
    return sorted(out, key=lambda x: -x["count"])


def all_insights(account) -> dict:
    from .money import format_money
    rises = tracker.price_increases()
    for r in rises:
        r["old_display"], r["new_display"] = format_money(r["old"], r["currency"]), format_money(r["new"], r["currency"])
    return {"upcoming": upcoming(account), "price_increases": rises,
            "not_opened": not_opened(account), "inactive_months": int(config.load_settings().get("inactive_months", 3))}
