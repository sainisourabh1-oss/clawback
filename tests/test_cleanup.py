"""The simple cleanup flow (2026-10-29 redesign): Delete all / Unsubscribe +
delete all per sender, confirmed by a click that quotes the count it showed
(nothing typed), Trash only with Undo, one Google/Microsoft sign-in for every
permission, and imports that never offer to delete."""
import json

import pytest

from fakes import FakeGmail
from clawback import actions, tracker
from clawback.actions import PlanError, build_plan, confirm
from clawback.scan import run_scan
from clawback.sources import gmail as gmail_mod
from clawback.sources.base import OfflineSource
from clawback.sources.gmail import MODIFY, READONLY, SEND, SETTINGS, SIGN_IN_SCOPES, GmailSource
from clawback.sources.mbox import MboxSource

NEWS = "deals@shopmart.example"          # 90 emails, one-click unsubscribe
MAILTO = "hello@recipebox.example"       # 30 emails, mailto unsubscribe
SPAM = "team@moonshot.example"


def gmail(raws, scopes=(MODIFY, SETTINGS)):
    fake = FakeGmail(raws)
    src = GmailSource("me", service=fake, granted=set(scopes))
    run_scan(src, limit=10000, region="US")
    return src, fake


def in_trash(fake):
    return [m for m in fake.store.values() if "TRASH" in m["labelIds"]]


# ---------------------------------------------------------------- delete all = Trash, with Undo
def test_delete_all_moves_to_trash_only_and_undo_restores(demo_raws):
    src, fake = gmail(demo_raws)
    plan = build_plan(src, "trash", [NEWS])
    assert plan.count == 1 and plan.to_dict()["emails"] == 90
    assert fake.mutating_calls() == [] and in_trash(fake) == []          # click 1 changes nothing
    res = confirm(src, plan.id, plan.count, plan.digest)
    assert res["ok"] == 1 and len(in_trash(fake)) == 90
    # Trash, never a permanent delete: the fake has no messages.delete call at all
    assert not any("delete" in c and "filter" not in c for c in fake.mutating_calls())
    s = tracker.get_sender("me", NEWS)
    assert s["trashed_count"] == 90 and s["trashed_at"]
    undo = build_plan(src, "undo", [res["results"][0]["entry"]])
    confirm(src, undo.id, undo.count, undo.digest)
    assert in_trash(fake) == []
    assert tracker.get_sender("me", NEWS)["trashed_count"] == 0           # no longer shown as deleted


def test_unsubscribe_and_delete_all_in_one_action(demo_raws, unsub_server):
    from clawback import oneclick
    post = lambda url: oneclick.post(url, context=unsub_server.client_ctx, allow_private=True)   # noqa: E731
    src, fake = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe_trash", [NEWS, MAILTO], {"also_trash": True, "also_block": True})
    d = plan.to_dict()
    assert d["count"] == 2 and d["emails"] == 120 and d["options"] == {}      # extra "also_" options ignored
    assert all(any("Trash" in e for e in s.effects) for s in plan.steps)
    assert unsub_server.requests == [] and fake.mutating_calls() == []
    res = confirm(src, plan.id, 2, plan.digest, http_post=post)
    assert res["ok"] == 2 and len(unsub_server.posts()) == 1 and fake.sent[0]["to"] == "leave@recipebox.example"
    assert len(in_trash(fake)) == 120
    for addr in (NEWS, MAILTO):
        assert tracker.get_sender("me", addr)["status"] == "unsubscribed"
    assert fake.filters == {}                                               # no block was asked for
    # Undo brings the emails back; the unsubscribe itself stays (it cannot be taken back)
    for r in res["results"]:
        u = build_plan(src, "undo", [r["entry"]])
        assert u.steps[0].effects == ["Restore emails from Trash"]
        confirm(src, u.id, u.count, u.digest)
    assert in_trash(fake) == [] and tracker.get_sender("me", NEWS)["status"] == "unsubscribed"


def test_unsubscribe_and_delete_on_a_spam_sender_only_deletes(demo_raws, unsub_server):
    src, fake = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe_trash", [SPAM])
    step = plan.steps[0]
    assert not step.blocked and step.method == "trash" and step.data["trash_only"]
    assert "looks like spam" in step.notes[0] and "only deleted" in step.notes[0]
    confirm(src, plan.id, 1, plan.digest)
    assert unsub_server.requests == [] and len(in_trash(fake)) == step.emails_affected > 0
    assert tracker.get_sender("me", SPAM)["status"] == "active"           # not "unsubscribed": it was not


def test_kept_or_important_senders_are_never_deleted(demo_raws):
    src, fake = gmail(demo_raws)
    tracker.keep_add("@shopmart.example")
    for action in ("trash", "unsubscribe_trash"):
        plan = build_plan(src, action, [NEWS])
        assert "keep-list" in plan.steps[0].blocked and plan.count == 0
        with pytest.raises(PlanError):
            confirm(src, plan.id, 0, plan.digest)
    assert fake.mutating_calls() == []


def test_unknown_link_domain_needs_the_tick_and_it_reaches_the_ui(demo_raws):
    """The "I checked this sender" tick was dropped on its way to the web app
    (ack was not a dataclass field); now it is part of the plan JSON."""
    src, _ = gmail(demo_raws)
    plan = build_plan(src, "unsubscribe_trash", ["events@webinarhub.example"]).to_dict()
    assert plan["steps"][0]["ack"] == "sender" and plan["steps"][0]["blocked"] and plan["count"] == 0
    plan = build_plan(src, "unsubscribe_trash", ["events@webinarhub.example"],
                      {"acknowledged": ["events@webinarhub.example"]}).to_dict()
    assert plan["count"] == 1 and plan["steps"][0]["data"]["also_trash"]


