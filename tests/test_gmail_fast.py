"""Gmail fast mode: IMAP + app password (or opt-in XOAUTH2).

What the user should get: a scan that never touches the Gmail API quota,
never reads their sent mail, re-reads nothing on the next scan, counts each
message once even after switching between the API and IMAP, and a clear,
specific message when the sign-in is refused.
"""
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

import realmail as R
from fakes import FakeIMAP
from clawback import tracker
from clawback.mailbox import _uidset, gmail_raw_query, iter_messages, xoauth2_string
from clawback.scan import run_scan
from clawback.sources.base import AuthError
from clawback.sources.imap import ImapSource

NOW = R.NOW


def _mail(n=12, sent=()):
    raws = [R.msg(f"Shop {i} <news{i}@shop{i}.example>", f"Weekly deals {i}", "Deals. Unsubscribe.",
                  days_ago=1 + 10 * i, lu=f"<https://shop{i}.example/u>", one_click=True)
            for i in range(n)]
    # Arrival order: oldest first, as UIDs ascend on a real server.
    return list(reversed(raws))


def _gmail(raws, **kw):
    return FakeIMAP(raws, gmail=True, **kw)


def _source(fake, **kw):
    return ImapSource("imap.gmail.com", "me@gmail.example", kw.pop("password", "abcdabcdabcdabcd"),
                      account="me@gmail.example", conn_factory=fake, **kw)


def test_date_window_is_an_exact_gmail_search_that_drops_sent_mail_on_the_server():
    raws = _mail(12)
    sent_uid = len(raws)                     # the newest message is one you sent
    fake = _gmail(raws, sent={sent_uid})
    since = NOW - timedelta(days=45)
    got = list(iter_messages("imap.gmail.com", "me", "pw", ["[Gmail]/All Mail"], conn_factory=fake, since=since))
    crit = fake.searches[0]
    assert crit[0] == "X-GM-RAW" and f"after:{int(since.timestamp()) - 1}" in crit[1] and "-in:sent" in crit[1]
    assert sent_uid not in fake.fetched_headers               # never downloaded
    assert all(m.date >= since for m in got) and len(got) == 4   # days_ago 11, 21, 31, 41 (1 is sent)


def test_message_ids_are_the_gmail_api_ids_so_rescans_skip_before_fetching():
    raws = _mail(8)
    fake = _gmail(raws)
    src = _source(fake)
    first = run_scan(src, region="US")
    ids = {r["source_id"] for r in tracker.connect().execute("SELECT source_id FROM messages WHERE account=?",
                                                            ("me@gmail.example",))}
    assert ids == {format(FakeIMAP.msgid(u), "x") for u in range(1, 9)}   # hex X-GM-MSGID = API message id
    fake.fetched_headers.clear()
    again = run_scan(_source(fake), region="US")
    assert fake.fetched_headers == []                     # nothing re-downloaded
    assert again.reused == 8 and again.categories == first.categories   # same result, nothing re-read


def test_servers_without_gmail_search_fall_back_to_since():
    fake = _gmail(_mail(6), raw_search=False)
    got = list(iter_messages("imap.gmail.com", "me", "pw", ["[Gmail]/All Mail"], conn_factory=fake,
                             since=NOW - timedelta(days=25)))
    assert [c[0] for c in fake.searches] == ["X-GM-RAW", "SINCE"]
    assert len(got) == 3


def test_uid_sets_are_compact_ranges():
    assert _uidset([b"5", b"1", b"2", b"3", b"9", b"10"]) == "1:3,5,9:10"
    assert _uidset([str(u).encode() for u in range(1, 5001)]) == "1:5000"


def test_round_trips_stay_small_for_a_year_of_mail():
    raws = _mail(40)
    fake = _gmail(raws)
    src = _source(fake)
    list(src.iter_messages())
    # login/select aside: 1 search + 1 ID map + 1 header chunk (+ bodies only for receipts)
    assert src.stats["round_trips"] <= 4 and src.stats["gmail_ext"]


