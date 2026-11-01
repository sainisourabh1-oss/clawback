"""Regression: Google's reply also lists scopes granted earlier (include_granted_scopes).
oauthlib used to reject it ("Scope has changed"), so the approval was never saved and the
user saw Google's consent window again and again."""
import os

from oauthlib.oauth2.rfc6749.parameters import validate_token_parameters
from oauthlib.oauth2.rfc6749.tokens import OAuth2Token

from clawback.sources import gmail


class _Session:
    def __init__(self, scope):
        self.token = {"scope": scope}


class _Creds:
    def __init__(self):
        self.scopes = None
        self._scopes = None


class _Flow:
    reply = ""

    def __init__(self):
        self.oauth2session = _Session(_Flow.reply)

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        return cls()

    def authorization_url(self, **kw):
        return "https://accounts.google.example/x", "s"

    def run_local_server(self, **kw):
        # what oauthlib does with Google's reply: must not raise once relaxed
        tok = OAuth2Token({"access_token": "a", "token_type": "Bearer", "scope": _Flow.reply},
                          old_scope=sorted(gmail.SIGN_IN_SCOPES))
        validate_token_parameters(tok)
        return _Creds()


def _src(tmp_path):
    s = gmail.GmailSource.__new__(gmail.GmailSource)
    s.client_secret = tmp_path / "c.json"
    s.on_auth_url = None
    s.last_auth_url = None
    return s


def test_reply_with_an_older_readonly_scope_is_accepted_and_saved(tmp_path, monkeypatch):
    monkeypatch.delenv("OAUTHLIB_RELAX_TOKEN_SCOPE", raising=False)
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: True)
    _Flow.reply = " ".join([gmail.READONLY, gmail.MODIFY, gmail.SETTINGS])
    creds = _src(tmp_path)._consent(_Flow, set(gmail.SIGN_IN_SCOPES))
    assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") == "1"
    assert set(creds._scopes) >= set(gmail.SIGN_IN_SCOPES)


def test_unticked_box_on_googles_page_gives_a_plain_message(tmp_path, monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: True)
    _Flow.reply = gmail.READONLY
    try:
        _src(tmp_path)._consent(_Flow, set(gmail.SIGN_IN_SCOPES))
    except gmail.AuthError as exc:
        assert "not every box was ticked" in str(exc)
    else:
        raise AssertionError("expected AuthError")
