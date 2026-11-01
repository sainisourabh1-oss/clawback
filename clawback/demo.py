"""Synthetic demo mailbox. Every sender is fictional and uses the reserved
``.example`` top-level domain (RFC 2606), so nothing here can reach a real
company. Used by demo mode in the UI and by the test-suite.
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

from .extract import from_bytes
from .sources.base import ScanCancelled, Source


def _msg(frm: str, subject: str, body: str, when: datetime, html: bool = False, unsub: str | None = None,
         one_click: bool = False, labels: str = "Inbox", auth: str = "mx.example; dkim=pass; spf=pass; dmarc=pass",
         list_id: str | None = None, reply_to: str | None = None, n: int = 0) -> bytes:
    m = EmailMessage()
    m["From"] = frm
    m["To"] = "you@mail.example"
    m["Subject"] = subject
    m["Date"] = format_datetime(when)
    m["Message-ID"] = "<demo-" + hashlib.sha1(f"{frm}|{subject}|{n}|{when.date()}".encode()).hexdigest()[:20] + "@mail.example>"
    m["Authentication-Results"] = auth
    m["X-Gmail-Labels"] = labels
    if unsub:
        m["List-Unsubscribe"] = unsub
    if one_click:
        m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if list_id:
        m["List-Id"] = list_id
    if reply_to:
        m["Reply-To"] = reply_to
    if html:
        m.set_content("View this email in your browser.")
        m.add_alternative(f"<html><body>{body}</body></html>", subtype="html")
    else:
        m.set_content(body)
    return bytes(m)


def mailbox(now: datetime | None = None, base_url: str = "https://{host}") -> list[bytes]:
    """The demo mailbox. ``base_url`` lets tests point one-click links at a
    local HTTPS test server (``{host}`` is replaced by the sender's host)."""
    now = now or datetime.now(timezone.utc)
    out: list[bytes] = []

    def url(host: str, path: str) -> str:
        return base_url.format(host=host).rstrip("/") + path

    def ago(days: float) -> datetime:
        return now - timedelta(days=days)

    # ---- paid subscriptions (mixed currencies)
    for i in range(6):
        out.append(_msg("Streamora <billing@streamora.example>", "Your Streamora receipt",
                        "<p>Thanks for your payment.</p><p>Plan: Standard</p><p>Amount charged: US$15.49</p>"
                        "<p>Your subscription renews monthly.</p>", ago(5 + 30 * i), html=True, n=i))
    for i in range(6):
        out.append(_msg("Tunewave <no-reply@tunewave.example>", "Tunewave Premium – Zahlungsbestätigung / payment confirmation",
                        "Your Tunewave Premium subscription was renewed.\nTotal: 10,99 € per month\nNext billing date: "
                        f"{(now + timedelta(days=25 - i * 0)).strftime('%d %b %Y')}", ago(5 + 30 * i), n=i))
    for i in range(4):
        out.append(_msg("CloudLocker <receipts@cloudlocker.example>", "CloudLocker receipt",
                        "CloudLocker 200 GB\nBilled monthly\nTotal ₹219.00 (INR)\nAuto-renews until you cancel.", ago(12 + 30 * i), n=i))
    out.append(_msg("Pixelforge <invoices@pixelforge.example>", "Your Pixelforge annual plan invoice",
                    "Pixelforge Pro annual plan\nAmount paid: A$239.88 per year\nYour plan renews automatically on "
                    f"{(now + timedelta(days=200)).strftime('%d %B %Y')}.", ago(165)))
    for i in range(3):
        out.append(_msg("Kotoba Lessons <info@kotoba.example>", "Kotoba monthly receipt",
                        "Subscription fee: 1,200 JPY per month. Thank you for learning with us.", ago(3 + 30 * i), n=i))
    out.append(_msg("FitPulse <hello@fitpulse.example>", "Your free trial ends in 3 days",
                    "Your FitPulse trial ends on " + (now + timedelta(days=3)).strftime("%d %b %Y") +
                    ". After that you will be charged £7.99 per month unless you cancel.", ago(4)))
    out.append(_msg("Example Bank Alerts <alerts@examplebank.example>", "UPI AutoPay mandate successfully created",
                    "Dear Customer, your UPI AutoPay mandate for Rs. 149.00 towards CricStream Plus has been created. "
                    "Frequency: Monthly. UMN: demo1234567890@exbank. To revoke, use your UPI app.", ago(40)))
    out.append(_msg("Example Bank Alerts <alerts@examplebank.example>", "Pre-debit notification for UPI AutoPay",
                    "Rs. 149.00 will be debited from your account on " + (now + timedelta(days=2)).strftime("%d %b %Y") +
                    " towards CricStream Plus under UPI AutoPay mandate UMN demo1234567890@exbank.", ago(1)))
    out.append(_msg("Euro Gym <billing@eurogym.example>", "SEPA Direct Debit pre-notification",
                    "We will collect EUR 29,90 by SEPA Direct Debit on " + (now + timedelta(days=6)).strftime("%d %B %Y") +
                    " for your monthly membership. Mandate reference: EG-2026-0042. Creditor ID: DE98ZZZ09999999999.", ago(2)))

    # ---- newsletters & marketing
    letters = [
        ("Daily Deals <deals@shopmart.example>", "Today only: 40% off everything", 90, 0.95, "one-click", "shopmart.example"),
        ("The Weekly Byte <news@weeklybyte.example>", "This week in tech", 26, 0.6, "one-click", "weeklybyte.example"),
        ("TravelNow <offers@travelnow.example>", "Your next getaway awaits", 45, 1.0, "one-click", "travelnow.example"),
        ("Recipe Box <hello@recipebox.example>", "5 dinners in 20 minutes", 30, 0.8, "mailto", "recipebox.example"),
        ("Gadget Store <promo@gadgetstore.example>", "New arrivals you'll love", 60, 1.0, "link", "gadgetstore.example"),
        ("Community Forum <digest@forum.example>", "Your daily digest", 120, 0.3, "one-click", "forum.example"),
        ("Webinar Hub <events@webinarhub.example>", "Join our free webinar", 15, 1.0, "mismatch", "webinarhub.example"),
        ("Fashion Picks <style@fashionpicks.example>", "Trending styles this week", 40, 0.98, "one-click", "fashionpicks.example"),
    ]
    for frm, subj, count, unread_ratio, method, host in letters:
        span = 180
        for i in range(count):
            when = ago(span * i / max(1, count) + 0.5)
            unread = i < round(count * unread_ratio)
            if method == "one-click":
                header, oc = f"<{url('unsubscribe.' + host, '/u?id=demo')}>, <mailto:unsubscribe@{host}>", True
            elif method == "mailto":
                header, oc = f"<mailto:leave@{host}?subject=unsubscribe>", False
            elif method == "link":
                header, oc = f"<{url('www.' + host, '/preferences')}>", False
            else:
                # A link and a mailto on the same unknown domain: one warning line.
                header, oc = f"<{url('track.clickmetrics.example', '/unsub?u=1')}>, <mailto:unsub@clickmetrics.example>", False
            labels = "Inbox,Category Promotions" + (",Unread" if unread else ",Opened")
            out.append(_msg(frm, subj, "Hello! Great things this week.\nUnsubscribe | Manage your subscription",
                            when, unsub=header, one_click=oc, labels=labels, list_id=f"<list.{host}>", n=i))

    # A newsletter without a List-Unsubscribe header: its unsubscribe link is
    # only in the email body ("Link in email"; opened by you after confirming).
    for i in range(10):
        out.append(_msg("Book Club <club@bookclub.example>", "This month's picks",
                        "<p>Our picks this month.</p><p style=\"font-size:12px\">Not interested any more? "
                        f"<a href=\"{url('bookclub.example', '/leave?u=demo')}\">Unsubscribe</a></p>",
                        ago(3 + 18 * i), html=True, labels="Inbox,Category Promotions" + (",Unread" if i < 8 else ",Opened"),
                        n=i))

    # ---- spam-like
    out.append(_msg("Prize Center <winner@prizes-xn--ppal-demo.example>", "Congratulations! You have won a gift card",
                    "Claim your prize now!", ago(3), unsub=f"<{url('xn--ppal-demo.example', '/u')}>",
                    auth="mx.example; dkim=fail; spf=fail; dmarc=fail", labels="Inbox,Unread"))
    for i in range(4):
        out.append(_msg("\"security@bank.example\" <alerts@secure-login.example>", "Urgent action required on your account",
                        "Verify your account now.", ago(2 + i * 9), unsub=f"<{url('203.0.113.7', '/stop')}>",
                        auth="mx.example; dkim=none; spf=softfail; dmarc=fail", labels="Inbox,Unread", n=i))
    for i in range(6):
        out.append(_msg("Crypto Gains <team@moonshot.example>", "Crypto giveaway: double your coins",
                        "Limited time only.", ago(1 + i * 5), unsub="<https://bit.ly/demo-unsub>",
                        auth="mx.example; dkim=pass; spf=pass; dmarc=fail", labels="Inbox,Unread", n=i))

    # ---- personal / transactional (never suggested)
    # ---- important / transactional (kept by default, never suggested)
    for i in range(3):
        out.append(_msg("Example Bank <statements@examplebank.example>", "Your account statement for "
                        + ("September", "August", "July")[i] + " 2026", "Your monthly account statement is attached.",
                        ago(8 + 30 * i), n=i, labels="Inbox,Opened"))
    for i in range(4):
        out.append(_msg("Codehub <notifications@codehub.example>", f"Re: [demo-org/app] Fix login redirect (PR #{12 + i})",
                        "A reviewer approved this pull request.\nReply to this email directly or unsubscribe.",
                        ago(1 + 3 * i), unsub=f"<{url('codehub.example', '/notifications/unsubscribe/' + str(i))}>",
                        list_id="demo-org/app <app.demo-org.codehub.example>", n=i))
    out.append(_msg("Asha Rao <asha@mail.example>", "Lunch on Friday?", "Are you free?", ago(2), labels="Inbox,Opened"))
    out.append(_msg("Parcel Co <tracking@parcel.example>", "Your parcel is on the way", "Tracking number PX123.", ago(6), labels="Inbox,Opened"))
    return out


class DemoSource(Source):
    """Demo mailbox source. Mailbox actions are simulated and clearly labelled."""
    kind = "demo"

    def __init__(self, account: str = "demo", now: datetime | None = None, base_url: str = "https://{host}"):
        super().__init__(account)
        self.now = now
        self.base_url = base_url
        # The default demo links point at fictional .example hosts: simulate the
        # HTTPS step too. Tests pass a base_url for a real local test server.
        self.simulate_http = base_url == "https://{host}"
        self.capabilities = {"one-click", "link", "mailto", "send", "trash", "archive", "spam", "block"}
        self.simulated: list[tuple] = []

    def iter_messages(self, limit: int | None = None, progress=None, cancel=None, since=None, skip_ids=None):
        from .progress import checkpoint
        from .sources.mbox import _labels_of, _message_id
        infos = []
        for i, raw in enumerate(mailbox(self.now, self.base_url), 1):
            labels, unread = _labels_of(raw)
            info = from_bytes(raw, _message_id(raw, str(i)), unread=unread, labels=labels)
            if since is None or info.date is None or info.date >= since:
                infos.append(info)
        infos.sort(key=lambda m: m.date.timestamp() if m.date else 0, reverse=True)   # newest first, like Gmail
        self.capped = limit is not None and len(infos) > limit
        infos = infos[:limit] if limit is not None else infos
        if progress:
            progress(0, len(infos), phase="reading")
        for i, info in enumerate(infos, 1):
            checkpoint(cancel)
            if progress:
                progress(i, len(infos), current_date=info.date)
            yield info

    # Simulated mailbox actions (demo only).
    def _sim(self, op, *args):
        self.simulated.append((op, args))

    def send_email(self, to, subject, body):
        self._sim("send", to, subject); return {"message_id": "demo", "simulated": True}

    def trash(self, ids):
        self._sim("trash", len(ids)); return {"op": "untrash", "ids": ids, "simulated": True}

    def untrash(self, ids):
        self._sim("untrash", len(ids))

    def archive(self, ids):
        self._sim("archive", len(ids)); return {"op": "unarchive", "ids": ids, "simulated": True}

    def unarchive(self, ids):
        self._sim("unarchive", len(ids))

    def report_spam(self, ids):
        self._sim("spam", len(ids)); return {"op": "unreport_spam", "ids": ids, "simulated": True}

    def unreport_spam(self, ids):
        self._sim("unspam", len(ids))

    def create_filter(self, address, mode):
        self._sim("filter", address, mode); return {"op": "delete_filter", "filter_id": f"demo-{address}", "simulated": True}

    def delete_filter(self, filter_id):
        self._sim("delete_filter", filter_id)
