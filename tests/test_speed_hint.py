""""Next scans take seconds": the scan-speed line is shown only where it is
true. Gmail API and IMAP scans are incremental (rescans skip mail already
read); Takeout/mbox imports and the demo read the whole file every time.

Measured (scripts/bench_scan.py, simulated, 200 ms RTT): API first scan of
3,000 messages 13.1 min; API rescan 1.4 s (nothing new) / 2.5 s (60 new);
IMAP rescan 0.9 s."""
import json
import re
from importlib import resources

import pytest
from typer.testing import CliRunner

import realmail as R
from fakes import FakeGmail, FakeIMAP, http_error
from helpers import write_mbox
from clawback import scan as scan_mod
from clawback.demo import mailbox
from clawback.scan import (HINT_FIRST_API, HINT_FIRST_IMAP, HINT_NEXT, HINT_RESCAN, HINT_RESCAN_DONE, HINT_TEXT,
                           speed_hint)
from clawback.sources.gmail import READONLY, GmailSource
from clawback.sources.imap import ImapSource
from clawback.web.server import App

STRINGS = json.loads(resources.files("clawback.web").joinpath("static/i18n/en.json").read_text("utf-8"))
ALL_HINTS = (HINT_FIRST_API, HINT_FIRST_IMAP, HINT_RESCAN, HINT_NEXT, HINT_RESCAN_DONE)


def render(key, result=None):
    """Same substitution as t() in app.js."""
    s = STRINGS[key]
    for k, v in (result or {}).items():
        s = s.replace("{" + k + "}", str(v))
    return s


def run(app, **kw):
    """Start a scan; return (snapshot while running, snapshot when finished)."""
    app.start_scan(**kw)
    running = app.job.snapshot()
    app.job.thread.join(60)
    return running, app.job.snapshot()


def _imap_raws():
    return [R.msg(f"Shop {i} <news@shop{i}.example>", f"Deals {i}", "Deals. Unsubscribe.", days_ago=2 + 9 * i,
                  lu=f"<https://shop{i}.example/u>", one_click=True) for i in range(8)]


def gmail_app(raws=None):
    app = App()
    app.add_source(GmailSource("work", service=FakeGmail(raws or mailbox()), granted={READONLY}))
    return app


# ------------------------------------------------------------------ web states
def test_gmail_api_first_scan_explains_the_wait_then_promises_fast_rescans():
    app = gmail_app()
    running, done = run(app)
    assert running["status"] == "running" and running["hint"] == HINT_FIRST_API
    text = render(running["hint"])
    assert "10 to 15 minutes" in text and "3,000" in text and "usually take seconds" in text
    assert done["status"] == "done" and done["result"]["reused"] == 0
    assert done["hint"] == HINT_NEXT
    assert render(done["hint"]) == "Next scans only read new mail, so they usually take seconds."


def test_gmail_api_rescan_says_only_new_mail_was_read_with_real_counts():
    raws = mailbox()
    app = gmail_app(raws)
    run(app)
    app.add_source(GmailSource("work", service=FakeGmail(raws), granted={READONLY}))
    running, done = run(app)
    assert running["hint"] == HINT_RESCAN            # never the 10-15 minute warning on a rescan
    assert "10 to 15" not in render(running["hint"])
    assert done["hint"] == HINT_RESCAN_DONE
    r = done["result"]
    assert r["reused"] > 0 and r["messages"] == 0
    assert render(done["hint"], r) == f"Only new mail was read: 0 new, {r['reused']} from earlier scans skipped."


def test_full_rescan_reads_everything_again_so_it_gets_the_slow_line():
    raws = mailbox()
    app = gmail_app(raws)
    run(app)
    app.add_source(GmailSource("work", service=FakeGmail(raws), granted={READONLY}))
    running, done = run(app, incremental=False)
    assert running["hint"] == HINT_FIRST_API and done["hint"] == HINT_NEXT


def test_imap_first_scan_gets_the_short_line_without_the_quota_warning():
    app = App()
    app.add_source(ImapSource("imap.gmail.com", "me@gmail.example", "pw", account="me@gmail.example",
                              conn_factory=FakeIMAP(list(reversed(_imap_raws())), gmail=True)))
    running, done = run(app)
    assert running["hint"] == HINT_FIRST_IMAP and "10 to 15" not in render(running["hint"])
    assert done["hint"] == HINT_NEXT


