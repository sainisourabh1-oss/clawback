"""Operator's real-Gmail test (round 3, item B): "Scan again" and the first
scan offer a quick update (default), a fresh full scan, or a custom scan
(since a date, or the newest N emails), each with a time estimate where one
can be made, and the results say what was scanned.

"The newest N" must mean the NEWEST N on every source and on every scan:
the limit counts emails an earlier scan already read (they are reused, not
read again), so asking again for the newest 5 reads nothing new. Before
this, IMAP counted only emails not read yet (so a second "newest 5" read the
next 5 older ones) and an export stopped at the first N in file order.
All data is invented."""
import random
from datetime import date, datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

import realmail as R
from fakes import FakeGmail, FakeIMAP
from helpers import write_mbox
from test_web import web  # noqa: F401 - the fixture
from clawback import scan as scan_mod
from clawback import tracker
from clawback.demo import DemoSource
from clawback.scan import CLASSIFIER_VERSION, estimate, run_scan, scan_request
from clawback.sources.gmail import READONLY, GmailSource
from clawback.sources.imap import ImapSource
from clawback.sources.mbox import MboxSource

DAYS = list(range(1, 31))          # message i arrived i days ago


def mail(days_ago: float, i: int) -> bytes:
    return R.msg(f"Shop {i % 4} <news@shop{i % 4}.example>", f"Deals {i}", "Deals. Unsubscribe.",
                 days_ago=days_ago, lu=f"<https://shop{i % 4}.example/u>", mid=f"<m{i}@shop.example>")


def subjects(account):
    db = tracker.connect()
    try:
        return {r[0] for r in db.execute("SELECT subject FROM messages WHERE account=?", (account,))}
    finally:
        db.close()


def want(n):
    """Subjects of the n newest emails (email i is DAYS[i] days old)."""
    return {f"Deals {i}" for i in sorted(range(len(DAYS)), key=lambda i: DAYS[i])[:n]}


# ------------------------------------------------------------------ what a choice means
def test_scan_request_maps_each_choice():
    today = date(2026, 10, 28)
    q = scan_request("quick", today=today)
    assert q["incremental"] and q["since"].date() == date(2025, 10, 28) and q["mode"] == "quick"
    f = scan_request("full", today=today)
    assert not f["incremental"] and f["since"].date() == date(2025, 10, 28)
    s = scan_request("since", "2026-01-01", today=today)
    assert s["incremental"] and s["since"].date() == date(2026, 1, 1) and s["last"] is None
    n = scan_request("last", last="500", today=today)
    assert n == {"mode": "last", "since": None, "limit": 500, "incremental": True, "last": 500}
    for bad in (("since", None), ("since", "2099-01-01"), ("since", "1 Jan"), ("last", 0), ("last", "x"),
                ("last", 1_000_001), ("everything", None)):
        with pytest.raises(ValueError):
            scan_request(bad[0], bad[1] if bad[0] == "since" else None, bad[1] if bad[0] != "since" else None,
                         today=today)


# ------------------------------------------------------------------ the newest N, every source
def shuffled_raws():
    raws = [mail(d, i) for i, d in enumerate(DAYS)]
    order = list(range(len(raws)))
    random.Random(7).shuffle(order)
    return raws, order


def test_gmail_api_newest_n_then_more_reuses_what_was_read():
    raws, order = shuffled_raws()
    fake = FakeGmail([raws[i] for i in order])          # stored in no particular order; Gmail lists newest first
    src = GmailSource("g", service=fake, granted={READONLY}, sleep=fake.clock.sleep, clock=fake.clock)
    r = run_scan(src, limit=5, since=None, mode="last", last=5)
    assert subjects("g") == want(5) and r.messages == 5 and r.capped
    r = run_scan(src, limit=5, since=None, mode="last", last=5)      # the same newest 5: nothing new to read
    assert r.messages == 0 and r.reused == 5 and subjects("g") == want(5)
    r = run_scan(src, limit=12, since=None, mode="last", last=12)
    assert r.messages == 7 and r.reused == 5 and subjects("g") == want(12)


