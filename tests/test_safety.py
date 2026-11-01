"""Goal B: nothing happens without BOTH user steps, plus one-click safety."""
from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeGmail
from clawback import actions, oneclick, tracker
from clawback.actions import PlanError, build_plan, confirm
from clawback.demo import DemoSource
from clawback.scan import run_scan
from clawback.sources.gmail import MODIFY, READONLY, SEND, SETTINGS, GmailSource

NEWS = "deals@shopmart.example"
NEWS2 = "news@weeklybyte.example"
SPAM = "team@moonshot.example"
MAILTO = "hello@recipebox.example"
MISMATCH = "events@webinarhub.example"
LINK = "promo@gadgetstore.example"


def gmail(raws, scopes=(READONLY, SEND, MODIFY, SETTINGS)):
    fake = FakeGmail(raws)
    src = GmailSource("me", service=fake, granted=set(scopes))
    run_scan(src, limit=10000, region="US")
    return src, fake


@pytest.fixture
def post(unsub_server):
    return lambda url: oneclick.post(url, context=unsub_server.client_ctx, allow_private=True)


def test_step1_contacts_nothing(demo_raws, unsub_server):
    src, fake = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe", [NEWS, NEWS2, MAILTO], {"also_trash": True, "also_block": True})
    assert unsub_server.requests == [] and fake.mutating_calls() == []
    assert plan.count == 3
    one = plan.steps[0]
    assert one.method == "one-click" and one.contact == "unsubscribe.shopmart.example"
    assert one.emails_affected == 90
    assert any("unsubscribe you automatically" in e and "unsubscribe.shopmart.example" in e for e in one.effects)
    assert any("Trash 90" in e for e in one.effects)
    # the technical detail is kept, apart from the plain line
    assert any("HTTPS POST" in d and "RFC 8058" in d and d.endswith(one.url) for d in one.details)
    assert not any("HTTPS POST" in e or "RFC" in e for e in one.effects)
    assert plan.steps[2].method == "mailto" and plan.steps[2].contact == "leave@recipebox.example"


def test_wrong_or_missing_count_runs_nothing(demo_raws, unsub_server, post):
    """Step 2 quotes the count its button showed; nothing is typed. A missing,
    stale or made-up count (or True, which Python would call 1) runs nothing."""
    src, fake = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe", [NEWS])
    for count in ("", None, True, 0, 2, -1, "yes", "UNSUBSCRIBE 1", "1.0"):
        with pytest.raises(PlanError):
            confirm(src, plan.id, count, plan.digest, http_post=post)
    assert unsub_server.requests == [] and fake.mutating_calls() == []
    assert tracker.audit_entries("me") == []


def test_wrong_digest_runs_nothing(demo_raws, unsub_server, post):
    src, _ = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe", [NEWS])
    other = build_plan(src, "unsubscribe", [NEWS2])
    for digest in ("", "0" * 64, other.digest):
        with pytest.raises(PlanError):
            confirm(src, plan.id, plan.count, digest, http_post=post)
    assert unsub_server.requests == []


def test_plan_runs_once_and_expires(demo_raws, unsub_server, post):
    src, _ = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe", [NEWS])
    result = confirm(src, plan.id, " 1 ", plan.digest, http_post=post)   # the count as JSON may arrive as text
    assert result["ok"] == 1
    with pytest.raises(PlanError, match="already"):
        confirm(src, plan.id, plan.count, plan.digest, http_post=post)
    assert len(unsub_server.posts()) == 1
    old = build_plan(src, "unsubscribe", [NEWS2], now=datetime.now(timezone.utc) - timedelta(minutes=16))
    with pytest.raises(PlanError, match="expired"):
        confirm(src, old.id, old.count, old.digest, http_post=post)
    assert len(unsub_server.posts()) == 1


def test_executor_cannot_be_called_without_confirm(demo_raws):
    src, fake = gmail(demo_raws)
    plan = build_plan(src, "trash", [NEWS])
    with pytest.raises(PermissionError):
        actions._execute(plan, src, None, lambda u: None, lambda u: None)
    with pytest.raises(PermissionError):
        actions._Confirmed(plan, object())
    assert fake.mutating_calls() == []


def test_tampered_stored_plan_is_refused(demo_raws, unsub_server, post):
    import json
    src, _ = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe", [NEWS])
    db = tracker.connect()
    payload = json.loads(db.execute("SELECT payload FROM plans WHERE id=?", (plan.id,)).fetchone()[0])
    payload["steps"][0]["url"] = "https://evil.example/x"
    db.execute("UPDATE plans SET payload=? WHERE id=?", (json.dumps(payload), plan.id)); db.commit(); db.close()
    with pytest.raises(PlanError):
        confirm(src, plan.id, plan.count, plan.digest, http_post=post)
    assert unsub_server.requests == []


