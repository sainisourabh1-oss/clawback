"""Real-Gmail finding (fix run 1, item 3): senders showed "How: None" although
their emails carry an unsubscribe link in the body. Clawback now finds that
link (never fetching it), shows it as "Link in email", and opens it only
after the typed confirmation."""
from email.message import EmailMessage

import pytest

from clawback.extract import find_unsubscribe_link, from_bytes


def html_mail(body_html: str, plain: str = "View this email in your browser.", lu: str | None = None) -> bytes:
    m = EmailMessage()
    m["From"] = "Shop <news@shop.example>"
    m["To"] = "you@mail.example"
    m["Subject"] = "Weekly picks"
    m["Date"] = "Mon, 19 Oct 2026 09:00:00 +0000"
    if lu:
        m["List-Unsubscribe"] = lu
    m.set_content(plain)
    m.add_alternative(f"<html><body>{body_html}</body></html>", subtype="html")
    return bytes(m)


FOOTER = ('<p>Big sale! <a href="https://shop.example/sale">Shop now</a></p>'
          '<p style="font-size:11px">You get this because you signed up. '
          '<a href="https://shop.example/account">My account</a> | '
          '<a href="https://click.shop.example/u?x=1&amp;y=2">Unsubscribe</a></p>')


def test_labelled_anchor_is_found_and_entities_decoded():
    info = from_bytes(html_mail(FOOTER), "1")
    assert info.body_seen and info.body_unsub == "https://click.shop.example/u?x=1&y=2"


def test_header_mail_does_not_look_in_the_body():
    info = from_bytes(html_mail(FOOTER, lu="<https://shop.example/lu>"), "1")
    assert info.body_unsub == "" and info.list_unsubscribe == ["https://shop.example/lu"]


@pytest.mark.parametrize("html, want", [
    # nearest link to the words wins over a farther one
    ('<p><a href="https://a.example/home">Home</a></p>' + "x" * 600 +
     '<p>To stop these emails <a href="https://a.example/s?id=9">click here</a>.</p>', "https://a.example/s?id=9"),
    ('<p>If you no longer wish to receive our emails, <a href="https://a.example/l/77">click here</a></p>', "https://a.example/l/77"),
    ('<p>Manage your email preferences <a href="https://a.example/p">here</a></p>', "https://a.example/p"),
    ('<p><a href="https://a.example/x">Opt out</a></p>', "https://a.example/x"),
    ('<p>Click <a href="https://a.example/n">here</a> to configure your notification preferences.</p>', "https://a.example/n"),
    # "preferences" about something else is not an unsubscribe link
    ('<p>Update your genre preferences anytime. <a href="https://a.example/g">Choose genres</a></p>', ""),
    ('<td><a href="https://a.example/hi"><img src="cid:1" alt="Unsubscribe"></a></td>', "https://a.example/hi"),
    ('<p>Para darse de baja <a href="https://a.example/es">pulse aquí</a></p>', "https://a.example/es"),
    ('<p><a href="https://a.example/fr">Se désabonner</a></p>', "https://a.example/fr"),
    ('<p><a href="https://a.example/de">Newsletter abbestellen</a></p>', "https://a.example/de"),
    ('<p>ईमेल बंद करने के लिए <a href="https://a.example/hi">सदस्यता समाप्त करें</a></p>', "https://a.example/hi"),
    ('<p><a href="https://a.example/ja">配信停止はこちら</a></p>', "https://a.example/ja"),
    # path says unsubscribe, no words near: still found (lowest confidence)
    ('<p><a href="https://a.example/email/unsubscribe/abc">Click</a></p>', "https://a.example/email/unsubscribe/abc"),
])
def test_finds_the_unsubscribe_link(html, want):
    assert find_unsubscribe_link([], [html]) == want


def test_no_link_when_the_words_are_hidden_or_absent():
    hidden = ('<div style="display:none">unsubscribe</div><a href="https://a.example/promo">Shop</a>'
              '<style>.unsubscribe{color:red}</style>')
    assert find_unsubscribe_link([], [hidden]) == ""
    assert find_unsubscribe_link([], ['<a href="https://a.example/promo">Shop now</a>']) == ""
    # only http(s): a mailto or javascript link is never returned
    assert find_unsubscribe_link([], ['<a href="mailto:x@a.example">Unsubscribe</a>']) == ""


