"""Outlook.com / Microsoft 365 through Microsoft Graph (EXPERIMENTAL, tested
with fakes: tests/fake_microsoft.py is a real local HTTP server).

What the user should get: one browser sign-in that asks only to read mail;
a scan that skips Sent, Drafts, Deleted Items and Junk; the next scan without
signing in again; a clear "sign in again" when Microsoft revokes access;
every action asking for its own permission first and still going through
the typed double confirmation; and tokens kept only in the private folder.
"""
import json
import stat

import pytest

import realmail as R
from fake_microsoft import FakeMicrosoft, browser
from clawback import actions, ms_oauth, tracker
from clawback.scan import run_scan
from clawback.sources.base import AuthError, SourceError
from clawback.sources.outlook import OutlookSource

CID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def ms():
    f = FakeMicrosoft()
    for i in range(5):
        f.add(R.msg(f"Shop {i} <news@shop{i}.example>", f"Deals {i}", "Deals. Unsubscribe.", days_ago=1 + i,
                    lu=f"<https://shop{i}.example/u>", one_click=True), read=i % 2 == 0)
    f.add(R.msg("Me <me@outlook.com>", "My own mail", "hi", days_ago=1), folder="sentitems")
    f.add(R.msg("Junk <j@junk.example>", "WIN", "Unsubscribe", days_ago=1, lu="<https://junk.example/u>"),
          folder="junkemail")
    f.add(R.msg("Gone <g@gone.example>", "Old", "x", days_ago=2), folder="deleteditems")
    f.add(R.msg("Archived <news@arch.example>", "Archived news", "Unsubscribe", days_ago=3,
                lu="<https://arch.example/u>"), folder="archive")
    yield f
    f.close()


def _src(f, account="me@outlook.com", **kw):
    signin = ms_oauth.MicrosoftSignIn(CID, account, authority=f.base, open_browser=kw.pop("open_browser", browser()),
                                      timeout=kw.pop("timeout", 10))
    return OutlookSource(account, signin=signin, graph=f.graph, sleep=kw.pop("sleep", lambda s: None))


def test_sign_in_asks_for_every_permission_at_once_and_uses_pkce_without_a_secret(ms):
    src = _src(ms)
    src.connect()
    p = ms.authorize_params[0]
    scopes = set(p["scope"].split())
    # One consent screen: reading plus everything cleanup needs, so no second sign-in later
    assert scopes == {"openid", "email", "offline_access", *ms_oauth.SIGN_IN_SCOPES}
    assert {ms_oauth.READ, ms_oauth.READWRITE, ms_oauth.SEND, ms_oauth.RULES} <= scopes
    assert p["login_hint"] == "me@outlook.com" and p["code_challenge_method"] == "S256"
    assert all("client_secret" not in f for f in ms.token_forms)
    assert not any(src.missing_permission(c) for c in ("trash", "send", "block"))
    assert src.describe()["needs_signin"] is False and len(ms.authorize_params) == 1


def test_scan_reads_the_mailbox_but_not_sent_junk_or_deleted(ms):
    src = _src(ms)
    infos = list(src.iter_messages())
    senders = {i.from_addr for i in infos}
    assert senders == {f"news@shop{i}.example" for i in range(5)} | {"news@arch.example"}
    assert all(i.source_id.startswith("AAMk-immutable-") for i in infos)
    first = next(i for i in infos if i.from_addr == "news@shop0.example")
    assert first.list_unsubscribe and first.one_click and first.unread is False
    assert [i.date for i in infos] == sorted((i.date for i in infos), reverse=True)


def test_tokens_are_saved_privately_and_the_next_scan_needs_no_sign_in(ms, isolated_home):
    opener = browser()
    run_scan(_src(ms, open_browser=opener), limit=None)
    path = ms_oauth.token_path("me@outlook.com")
    assert path.parent == isolated_home / "tokens" and path.name == "outlook-me@outlook.com.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["account"] == "me@outlook.com" and data["refresh_token"] and "password" not in json.dumps(data)
    if hasattr(stat, "S_IRWXG"):
        import os
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(opener.opened) == 1
    second = browser()
    result = run_scan(_src(ms, open_browser=second), limit=None)
    assert second.opened == [] and result.reused == 6 and result.account == "me@outlook.com"