def test_spam_like_sender_is_never_unsubscribed(demo_raws, unsub_server, post):
    src, _ = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe", [SPAM])
    assert plan.steps[0].blocked and "Block" in plan.steps[0].blocked
    assert plan.count == 0
    with pytest.raises(PlanError):
        confirm(src, plan.id, plan.count, plan.digest, http_post=post)
    assert unsub_server.requests == []


def test_keep_list_sender_is_never_touched(demo_raws):
    src, fake = gmail(demo_raws)
    tracker.keep_add("@shopmart.example")
    for action in ("unsubscribe", "trash", "block"):
        plan = build_plan(src, action, [NEWS])
        assert "keep-list" in plan.steps[0].blocked
    assert fake.mutating_calls() == []


def test_domain_mismatch_needs_explicit_acknowledgement(demo_raws):
    src, _ = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe", [MISMATCH])
    assert "or a known email provider" in plan.steps[0].blocked and plan.steps[0].ack == "sender"
    plan = build_plan(src, "unsubscribe", [MISMATCH], {"acknowledged": [MISMATCH]})
    assert not plan.steps[0].blocked and plan.steps[0].warnings


def test_missing_scope_blocks_confirmation(demo_raws):
    src, fake = gmail(demo_raws, scopes=(READONLY,))
    plan = build_plan(src, "trash", [NEWS])
    assert plan.steps[0].permission
    with pytest.raises(PlanError, match="permission"):
        confirm(src, plan.id, plan.count, plan.digest)
    assert fake.mutating_calls() == []


def test_dry_run_changes_nothing_but_logs(demo_raws, unsub_server, post):
    src, fake = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe", [NEWS], {"also_trash": True}, dry_run=True)
    result = confirm(src, plan.id, plan.count, plan.digest, http_post=post)
    assert result["dry_run"] and result["ok"] == 1
    assert unsub_server.requests == [] and fake.mutating_calls() == []
    assert tracker.audit_entries("me")[0]["dry_run"] == 1
    assert tracker.get_sender("me", NEWS)["status"] == "active"


