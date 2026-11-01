"""Operator's real-Gmail test (round 3, item A): accounts were shown by
internal labels ("personal", "default"), and signing in with Google again
from the connect screen made a NEW account "default" for a mailbox that was
already saved as "personal", which then rescanned from scratch.

Now every account is shown by its email address where known (else "Demo
mailbox" or the imported file's name), and a Google sign-in for an address
this computer already knows reopens that account: its data, its sign-in
file, its history. Old labels and token files keep working untouched.
All addresses here are invented."""
import json
import time

import pytest
from typer.testing import CliRunner

from test_web import web  # noqa: F401 - the fixture
from clawback import config, saved, tracker
from clawback.senders import SenderSummary

ADDR = "pat.example@gmail.example"
OTHER = "sam.example@gmail.example"


def save_results(account: str, kind: str, address: str | None, messages: int = 3) -> None:
    tracker.upsert_senders(account, [SenderSummary(address="news@shop.example", domain="shop.example",
                                                   message_count=messages, category="newsletter", bulk=True)])
    tracker.record_messages(account, [(f"{account}-{i}", "news@shop.example", "2026-10-01T10:00:00+00:00", "Hi", 0)
                                      for i in range(messages)], cv=1)
    sid = tracker.begin_scan(account, kind, tracker.now_iso(), None)
    tracker.finish_scan(sid, None, messages=messages)
    tracker.record_identity(account, kind, address)


def token(label: str, body: str = '{"old": true}'):
    path = config.token_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def google(monkeypatch):
    """Google sign-in stand-in: service() writes the token a real consent
    would write (only when there is none) and getProfile answers ``who``."""
    who = {"address": ADDR, "consents": 0, "fail_profile": False}

    def service(self):
        if not self.token_file.exists():
            who["consents"] += 1
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(json.dumps({"new": who["consents"], "scopes": ["gmail.readonly"]}), encoding="utf-8")
        return object()

    def mailbox_address(self):
        if who["fail_profile"]:
            raise OSError("network down")
        return who["address"]
    monkeypatch.setattr("clawback.sources.gmail.GmailSource.service", service)
    monkeypatch.setattr("clawback.sources.gmail.GmailSource.mailbox_address", mailbox_address)
    return who


def wait_auth(web):
    for _ in range(200):
        a = web.get("/api/auth")[1]
        if a["status"] != "waiting":
            return a
        time.sleep(0.02)
    raise AssertionError("sign-in did not finish")


def pending_tokens():
    folder = config.home() / "tokens"
    return sorted(p.name for p in folder.glob(saved.PENDING_PREFIX + "*")) if folder.is_dir() else []


# ------------------------------------------------------------------ names
def test_names_use_the_email_address_and_sensible_fallbacks():
    save_results("personal", "gmail", ADDR)
    save_results("me@imap.example", "imap", "me@imap.example")
    save_results("demo", "demo", None)
    save_results("demo-work", "demo", None)
    save_results("import:1761600000-takeout-2026.zip", "mbox", None)
    save_results("import:old-export.mbox", "mbox", None)
    save_results("work", "gmail", None)            # never told us its address
    names = saved.display_names(tracker.scanned_accounts())
    assert names == {"personal": ADDR, "me@imap.example": "me@imap.example", "demo": "Demo mailbox",
                     "demo-work": "Demo mailbox (demo-work)", "import:1761600000-takeout-2026.zip": "takeout-2026.zip",
                     "import:old-export.mbox": "old-export.mbox", "work": "work"}


def test_two_saved_copies_of_one_mailbox_stay_apart_and_the_real_history_is_the_main_one():
    save_results("personal", "gmail", ADDR, messages=40)     # the full history
    save_results("default", "gmail", ADDR, messages=5)       # the accidental second sign-in
    names = saved.display_names(["personal", "default"])
    assert names == {"personal": ADDR, "default": f"{ADDR} (default)"}
    assert saved.label_for_address(ADDR.upper()) == "personal"


def test_state_shows_names_and_marks_the_copy(web):
    token("personal")
    token("default")
    save_results("personal", "gmail", ADDR, messages=40)
    save_results("default", "gmail", ADDR, messages=5)
    state = web.get("/api/state")[1]
    assert state["names"]["personal"] == ADDR and state["names"]["default"] == f"{ADDR} (default)"
    rows = {r["account"]: r for r in state["saved"]}
    assert rows["personal"]["name"] == ADDR and rows["personal"]["copy_of"] is None
    assert rows["default"]["copy_of"] == ADDR
    assert "label_suggestion" not in state


def test_a_copy_is_listed_right_under_its_main_account(web):
    token("personal")
    token("default")
    save_results("default", "gmail", ADDR, messages=5)
    save_results("me@imap.example", "imap", "me@imap.example")
    save_results("personal", "gmail", ADDR, messages=40)      # scanned last of the three
    order = [r["account"] for r in web.get("/api/state")[1]["saved"]]
    assert order.index("default") == order.index("personal") + 1, order


