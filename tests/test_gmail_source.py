"""Gmail source against a fake API: scan parity, rate limits, auth errors (bug 8)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeGmail, http_error
from clawback import tracker
from clawback.demo import mailbox
from clawback.scan import run_scan
from clawback.sources.base import AuthError
from clawback.sources.gmail import MODIFY, READONLY, SEND, SETTINGS, GmailSource


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAWBACK_HOME", str(tmp_path / "home"))


def test_gmail_scan_matches_mbox_scan(tmp_path):
    from helpers import write_mbox
    from clawback.sources.mbox import MboxSource
    now = datetime.now(timezone.utc)
    raws = mailbox(now)
    g = run_scan(GmailSource("g", service=FakeGmail(raws), granted={READONLY}), limit=10000, region="US")
    m = run_scan(MboxSource(write_mbox(tmp_path / "d.mbox", raws), account="m"), region="US")
    key = lambda subs: sorted((s.vendor, s.amount, s.currency, s.cycle) for s in subs)
    assert key(g.subscriptions) == key(m.subscriptions)
    assert g.categories == m.categories
    gs = {r["address"]: r["unread_count"] for r in tracker.list_senders("g")}
    ms = {r["address"]: r["unread_count"] for r in tracker.list_senders("m")}
    assert gs == ms


def test_scan_is_read_only():
    fake = FakeGmail(mailbox())
    run_scan(GmailSource("g", service=fake, granted={READONLY}), region="US")
    assert fake.mutating_calls() == []


def test_rate_limit_429_backs_off_and_honours_retry_after():
    fake = FakeGmail(mailbox()[:5])
    fake.failures["get"] = [http_error(429, retry_after="7"), http_error(503), http_error(403, "userRateLimitExceeded")]
    sleeps = []
    src = GmailSource("g", service=fake, sleep=lambda s: (sleeps.append(s), fake.clock.sleep(s)), clock=fake.clock,
                      granted={READONLY})
    infos = list(src.iter_messages(10))
    assert len(infos) == 5
    # One batch of 5; the three throttled sub-requests are retried together
    # after the longest Retry-After, and the pacer halves its rate.
    assert fake.batches[:2] == [5, 3] and sleeps[0] == 7.0
    assert src.pacer.throttles == 1 and src.pacer.rate < src.pacer.limit


def test_persistent_rate_limit_eventually_raises():
    fake = FakeGmail(mailbox()[:2])
    fake.failures["list"] = [http_error(429)] * 10
    with pytest.raises(Exception) as exc:
        list(GmailSource("g", service=fake, sleep=lambda s: None, granted={READONLY}).iter_messages(10))
    assert getattr(exc.value.resp, "status", None) == 429


def test_deleted_message_between_list_and_get_is_skipped():
    fake = FakeGmail(mailbox()[:3])
    fake.failures["get"] = [http_error(404)]
    assert len(list(GmailSource("g", service=fake, granted={READONLY}).iter_messages(10))) == 2


def test_bug8_revoked_token_gives_reconnect_advice_not_imap_advice():
    fake = FakeGmail(mailbox()[:2])
    fake.failures["list"] = [http_error(401)]
    with pytest.raises(AuthError) as exc:
        list(GmailSource("g", service=fake, granted={READONLY}).iter_messages(10))
    msg = str(exc.value)
    assert "Reconnect Gmail" in msg and "IMAP" not in msg and "app password" not in msg


def test_bug8_refresh_failure_deletes_token_and_reruns_consent(tmp_path, monkeypatch):
    pytest.importorskip("google.oauth2.credentials")
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    token = tmp_path / "token.json"
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}", encoding="utf-8")
    token.write_text(json.dumps({"token": "old", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                                 "token_uri": "https://oauth2.googleapis.com/token", "scopes": [READONLY],
                                 "expiry": (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}),
                     encoding="utf-8")

    def refuse(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")
    monkeypatch.setattr(Credentials, "refresh", refuse)
    consents = []

    class NewCreds:
        scopes = [READONLY]
        valid = True

        def to_json(self):
            return json.dumps({"token": "new", "scopes": [READONLY]})

    def flow():
        consents.append(token.exists())
        return NewCreds()
    src = GmailSource("default", token_file=token, client_secret=secret, flow_runner=flow)
    creds = src._credentials({READONLY})
    assert isinstance(creds, NewCreds)
    assert consents == [False]            # dead token removed before asking again
    assert json.loads(token.read_text())["token"] == "new"


def test_missing_client_secret_explains_takeout_alternative(tmp_path):
    src = GmailSource("default", token_file=tmp_path / "t.json", client_secret=tmp_path / "none.json")
    with pytest.raises(AuthError, match="Takeout"):
        src._credentials({READONLY})


def test_incremental_scopes_are_reported_per_action():
    src = GmailSource("g", service=FakeGmail([]), granted={READONLY})
    assert src.missing_permission("mailto")
    assert src.missing_permission("trash")
    assert src.missing_permission("block")
    assert src.missing_permission("one-click") is None
    src = GmailSource("g", service=FakeGmail([]), granted={READONLY, SEND, MODIFY, SETTINGS})
    assert not any(src.missing_permission(c) for c in ("mailto", "trash", "archive", "spam", "block"))


def test_scan_record_has_newest_and_oldest_dates():
    """Real-Gmail finding C: scans.newest was NULL after a successful scan."""
    now = datetime.now(timezone.utc)
    raws = mailbox(now)
    run_scan(GmailSource("g", service=FakeGmail(raws), granted={READONLY}), limit=10000, region="US")
    db = tracker.connect()
    row = db.execute("SELECT newest, oldest, messages, error FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    assert row["error"] is None and row["messages"] == len(raws)
    newest, oldest = tracker.parse_dt(row["newest"]), tracker.parse_dt(row["oldest"])
    assert newest is not None and oldest is not None and oldest < newest <= now + timedelta(minutes=1)


def test_consent_hands_out_the_sign_in_url_and_uses_a_timeout(tmp_path, monkeypatch):
    import webbrowser
    opened, shown, calls = [], [], {}
    monkeypatch.setattr(webbrowser, "open", lambda url, **kw: opened.append(url))

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            return cls()

        def authorization_url(self, **kw):
            return "https://accounts.google.com/o/oauth2/auth?client_id=x&code_challenge=y", "state"

        def run_local_server(self, **kw):
            calls.update(kw)
            self.authorization_url()
            return "creds"
    src = GmailSource("g", on_auth_url=shown.append)
    assert src._consent(Flow, {READONLY}) == "creds"
    assert shown == opened == ["https://accounts.google.com/o/oauth2/auth?client_id=x&code_challenge=y"]
    assert calls["timeout_seconds"] == 300 and calls["host"] == "127.0.0.1" and calls["open_browser"] is False


def test_consent_timeout_is_a_clear_message(tmp_path):
    pytest.importorskip("google_auth_oauthlib")
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}", encoding="utf-8")

    class WSGITimeoutError(Exception):
        pass

    def flow():
        raise WSGITimeoutError("timed out")
    src = GmailSource("g", token_file=tmp_path / "t.json", client_secret=secret, flow_runner=flow)
    with pytest.raises(AuthError) as exc:
        src.service()
    assert "timed out after 5 minutes" in str(exc.value) and "Nothing was saved" in str(exc.value)


def test_cli_prints_the_sign_in_url(capsys):
    from clawback import cli
    cli._print_auth_url("https://accounts.google.com/o/oauth2/auth?x=1")
    out = capsys.readouterr().out
    assert "If your browser didn't open, visit: https://accounts.google.com/o/oauth2/auth?x=1" in out