def test_expired_access_token_is_refreshed_once_silently(ms):
    src = _src(ms)
    src.connect()
    ms.expire_access_tokens()
    assert len(list(src.iter_messages())) == 6
    assert [f["grant_type"] for f in ms.token_forms] == ["authorization_code", "refresh_token"]


def test_revoked_access_says_sign_in_again(ms):
    src = _src(ms)
    src.connect()
    ms.revoke_all()
    with pytest.raises(AuthError, match="Sign in with Microsoft again"):
        list(src.iter_messages())


def test_denied_consent_changes_nothing(ms):
    ms.consent = "deny"
    with pytest.raises(AuthError, match="cancelled or permission was not given"):
        _src(ms).connect()
    assert not ms_oauth.token_path("me@outlook.com").exists()


def test_signing_in_as_another_mailbox_is_refused(ms):
    ms.signed_in_as = "someone.else@outlook.com"
    with pytest.raises(AuthError, match="signed in to Microsoft as someone.else@outlook.com"):
        _src(ms).connect()
    assert not ms_oauth.token_path("me@outlook.com").exists()


def test_no_answer_from_the_browser_times_out():
    f = FakeMicrosoft()
    try:
        with pytest.raises(AuthError, match="No answer"):
            _src(f, open_browser=lambda url: True, timeout=0.5).connect()
    finally:
        f.close()


def test_wrong_client_id_is_explained():
    f = FakeMicrosoft(client_id="99999999-2222-3333-4444-555555555555")
    try:
        with pytest.raises(AuthError, match=r"check the Application \(client\) ID"):
            _src(f, timeout=1).connect()
    finally:
        f.close()
    with pytest.raises(SourceError, match="Application \\(client\\) ID"):
        ms_oauth.MicrosoftSignIn("not-a-guid", "me@outlook.com")


def test_throttling_waits_for_retry_after(ms):
    waits = []
    src = _src(ms, sleep=waits.append)
    src.connect()
    ms.throttle = 2
    assert len(list(src.iter_messages())) == 6
    assert waits == [3.0, 3.0]


def test_paging_and_the_date_window(ms):
    from datetime import timedelta
    import clawback.sources.outlook as O
    old = O.PAGE
    O.PAGE = 2
    try:
        src = _src(ms)
        assert len(list(src.iter_messages())) == 6
        assert sum(1 for m, p in ms.requests if p.startswith("/v1.0/me/messages?")) >= 4
        recent = list(src.iter_messages(since=R.NOW - timedelta(days=2, hours=12)))
        assert {i.from_addr for i in recent} == {"news@shop0.example", "news@shop1.example"}
    finally:
        O.PAGE = old


def test_mail_without_internet_headers_still_has_a_sender(ms):
    f = FakeMicrosoft(omit_headers=True)
    try:
        f.add(R.msg("Team <team@corp.example>", "Hello", "x", days_ago=1))
        infos = list(_src(f).iter_messages())
        assert infos[0].from_addr == "team@corp.example" and infos[0].subject == "Hello"
    finally:
        f.close()


# ----------------------------------------------------------------- actions
def _scan(ms):
    src = _src(ms)
    run_scan(src, limit=None)
    return src


