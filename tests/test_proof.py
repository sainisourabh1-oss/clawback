"""Goal H: proof the unsubscribe worked, the ignored list and escalation."""
from datetime import datetime, timedelta, timezone

from fakes import FakeGmail
from clawback import proof, tracker
from clawback.actions import build_plan, confirm
from clawback.demo import mailbox
from clawback.scan import run_scan
from clawback.sources.gmail import MODIFY, READONLY, SEND, SETTINGS, GmailSource

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)   # a Tuesday


def setup_sender(method, address="news@letters.example"):
    tracker.record_messages("me", [("old1", address, (T0 - timedelta(days=3)).isoformat(), "Issue 1", 1)])
    from clawback.senders import SenderSummary
    tracker.upsert_senders("me", [SenderSummary(address=address, domain="letters.example", category="newsletter")])
    tracker.set_sender_status("me", address, "unsubscribed", method=method, when=T0)
    return tracker.get_sender("me", address)


def add_mail(address, when, sid):
    tracker.record_messages("me", [(sid, address, when.isoformat(), "Still here", 1)])


def test_grace_periods():
    assert proof.grace_end(T0, "one-click") == T0 + timedelta(days=2)
    # 10 business days from Tue 1 Sep 2026 = Tue 15 Sep 2026 (two weekends skipped)
    assert proof.grace_end(T0, "mailto").date().isoformat() == "2026-09-15"
    assert proof.add_business_days(datetime(2026, 9, 4, tzinfo=timezone.utc), 1).weekday() == 0  # Fri -> Mon


def test_pending_inside_grace_period():
    s = setup_sender("one-click")
    add_mail(s["address"], T0 + timedelta(days=1), "g1")  # within grace: allowed
    r = proof.evaluate(s, "me", now=T0 + timedelta(days=1, hours=1))
    assert r["verdict"] == "pending" and r["within_grace"] == 1 and r["after_grace"] == 0


def test_pending_when_no_mail_seen_after_grace_yet():
    s = setup_sender("one-click")
    r = proof.evaluate(s, "me", now=T0 + timedelta(days=10))
    assert r["verdict"] == "pending"   # our view of the mailbox ends before the grace end


def test_honoured_when_mailbox_seen_past_grace_and_sender_silent():
    s = setup_sender("one-click")
    add_mail("friend@mail.example", T0 + timedelta(days=5), "other")
    r = proof.evaluate(s, "me", now=T0 + timedelta(days=6))
    assert r["verdict"] == "honoured"


def test_still_sending_goes_on_the_ignored_list_with_timeline():
    s = setup_sender("one-click")
    add_mail(s["address"], T0 + timedelta(days=4), "late1")
    add_mail(s["address"], T0 + timedelta(days=9), "late2")
    r = proof.evaluate(s, "me", now=T0 + timedelta(days=10))
    assert r["verdict"] == "still_sending" and r["after_grace"] == 2
    kinds = [(e["type"], e.get("phase")) for e in r["timeline"]]
    assert kinds == [("message", "before"), ("unsubscribed", None), ("grace_end", None), ("message", "after"), ("message", "after")]
    assert [x["address"] for x in proof.ignored("me", now=T0 + timedelta(days=10))] == [s["address"]]


def test_mailto_sender_gets_ten_business_days():
    s = setup_sender("mailto")
    add_mail(s["address"], T0 + timedelta(days=8), "g")
    r = proof.evaluate(s, "me", now=T0 + timedelta(days=9))
    assert r["verdict"] == "pending" and r["within_grace"] == 1


def test_full_flow_unsubscribe_rescan_ignored_escalate(unsub_server):
    """Scan -> unsubscribe (double confirmed) -> sender keeps mailing -> escalate
    (double confirmed: Gmail filter + report spam) -> undo is available."""
    from clawback import oneclick
    now = datetime.now(timezone.utc)
    fake = FakeGmail(mailbox(now - timedelta(days=20), base_url=unsub_server.base_url))
    src = GmailSource("me", service=fake, granted={READONLY, SEND, MODIFY, SETTINGS})
    run_scan(src, region="US")
    addr = "news@weeklybyte.example"
    plan = build_plan(src, "unsubscribe", [addr])
    confirm(src, plan.id, plan.count, plan.digest,
            http_post=lambda u: oneclick.post(u, context=unsub_server.client_ctx, allow_private=True))
    # The unsubscribe happened 20 days ago in this story; the sender kept mailing.
    db = tracker.connect()
    db.execute("UPDATE senders SET unsubscribed_at=? WHERE address=?", ((now - timedelta(days=19)).isoformat(), addr))
    db.commit(); db.close()
    for m in FakeGmail(mailbox(now, base_url=unsub_server.base_url)).store.values():
        fake.store["n" + m["id"]] = dict(m, id="n" + m["id"])
    run_scan(src, region="US")
    ignored = proof.ignored("me")
    assert [r["address"] for r in ignored] == [addr] and ignored[0]["after_grace"] > 0
    esc = build_plan(src, "escalate", [addr])
    assert esc.count == 1 and fake.filters == {}
    confirm(src, esc.id, esc.count, esc.digest)
    assert list(fake.filters.values())[0]["criteria"] == {"from": addr}
    spammed = [m for m in fake.store.values() if "SPAM" in m["labelIds"]]
    assert spammed and all("INBOX" not in m["labelIds"] for m in spammed)
    assert tracker.get_sender("me", addr)["status"] == "escalated"
    assert proof.ignored("me") == []   # escalated senders leave the to-do list
