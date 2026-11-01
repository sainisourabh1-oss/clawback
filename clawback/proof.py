"""Did the unsubscribe work? Track whether a sender keeps mailing afterwards.

Grace periods (configurable in settings):

* **RFC 8058 one-click: 2 calendar days.** Google's sender-guidelines FAQ
  (support.google.com/a/answer/14229414, checked 2026-10-24) says "we
  recommend that you fulfill unsubscribe requests within 48 hours" and treats
  missed unsubscribes as a compliance issue for bulk senders; one-click senders
  are exactly those bulk senders. It is a recommendation, not a law, so the
  verdict says "still sending", never "illegal".
* **mailto / web-page unsubscribes: 10 business days.** The US CAN-SPAM Act
  (15 U.S.C. §7704(a)(4)) allows 10 business days to stop sending, the most
  lenient common rule; we use it so we never call a sender non-compliant early.
  Business days skip Saturdays and Sundays only (public holidays vary by country).

Verdicts:

* ``pending``   – still inside the grace period, or no mail has been seen
                  after the grace period ended (scan or import again later);
* ``honoured``  – mail has been observed past the grace end and none of it
                  came from this sender after the grace end;
* ``still_sending`` – at least one message from this sender is dated after
                  the grace end. These form the "ignored your unsubscribe" list.

Message dates are the provider's receive time where available (Gmail) or the
Date header, which a sender can set; we report counts, never certainty.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config, tracker

PENDING, HONOURED, STILL_SENDING = "pending", "honoured", "still_sending"


def add_business_days(start: datetime, days: int) -> datetime:
    d = start
    added = 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def grace_end(unsubscribed_at: datetime, method: str | None, settings: dict | None = None) -> datetime:
    s = settings or config.load_settings()
    if method == "one-click":
        return unsubscribed_at + timedelta(days=int(s.get("grace_days_one_click", 2)))
    return add_business_days(unsubscribed_at, int(s.get("grace_business_days_other", 10)))


def observed_until(account: str, path=None) -> datetime | None:
    """Newest message date seen for this account: how far our view of the mailbox reaches."""
    db = tracker.connect(path)
    try:
        row = db.execute("SELECT MAX(received) FROM messages WHERE account=?", (account,)).fetchone()
    finally:
        db.close()
    return tracker.parse_dt(row[0]) if row and row[0] else None


def evaluate(sender: dict, account: str, now: datetime | None = None, path=None, settings: dict | None = None,
             horizon: datetime | None = None) -> dict | None:
    """Proof status and evidence timeline for one sender (None if never unsubscribed)."""
    unsub_at = tracker.parse_dt(sender.get("unsubscribed_at"))
    if not unsub_at:
        return None
    now = now or datetime.now(timezone.utc)
    end = grace_end(unsub_at, sender.get("unsub_method"), settings)
    msgs = tracker.sender_messages(account, sender["address"], path)
    timeline = []
    after_grace = within_grace = 0
    for m in msgs:
        when = tracker.parse_dt(m["received"])
        if when is None:
            continue
        phase = "before" if when <= unsub_at else ("grace" if when <= end else "after")
        if phase == "grace":
            within_grace += 1
        elif phase == "after":
            after_grace += 1
        timeline.append({"date": when.isoformat(), "type": "message", "phase": phase, "subject": m["subject"]})
    timeline.append({"date": unsub_at.isoformat(), "type": "unsubscribed", "method": sender.get("unsub_method") or ""})
    timeline.append({"date": end.isoformat(), "type": "grace_end"})
    timeline.sort(key=lambda e: e["date"])
    horizon = horizon if horizon is not None else observed_until(account, path)
    if after_grace:
        verdict = STILL_SENDING
    elif now < end or not horizon or horizon < end:
        verdict = PENDING
    else:
        verdict = HONOURED
    return {"address": sender["address"], "verdict": verdict, "unsubscribed_at": unsub_at.isoformat(),
            "grace_end": end.isoformat(), "method": sender.get("unsub_method"), "after_grace": after_grace,
            "within_grace": within_grace, "observed_until": horizon.isoformat() if horizon else None,
            "timeline": timeline}


def refresh(account: str, now: datetime | None = None, path=None) -> list[dict]:
    settings = config.load_settings()
    horizon = observed_until(account, path)
    out = []
    for s in tracker.list_senders(account, path=path):
        if s["status"] in {"unsubscribed", "escalated", "blocked"}:
            result = evaluate(s, account, now, path, settings, horizon)
            if result:
                result["status"] = s["status"]
                out.append(result)
    return out


def ignored(account: str, now: datetime | None = None, path=None) -> list[dict]:
    """The "ignored your unsubscribe" list (not yet escalated)."""
    return [r for r in refresh(account, now, path) if r["verdict"] == STILL_SENDING and r["status"] == "unsubscribed"]