def test_imap_newest_n_counts_emails_already_read():
    raws = [mail(d, i) for i, d in enumerate(DAYS)]
    arrival = sorted(range(len(raws)), key=lambda i: -DAYS[i])    # UIDs ascend with arrival
    fake = FakeIMAP([raws[i] for i in arrival], gmail=True)
    src = ImapSource("imap.example", "me@imap.example", "pw", ["INBOX"], account="me@imap.example", conn_factory=fake)
    r = run_scan(src, limit=5, since=None, mode="last", last=5)
    assert subjects("me@imap.example") == want(5) and r.capped
    r = run_scan(src, limit=5, since=None, mode="last", last=5)
    # before: the second "newest 5" read the next 5 older emails
    assert r.messages == 0 and r.reused == 5 and subjects("me@imap.example") == want(5)
    r = run_scan(src, limit=9, since=None, mode="last", last=9)
    assert r.messages == 4 and subjects("me@imap.example") == want(9)


def test_imap_newest_n_across_two_folders_goes_by_date():
    raws = [mail(d, i) for i, d in enumerate(DAYS)]
    inbox = [i for i in range(len(raws)) if i % 3]          # interleaved in time
    archive = [i for i in range(len(raws)) if not i % 3]
    box = lambda ids: [raws[i] for i in sorted(ids, key=lambda i: -DAYS[i])]   # noqa: E731 - arrival order
    fake = FakeIMAP([], boxes={"INBOX": box(inbox), "Archive": box(archive)})
    src = ImapSource("imap.example", "me@imap.example", "pw", ["INBOX", "Archive"], account="a", conn_factory=fake)
    r = run_scan(src, limit=7, since=None, mode="last", last=7)
    assert subjects("a") == want(7) and r.messages == 7 and r.capped


def test_takeout_export_newest_n_is_by_date_not_file_order(tmp_path):
    raws, order = shuffled_raws()
    path = write_mbox(tmp_path / "export.mbox", [raws[i] for i in order])
    r = run_scan(MboxSource(path, account="x"), limit=6, since=None, mode="last", last=6)
    assert subjects("x") == want(6) and r.messages == 6 and r.capped
    r = run_scan(MboxSource(path, account="y"), limit=100, since=None)            # more than there is
    assert r.messages == len(DAYS) and not r.capped


def test_demo_newest_n():
    now = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
    r = run_scan(DemoSource(now=now), limit=10, since=None, mode="last", last=10)
    dates = sorted((m.date for m in DemoSource(now=now).iter_messages()), reverse=True)
    db = tracker.connect()
    try:
        got = sorted((r[0] for r in db.execute("SELECT received FROM messages WHERE account='demo'")), reverse=True)
    finally:
        db.close()
    assert r.messages == 10 and got == [tracker._iso(d) for d in dates[:10]]


def test_outlook_newest_n_counts_emails_already_read():
    from fake_microsoft import FakeMicrosoft, browser
    from clawback import ms_oauth
    from clawback.sources.outlook import OutlookSource
    f = FakeMicrosoft()
    try:
        for i, d in enumerate(DAYS):
            f.add(mail(d, i))

        def src():
            signin = ms_oauth.MicrosoftSignIn("11111111-2222-3333-4444-555555555555", "me@outlook.com",
                                              authority=f.base, open_browser=browser(), timeout=10)
            return OutlookSource("me@outlook.com", signin=signin, graph=f.graph, sleep=lambda s: None)
        run_scan(src(), limit=5, since=None, mode="last", last=5)
        assert subjects("me@outlook.com") == want(5)
        r = run_scan(src(), limit=5, since=None, mode="last", last=5)
        assert r.messages == 0 and r.reused == 5 and subjects("me@outlook.com") == want(5)
    finally:
        f.close()


# ------------------------------------------------------------------ the scan record says what was scanned
def test_scan_record_keeps_the_choice():
    src = DemoSource()
    run_scan(src, limit=10, since=None, mode="last", last=10)
    last = tracker.last_scan("demo")
    assert last["mode"] == "last" and last["last_n"] == 10 and last["read"] == 10 and last["since"] is None
    since = datetime(2026, 9, 1, tzinfo=timezone.utc)
    run_scan(src, since=since, mode="since")
    last = tracker.last_scan("demo")
    assert last["mode"] == "since" and last["since"].startswith("2026-09-01") and last["last_n"] is None


