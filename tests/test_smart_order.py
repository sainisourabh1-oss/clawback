"""Gmail API path: what is quota-bound is read in the most useful order, and
the time estimate comes from the quota (units left / units per minute).

User-visible outcome: subscriptions and newsletters appear in the partial
results first even when they are older than a pile of personal mail, the
partial view never claims "scanned back to <date>" while reading out of date
order, and the first ETA is close to the real duration."""
import math
from datetime import timedelta

import pytest

from fakes import FakeClock, FakeGmail
from realmail import NOW, msg
from clawback import scan as scan_mod
from clawback.progress import Tracker
from clawback.scan import run_scan
from clawback.sources import gmail as gmail_mod
from clawback.sources.gmail import ORDER_NOTE, READONLY, GmailSource
from clawback.sources.pacing import WINDOW, Pacer


def gsrc(fake):
    return GmailSource("g", service=fake, granted={READONLY}, sleep=fake.clock.sleep, clock=fake.clock)


def mailbox():
    personal = [msg(f"Friend {i} <friend{i}@home.example>", f"Lunch {i}?", "See you at noon.", days_ago=0.1 + i * 0.1)
                for i in range(150)]                                   # newest 15 days: personal only
    bulk = [msg(f"Shop {i % 5} <news@shop{i % 5}.example>", f"Deals {i}", "Weekly deals. Unsubscribe.",
                days_ago=20 + i, lu=f"<https://shop{i % 5}.example/u/{i}>") for i in range(40)]
    paid = [msg("Svc <billing@svc.example>", f"Your receipt #{i}", "Amount charged: US$9.99. Renews monthly.",
                days_ago=30 + 30 * i) for i in range(3)]
    return personal + bulk + paid


def test_likely_subscriptions_and_newsletters_are_read_first():
    fake = FakeGmail(mailbox())
    order = [m.from_addr for m in gsrc(fake).iter_messages(since=NOW - timedelta(days=400))]
    # two extra list queries (5 units a page): the priority order and the
    # body count for the time estimate
    assert fake.priority_lists == 1 and fake.body_lists == 1
    first = order[:43]
    assert set(first) == {f"news@shop{i}.example" for i in range(5)} | {"billing@svc.example"}
    assert all(a.startswith("friend") for a in order[43:])


def test_partial_view_claims_no_date_during_the_priority_pass(monkeypatch):
    monkeypatch.setattr(scan_mod, "PREVIEW_SECONDS", 0.0)
    fake = FakeGmail(mailbox())
    seen = []
    run_scan(gsrc(fake), since=NOW - timedelta(days=400), on_preview=seen.append)
    early, late = seen[:43], seen[45:]
    assert all(p["scanned_back_to"] is None and p["note"] == ORDER_NOTE for p in early)
    assert any(s["vendor"] for s in early[-1]["subscriptions"])           # the receipt is already shown
    assert all(p["scanned_back_to"] and p["note"] == "" for p in late)
    backs = [p["scanned_back_to"] for p in late]
    assert backs == sorted(backs, reverse=True)


def test_first_eta_comes_from_quota_units_and_is_close_to_the_real_time():
    extra = [msg(f"Pal {i} <pal{i}@home.example>", f"Photos {i}", "Here they are.", days_ago=16 + i * 0.2)
             for i in range(1800)]
    fake = FakeGmail(mailbox() + extra, latency=0.2, per_item=0.002, quota_per_minute=6000)
    src = gsrc(fake)
    prog = Tracker(clock=fake.clock)
    etas = []

    def spy(done, total=None, current_date=None, **kw):
        prog(done, total, current_date, **kw)
        if prog.phase == "reading" and prog.eta_seconds is not None:
            etas.append((fake.clock(), prog.eta_seconds))
    t0 = fake.clock()
    list(src.iter_messages(since=NOW - timedelta(days=800), progress=spy))
    real = fake.clock() - t0
    first_eta = etas[0][1]
    assert fake.rejected == 0 and real > 300          # quota-bound: several minutes
    # within 20%: the estimate ignores the first minute's burst allowance and
    # round trips, both small against a quota-bound scan
    assert abs(first_eta - real) / real < 0.20, (first_eta, real)
    # an ETA exists from the first report (no "estimating..." phase)
    assert etas[0][0] - t0 < 5