def test_one_click_post_is_rfc8058_and_carries_no_credentials(demo_raws, unsub_server, post):
    src, _ = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe", [NEWS])
    confirm(src, plan.id, plan.count, plan.digest, http_post=post)
    req = unsub_server.posts()[0]
    assert req["body"] == b"List-Unsubscribe=One-Click"
    assert req["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert not {"Cookie", "Authorization", "Referer"} & set(req["headers"])
    assert req["host"].startswith("unsubscribe.shopmart.example")
    assert tracker.get_sender("me", NEWS)["status"] == "unsubscribed"


def test_mailto_sends_from_gmail_and_trash_has_undo(demo_raws, post):
    src, fake = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe", [MAILTO], {"also_trash": True})
    confirm(src, plan.id, plan.count, plan.digest, http_post=post)
    assert fake.sent == [{"to": "leave@recipebox.example", "subject": "unsubscribe"}]
    trashed = [m for m in fake.store.values() if "TRASH" in m["labelIds"]]
    assert len(trashed) == 30
    entry = next(e for e in tracker.audit_entries("me") if e["action"] == "trash")
    undo = build_plan(src, "undo", [entry["id"]])
    assert undo.count == 1 and fake.store[trashed[0]["id"]]["labelIds"].count("TRASH") == 1
    confirm(src, undo.id, undo.count, undo.digest)
    assert not [m for m in fake.store.values() if "TRASH" in m["labelIds"]]


def test_block_creates_filter_and_undo_deletes_it(demo_raws):
    src, fake = gmail(demo_raws)
    plan = build_plan(src, "block", [SPAM], {"block_mode": "trash"})
    confirm(src, plan.id, plan.count, plan.digest)
    assert list(fake.filters.values()) == [{"criteria": {"from": SPAM}, "action": {"addLabelIds": ["TRASH"]}}]
    entry = tracker.audit_entries("me")[0]
    undo = build_plan(src, "undo", [entry["id"]])
    confirm(src, undo.id, undo.count, undo.digest)
    assert fake.filters == {}
    assert tracker.get_sender("me", SPAM)["status"] == "active"


def test_network_failure_mid_plan_reports_each_sender(demo_raws, unsub_server, post):
    src, _ = gmail(demo_raws)
    unsub_server.mode["unsubscribe.weeklybyte.example"] = (500, None)
    plan = build_plan(src, "unsubscribe", [NEWS, NEWS2, "digest@forum.example"])

    calls = []

    def flaky(url):
        calls.append(url)
        if "forum" in url:
            raise OSError("connection reset")
        return post(url)
    result = confirm(src, plan.id, plan.count, plan.digest, http_post=flaky)
    by = {r["target"]: r for r in result["results"]}
    assert by[NEWS]["ok"] and not by[NEWS2]["ok"] and not by["digest@forum.example"]["ok"]
    assert "HTTP 500" in by[NEWS2]["detail"] and "connection reset" in by["digest@forum.example"]["detail"]
    assert len(calls) == 3 and len(tracker.audit_entries("me")) == 3
    assert tracker.get_sender("me", NEWS2)["status"] == "active"   # failure is not recorded as success


def test_link_method_opens_browser_only_after_confirm(demo_raws):
    src, _ = gmail(demo_raws)
    opened = []
    plan = build_plan(src, "unsubscribe", [LINK])
    assert opened == []
    result = confirm(src, plan.id, plan.count, plan.digest, opener=opened.append)
    assert opened == [plan.steps[0].url] and result["results"][0]["needs_user"]


# ---------------------------------------------------------------- one-click client
def test_one_click_refuses_unsafe_urls(unsub_server):
    ctx = unsub_server.client_ctx
    for url, why in [("http://plain.example/u", "HTTPS"), ("https://user:pw@plain.example/u", "password"),
                     ("https://bit.ly/x", "shortener"), ("https://xn--pypal-4ve.example/u", "punycode"),
                     ("https://10.0.0.1/u", "IP")]:
        r = oneclick.post(url, context=ctx)
        assert not r.ok and why.lower() in r.detail.lower(), (url, r.detail)
    # *.example resolves to 127.0.0.1 in tests: refused unless private hosts are explicitly allowed
    r = oneclick.post(f"https://plain.example:{unsub_server.port}/u", context=ctx)
    assert not r.ok and "private" in r.detail
    assert unsub_server.requests == []


def test_one_click_redirects_only_to_safe_https(unsub_server):
    ctx = unsub_server.client_ctx
    unsub_server.mode["redirect.example"] = (302, "http://plain.example/next")
    r = oneclick.post(f"https://redirect.example:{unsub_server.port}/u", context=ctx, allow_private=True)
    assert not r.ok and "after redirect" in r.detail
    unsub_server.mode["redirect.example"] = (307, f"https://plain.example:{unsub_server.port}/done")
    r = oneclick.post(f"https://redirect.example:{unsub_server.port}/u", context=ctx, allow_private=True)
    assert r.ok and unsub_server.requests[-1]["method"] == "POST" and unsub_server.requests[-1]["path"] == "/done"


def test_one_click_timeout_and_tls_failure_are_reported(unsub_server):
    import ssl
    r = oneclick.post(f"https://plain.example:{unsub_server.port}/u", context=ssl.create_default_context(), allow_private=True)
    assert not r.ok and "network error" in r.detail  # untrusted certificate
    r = oneclick.post("https://fail.example:9/u", context=unsub_server.client_ctx, allow_private=True, timeout=2)
    assert not r.ok and "network error" in r.detail


def test_demo_source_actions_are_simulated():
    src = DemoSource()
    run_scan(src, region="US")
    plan = build_plan(src, "trash", [NEWS])
    confirm(src, plan.id, plan.count, plan.digest)
    assert src.simulated == [("trash", (90,))]


def test_demo_mismatch_link_and_mailto_show_one_warning(demo_raws):
    src, _ = gmail(demo_raws)
    from clawback import tracker
    s = {x["address"]: x for x in tracker.list_senders(src.account)}[MISMATCH]
    assert s["flags"] == ["unsubscribe goes to clickmetrics.example, which isn't the sender (webinarhub.example) "
                          "or a known email provider"]
    assert s["category"] == "newsletter"


def test_phishing_signals_named_in_the_readme_are_raised():
    # README "Phishing checks": DMARC/DKIM/SPF failures, display-name spoofing
    # (another address, or a brand the mail is not from) and replies elsewhere.
    from clawback.extract import MessageInfo
    from clawback.senders import spam_signals

    def says(**kw):
        base = dict(source_id="1", from_addr="alerts@secure-login.example", from_name="", domain="secure-login.example",
                    subject="Your account", date=datetime(2026, 10, 1, tzinfo=timezone.utc))
        return " | ".join(text for _, text in spam_signals(MessageInfo(**{**base, **kw})))

    assert "fails DMARC" in says(auth_results="mx.example; dmarc=fail header.from=secure-login.example")
    assert "DKIM fail" in says(auth_results="mx.example; dkim=fail header.d=secure-login.example")
    assert "display name shows a different address (service@paypal.com)" in says(from_name="service@paypal.com")
    assert "name mentions Paypal but mail is from secure-login.example" in says(from_name="PayPal Security")
    assert "replies go to a different domain" in says(reply_to="collect@elsewhere.example")
    assert says(from_name="Secure Login", domain="secure-login.example") == ""   # an ordinary sender: nothing