# ------------------------------------------------------------------ time estimates
def fill(account, n, days_back=300, cv=CLASSIFIER_VERSION, since=None, kind="gmail"):
    now = datetime.now(timezone.utc)
    rows = [(f"{account}-{i}", "news@shop.example", tracker._iso(now - timedelta(days=days_back * i / n)), "x", 0)
            for i in range(n)]
    tracker.record_messages(account, rows, cv=cv)
    sid = tracker.begin_scan(account, kind, tracker.now_iso(), since, mode="quick")
    tracker.finish_scan(sid, None, messages=n)


def test_estimate_first_gmail_scan_has_no_count_yet():
    e = estimate("gmail", "new", "quick")
    assert e["first"] and e["seconds"] is None and e["basis"] == "estimate.first" and not e["reread"]


def test_estimate_quick_update_takes_seconds():
    fill("g", 3000, since=scan_mod.parse_since("1y"))
    e = estimate("gmail", "g", "quick")
    assert e["seconds"] == [1.0, 10.0] and e["basis"] == "estimate.onlyNew" and not e["reread"]


def test_estimate_full_gmail_scan_matches_the_measured_10_to_15_minutes():
    fill("g", 3000, since=scan_mod.parse_since("1y"))
    e = estimate("gmail", "g", "full")
    assert e["emails"] == 3000 and not e["at_least"]
    lo, hi = e["seconds"]
    assert 9 * 60 <= lo <= 11 * 60 and 14 * 60 <= hi <= 16 * 60, e["seconds"]


def test_estimate_after_a_classifier_upgrade_says_it_reads_everything_once():
    fill("g", 3000, cv=CLASSIFIER_VERSION - 1, since=scan_mod.parse_since("1y"))
    e = estimate("gmail", "g", "quick")
    assert e["reread"] and e["emails"] == 3000 and e["seconds"][1] >= 14 * 60
    assert scan_mod.upgrade_reread("gmail", "g") and not scan_mod.upgrade_reread("demo", "g")


def test_estimate_since_an_older_date_than_ever_read_is_open_ended():
    fill("g", 300, days_back=60, since=datetime.now(timezone.utc) - timedelta(days=61))
    old = (date.today() - timedelta(days=200)).isoformat()
    e = estimate("gmail", "g", "since", since=old)
    assert e["at_least"] and e["basis"] == "estimate.older"
    recent = (date.today() - timedelta(days=30)).isoformat()
    assert estimate("gmail", "g", "since", since=recent)["basis"] == "estimate.onlyNew"


def test_estimate_newest_n():
    e = estimate("gmail", "new", "last", last=600)
    assert e["emails"] == 600 and e["basis"] == "estimate.upTo" and e["seconds"][0] < 3 * 60
    fill("g", 1000)
    assert estimate("gmail", "g", "last", last=600)["basis"] == "estimate.onlyNew"
    e = estimate("imap", "g", "last", last=1500)
    assert e["emails"] == 500 and e["basis"] == "estimate.upToNew"
    assert estimate("mbox", "g", "full")["seconds"] is None


def test_estimate_and_scan_options_over_the_api(web):
    web.post("/api/connect", {"kind": "demo"})
    st, e, _ = web.get("/api/scan/estimate?mode=last&last=50")
    assert st == 200 and e["kind"] == "demo" and e["seconds"] == [1.0, 10.0]
    assert web.get("/api/scan/estimate?mode=last&last=0")[0] == 400
    st, job, _ = web.post("/api/scan", {"mode": "last", "last": 25})
    assert st == 200 and job["mode"] == "last" and job["last"] == 25
    web.app.job.thread.join(30)
    done = web.get("/api/scan")[1]
    assert done["result"]["messages"] == 25 and done["result"]["mode"] == "last"
    state = web.get("/api/state")[1]
    assert state["last_scan"]["mode"] == "last" and state["last_scan"]["last_n"] == 25
    assert web.post("/api/scan", {"mode": "since", "since": "2099-01-01"})[0] == 400