def test_plain_text_link_after_the_words():
    text = "Thanks for reading.\nTo unsubscribe, visit https://news.example/u/abc123.\nhttps://news.example/home"
    assert find_unsubscribe_link([text], []) == "https://news.example/u/abc123"


def test_metadata_only_message_has_no_body_seen():
    m = EmailMessage()
    m["From"] = "a@b.example"
    m["Subject"] = "x"
    info = from_bytes(bytes(m), "1")
    assert not info.body_seen and info.body_unsub == ""


# ---------------------------------------------------------------- end to end, every source
from datetime import datetime, timedelta, timezone  # noqa: E402

from clawback import actions, tracker  # noqa: E402
from clawback.demo import _msg  # noqa: E402
from clawback.scan import run_scan  # noqa: E402

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
CLUB = "club@bookclub.example"
LEAVE = "https://bookclub.example/leave?u=7"


def club_raws(n=3, footer=None):
    """A newsletter whose emails have no List-Unsubscribe header, only a
    footer link (and nothing that makes a scan read the body)."""
    footer = footer or f'<p>Not interested? <a href="{LEAVE}">Unsubscribe</a></p>'
    return [_msg(f"Book Club <{CLUB}>", "This month's picks", f"<p>Read these.</p>{footer}", NOW - timedelta(days=7 * i),
                 html=True, labels="Inbox,Category Promotions", n=i) for i in range(n)]


def sender(account):
    return {s["address"]: s for s in tracker.list_senders(account)}[CLUB]


def plan_and_confirm(src):
    plan = actions.build_plan(src, "unsubscribe", [CLUB])
    step = plan.steps[0]
    opened = []
    assert not step.blocked and step.method == "body-link" and step.contact == "bookclub.example"
    assert "Open the unsubscribe link from Book Club's email (bookclub.example)" in step.effects[0]
    assert plan.count == 1
    res = actions.confirm(src, plan.id, plan.count, plan.digest, http_post=lambda u: pytest.fail("no POST"),
                          opener=opened.append)
    r = res["results"][0]
    assert r["ok"] and r["open"] == LEAVE and opened == [LEAVE]   # opened only after the confirm click
    return res


def test_gmail_api_after_scan_top_up_reads_one_body_and_never_contacts_the_link():
    from fakes import FakeGmail
    from clawback.sources.gmail import GmailSource
    from clawback.web.server import App
    fake = FakeGmail(club_raws())
    src = GmailSource("me", service=fake, granted={"https://www.googleapis.com/auth/gmail.readonly"})
    run_scan(src, limit=100, region="US")
    s = sender("me")
    assert s["category"] == "newsletter" and s["body_link"] is None and s["body_checked"] is None
    assert fake.formats.count("full") == 0                  # the scan read headers only
    app = App()
    app.add_source(src)
    app.start_topup(src)
    app.topup_thread.join(10)
    assert app.topup == {"status": "done", "account": "me", "done": 1, "total": 1, "found": 1, "error": None}
    assert fake.formats.count("full") == 1                  # ONE recent body, the newest
    s = sender("me")
    assert s["body_link"] == {"url": LEAVE, "host": "bookclub.example", "level": None, "text": ""}
    assert fake.mutating_calls() == []
    # a second top-up has nothing left to read (cached)
    app.start_topup(src)
    assert app.topup_thread is None or not app.topup_thread.is_alive()
    assert fake.formats.count("full") == 1
    plan_and_confirm(src)
    assert sender("me")["status"] == "unsubscribed"


