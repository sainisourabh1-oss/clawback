"""The All accounts view.

What the user should see: one list across every scanned account; the same
Gmail connected twice (API + IMAP) counted once; the same newsletter in two
different mailboxes added up and tagged with both; money per currency and
never summed across currencies; and every action going to the right
account, through the typed confirmation, once per mailbox.
"""
import pytest

import realmail as R
from fakes import FakeGmail, FakeIMAP
from clawback import actions, combined, oneclick, savings, tracker
from clawback.scan import run_scan
from clawback.sources.gmail import READONLY, GmailSource
from clawback.sources.imap import ImapSource
from test_web import web  # noqa: F401  (the real-HTTP web fixture)


def _mail(n=4):
    raws = [R.msg(f"Shop {i} <news@shop{i}.example>", f"Deals {i}", "Deals. Unsubscribe.", days_ago=1 + 5 * i,
                  lu=f"<https://shop{i}.example/u>", one_click=True) for i in range(n)]
    return list(reversed(raws))


@pytest.fixture
def three_accounts():
    """Gmail via the API (label "default"), the SAME Gmail over IMAP, and a
    different Yahoo mailbox that shares one newsletter."""
    raws = _mail(4)
    api = FakeGmail(raws)
    # The Gmail API id and IMAP's X-GM-MSGID (in hex) are the same number on
    # the real service; give the fake API the ids the fake IMAP server uses.
    ids = {f"m{i:05d}": format(FakeIMAP.msgid(i + 1), "x") for i in range(len(raws))}
    api.store = {ids[k]: dict(m, id=ids[k]) for k, m in api.store.items()}
    api.email_address = "Me@Gmail.com"
    run_scan(GmailSource("default", service=api, granted={READONLY}), limit=None, region="US")
    run_scan(ImapSource("imap.gmail.com", "me@gmail.com", "pw", account="me@gmail.com",
                        conn_factory=FakeIMAP(raws, gmail=True)), limit=None, region="US")
    yahoo = [R.msg("Shop 1 <news@shop1.example>", "Deals 1", "Deals. Unsubscribe.", days_ago=2,
                   lu="<https://shop1.example/u>", one_click=True),
             R.msg("Only Yahoo <hi@yonly.example>", "Hello", "Unsubscribe", days_ago=3, lu="<https://yonly.example/u>")]
    run_scan(ImapSource("imap.mail.yahoo.com", "me@yahoo.com", "pw", account="me@yahoo.com", provider="yahoo",
                        conn_factory=FakeIMAP(yahoo)), limit=None, region="US")


def test_same_gmail_through_api_and_imap_is_one_mailbox(three_accounts):
    gs = combined.groups()
    assert ["default", "me@gmail.com"] in gs and ["me@yahoo.com"] in gs
    assert combined.label(["default", "me@gmail.com"]) == "me@gmail.com"


def test_message_counts_are_not_doubled_but_other_mailboxes_add_up(three_accounts):
    rows = {r["address"]: r for r in combined.senders()}
    # 1 email each in the Gmail mailbox (seen twice), shop1 also once in Yahoo
    assert rows["news@shop0.example"]["message_count"] == 1
    assert rows["news@shop0.example"]["accounts"] == ["default", "me@gmail.com"]
    assert rows["news@shop1.example"]["message_count"] == 2 and rows["news@shop1.example"]["mailboxes"] == 2
    assert rows["news@shop1.example"]["accounts"] == ["default", "me@gmail.com", "me@yahoo.com"]
    assert rows["hi@yonly.example"]["accounts"] == ["me@yahoo.com"]
    assert len(rows) == 5


def test_shared_gmail_ids_group_accounts_even_without_an_address():
    raws = _mail(3)
    run_scan(ImapSource("imap.gmail.com", "legacy-user", "pw", account="old-label",
                        conn_factory=FakeIMAP(raws, gmail=True)), limit=None, region="US")
    run_scan(ImapSource("imap.gmail.com", "someone@gmail.com", "pw", account="someone@gmail.com",
                        conn_factory=FakeIMAP(raws, gmail=True)), limit=None, region="US")
    assert ["old-label", "someone@gmail.com"] in combined.groups()


