"""Scan window ("scan back to"), newest-first reading, progress/ETA, cancel
with partial results, incremental rescans, quota pacing and the IMAP / mbox
fast paths. Speed is measured on fakes with a virtual clock."""
from datetime import date, datetime, timedelta, timezone

import pytest

from fakes import FakeClock, FakeGmail, FakeIMAP
from helpers import write_mbox
from realmail import msg
from clawback import tracker
from clawback.progress import ScanControl, Tracker, describe, eta_text
from clawback.scan import parse_since, run_scan
from clawback.sources.base import ScanCancelled
from clawback.sources.gmail import READONLY, GmailSource, _gmail_query
from clawback.sources.imap import ImapSource
from clawback.sources.mbox import MboxSource, quick_date
from clawback.sources.pacing import Pacer

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))


def newsletters(n, step_days=1.0, start=NOW):
    return [msg(f"Shop {i % 7} <news@shop{i % 7}.example>", f"Deals {i}", "Weekly deals. Unsubscribe.",
                days_ago=i * step_days, lu=f"<https://shop{i % 7}.example/u/{i}>", now=start) for i in range(n)]


def gsrc(fake, **kw):
    return GmailSource("g", service=fake, granted={READONLY}, sleep=fake.clock.sleep, clock=fake.clock, **kw)


# ------------------------------------------------------------ window parsing
def test_presets_are_calendar_months_from_local_midnight():
    today = date(2026, 10, 24)
    assert parse_since("3m", today, IST) == datetime(2026, 7, 24, tzinfo=IST)
    assert parse_since("1y", today, IST) == datetime(2025, 10, 24, tzinfo=IST)
    assert parse_since("1m", date(2026, 3, 31), IST) == datetime(2026, 2, 28, tzinfo=IST)  # clamped day
    assert parse_since("all", today, IST) is None
    assert parse_since("2026-03-12", today, IST) == datetime(2026, 3, 12, tzinfo=IST)
    with pytest.raises(ValueError):
        parse_since("2027-01-01", today, IST)
    with pytest.raises(ValueError):
        parse_since("last tuesday", today, IST)


def test_gmail_query_uses_epoch_seconds_not_pacific_dates():
    since = datetime(2026, 3, 12, tzinfo=IST)
    q = _gmail_query("", since)
    assert f"after:{int(since.timestamp()) - 1}" in q and "-in:spam" in q and "/" not in q


# ------------------------------------------------------------ boundary and order
def test_gmail_window_boundary_is_inclusive_in_the_users_time_zone():
    since = datetime(2026, 10, 20, tzinfo=IST)            # 19 Oct 18:30 UTC
    raws = [msg("A <a@x.example>", "at boundary", "x", now=since, days_ago=0),
            msg("A <a@x.example>", "one second early", "x", now=since - timedelta(seconds=1), days_ago=0),
            msg("A <a@x.example>", "same UTC date, earlier IST day", "x", now=since - timedelta(hours=3), days_ago=0),
            msg("A <a@x.example>", "inside", "x", now=since + timedelta(days=2), days_ago=0)]
    infos = list(gsrc(FakeGmail(raws)).iter_messages(since=since))
    assert sorted(i.subject for i in infos) == ["at boundary", "inside"]


def test_gmail_reads_newest_first_and_stops_at_the_window():
    raws = newsletters(40, step_days=1)
    since = NOW - timedelta(days=9, hours=12)
    infos = list(gsrc(FakeGmail(raws)).iter_messages(since=since))
    assert len(infos) == 10
    dates = [i.date for i in infos]
    assert dates == sorted(dates, reverse=True) and min(dates) >= since


def test_limit_is_optional_and_a_hit_cap_is_reported():
    fake = FakeGmail(newsletters(30))
    src = gsrc(fake)
    res = run_scan(src, limit=10, since=NOW - timedelta(days=400))
    assert res.messages == 10 and res.capped is True
    res2 = run_scan(gsrc(FakeGmail(newsletters(30))), since=NOW - timedelta(days=400), incremental=False)
    assert res2.messages == 30 and res2.capped is False