def test_imap_on_demand_reads_the_newest_email_once():
    from fakes import FakeIMAP
    from clawback.sources.imap import ImapSource
    from clawback.web.server import App
    fake = FakeIMAP(club_raws(), gmail=True)
    src = ImapSource("imap.gmail.com", "me@gmail.example", "pw", account="me@gmail.example", conn_factory=fake)
    run_scan(src, limit=100, region="US")
    assert fake.full_fetches == 0
    app = App()
    app.add_source(src)
    got = app.body_link("me@gmail.example", CLUB)
    assert got["body_link"]["url"] == LEAVE and got["body_checked"]
    assert fake.full_fetches == 1 and fake.selected_readonly is True
    app.body_link("me@gmail.example", CLUB)                 # cached: no second read
    assert fake.full_fetches == 1
    plan_and_confirm(src)


def test_mbox_scan_finds_the_link_without_any_extra_read(tmp_path):
    from helpers import write_mbox
    from clawback.sources.mbox import MboxSource
    src = MboxSource(write_mbox(tmp_path / "club.mbox", club_raws()), account="file")
    run_scan(src, region="US")
    s = sender("file")
    assert s["body_link"]["url"] == LEAVE and s["body_checked"]
    assert tracker.body_link_candidates("file") == []


def test_unknown_or_unsafe_body_link_gets_the_same_trust_check(tmp_path):
    from helpers import write_mbox
    from clawback.sources.mbox import MboxSource
    raws = club_raws(footer='<p><a href="https://trk.otherhost.example/x">Unsubscribe</a></p>')
    src = MboxSource(write_mbox(tmp_path / "a.mbox", raws), account="a")
    run_scan(src, region="US")
    bl = sender("a")["body_link"]
    assert bl["level"] == "warn" and "otherhost.example" in bl["text"]
    plan = actions.build_plan(src, "unsubscribe", [CLUB])
    assert plan.steps[0].blocked and plan.steps[0].ack == "sender"
    raws = club_raws(footer='<p><a href="http://bookclub.example/leave">Unsubscribe</a></p>')
    src = MboxSource(write_mbox(tmp_path / "b.mbox", raws), account="b")
    run_scan(src, region="US")
    plan = actions.build_plan(src, "unsubscribe", [CLUB])
    assert plan.steps[0].blocked and "unencrypted http" in plan.steps[0].blocked


def test_outlook_on_demand_reads_one_message_value():
    from fake_microsoft import FakeMicrosoft, browser
    from clawback import ms_oauth
    from clawback.sources.outlook import OutlookSource
    from clawback.web.server import App
    f = FakeMicrosoft()
    try:
        for raw in club_raws():
            f.add(raw)
        signin = ms_oauth.MicrosoftSignIn("11111111-2222-3333-4444-555555555555", "me@outlook.com",
                                          authority=f.base, open_browser=browser(), timeout=10)
        src = OutlookSource("me@outlook.com", signin=signin, graph=f.graph, sleep=lambda s: None)
        src.connect()
        run_scan(src, limit=100, region="US")
        before = [r for r in f.requests if r[1].endswith("/$value")]
        app = App()
        app.add_source(src)
        got = app.body_link("me@outlook.com", CLUB)
        assert got["body_link"]["url"] == LEAVE
        after = [r for r in f.requests if r[1].endswith("/$value")]
        assert len(after) - len(before) == 1 and all(m == "GET" for m, _ in f.requests if "/messages" in _)
    finally:
        f.close()


from test_web import web  # noqa: E402,F401 - the fixture


def test_web_endpoint_needs_csrf_and_returns_the_sender(web):
    # the demo mailbox has a body-link sender (its bodies are read by the scan)
    st, _, _ = web.req("POST", "/api/sender/body-link", {"address": "x@y.example"})
    assert st == 403
    web.post("/api/connect", {"kind": "demo"})
    web.post("/api/scan", {"limit": 100000})
    import time
    for _ in range(200):
        if web.get("/api/scan")[1]["status"] != "running":
            break
        time.sleep(0.05)
    st, s, _ = web.post("/api/sender/body-link", {"address": "club@bookclub.example"})
    assert st == 200 and s["body_link"]["host"] == "bookclub.example"
    st, err, _ = web.post("/api/sender/body-link", {"address": "nobody@none.example"})
    assert st == 404
