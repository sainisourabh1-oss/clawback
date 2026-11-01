"""A scan never ends without saying why.

Real case (2026-10-25): a 25-minute Gmail scan ended with exit code 1 and no
message at all, and left no record. The process had been ended from outside
(nothing inside it could report anything). Now:

* every scan is recorded when it starts and completed when it ends, with the
  error wherever it was raised (reading, or the work after reading);
* a scan whose process vanished is recognised by its silent heartbeat and the
  next CLI run and the web app say what happened;
* any unexpected error in `clawback scan` prints a plain reason, not a bare
  exit code.
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from clawback import cli, config, scan, tracker
from clawback.demo import DemoSource
from clawback.scan import run_scan
from test_web import web  # noqa: F401  (the real-HTTP web fixture)


class FailingSource(DemoSource):
    def iter_messages(self, limit=None, progress=None, cancel=None, since=None, skip_ids=None):
        for i, info in enumerate(super().iter_messages(limit, progress, cancel, since=since, skip_ids=skip_ids)):
            if i == 25:
                raise RuntimeError("connection reset by the server")
            yield info


def _scans():
    db = tracker.connect()
    try:
        return [dict(r) for r in db.execute("SELECT * FROM scans ORDER BY id")]
    finally:
        db.close()


def _age_heartbeats(seconds=600):
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    db = tracker.connect()
    db.execute("UPDATE scans SET heartbeat=?, started_at=? WHERE finished_at IS NULL", (old, old))
    db.commit()
    db.close()


def test_error_while_reading_is_recorded_and_shown_as_the_last_failure():
    with pytest.raises(RuntimeError):
        run_scan(FailingSource("acct"), region="US")
    (row,) = _scans()
    assert row["finished_at"] and row["error"] == "RuntimeError: connection reset by the server"
    assert tracker.last_failure("acct")["error"] == "RuntimeError: connection reset by the server"


def test_error_after_reading_is_recorded_too(monkeypatch):
    # Before, only errors inside the reading loop were logged: a failure while
    # combining results left no record at all.
    def boom(*a, **k):
        raise ValueError("could not combine results")
    monkeypatch.setattr(scan, "aggregate", boom)
    with pytest.raises(ValueError):
        run_scan(DemoSource("acct"), region="US")
    (row,) = _scans()
    assert row["error"] == "ValueError: could not combine results" and row["finished_at"]


def test_a_successful_scan_clears_the_failure():
    with pytest.raises(RuntimeError):
        run_scan(FailingSource("acct"), region="US")
    run_scan(DemoSource("acct"), region="US")
    assert tracker.last_failure("acct") is None
    assert [r["error"] for r in _scans()] == ["RuntimeError: connection reset by the server", None]


def test_a_killed_scan_is_reported_by_the_next_run(tmp_path):
    # A real separate process starts a scan record and is then ended abruptly
    # (os._exit: no exception handler, no cleanup), like `taskkill /F`.
    env = dict(os.environ, CLAWBACK_HOME=str(config.home()))
    code = ("from clawback import tracker; import os; "
            "tracker.begin_scan('acct', 'gmail', tracker.now_iso(), None); os._exit(1)")
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 1
    assert tracker.close_abandoned() == []          # heartbeat still fresh: could be another window scanning
    _age_heartbeats()
    out = CliRunner().invoke(cli.app, ["scan", "--demo"])
    assert out.exit_code == 0, out.output
    flat = " ".join(out.output.split())          # line wrapping depends on the terminal width
    assert "The previous scan of acct" in flat and "did not finish" in flat
    assert "Clawback was closed or stopped" in flat
    killed = [r for r in _scans() if r["account"] == "acct"]
    assert killed[0]["error"] == tracker.ABANDONED_TEXT and killed[0]["finished_at"]
    assert tracker.last_failure("acct")["error"] == tracker.ABANDONED_TEXT


def test_cli_prints_a_reason_for_any_unexpected_error(monkeypatch):
    def boom(*a, **k):
        raise KeyError("token")
    monkeypatch.setattr("clawback.scan.run_scan", boom)
    out = CliRunner().invoke(cli.app, ["scan", "--demo"])
    text = " ".join(out.output.split())
    assert out.exit_code == 2
    assert "Scan failed: KeyError: 'token'" in text
    assert "Nothing in your mailbox was changed" in text


def test_web_state_carries_the_reason_of_a_scan_killed_in_an_earlier_session(web):
    web.app.add_source(DemoSource(account="demo"))
    tracker.begin_scan("demo", "demo", tracker.now_iso(), None)   # an earlier session, then killed
    _age_heartbeats()
    tracker.close_abandoned()                                     # what make_server does at start-up
    state = web.get("/api/state")[1]
    assert state["active"] == "demo" and state["scan"]["status"] == "idle"
    assert state["last_failure"]["error"] == tracker.ABANDONED_TEXT
    web.scan()
    assert web.get("/api/state")[1]["last_failure"] is None