# ------------------------------------------------------------ speed (virtual clock)
def test_gmail_scan_is_paced_under_quota_and_uses_metadata_for_most_mail():
    raws = newsletters(600) + [msg("Svc <billing@svc.example>", f"Your receipt #{i}", "Amount charged: US$9.99. "
                                   "Renews monthly.", days_ago=i * 3) for i in range(30)]
    fake = FakeGmail(raws, latency=0.2, per_item=0.002, quota_per_minute=6000)
    src = gsrc(fake)
    t0 = fake.clock()
    res = run_scan(src, since=NOW - timedelta(days=800))
    minutes = (fake.clock() - t0) / 60
    assert res.messages == 630
    assert fake.rejected == 0, "the pacer must keep the scan under the per-minute quota"
    assert fake.formats.count("full") == 30 and fake.formats.count("metadata") == 630
    floor = fake.units_spent / 6000  # minutes Google's quota allows at best
    assert minutes < floor * 1.15, (minutes, floor)
    # the old way (format=full for every message) needs 3x the units
    assert fake.units_spent < 630 * 60 / 2.5


def test_pacer_sliding_window_never_exceeds_budget():
    clock = FakeClock()
    p = Pacer(6000, clock=clock, sleep=clock.sleep)
    log = []
    for _ in range(40):
        p.take(1000)
        log.append(clock())
    for i, t in enumerate(log):
        in_window = [x for x in log[:i + 1] if t - x < 60]
        assert len(in_window) * 1000 <= 6000 * 0.95 + 1000


def test_imap_header_first_scan_is_fast_and_fetches_few_bodies():
    raws = newsletters(3000, step_days=0.1) + [msg("Svc <billing@svc.example>", f"Your receipt #{i}",
                                                   "Amount charged: US$9.99. Renews monthly.", days_ago=i)
                                               for i in range(50)]
    clock = FakeClock()
    imap = FakeIMAP(raws, clock=clock, latency=0.2, bytes_per_second=2e6, gmail=True)
    src = ImapSource("imap.gmail.com", "you@mail.example", "pw", ["[Gmail]/All Mail"], conn_factory=imap)
    t0 = clock()
    res = run_scan(src, since=NOW - timedelta(days=400))
    assert res.messages == 3050 and imap.full_fetches == 50
    assert clock() - t0 < 60, "3,000+ messages over IMAP should take well under a minute"


def test_imap_since_searches_a_day_early_and_filters_exactly():
    since = datetime(2026, 10, 20, tzinfo=IST)
    raws = [msg("A <a@x.example>", "early", "x", now=since - timedelta(hours=2), days_ago=0),
            msg("A <a@x.example>", "in", "x", now=since + timedelta(hours=1), days_ago=0)]
    imap = FakeIMAP(raws)
    infos = list(ImapSource("imap.example", "u", "p", conn_factory=imap).iter_messages(since=since))
    assert [i.subject for i in infos] == ["in"]


def test_mbox_skips_older_messages_from_headers_only(tmp_path):
    since = NOW - timedelta(days=5)
    raws = newsletters(10, step_days=1)
    infos = list(MboxSource(write_mbox(tmp_path / "x.mbox", raws)).iter_messages(since=since))
    assert len(infos) == 6 and all(i.date >= since for i in infos)   # day 0..5, the 5th exactly at the boundary
    no_date = b"From: a@x.example\nReceived: from mx by mx; Mon, 19 Oct 2026 10:00:00 +0000\nSubject: s\n\nbody"
    assert quick_date(no_date) == datetime(2026, 10, 19, 10, tzinfo=timezone.utc)