# ---------------------------------------------------------------- packing
# Regression (bench 16.1 vs 14.1 min): the priority pass is mostly billing
# mail, i.e. full bodies at 60 units; 50 of them in one request (3,000 units)
# packed so badly into the 5,700-unit window that the pacer idled.

class RecordingPacer(Pacer):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.takes: list[tuple[float, float]] = []      # (units, budget at the time)

    def take(self, units):
        self.takes.append((float(units), self.budget))
        super().take(units)


def billing_heavy(n: int, receipts_every: int = 10):
    """n messages, newest first; every ``receipts_every``-th is a receipt
    (priority query hit that needs a full body), the rest personal mail the
    priority query does not match: the shape that caused the regression."""
    out = []
    for i in range(n):
        if i % receipts_every == 0:
            out.append(msg(f"Svc{i % 7} <billing@svc{i % 7}.example>", f"Your receipt #{i}",
                           "Amount charged: US$9.99. Renews monthly.", days_ago=0.05 + i * 0.1))
        else:
            out.append(msg(f"Pal {i % 40} <pal{i % 40}@home.example>", f"Photos {i}", "Here they are.",
                           days_ago=0.05 + i * 0.1))
    return out


def timed_scan(raws, quota=6000, pacer_quota=None):
    fake = FakeGmail(raws, latency=0.2, per_item=0.002, quota_per_minute=quota)
    pacer = RecordingPacer(pacer_quota or quota, clock=fake.clock, sleep=fake.clock.sleep)
    src = GmailSource("g", service=fake, granted={READONLY}, sleep=fake.clock.sleep, clock=fake.clock, pacer=pacer)
    prog = Tracker(clock=fake.clock)
    etas, infos = [], []

    def spy(done, total=None, current_date=None, **kw):
        prog(done, total, current_date, **kw)
        if prog.phase == "reading" and prog.eta_seconds is not None:
            etas.append((fake.clock(), prog.eta_seconds))
    t0 = fake.clock()
    for info in src.iter_messages(since=NOW - timedelta(days=800), progress=spy):
        infos.append(info)
    return fake, src, pacer, fake.clock() - t0, [(t - t0, e) for t, e in etas], infos


def window_steps_floor(units: float, budget: float) -> float:
    """Fastest possible finish for a sliding-window pacer: the first window's
    budget at once, then one whole window per further budget's worth."""
    return WINDOW * max(0, math.ceil((units - budget) / budget))


def test_no_request_takes_more_than_an_eighth_of_the_window_and_the_scan_hits_the_quota_floor():
    fake, src, pacer, real, _, _ = timed_scan(billing_heavy(1000))
    assert fake.rejected == 0
    assert max(u for u, _ in pacer.takes) <= pacer.limit * gmail_mod.TAKE_FRACTION
    assert max(fake.batches) <= 35                  # 5,700 / 8 // 20
    # User-visible: the scan ends in the first window step its units allow
    # (plus round trips), not a minute or more later.
    floor = window_steps_floor(fake.units_spent, pacer.limit)
    assert real <= floor + 10, (real, floor)


def test_the_old_whole_batch_takes_would_fail_the_floor_check(monkeypatch):
    """Guards the guard: with one take per 50-id batch (the regression) the
    same scan misses the floor by at least one window."""
    monkeypatch.setattr(gmail_mod, "TAKE_FRACTION", 1.0)
    fake, _, pacer, real, _, _ = timed_scan(billing_heavy(1000))
    assert real > window_steps_floor(fake.units_spent, pacer.limit) + WINDOW - 10


def test_priority_mail_still_arrives_first_when_requests_are_split():
    fake, _, _, _, _, infos = timed_scan(billing_heavy(1000))
    order = [i.from_addr for i in infos]
    assert fake.priority_lists == 1
    assert all(a.startswith("billing@") for a in order[:100])
    assert all(a.startswith("pal") for a in order[100:])


def test_eta_is_accurate_from_the_first_report_to_the_end():
    _, _, _, real, etas, _ = timed_scan(billing_heavy(1000))
    first_at, first = etas[0]
    assert first_at < 5
    # bench: -1%; the bound users were promised is 20%, kept tight here so a
    # drift shows up long before it is visible
    assert abs(first - (real - first_at)) / (real - first_at) < 0.10, (first, real)
    for at, eta in etas:
        left = real - at
        assert abs(eta - left) <= max(0.10 * left, 10.0), (at, eta, left)


