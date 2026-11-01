"""Progressive results: while a scan runs the user sees what was found so
far, newest mail first, labelled "partial: scanned back to <date>"."""
from datetime import timedelta

import pytest

import realmail as R
from fakes import FakeIMAP
from clawback import scan as scan_mod
from clawback.scan import run_scan
from clawback.sources.imap import ImapSource


@pytest.fixture(autouse=True)
def every_message(monkeypatch):
    monkeypatch.setattr(scan_mod, "PREVIEW_SECONDS", 0.0)


def _raws():
    news = [R.msg(f"Shop {i} <news@shop{i}.example>", f"Deals {i}", "Deals. Unsubscribe.", days_ago=2 + 9 * i,
                  lu=f"<https://shop{i}.example/u>", one_click=True) for i in range(10)]
    return list(reversed(news + R.google_play()))        # oldest first = ascending UIDs


def _src(fake):
    return ImapSource("imap.gmail.com", "me@gmail.example", "pw", account="me@gmail.example", conn_factory=fake)


def test_previews_grow_newest_first_and_say_how_far_back():
    seen = []
    res = run_scan(_src(FakeIMAP(_raws(), gmail=True)), region="IN", on_preview=seen.append)
    assert len(seen) == res.messages
    counts = [p["messages"] for p in seen]
    assert counts == sorted(counts) and counts[-1] == res.messages
    backs = [p["scanned_back_to"] for p in seen]
    assert backs == sorted(backs, reverse=True)              # each preview reaches further back
    assert all(p["partial"] for p in seen)
    last = seen[-1]
    # the paid subscription appears in the partial view before the scan ends
    assert any(s["display"] == "₹169" for s in last["subscriptions"])
    stamps = [s["last_seen"] for s in last["senders"]]
    assert stamps == sorted(stamps, reverse=True)            # newest senders first


def test_rescan_preview_keeps_what_earlier_scans_found():
    fake = FakeIMAP(_raws(), gmail=True)
    run_scan(_src(fake), region="IN")
    fake.messages.append(R.msg("New <hi@new.example>", "Hello", "Hi. Unsubscribe.", days_ago=0.5,
                               lu="<https://new.example/u>"))
    fake.dates.append(R.NOW - timedelta(days=0.5))
    seen = []
    run_scan(_src(fake), region="IN", on_preview=seen.append)
    assert len(seen) == 1 and seen[0]["messages"] == 1
    assert any(s["display"] == "₹169" for s in seen[0]["subscriptions"])   # from stored detections


def test_a_failing_preview_never_breaks_the_scan():
    def boom(_):
        raise RuntimeError("UI went away")
    res = run_scan(_src(FakeIMAP(_raws(), gmail=True)), region="IN", on_preview=boom)
    assert res.messages == 14 and res.subscriptions


def test_web_job_publishes_preview_and_progress_line():
    from clawback.web.server import App
    app = App()
    app.connect({"kind": "demo"})
    app.start_scan()
    app.job.thread.join(30)
    snap = app.job.snapshot()
    assert snap["status"] == "done" and snap["preview_seq"] > 0
    assert app.job.preview["partial"] and app.job.preview["scanned_back_to"]
    assert snap["progress"]["line"].startswith(f"{snap['result']['messages']:,} of")