# ------------------------------------------------------------ cancel, pause, incremental
def test_cancel_mid_scan_keeps_partial_results():
    fake = FakeGmail(newsletters(300))
    control = ScanControl()
    seen = []

    def progress(snap):
        seen.append(snap)
        if snap["done"] >= 120:
            control.cancel()
    res = run_scan(gsrc(fake), progress=progress, cancel=control, since=NOW - timedelta(days=400))
    assert res.partial is True and 100 <= res.messages < 300
    assert len(tracker.list_senders("g")) == 7       # senders found so far are saved
    db = tracker.connect()
    row = db.execute("SELECT partial, messages FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    assert row["partial"] == 1 and row["messages"] == res.messages


def test_rescan_continues_where_a_cancelled_scan_stopped():
    raws = newsletters(200)
    control = ScanControl()
    run_scan(gsrc(FakeGmail(raws)), cancel=control, since=NOW - timedelta(days=400),
             progress=lambda s: control.cancel() if s["done"] >= 50 else None)
    fake = FakeGmail(raws)
    res = run_scan(gsrc(fake), since=NOW - timedelta(days=400))
    assert res.reused >= 50 and res.messages == 200 - res.reused
    assert fake.formats.count("metadata") == res.messages


def test_incremental_rescan_reads_only_new_mail_and_keeps_totals():
    receipts = [msg("Svc <billing@svc.example>", f"Your receipt {i}", "Amount charged: US$9.99. Renews monthly.",
                    days_ago=30 * i + 2) for i in range(4)]
    raws = newsletters(50) + receipts
    first = run_scan(gsrc(FakeGmail(raws)), since=NOW - timedelta(days=400))
    newer = [msg("Svc <billing@svc.example>", "Your receipt new", "Amount charged: US$9.99. Renews monthly.",
                 days_ago=0.5, now=NOW + timedelta(days=1))]
    fake = FakeGmail(raws + newer)
    second = run_scan(gsrc(fake), since=NOW - timedelta(days=400))
    assert second.messages == 1 and second.reused == len(raws)
    assert fake.formats.count("metadata") == 1
    subs = [s for s in second.subscriptions if s.domain == "svc.example"]
    assert len(subs) == 1 and subs[0].hits == 5 and subs[0].amount == 9.99   # same as a full rescan
    assert len(first.subscriptions) == len(second.subscriptions)


def test_incremental_rescan_refreshes_read_state_cheaply():
    raws = newsletters(20)
    fake = FakeGmail(raws)
    for m in fake.store.values():
        m["labelIds"] = sorted(set(m["labelIds"]) | {"UNREAD"})
    run_scan(gsrc(fake), since=NOW - timedelta(days=400))
    assert sum(r["unread_count"] for r in tracker.list_senders("g")) == 20
    for m in fake.store.values():
        m["labelIds"] = sorted(set(m["labelIds"]) - {"UNREAD"})
    fake.formats.clear()
    run_scan(gsrc(fake), since=NOW - timedelta(days=400))
    db = tracker.connect()
    unread = db.execute("SELECT SUM(unread) FROM messages WHERE account='g'").fetchone()[0]
    db.close()
    assert unread == 0 and fake.formats == []


def test_pause_blocks_and_resume_continues():
    import threading
    control = ScanControl()
    control.pause()
    out = []
    t = threading.Thread(target=lambda: out.append(run_scan(gsrc(FakeGmail(newsletters(10))), cancel=control,
                                                            since=NOW - timedelta(days=400))))
    t.start()
    t.join(0.5)
    assert t.is_alive() and not out, "a paused scan must wait"
    control.resume()
    t.join(10)
    assert out and out[0].messages == 10 and out[0].partial is False


# ------------------------------------------------------------ ETA
def test_eta_waits_for_data_then_is_steady_and_never_negative():
    clock = FakeClock()
    tr = Tracker(clock=clock)
    tr(0, 1000, phase="reading")
    etas = []
    rate = 10.0
    done = 0
    for step in range(200):
        clock.sleep(1.0)
        r = rate * (0.5 if step % 10 == 0 else 1.0)      # every 10th second is slow (a batch boundary)
        done = min(1000, done + int(r))
        tr(done, 1000)
        etas.append(tr.eta_seconds)
        if done >= 1000:
            break
    assert etas[0] is None and eta_text(None) == "estimating…"
    shown = [e for e in etas if e is not None]
    assert all(e >= 0 for e in shown) and shown[-1] == 0
    jumps = [b - a for a, b in zip(shown, shown[1:])]
    assert max(jumps) <= 0.25 * max(shown) + 1, "the ETA must not jump upwards"
    mid = shown[len(shown) // 3]
    true_left = (1000 - 10 * (len(etas) - len(shown) + len(shown) // 3)) / 9.5
    assert abs(mid - true_left) / true_left < 0.35


def test_progress_line_matches_the_spec():
    snap = {"done": 1240, "total": 3180, "percent": 39, "eta_text": "about 4 min left", "current_date": "2026-03-12",
            "phase": "reading", "unit": "messages"}
    assert describe(snap) == "1,240 of 3,180 emails · 39% · about 4 min left · back to 12 Mar 2026"


def test_progress_reports_senders_and_date_reached():
    snaps = []
    run_scan(gsrc(FakeGmail(newsletters(30, step_days=2))), progress=snaps.append, since=NOW - timedelta(days=400))
    last = snaps[-1]
    assert last["senders"] == 7 and last["total"] == 30 and last["done"] == 30
    assert last["current_date"] == (NOW - timedelta(days=58)).date().isoformat()
    assert any(s["phase"] == "counting" for s in snaps)