def test_tiny_mailbox_is_one_request_with_no_priority_query():
    fake, src, _, real, etas, infos = timed_scan(billing_heavy(5))
    assert len(infos) == 5 and fake.priority_lists == 0 and src.order_note == ""
    assert fake.batches[0] == 5                      # one metadata request for all five
    assert etas[0][1] == 0.0 and real < 5            # fits the free window: no waiting


def test_empty_priority_result_keeps_date_order_and_claims_dates_at_once():
    raws = [msg(f"Pal {i} <pal{i}@home.example>", f"Photos {i}", "Here they are.", days_ago=0.1 + i * 0.1)
            for i in range(150)]
    fake = FakeGmail(raws)
    src = gsrc(fake)
    dates = []
    infos = list(src.iter_messages(since=NOW - timedelta(days=400),
                                   progress=lambda d, t=None, current_date=None, **kw: dates.append(current_date)))
    assert fake.priority_lists == 1 and src.order_note == ""
    assert [i.date for i in infos] == sorted((i.date for i in infos), reverse=True)
    assert next(d for d in dates if d is not None) == infos[0].date


def test_mixed_chunk_splits_full_bodies_into_eleven_id_requests():
    raws = [msg("Svc <billing@svc.example>", f"Your receipt #{i}", "Amount charged: US$9.99.", days_ago=1 + i)
            for i in range(30)] + [msg(f"Pal {i} <pal{i}@home.example>", f"Hi {i}", "Hello.", days_ago=40 + i)
                                   for i in range(5)]
    fake = FakeGmail(raws)
    infos = list(gsrc(fake).iter_messages(since=NOW - timedelta(days=400)))
    assert len(infos) == 35
    assert fake.batches == [35, 11, 11, 8]           # one metadata request, bodies at <= 5,700/8 // 60
    assert fake.formats.count("full") == 30


def test_rate_limit_mid_scan_retries_only_the_rejected_part_and_shrinks_requests():
    """Another app shares the quota: Google allows 3,000 units a minute but
    the pacer assumes 6,000. Rejected sub-requests are retried (never in a
    bigger request than the one rejected), every message arrives exactly
    once, and after a 429 each request is sized from the halved budget."""
    fake, src, pacer, _, _, infos = timed_scan(billing_heavy(600, receipts_every=2), quota=3000, pacer_quota=6000)
    assert fake.rejected > 0 and pacer.throttles > 0
    ids = [i.source_id for i in infos]
    assert len(ids) == 600 and len(set(ids)) == 600
    assert sum(1 for i in infos if i.body) >= 300        # the receipts got their bodies despite 429s
    for units, budget in pacer.takes:
        assert units <= max(budget * gmail_mod.TAKE_FRACTION, gmail_mod.UNITS["full"])


# ---------------------------------------------------------------- pacer
def test_pacer_seconds_for_counts_whole_window_steps():
    clock = FakeClock()
    p = Pacer(6000, clock=clock, sleep=clock.sleep)       # budget 5,700
    assert p.seconds_for(5700) == 0.0                    # fits the empty window now
    assert p.seconds_for(5701) == pytest.approx(60.0)    # 1 unit waits for the window to turn
    assert p.seconds_for(5700 * 3) == pytest.approx(120.0)
    p.take(2000)
    clock.sleep(10)
    p.take(2000)                                         # used 4,000, free 1,700
    assert p.seconds_for(1700) == 0.0
    assert p.seconds_for(1700 + 2000) == pytest.approx(50.0)   # first entry expires 60 s after it was taken
    assert p.seconds_for(1700 + 2001) == pytest.approx(60.0)   # needs the second one too


def test_settle_keeps_units_in_our_window_until_they_leave_googles():
    clock = FakeClock()
    p = Pacer(6000, clock=clock, sleep=clock.sleep)
    p.take(5700)
    clock.sleep(0.3)                                     # the request's round trip
    p.settle()
    clock.sleep(59.8)                                    # 60.1 s after take(), 59.8 s after the answer
    t = clock()
    p.take(100)
    assert clock() - t == pytest.approx(0.2)             # waited until 60 s after the answer