def test_other_providers_imap_ids_never_merge_mailboxes():
    # "INBOX:1" in two different mailboxes is two different emails.
    run_scan(ImapSource("imap.mail.yahoo.com", "a@yahoo.com", "pw", account="a@yahoo.com", conn_factory=FakeIMAP(_mail(2))),
             limit=None, region="US")
    run_scan(ImapSource("imap.aol.com", "b@aol.com", "pw", account="b@aol.com", conn_factory=FakeIMAP(_mail(2))),
             limit=None, region="US")
    assert ["a@yahoo.com"] in combined.groups() and ["b@aol.com"] in combined.groups()
    assert {r["address"]: r for r in combined.senders()}["news@shop0.example"]["message_count"] == 2


def _sub(account, vendor, domain, amount, currency):
    db = tracker.connect()
    try:
        now = tracker.now_iso()
        db.execute("INSERT INTO subscriptions(account, vendor, domain, amount, currency, cycle, monthly_cost, status, "
                   "cancelled_at, hits, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (account, vendor, domain, amount, currency, "monthly", amount, "cancelled", "2026-09-24T00:00:00+00:00",
                    3, now, now))
        db.commit()
    finally:
        db.close()


def test_savings_count_a_twice_connected_subscription_once_and_never_mix_currencies(three_accounts):
    _sub("default", "Play", "play.example", 169.0, "INR")
    _sub("me@gmail.com", "Play", "play.example", 169.0, "INR")      # the same subscription, seen twice
    _sub("me@yahoo.com", "Stream", "stream.example", 9.99, "USD")
    s = combined.savings(settings={"home_currency": "INR"})
    money = {m["currency"]: m["monthly"] for m in s["money"]}
    assert money == {"INR": 169.0, "USD": 9.99}
    assert [m["currency"] for m in s["money"]] == ["INR", "USD"]
    # the naive per-account sum would have double counted
    assert {m["currency"]: m["monthly"] for m in savings.compute(None, settings={"home_currency": "INR"})["money"]}["INR"] == 338.0
    subs = combined.subscriptions()
    play = [x for x in subs if x["domain"] == "play.example"]
    assert len(play) == 1 and play[0]["accounts"] == ["default", "me@gmail.com"]


def test_actions_go_to_each_mailbox_once_through_the_confirm_click(three_accounts):
    posts = []
    plan = actions.build_group_plan({}, "unsubscribe", ["news@shop1.example", "news@shop2.example"])
    parts = {p["account"]: p for p in plan["group"]}
    # shop1 is in two mailboxes: once for the Gmail mailbox, once for Yahoo; shop2 only in Gmail
    assert set(parts) == {"default", "me@yahoo.com"}
    assert plan["count"] == 3 and plan["emails"] > 0
    assert {s["account"] for s in plan["steps"]} == {"default", "me@yahoo.com"}
    with pytest.raises(actions.PlanError, match="does not match"):
        actions.confirm_group({}, plan["id"], 2, plan["digest"], http_post=posts.append)
    with pytest.raises(actions.PlanError, match="does not match"):
        actions.confirm_group({}, plan["id"], 3, "0" * 64, http_post=posts.append)
    assert posts == []
    res = actions.confirm_group({}, plan["id"], "3", plan["digest"],
                                http_post=lambda u: posts.append(u) or oneclick.PostResult(True, 200, "HTTP 200", u))
    assert res["ok"] == 3 and len(posts) == 3
    assert sorted(r["account"] for r in res["results"]) == ["default", "default", "me@yahoo.com"]
    assert tracker.get_sender("default", "news@shop1.example")["status"] == "unsubscribed"
    assert tracker.get_sender("me@yahoo.com", "news@shop1.example")["status"] == "unsubscribed"
    entries = tracker.audit_entries(None)
    assert sorted(e["account"] for e in entries) == ["default", "default", "me@yahoo.com"]
    with pytest.raises(actions.PlanError, match="already"):
        actions.confirm_group({}, plan["id"], 3, plan["digest"], http_post=posts.append)


