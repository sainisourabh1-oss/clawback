"""CLI: same two steps as the web app: the printed plan, then one yes/no
question that names the action and the count and defaults to No. Nothing is
typed but y; --yes never skips the question."""
import pytest
from typer.testing import CliRunner

from clawback import oneclick, tracker
from clawback.cli import app

runner = CliRunner()
NEWS = "deals@shopmart.example"
YES = "y\n"


@pytest.fixture
def posts(monkeypatch):
    sent = []

    def fake_post(url, **kw):
        sent.append(url)
        return oneclick.PostResult(True, 200, "HTTP 200", url)
    monkeypatch.setattr(oneclick, "post", fake_post)
    return sent


@pytest.fixture
def scanned():
    r = runner.invoke(app, ["scan", "--demo"])
    assert r.exit_code == 0, r.output
    assert "Read" in r.output


def test_yes_does_not_skip_the_final_question(scanned, posts):
    # Enter, no, end of input, and an old typed phrase all mean No
    for answer in ("\n", "n\n", "", "UNSUBSCRIBE 1\n"):
        r = runner.invoke(app, ["unsubscribe", NEWS, "--yes"], input=answer)
        assert r.exit_code == 1 and "Not confirmed. Nothing was done." in r.output, (answer, r.output)
        assert "Unsubscribe from 1 sender? This cannot be undone from Clawback [y/N]" in r.output
    assert posts == [] and tracker.audit_entries() == []
    r = runner.invoke(app, ["unsubscribe", NEWS, "--yes"], input=YES)
    assert r.exit_code == 0, r.output
    assert "1 done" in r.output and "simulated" in r.output        # the demo never sends real HTTP
    assert [e["target"] for e in tracker.audit_entries()] == [NEWS]


def test_the_question_defaults_to_no_and_names_the_count(scanned, posts):
    r = runner.invoke(app, ["unsubscribe", NEWS], input="\n")
    assert "Not confirmed. Nothing was done." in r.output and posts == []
    assert tracker.audit_entries() == []
    r = runner.invoke(app, ["unsubscribe", NEWS], input=YES)
    assert r.exit_code == 0 and len(tracker.audit_entries()) == 1


def test_plan_is_printed_before_anything_happens(scanned, posts):
    r = runner.invoke(app, ["trash", NEWS, "team@moonshot.example"], input="n\n")
    assert "Move 90 email(s) from deals@shopmart.example to Trash" in r.output
    assert "emails from 2 senders to Trash? You can restore them for 30 days [y/N]" in r.output
    assert "Nothing was done" in r.output and tracker.audit_entries() == []


def test_delete_and_unsubscribe_delete_commands(scanned, posts):
    r = runner.invoke(app, ["delete", NEWS], input=YES)
    assert r.exit_code == 0 and "moved 90 email(s) to Trash" in r.output, r.output
    r = runner.invoke(app, ["unsubscribe-delete", "news@weeklybyte.example"], input="n\n")
    assert "Unsubscribe from 1 sender and move their" in r.output and "Nothing was done" in r.output
    r = runner.invoke(app, ["unsubscribe-delete", "news@weeklybyte.example"], input=YES)
    assert r.exit_code == 0 and "to Trash" in r.output, r.output
    assert {e["action"] for e in tracker.audit_entries()} == {"trash", "unsubscribe_trash"}
    assert tracker.get_sender("demo", "news@weeklybyte.example")["status"] == "unsubscribed"


def test_dry_run_and_spam_blocking(scanned, posts):
    r = runner.invoke(app, ["unsubscribe", "team@moonshot.example", "--yes"], input=YES)
    assert "WILL NOT RUN" in r.output and "Nothing in this selection can run" in r.output and posts == []
    r = runner.invoke(app, ["unsubscribe", NEWS, "--dry-run"], input=YES)
    assert r.exit_code == 0 and "DRY RUN" in r.output and posts == []


def test_savings_proof_log_and_undo_commands(scanned, posts):
    runner.invoke(app, ["archive", NEWS], input=YES)
    r = runner.invoke(app, ["log"])
    assert "archive" in r.output and "clawback undo" in r.output
    entry = tracker.audit_entries()[0]["id"]
    r = runner.invoke(app, ["undo", str(entry)], input=YES)
    assert r.exit_code == 0 and "1 done" in r.output
    sub = next(s["id"] for s in tracker.list_subscriptions() if s["vendor"].startswith("Tunewave"))
    runner.invoke(app, ["review", str(sub), "cancelled"])
    r = runner.invoke(app, ["savings"])
    assert "€131.88/year" in r.output
    runner.invoke(app, ["unsubscribe", NEWS], input=YES)
    r = runner.invoke(app, ["proof"])
    assert NEWS in r.output


def test_gmail_auth_error_gives_gmail_advice(monkeypatch):
    from clawback.sources import gmail
    from clawback.sources.base import AuthError

    def boom(self, *a, **k):
        raise AuthError("Gmail rejected the saved sign-in (expired or revoked). Reconnect Gmail to sign in again.")
    monkeypatch.setattr(gmail.GmailSource, "iter_messages", boom)
    monkeypatch.setattr(gmail.GmailSource, "service", lambda self: object())          # signed in
    monkeypatch.setattr(gmail.GmailSource, "mailbox_address", lambda self: "pat@gmail.example")
    r = runner.invoke(app, ["scan", "--gmail"])
    assert r.exit_code == 2 and "Reconnect Gmail" in r.output and "app password" not in r.output


def test_import_scan_from_cli(tmp_path):
    from helpers import write_mbox
    from clawback.demo import mailbox
    p = write_mbox(tmp_path / "x.mbox", mailbox())
    r = runner.invoke(app, ["scan", "--import", str(p)])
    assert r.exit_code == 0 and "Streamora" in r.output