def test_upgrade_hint_while_running(web, monkeypatch):
    fake = FakeGmail([mail(d, i) for i, d in enumerate(DAYS)])
    src = GmailSource("g", service=fake, granted={READONLY})
    run_scan(src, since=None)
    db = tracker.connect()
    db.execute("UPDATE messages SET cv=? WHERE account='g'", (CLASSIFIER_VERSION - 1,))
    db.commit()
    db.close()
    web.app.add_source(src)
    st, job, _ = web.post("/api/scan", {"mode": "quick"})
    assert job["hint"] == "scan.hint.upgradeApi" and job["full_read"]
    web.app.job.thread.join(30)
    st, job, _ = web.post("/api/scan", {"mode": "quick"})
    assert job["hint"] == "scan.hint.rescan"
    web.app.job.thread.join(30)


# ------------------------------------------------------------------ CLI
def test_cli_last_n():
    from clawback.cli import app
    r = CliRunner().invoke(app, ["scan", "--demo", "--last", "10"])
    assert r.exit_code == 0 and "of your newest 10" in r.output and "NOT read" not in r.output, r.output
    assert tracker.last_scan("demo")["mode"] == "last"
    r = CliRunner().invoke(app, ["scan", "--demo", "--last", "10", "--since", "3m"])
    assert r.exit_code == 2 and "--last" in r.output
    # even the default window spelled out: --last and --since are two answers to one question
    r = CliRunner().invoke(app, ["scan", "--demo", "--last", "10", "--since", "1y"])
    assert r.exit_code == 2 and "--last" in r.output
    r = CliRunner().invoke(app, ["scan", "--demo", "--last", "0"])
    assert r.exit_code == 2


def test_cli_records_what_was_asked_for():
    from clawback.cli import app
    assert CliRunner().invoke(app, ["scan", "--demo"]).exit_code == 0
    assert tracker.last_scan("demo")["mode"] == "quick"
    assert CliRunner().invoke(app, ["scan", "--demo", "--since", "3m"]).exit_code == 0
    assert tracker.last_scan("demo")["mode"] == "since"
    assert CliRunner().invoke(app, ["scan", "--demo", "--full"]).exit_code == 0
    assert tracker.last_scan("demo")["mode"] == "full"
    future = (date.today() + timedelta(days=2)).isoformat()
    r = CliRunner().invoke(app, ["scan", "--demo", "--since", future])
    assert r.exit_code == 2 and "future" in r.output


# ------------------------------------------------------------------ edge cases of a chosen date
def test_a_date_before_email_existed_means_all_mail():
    """Gmail's after: takes seconds since 1970; a date picker allows year 1."""
    assert scan_mod.parse_since("0001-01-01") is None
    assert scan_mod.parse_since("1969-12-31") is None
    assert scan_mod.parse_since("1971-01-01").year == 1971
    req = scan_request("since", "1900-05-01")
    assert req["mode"] == "since" and req["since"] is None and req["incremental"]
    fill("g", 100, days_back=60, since=datetime.now(timezone.utc) - timedelta(days=61))
    e = estimate("gmail", "g", "since", since="1900-05-01")
    assert e["basis"] == "estimate.olderAll" and e["at_least"]


def test_newest_n_larger_than_the_mailbox_reads_it_all_and_is_not_capped():
    everything = run_scan(DemoSource(account="d2"), since=None).messages
    r = run_scan(DemoSource(), limit=1_000_000, since=None, mode="last", last=1_000_000)
    assert r.messages == everything > 0 and not r.capped
    with pytest.raises(ValueError):
        scan_request("last", last=1_000_001)
    with pytest.raises(ValueError):
        scan_request("last", last="ten")


def test_scope_of_a_stopped_scan_is_recorded(web):
    """The results' "what that scan covered" line needs partial/capped."""
    sid = tracker.begin_scan("p", "gmail", tracker.now_iso(), None, mode="last", last_n=50)
    tracker.finish_scan(sid, None, messages=20, partial=True)
    last = tracker.last_scan("p")
    assert last["partial"] and last["mode"] == "last" and last["last_n"] == 50 and last["read"] == 20
