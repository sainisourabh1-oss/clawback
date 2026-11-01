"""Real-Gmail finding (fix run 1, item 5): after a restart the app did not
offer the accounts it already had (a saved Google sign-in and saved
results), and the Gmail label field's default "default" silently started a
new, empty account. Saved accounts are now listed and open in one click."""
import time

import pytest

from test_web import web  # noqa: F401 - the fixture
from clawback import config, ms_oauth, saved, tracker
from clawback.senders import SenderSummary


def make_token(name: str) -> None:
    folder = config.home() / "tokens"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text("{}", encoding="utf-8")   # contents are never read


def save_results(account: str, kind: str | None = None) -> None:
    tracker.upsert_senders(account, [SenderSummary(address="news@shop.example", domain="shop.example",
                                                   message_count=3, category="newsletter", bulk=True)])
    sid = tracker.begin_scan(account, kind or "imap", tracker.now_iso(), None)
    tracker.finish_scan(sid, None, messages=3)
    if kind:
        tracker.record_identity(account, kind, account if "@" in account else None)


def test_saved_accounts_come_from_tokens_and_saved_results():
    make_token("personal")
    make_token("work.imap")
    make_token("outlook-me@outlook.example")
    save_results("me@yahoo.example", "imap")
    save_results("personal", "gmail")
    rows = {r["account"]: r for r in saved.saved_accounts()}
    assert set(rows) == {"personal", "work", "me@outlook.example", "me@yahoo.example"}
    assert rows["personal"]["reconnect"] == {"kind": "gmail", "account": "personal"}
    assert rows["personal"]["last_scan"]["senders"] == 1
    assert rows["work"]["reconnect"]["xoauth2"] is True and rows["work"]["kind"] == "gmail_imap"
    assert rows["me@outlook.example"]["reconnect"] is None          # no Microsoft app registration saved yet
    ms_oauth.save_client("11111111-2222-3333-4444-555555555555", "common")
    rows = {r["account"]: r for r in saved.saved_accounts()}
    assert rows["me@outlook.example"]["reconnect"] == {"kind": "outlook", "user": "me@outlook.example"}
    # an app-password account: its password is never saved, so results only
    assert rows["me@yahoo.example"]["reconnect"] is None and rows["me@yahoo.example"]["kind"] == "imap"


def test_state_lists_saved_accounts(web):
    make_token("personal")
    (config.home() / "token.json").write_text("{}", encoding="utf-8")   # v1 "default" sign-in
    state = web.get("/api/state")[1]
    assert {r["account"] for r in state["saved"]} == {"personal", "default"}


def test_open_results_only_account_shows_its_results_without_a_connection(web):
    save_results("me@yahoo.example", "imap")
    st, r, _ = web.post("/api/open", {"account": "me@yahoo.example"})
    assert st == 200 and r["saved_only"] is True
    state = web.get("/api/state")[1]
    assert state["active"] == "me@yahoo.example" and state["sources"]["me@yahoo.example"]["kind"] == "saved"
    assert [s["address"] for s in web.get("/api/senders?category=newsletter")[1]["senders"]] == ["news@shop.example"]
    job = web.scan()                                   # cannot scan without the password: says so, keeps the results
    assert job["status"] == "error" and "connect it again" in job["error"]
    assert web.get("/api/state")[1]["active"] == "me@yahoo.example"


def test_open_gmail_reconnects_with_the_saved_sign_in(web, monkeypatch):
    make_token("personal")
    save_results("personal", "gmail")
    calls = []
    monkeypatch.setattr("clawback.sources.gmail.GmailSource.service", lambda self: calls.append(self.account) or object())
    st, r, _ = web.post("/api/open", {"account": "personal"})
    assert st == 200 and r["pending"] is True
    for _ in range(100):
        if web.get("/api/auth")[1]["status"] != "waiting":
            break
        time.sleep(0.02)
    assert web.get("/api/auth")[1]["status"] == "done" and set(calls) == {"personal"}   # sign-in (+ its address)
    state = web.get("/api/state")[1]
    assert state["active"] == "personal" and state["sources"]["personal"]["kind"] == "gmail"
    # opening it again just switches to it
    assert web.post("/api/open", {"account": "personal"})[1] == {"account": "personal", "active": "personal"}


def test_unknown_saved_account_is_404(web):
    assert web.post("/api/open", {"account": "nobody"})[0] == 404