# ------------------------------------------------------------------ one account per address
def test_signing_in_again_with_a_saved_address_reopens_its_account(web, google):
    old = token("personal")
    save_results("personal", "gmail", ADDR, messages=40)
    st, r, _ = web.post("/api/connect", {"kind": "gmail"})
    assert st == 200 and r["pending"] is True
    a = wait_auth(web)
    assert a["status"] == "done" and a["account"] == "personal", a
    state = web.get("/api/state")[1]
    assert state["active"] == "personal"
    assert set(state["sources"]) == {"personal"}                     # no second account
    assert {r["account"] for r in state["saved"]} == {"personal"}
    assert json.loads(old.read_text(encoding="utf-8"))["new"] == 1    # the fresh sign-in now lives there
    assert pending_tokens() == [] and not config.token_path("default").exists()
    # its saved history is what the results show: nothing starts from scratch
    assert state["last_scan"]["messages"] == 40
    assert tracker.has_known_messages("personal", 1)
    assert state["names"]["personal"] == ADDR


def test_signing_in_with_a_new_address_names_the_account_by_it(web, google):
    save_results("personal", "gmail", ADDR)
    google["address"] = OTHER.upper()
    web.post("/api/connect", {"kind": "gmail"})
    a = wait_auth(web)
    assert a["status"] == "done" and a["account"] == OTHER
    state = web.get("/api/state")[1]
    assert state["active"] == OTHER and state["names"][OTHER] == OTHER
    assert config.token_path(OTHER).name == f"{OTHER}.json" and config.token_path(OTHER).exists()
    assert tracker.identities()[OTHER]["address"] == OTHER
    assert pending_tokens() == []
    rows = {r["account"]: r for r in state["saved"]}
    assert rows[OTHER]["reconnect"] == {"kind": "gmail", "account": OTHER}


def test_legacy_default_sign_in_is_reused_in_place(web, google):
    legacy = token("default")
    assert legacy.name == "token.json"                                 # the v1 location
    save_results("default", "gmail", ADDR)
    web.post("/api/connect", {"kind": "gmail"})
    assert wait_auth(web)["account"] == "default"
    assert json.loads(legacy.read_text(encoding="utf-8"))["new"] == 1
    assert not (config.home() / "tokens" / f"{ADDR}.json").exists()


def test_google_not_saying_the_address_saves_nothing(web, google):
    google["fail_profile"] = True
    web.post("/api/connect", {"kind": "gmail"})
    a = wait_auth(web)
    assert a["status"] == "error" and "did not say which Gmail address" in a["error"] and "network down" in a["error"]
    state = web.get("/api/state")[1]
    assert state["sources"] == {} and state["saved"] == [] and pending_tokens() == []


def test_reopening_a_saved_label_learns_its_address_for_the_name(web, google):
    token("personal")
    save_results("personal", "gmail", None)
    assert web.get("/api/state")[1]["names"]["personal"] == "personal"
    web.post("/api/open", {"account": "personal"})
    assert wait_auth(web)["status"] == "done"
    assert google["consents"] == 0                                    # its own sign-in, no new consent
    assert web.get("/api/state")[1]["names"]["personal"] == ADDR


def test_an_address_connection_reuses_the_existing_label_whatever_the_case():
    save_results("personal", "gmail", ADDR)
    save_results("me@imap.example", "imap", "me@imap.example")
    assert saved.account_label(ADDR.upper()) == "personal"
    assert saved.account_label("Me@IMAP.example") == "me@imap.example"
    assert saved.account_label("New.Person@Mail.example") == "new.person@mail.example"


def test_a_sign_in_in_progress_is_never_listed():
    folder = config.home() / "tokens"
    folder.mkdir(parents=True)
    (folder / f"{saved.PENDING_PREFIX}abc.json").write_text("{}", encoding="utf-8")
    assert saved.saved_accounts() == []


# ------------------------------------------------------------------ CLI
def test_cli_gmail_reuses_the_one_saved_sign_in_and_names_new_ones_by_address(google):
    from clawback import cli
    token("personal")
    save_results("personal", "gmail", ADDR)
    src = cli._gmail_source(None, "")
    assert src.account == "personal" and google["consents"] == 0
    src = cli._gmail_source(ADDR, "")                                  # by address
    assert src.account == "personal" and google["consents"] == 0
    google["address"] = OTHER
    src = cli._gmail_source("new", "")
    assert src.account == OTHER and google["consents"] == 1 and config.token_path(OTHER).exists()


def test_cli_with_two_saved_gmail_accounts_asks_which(google):
    from clawback.cli import app
    token("personal")
    token(OTHER)
    save_results("personal", "gmail", ADDR)
    save_results(OTHER, "gmail", OTHER)
    r = CliRunner().invoke(app, ["scan", "--gmail"])
    assert r.exit_code == 2 and "--account " + ADDR in r.output and "--account " + OTHER in r.output
    assert google["consents"] == 0


def test_cli_accepts_the_address_for_account_options(monkeypatch):
    from clawback.cli import app
    save_results("personal", "gmail", ADDR)
    asked = []
    real = tracker.list_senders
    monkeypatch.setattr(tracker, "list_senders", lambda account, *a, **k: asked.append(account) or real(account, *a, **k))
    r = CliRunner().invoke(app, ["senders", "--account", ADDR])
    assert r.exit_code == 0 and asked == ["personal"]