def test_query_quotes_and_epoch():
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert gmail_raw_query(since) == f'"after:{int(since.timestamp()) - 1} -in:sent -in:chats"'
    assert gmail_raw_query(None) == '"-in:sent -in:chats"'


# ----------------------------------------------------------------- sign-in
def test_xoauth2_sends_the_standard_sasl_string_and_never_a_password():
    fake = _gmail(_mail(3), oauth_token="ya29.token")
    src = ImapSource("imap.gmail.com", "me@gmail.example", None, conn_factory=fake, oauth_token=lambda: "ya29.token")
    assert len(list(src.iter_messages())) == 3
    assert fake.auth_strings == [b"user=me@gmail.example\x01auth=Bearer ya29.token\x01\x01"]
    assert xoauth2_string("a@b.c", "t") == b"user=a@b.c\x01auth=Bearer t\x01\x01"
    assert src.describe()["auth"] == "xoauth2"


def test_refused_xoauth2_token_says_what_to_do():
    fake = _gmail(_mail(1), oauth_token="right")
    src = ImapSource("imap.gmail.com", "me@gmail.example", None, conn_factory=fake, oauth_token="wrong")
    with pytest.raises(AuthError, match="Connect again"):
        list(src.iter_messages())


def test_refused_app_password_points_to_app_passwords_page():
    class Refuse(FakeIMAP):
        def login(self, user, password):
            import imaplib
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
    src = _source(Refuse(_mail(1), gmail=True))
    with pytest.raises(AuthError) as exc:
        list(src.iter_messages())
    assert "myaccount.google.com/apppasswords" in str(exc.value) and "AUTHENTICATIONFAILED" in str(exc.value)


def test_gmail_fast_defaults_to_all_mail():
    assert ImapSource("imap.gmail.com", "me", "pw").folders == ["[Gmail]/All Mail"]


# ----------------------------------------------------------------- web + CLI
def test_web_fast_connect_needs_address_and_app_password(tmp_path, monkeypatch):
    # connect now signs in once before the account replaces the one on screen;
    # a local fake server answers (no real mail server is contacted)
    from fakes import FakeIMAP
    fake = FakeIMAP([])
    monkeypatch.setattr("clawback.sources.imap.open_imap", lambda host, *a, **kw: fake)
    from clawback.web.server import ApiError, App
    app = App()
    with pytest.raises(ApiError, match="Gmail address"):
        app.connect({"kind": "gmail_imap", "user": "nope", "password": "x"})
    with pytest.raises(ApiError, match="apppasswords"):
        app.connect({"kind": "gmail_imap", "user": "me@gmail.com", "password": "  "})
    src = app.connect({"kind": "gmail_imap", "user": "me@gmail.com", "password": "abcd efgh ijkl mnop"})
    assert src.host == "imap.gmail.com" and src.folders == ["[Gmail]/All Mail"] and src._password == "abcdefghijklmnop"


def test_web_xoauth2_needs_the_full_access_acknowledgement():
    from clawback.web.server import ApiError, App
    app = App()
    with pytest.raises(ApiError, match="Full mailbox access"):
        app.connect({"kind": "gmail_imap", "user": "me@gmail.com", "xoauth2": True})
    assert app.auth.get("status") != "waiting"            # nothing started


def test_cli_xoauth2_refuses_without_accept_flag():
    from clawback.cli import app
    out = CliRunner().invoke(app, ["scan", "--gmail-fast", "--user", "me@gmail.com", "--xoauth2"])
    assert out.exit_code == 2
    assert "Full mailbox access" in out.output and "--accept-full-access" in out.output


def test_cli_help_states_both_trade_offs():
    import re
    from clawback.cli import app
    raw = CliRunner().invoke(app, ["scan", "--help"]).output
    text = " ".join(re.sub(r"[^\w(),.-]+", " ", raw).split())      # drop the help box borders
    assert "Fast scan (about a minute)" in text and "the scan only reads. 10 to 15 min the first time" in text
    assert "one consent for scanning and cleanup (gmail.modify" in text   # it is not a read-only sign-in any more
