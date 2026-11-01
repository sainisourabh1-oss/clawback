"""Scan-speed benchmark on fake backends with realistic latency and quota.

Simulated time (a virtual clock) is reported, so the benchmark runs in
seconds while modelling a scan that takes minutes:

* Gmail API: 200 ms per HTTP round trip (+2 ms per request inside a batch),
  per-user quota 6,000 units/minute enforced over a rolling minute (429 when
  exceeded), messages.get 20 units (60 for format=full), list 5 units
  (developers.google.com/workspace/gmail/api/reference/quota, 2026-10-16).
* IMAP: 200 ms per command and 2 MB/s download.

Mailbox: N synthetic messages over the last year, mixed like a typical
inbox (70% marketing/newsletters, 10% receipts and billing, 10% bank/security
alerts, 10% personal).

    python scripts/bench_scan.py --messages 3000
    python scripts/bench_scan.py --messages 3000 --repo path/to/old/checkout   # compare an older version
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

# Progress lines contain currency symbols; keep them printable when output is piped on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
# New mail for the rescan measurement: 60 messages is about a week of a
# 3,000-messages-a-year inbox (3,000 / 52 = 58).
NEW_MAIL = 60


def mailbox(n: int, now: datetime) -> list[bytes]:
    out = []
    for i in range(n):
        kind = i % 10
        m = EmailMessage()
        when = now - timedelta(days=365 * i / n, minutes=i)
        if kind < 7:
            brand = f"brand{i % 120}"
            m["From"] = f"{brand.title()} <news@{brand}.example>"
            m["Subject"] = f"Weekly picks from {brand.title()} #{i}"
            m["List-Unsubscribe"] = f"<https://{brand}.example/u/{i}>"
            m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
            body = "Hello! New arrivals this week. " * 40 + "Unsubscribe."
        elif kind == 7:
            m["From"] = f"Service{i % 15} <billing@service{i % 15}.example>"
            m["Subject"] = f"Your receipt for {when:%B %Y}"
            body = "Thanks for your payment. Plan: Pro\nAmount charged: US$9.99\nYour subscription renews monthly."
        elif kind == 8:
            m["From"] = "Example Bank <alerts@examplebank.example>"
            m["Subject"] = "Security alert: new sign-in"
            body = "A new sign-in to your account was detected."
        else:
            m["From"] = f"Friend {i % 30} <friend{i % 30}@mail.example>"
            m["Subject"] = f"Weekend plans {i}"
            body = "Are you free on Saturday?"
        m["To"] = "you@mail.example"
        m["Date"] = format_datetime(when)
        m["Message-ID"] = f"<bench-{i}@mail.example>"
        m["Authentication-Results"] = "mx.example; dkim=pass header.i=@example; spf=pass; dmarc=pass"
        m.set_content(body)
        out.append(bytes(m))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, default=3000)
    ap.add_argument("--repo", default=str(HERE.parent), help="checkout whose clawback package to benchmark")
    ap.add_argument("--latency", type=float, default=0.2)
    ap.add_argument("--quota", type=float, default=6000)
    args = ap.parse_args()
    sys.path[:0] = [args.repo, str(HERE.parent / "tests")]
    os.environ["CLAWBACK_HOME"] = tempfile.mkdtemp(prefix="clawback-bench-")
    from fakes import FakeClock, FakeGmail, FakeIMAP  # noqa: E402
    import clawback.sources.gmail as gmail_mod  # noqa: E402
    from clawback.scan import run_scan  # noqa: E402
    from clawback.sources.gmail import READONLY, GmailSource  # noqa: E402

    now = datetime.now(timezone.utc)
    raws = mailbox(args.messages, now)

    clock = FakeClock()
    fake = FakeGmail(raws, clock=clock, latency=args.latency, per_item=0.002, quota_per_minute=args.quota)
    import types
    gmail_mod.time = types.SimpleNamespace(monotonic=clock, sleep=clock.sleep)
    try:
        src = GmailSource("bench", service=fake, granted={READONLY}, sleep=clock.sleep, clock=clock)
    except TypeError:  # older versions have no clock parameter
        src = GmailSource("bench", service=fake, granted={READONLY}, sleep=clock.sleep)
    t0 = clock()
    import inspect
    first_eta = []

    def watch(snap):
        if snap.get("phase") == "reading" and snap.get("eta_seconds") is not None and not first_eta:
            first_eta.append((clock() - t0, snap["eta_seconds"]))
    if "since" in inspect.signature(run_scan).parameters:
        try:
            res = run_scan(src, since=now - timedelta(days=400), progress=watch)
        except TypeError:
            res = run_scan(src, since=now - timedelta(days=400))
    else:
        res = run_scan(src, limit=args.messages)
    gm = clock() - t0
    print(f"Gmail API : {res.messages:,} messages in {gm / 60:5.1f} simulated min = {res.messages / gm:5.2f} msg/s "
          f"(units {fake.units_spent:,}, 429s {fake.rejected}, full bodies {fake.formats.count('full'):,})")
    if first_eta:
        at, eta = first_eta[0]
        print(f"            first ETA at {at:4.1f} s: {eta / 60:4.1f} min left; actual {(gm - at) / 60:4.1f} min "
              f"({(eta - (gm - at)) / (gm - at) * 100:+.0f}%)")
    # Next scans on the API path: the same mailbox (nothing new), then a week
    # of new mail (NEW_MAIL messages, ~2 days' worth of a 3,000/year inbox x 3).
    # Both start one quota minute later, like a real "scan again" would.
    if "since" in inspect.signature(run_scan).parameters:
        for label, extra in (("nothing new", 0), (f"{NEW_MAIL} new", NEW_MAIL)):
            clock.sleep(60)
            newer = [m for m in mailbox(extra, now + timedelta(days=7))] if extra else []
            fake2 = FakeGmail(raws + newer, clock=clock, latency=args.latency, per_item=0.002,
                              quota_per_minute=args.quota)
            src2 = GmailSource("bench", service=fake2, granted={READONLY}, sleep=clock.sleep, clock=clock)
            t1 = clock()
            r = run_scan(src2, since=now - timedelta(days=400))
            print(f"API rescan: {label:>11}: {r.reused:,} already read, {r.messages:,} new, "
                  f"{clock() - t1:5.1f} simulated s (units {fake2.units_spent:,}, 429s {fake2.rejected})")

    try:
        from clawback.sources.imap import ImapSource
        iclock = FakeClock()
        imap = FakeIMAP(raws, clock=iclock, latency=args.latency, bytes_per_second=2e6, gmail=True)
        isrc = ImapSource("imap.gmail.com", "you@mail.example", "app-password", ["[Gmail]/All Mail"],
                          account="bench-imap", conn_factory=imap)
        t0 = iclock()
        if "since" in inspect.signature(run_scan).parameters:
            r2 = run_scan(isrc, since=now - timedelta(days=400))
        else:
            r2 = run_scan(isrc, limit=args.messages)
        im = iclock() - t0
        print(f"Gmail fast: {r2.messages:,} messages in {im:6.1f} simulated s  = {r2.messages / im:6.1f} msg/s "
              f"(IMAP + app password; full bodies {imap.full_fetches:,}, round trips "
              f"{getattr(isrc, 'stats', {}).get('round_trips', '?')})")
        # Next scan: nothing new arrived, so nothing is downloaded again.
        isrc2 = ImapSource("imap.gmail.com", "you@mail.example", "app-password", ["[Gmail]/All Mail"],
                           account="bench-imap", conn_factory=imap)
        t1 = iclock()
        r3 = run_scan(isrc2, since=now - timedelta(days=400))
        re_s = iclock() - t1
        print(f"Rescan    : {r3.reused:,} already read, {r3.messages:,} new, {re_s:6.1f} simulated s")
    except Exception as exc:  # noqa: BLE001 - older checkouts may not run against the new fake
        print(f"IMAP      : not measurable with this checkout ({type(exc).__name__}: {exc})")


if __name__ == "__main__":
    main()