# ---------------------------------------------------------------- one sign-in for everything
def test_every_google_sign_in_asks_for_the_full_set_once(tmp_path, monkeypatch):
    asked = []

    class Creds:
        valid = True
        scopes = sorted(SIGN_IN_SCOPES)

        def to_json(self):   # what google-auth writes: reloadable by Credentials.from_authorized_user_file
            return json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                               "token_uri": "https://oauth2.googleapis.com/token", "scopes": self.scopes,
                               "expiry": "2999-01-01T00:00:00Z"})
    monkeypatch.setattr(GmailSource, "_consent", lambda self, flow, scopes: asked.append(set(scopes)) or Creds())
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}", encoding="utf-8")
    src = GmailSource("g", token_file=tmp_path / "token.json", client_secret=secret)
    src._credentials({READONLY})                      # what a scan asks for
    assert asked == [{MODIFY, SETTINGS}]              # ...but the sign-in asks for everything, once
    assert gmail_mod.FULL_MAIL not in asked[0]        # never the "delete for good" scope
    assert src.can_clean_up() and src.describe()["needs_signin"] is False
    assert not any(src.missing_permission(c) for c in ("mailto", "send", "trash", "archive", "spam", "block"))
    src._credentials({READONLY})
    assert len(asked) == 1                            # the saved sign-in is reused: no second consent


def test_a_read_only_token_still_scans_and_asks_once_to_sign_in_again(tmp_path, monkeypatch, demo_raws):
    asked = []
    monkeypatch.setattr(GmailSource, "_consent", lambda self, flow, scopes: asked.append(set(scopes)))
    src = GmailSource("me", service=FakeGmail(demo_raws), granted={READONLY})
    run_scan(src, limit=10000, region="US")           # scanning needs nothing new
    assert asked == [] and src.describe()["needs_signin"] is True and src.describe()["can_delete"] is True
    plan = build_plan(src, "trash", [NEWS])
    assert "only allows reading" in plan.steps[0].permission and "will not be asked again" in plan.steps[0].permission
    assert plan.to_dict()["permissions"] == [gmail_mod.REAUTH_WHY]
    with pytest.raises(PlanError, match="permission"):
        confirm(src, plan.id, plan.count, plan.digest)
    calls = []
    monkeypatch.setattr(GmailSource, "_credentials", lambda self, scopes, force_consent=False: calls.append((set(scopes), force_consent)))
    src.grant("trash")
    assert calls == [({MODIFY, SETTINGS}, True)]      # whichever action asked, the full set in one consent


def test_old_send_and_modify_tokens_count_as_covered():
    assert gmail_mod.covered({MODIFY}, {READONLY, SEND})
    assert not gmail_mod.covered({READONLY}, {MODIFY})
    assert not gmail_mod.covered({MODIFY}, {SETTINGS})
    src = GmailSource("g", service=FakeGmail([]), granted={READONLY, SEND, MODIFY, SETTINGS})
    assert src.can_clean_up()


# ---------------------------------------------------------------- imports cannot delete
def test_imports_never_offer_delete_and_say_why(tmp_path, demo_raws):
    from helpers import write_mbox
    from clawback.demo import mailbox
    src = MboxSource(write_mbox(tmp_path / "t.mbox", mailbox()))
    run_scan(src, limit=None, region="US")
    d = src.describe()
    assert d["can_delete"] is False and d["needs_signin"] is False
    for s in (src, OfflineSource(src.account)):
        for action in ("trash", "unsubscribe_trash"):
            plan = build_plan(s, action, [NEWS])
            assert plan.count == 0 and "imported from a file" in plan.steps[0].blocked


def test_api_plan_and_confirm_use_the_count_never_a_phrase(demo_raws):
    """Over HTTP: /api/plan returns count + emails for the confirm button;
    /api/confirm with the old typed phrase field does nothing."""
    import threading
    from clawback.demo import DemoSource
    from clawback.web.server import App, make_server
    import http.client
    httpd, app = make_server(App(), 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        app.add_source(DemoSource())

        def post(path, body):
            c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
            c.request("POST", path, body=json.dumps(body), headers={"Host": f"127.0.0.1:{app.port}",
                      "Content-Type": "application/json", "X-Clawback-CSRF": app.csrf})
            r = c.getresponse()
            out = r.status, json.loads(r.read())
            c.close()
            return out
        post("/api/scan", {"limit": 10000})
        import time
        for _ in range(400):
            if app.job.snapshot()["status"] != "running":
                break
            time.sleep(0.05)
        st, plan = post("/api/plan", {"action": "trash", "targets": [NEWS]})
        assert st == 200 and plan["count"] == 1 and plan["emails"] == 90 and "phrase" not in plan
        st, _ = post("/api/confirm", {"plan_id": plan["id"], "phrase": "TRASH 1", "digest": plan["digest"]})
        assert st == 409 and tracker.audit_entries() == []
        st, res = post("/api/confirm", {"plan_id": plan["id"], "count": 1, "digest": plan["digest"]})
        assert st == 200 and res["ok"] == 1 and res["results"][0]["trashed"] == 90
        assert app.sources[app.active].simulated == [("trash", (90,))]
    finally:
        httpd.shutdown()
