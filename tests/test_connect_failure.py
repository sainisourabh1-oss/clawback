"""Real-Gmail finding (fix run 1, item 4): a refused sign-in looked like a
finished, empty scan. It replaced the account on screen (which had results)
with an account full of zeros, and the active account was missing from the
accounts list. Now a refused sign-in is reported at connect time, a failed
first scan never replaces the account you were looking at, and an account
without results says "Not scanned yet"."""
import time

import pytest

from fakes import FakeIMAP
from test_web import web  # noqa: F401 - the fixture
from clawback.sources.base import AuthError, Source


def wait_scan(web):
    for _ in range(400):
        st = web.get("/api/scan")[1]
        if st["status"] != "running":
            return st
        time.sleep(0.02)
    raise AssertionError("scan did not finish")


@pytest.fixture
def refusing_imap(monkeypatch):
    fake = FakeIMAP([], gmail=True, password="right-password")
    monkeypatch.setattr("clawback.sources.imap.open_imap", lambda host, *a, **kw: fake)
    return fake


def test_wrong_app_password_is_refused_at_connect_and_keeps_the_account_on_screen(web, refusing_imap):
    web.post("/api/connect", {"kind": "demo"})
    web.post("/api/scan", {"limit": 100000})
    assert wait_scan(web)["status"] == "done"
    st, err, _ = web.post("/api/connect", {"kind": "gmail_imap", "user": "me@gmail.example", "password": "wrong pass word"})
    assert st == 400 and "Server said: [AUTHENTICATIONFAILED]" in err["error"]
    assert refusing_imap.logins == ["me@gmail.example"]           # signed in once, read nothing
    assert refusing_imap.commands == []
    state = web.get("/api/state")[1]
    assert state["active"] == "demo" and list(state["sources"]) == ["demo"]
    assert state["active"] in state["accounts"]


def test_right_app_password_connects(web, refusing_imap):
    st, body, _ = web.post("/api/connect", {"kind": "gmail_imap", "user": "me@gmail.example", "password": "right-password"})
    assert st == 200 and body["account"] == "me@gmail.example"
    state = web.get("/api/state")[1]
    # connected but never scanned: listed, active, and not counted as scanned
    assert state["active"] == "me@gmail.example" and "me@gmail.example" in state["accounts"]
    assert state["scanned"] == [] and state["last_scan"] is None


class Refused(Source):
    kind = "gmail"

    def iter_messages(self, *a, **kw):
        raise AuthError("Gmail rejected the saved sign-in (expired or revoked). Reconnect Gmail to sign in again.")
        yield  # pragma: no cover


def test_failed_first_scan_goes_back_to_the_account_with_results(web):
    from clawback.web import server
    web.post("/api/connect", {"kind": "demo"})
    web.post("/api/scan", {"limit": 100000})
    wait_scan(web)
    app = web.app
    app.add_source(Refused("new@gmail.example"))
    assert web.get("/api/state")[1]["active"] == "new@gmail.example"
    web.post("/api/scan", {"limit": 100})
    job = wait_scan(web)
    assert job["status"] == "error" and job["account"] == "new@gmail.example"
    state = web.get("/api/state")[1]
    assert state["active"] == "demo" and "new@gmail.example" not in state["sources"]
    assert "new@gmail.example" not in state["accounts"]
    assert server.tracker.last_scan("demo") is not None


def test_failed_first_scan_with_no_other_account_leaves_nothing_active(web):
    web.app.add_source(Refused("new@gmail.example"))
    web.post("/api/scan", {"limit": 100})
    wait_scan(web)
    state = web.get("/api/state")[1]
    assert state["active"] is None and state["sources"] == {}