def test_a_connected_source_is_preferred_for_mailbox_actions(three_accounts):
    imap = ImapSource("imap.gmail.com", "me@gmail.com", "pw", account="me@gmail.com", conn_factory=FakeIMAP([]))
    plan = actions.build_group_plan({"me@gmail.com": imap}, "unsubscribe", ["news@shop0.example"])
    assert [p["account"] for p in plan["group"]] == ["me@gmail.com"]


def test_actions_that_need_one_account_are_refused_in_all_view(three_accounts):
    with pytest.raises(actions.PlanError, match="Choose one account"):
        actions.build_group_plan({}, "undo", ["1"])
    with pytest.raises(actions.PlanError, match="Unknown sender"):
        actions.build_group_plan({}, "unsubscribe", ["nobody@x.example"])


def test_cancelling_a_group_review_cancels_every_part(three_accounts):
    plan = actions.build_group_plan({}, "unsubscribe", ["news@shop1.example"])
    actions.cancel_plan(plan["id"])
    for part in plan["group"]:
        assert actions.get_plan(part["plan_id"])["status"] == "cancelled"
    with pytest.raises(actions.PlanError, match="cancelled"):
        actions.confirm_group({}, plan["id"], plan["count"], plan["digest"])


def test_web_all_view(three_accounts, web):
    from clawback.web.server import ALL
    st, state, _ = web.get("/api/state")
    assert state["all"]["available"]
    assert {"label": "me@gmail.com", "accounts": ["default", "me@gmail.com"]} in state["all"]["mailboxes"]
    assert web.post("/api/use", {"account": ALL})[1]["active"] == ALL
    rows = {r["address"]: r for r in web.get("/api/senders?category=newsletter")[1]["senders"]}
    assert rows["news@shop1.example"]["message_count"] == 2
    st, plan, _ = web.post("/api/plan", {"action": "unsubscribe", "targets": ["news@shop3.example"]})
    assert st == 200 and plan["account"] == ALL and plan["count"] == 1
    st, err, _ = web.post("/api/confirm", {"plan_id": plan["id"], "count": 9, "digest": plan["digest"]})
    assert st == 409 and "does not match" in str(err)   # plan errors are 409 everywhere
    st, err, _ = web.post("/api/scan", {})
    assert st == 409 and "Choose one account" in str(err)
    money = web.get("/api/savings")[1]["money"]
    assert isinstance(money, list)


def test_switching_account_still_shows_its_last_scan(web):
    # Two mailboxes in one session: scan the first, then the second. Going
    # back to the first must still give the header line "Last scan <date> ·
    # N emails from M senders", from the stored record (the running job
    # only describes the second scan).
    from clawback.demo import DemoSource
    from clawback.web.server import ALL
    web.app.add_source(DemoSource(account="home"))
    first = web.scan()
    web.app.add_source(DemoSource(account="work"))
    assert web.scan()["account"] == "work"
    assert web.post("/api/use", {"account": "home"})[1]["active"] == "home"
    state = web.get("/api/state")[1]
    assert state["scan"]["account"] == "work"
    last = state["last_scan"]
    assert last["messages"] == first["result"]["messages"] and last["senders"] == first["result"]["senders"]
    assert last["finished"][:10] == first["finished"][:10]
    assert web.post("/api/use", {"account": ALL})[1]["active"] == ALL
    assert web.get("/api/state")[1]["last_scan"] is None   # the All view has its own line
    assert tracker.last_scan("never-scanned") is None
