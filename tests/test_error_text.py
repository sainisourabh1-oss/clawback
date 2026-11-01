"""Real-Gmail finding (fix run 1, item 6): errors showed raw server text
("Server said: [AUTHENTICATIONFAILED] Invalid credentials (Failure)") and
Python exception names. Every error now goes out as a plain reason with the
fix first and the raw text apart, as technical details."""
import time

import pytest

from fakes import FakeIMAP
from test_web import web  # noqa: F401 - the fixture
from clawback import errors
from clawback.sources.base import Source


def test_server_text_is_kept_apart_from_the_reason():
    e = errors.explain("Gmail refused the sign-in. Use a 16-letter app password from "
                       "https://myaccount.google.com/apppasswords (your normal password does not work). "
                       "Server said: [AUTHENTICATIONFAILED] Invalid credentials (Failure)")
    assert e["reason"].startswith("Gmail refused the sign-in.") and "Server said" not in e["reason"]
    assert "AUTHENTICATIONFAILED" not in e["reason"]
    assert e["technical"] == "Server said: [AUTHENTICATIONFAILED] Invalid credentials (Failure)"


def test_a_bare_exception_gets_a_plain_reason():
    e = errors.explain("KeyError: 'token'")
    assert e == {"reason": errors.UNEXPECTED, "technical": "KeyError: 'token'"}
    e = errors.explain("Could not connect to imap.mail.example:993. Check the server name, the port and your internet "
                       "connection, then try again. Technical details: ConnectionResetError: [Errno 104] reset")
    assert "Errno" not in e["reason"] and e["technical"] == "ConnectionResetError: [Errno 104] reset"
    # an older stored message with the exception in brackets
    e = errors.explain("Gmail did not answer (TimeoutError: timed out). Try again later.")
    assert e == {"reason": "Gmail did not answer. Try again later.", "technical": "TimeoutError: timed out"}


def test_plain_sentences_stay_whole():
    assert errors.explain("Enter your email address") == {"reason": "Enter your email address", "technical": None}
    assert errors.explain("") is None


def test_annotate_reaches_nested_errors_and_failed_results():
    out = errors.annotate({"scan": {"error": "ValueError: x"}, "results": [{"ok": False, "detail": "OSError: y"},
                                                                         {"ok": True, "detail": "Unsubscribed"}]})
    assert out["scan"]["error_info"]["technical"] == "ValueError: x"
    assert out["results"][0]["detail_info"]["technical"] == "OSError: y"
    assert "detail_info" not in out["results"][1]


def test_refused_app_password_reason_has_the_link_and_folds_the_server_text(web, monkeypatch):
    fake = FakeIMAP([], gmail=True, password="right-password")
    monkeypatch.setattr("clawback.sources.imap.open_imap", lambda host, *a, **kw: fake)
    st, err, _ = web.post("/api/connect", {"kind": "gmail_imap", "user": "me@gmail.example", "password": "wrong pass word"})
    assert st == 400
    info = err["error_info"]
    assert "https://myaccount.google.com/apppasswords" in info["reason"]
    assert "Server said" not in info["reason"] and info["technical"].startswith("Server said: [AUTHENTICATIONFAILED]")


class Broken(Source):
    kind = "imap"

    def iter_messages(self, *a, **kw):
        raise KeyError("token")
        yield  # pragma: no cover


def test_unexpected_scan_failure_reads_as_a_sentence(web):
    web.app.add_source(Broken("me@mail.example"))
    web.post("/api/scan", {"limit": 10})
    for _ in range(200):
        job = web.get("/api/scan")[1]
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "error" and job["error_info"] == {"reason": errors.UNEXPECTED, "technical": "KeyError: 'token'"}
    assert web.get("/api/state")[1]["dropped"]["error_info"]["technical"] == "KeyError: 'token'"