def test_takeout_import_and_demo_never_claim_fast_rescans(tmp_path):
    app = App()
    app.connect({"kind": "mbox", "path": str(write_mbox(tmp_path / "t.mbox", mailbox()))})
    for _ in range(2):                    # a second import reads the whole file again
        running, done = run(app)
        assert running["hint"] is None and done["hint"] is None and done["status"] == "done"
    app.connect({"kind": "demo"})
    running, done = run(app)
    assert running["hint"] is None and done["hint"] is None


def test_capped_scan_gets_no_promise():
    app = gmail_app()
    _, done = run(app, limit=3)
    assert done["result"]["capped"] and done["hint"] is None


def test_failed_scan_clears_the_line():
    app = App()
    fake = FakeGmail(mailbox())
    fake.failures["list"] = [http_error(401)]
    app.add_source(GmailSource("work", service=fake, granted={READONLY}, sleep=lambda s: None))
    running, done = run(app)
    assert running["hint"] == HINT_FIRST_API
    assert done["status"] == "error" and done["hint"] is None


@pytest.mark.parametrize("result", [{"partial": True, "reused": 0}, {"capped": True, "reused": 5}, None])
def test_cancelled_or_capped_or_missing_results_get_no_done_line(result):
    assert speed_hint("gmail", "done", True, result) is None
    assert speed_hint("imap", "done", False, result) is None


@pytest.mark.parametrize("kind", ["mbox", "demo", "offline", ""])
def test_non_incremental_kinds_get_nothing_in_any_state(kind):
    for status in ("running", "done", "error", "idle"):
        assert speed_hint(kind, status, True, {"reused": 0}) is None


# ------------------------------------------------------------------ strings
def test_every_hint_has_text_and_no_absolute_claims():
    for key in ALL_HINTS:
        assert key in STRINGS, key
        for text in (STRINGS[key], HINT_TEXT[key]):
            assert not re.search(r"\b(guarantee\w*|always|never fails|instant\w*)\b", text, re.I), text
    # CLI and web say the same thing
    assert HINT_TEXT[HINT_FIRST_API] == STRINGS[HINT_FIRST_API]
    assert HINT_TEXT[HINT_NEXT] == STRINGS[HINT_NEXT]


def test_app_js_renders_the_server_chosen_line():
    js = resources.files("clawback.web").joinpath("static/app.js").read_text("utf-8")
    assert "job.hint" in js and "scan-hint" in js


# ------------------------------------------------------------------ CLI
def test_cli_gmail_first_scan_then_rescan(monkeypatch):
    import clawback.sources.gmail as gmail_mod
    raws = mailbox()
    fake = FakeGmail(raws)
    fake.email_address = "pat@gmail.example"           # users.getProfile names the new account
    monkeypatch.setattr(gmail_mod, "GmailSource",
                        lambda account, query="", on_auth_url=None, **kw: GmailSource(account, service=fake,
                                                                                       granted={READONLY}, **kw))
    from clawback.cli import app
    runner = CliRunner()
    out = " ".join(runner.invoke(app, ["scan", "--gmail"]).output.split())
    assert "10 to 15 minutes" in out and HINT_TEXT[HINT_NEXT] in out
    out = " ".join(runner.invoke(app, ["scan", "--gmail"]).output.split())
    assert "10 to 15" not in out and HINT_TEXT[HINT_NEXT] not in out
    assert "already read by an earlier scan" in out


def test_cli_demo_scan_has_no_speed_line():
    from clawback.cli import app
    out = " ".join(CliRunner().invoke(app, ["scan", "--demo"]).output.split())
    assert "Read" in out and "usually take seconds" not in out and "10 to 15" not in out


# ------------------------------------------------------------------ README FAQ
def test_readme_faq_explains_the_first_scan_with_consistent_unit_math():
    from pathlib import Path
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text("utf-8")
    faq = readme.split("**Why does the first Gmail scan take 10 to 15 minutes?**", 1)[1].split("\n**Is the money", 1)[0]
    faq = faq.split("**Will this find", 1)[0]
    assert "https://developers.google.com/workspace/gmail/api/reference/quota" in faq
    assert "6,000 units per minute" in faq and "**20 units**" in faq and "costs **60**" in faq
    assert 3000 * 20 == 60_000 and 60_000 / 6000 == 10 and "3,000 × 20 = 60,000 units" in faq and "**10 minutes**" in faq
    assert 3000 * 60 == 180_000 and "180,000 units = 30 minutes" in faq
    assert "app password" in faq and "--gmail-fast" in faq      # the fast alternative
    assert "not measurements on a" in faq                        # simulated figures are labelled as such
    assert not re.search(r"\bguarantee", faq, re.I)