def test_a_read_only_sign_in_asks_once_for_the_full_set_then_trash_and_undo(ms):
    src = _scan(ms)
    src.signin._data["scopes"] = [ms_oauth.READ]          # a token saved by an older version: read only
    assert src.describe()["needs_signin"] is True
    plan = actions.build_plan(src, "trash", ["news@shop1.example"], {})
    step = plan.steps[0]
    assert "Deleted Items" in step.permission and "only allows reading" in step.permission
    with pytest.raises(actions.PlanError, match="permission"):
        actions.confirm(src, plan.id, plan.count, plan.digest)
    src.grant("trash")
    assert set(ms_oauth.SIGN_IN_SCOPES) <= set(ms.authorize_params[-1]["scope"].split())
    assert not any(src.missing_permission(c) for c in ("trash", "send", "block"))
    plan = actions.build_plan(src, "trash", ["news@shop1.example"], {})
    with pytest.raises(actions.PlanError):
        actions.confirm(src, plan.id, 2, plan.digest)
    mid = next(m["id"] for m in ms.messages if m["from"]["emailAddress"]["address"] == "news@shop1.example")
    assert ms.folder_of(mid) == "inbox"
    res = actions.confirm(src, plan.id, plan.count, plan.digest)
    assert res["results"][0]["ok"] and ms.folder_of(mid) == "deleteditems"
    undo = actions.build_plan(src, "undo", [res["results"][0]["entry"]], {})
    actions.confirm(src, undo.id, undo.count, undo.digest)
    assert ms.folder_of(mid) == "inbox"


def test_sending_and_blocking_use_their_permissions(ms):
    src = _scan(ms)
    src.grant("send")
    src.send_email("unsub@shop2.example", "unsubscribe", "Please remove me")
    assert ms.sent[0]["message"]["toRecipients"][0]["emailAddress"]["address"] == "unsub@shop2.example"
    src.grant("block")
    undo = src.create_filter("news@shop3.example", "trash")
    rule = ms.rules[undo["filter_id"]]
    assert rule["conditions"]["fromAddresses"][0]["emailAddress"]["address"] == "news@shop3.example"
    assert rule["actions"]["moveToFolder"] == "F-deleteditems"
    src.delete_filter(undo["filter_id"])
    assert not ms.rules


def test_action_without_permission_is_refused_by_outlook_not_silently_done(ms):
    src = _scan(ms)
    mid = ms.messages[0]["id"]
    # Simulate a token Outlook no longer honours for writing (permission removed
    # on Microsoft's side) while Clawback still thinks it has it: the move must fail loudly.
    for tok in ms.tokens:
        ms.tokens[tok] = {"mail.read"}
    with pytest.raises(AuthError, match="needs permission"):
        src.trash([mid])


def test_archive_without_an_archive_folder_is_explained():
    f = FakeMicrosoft(no_archive=True)
    try:
        f.add(R.msg("A <a@a.example>", "x", "Unsubscribe", days_ago=1, lu="<https://a.example/u>"))
        src = _src(f)
        src.grant("archive")
        with pytest.raises(SourceError, match="no Archive folder"):
            src.archive([f.messages[0]["id"]])
    finally:
        f.close()


def test_actions_are_filed_under_the_signed_in_account(ms):
    src = _scan(ms)
    assert "me@outlook.com" in tracker.accounts()
    assert src.describe()["label"] == "experimental"


# ----------------------------------------------------------------- web + CLI
def test_web_connect_validates_and_saves_the_client_id(monkeypatch):
    from clawback.web.server import ApiError, App
    app = App()
    with pytest.raises(ApiError, match="Microsoft email"):
        app.connect({"kind": "outlook", "user": "nope", "client_id": CID})
    with pytest.raises(ApiError, match="client\\) ID"):
        app.connect({"kind": "outlook", "user": "me@outlook.com", "client_id": "abc"})
    with pytest.raises(ApiError, match="Account type"):
        app.connect({"kind": "outlook", "user": "me@outlook.com", "client_id": CID, "tenant": "evil.example"})
    import clawback.sources.outlook as O
    monkeypatch.setattr(O.OutlookSource, "connect", lambda self: None)
    src = app.connect({"kind": "outlook", "user": "Me@Outlook.com", "client_id": CID})
    assert src.account == "me@outlook.com" and ms_oauth.load_client() == {"client_id": CID, "tenant": "common"}


def test_cli_outlook_needs_a_client_id_the_first_time():
    from typer.testing import CliRunner
    from clawback.cli import app
    r = CliRunner().invoke(app, ["scan", "--outlook", "--user", "me@outlook.com"])
    assert r.exit_code == 2 and "--client-id" in r.output
