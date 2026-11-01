"""Web app: bug 9 (Host / CSRF / Origin), API double confirmation, end-to-end flow
scan -> select -> plan -> typed confirm -> one-click POST -> audit -> savings."""
import http.client
import json
import threading
import time

import pytest

from helpers import write_mbox
from clawback import oneclick
from clawback.demo import DemoSource, mailbox
from clawback.web.server import App, make_server

NEWS = "deals@shopmart.example"


@pytest.fixture
def web(unsub_server):
    app = App(http_post=lambda u: oneclick.post(u, context=unsub_server.client_ctx, allow_private=True))
    httpd, app = make_server(app, 0)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()

    class Client:
        port = app.port
        csrf = app.csrf

        def req(self, method, path, body=None, headers=None, raw=None):
            c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
            hdrs = {"Host": f"127.0.0.1:{app.port}"}
            if body is not None:
                raw = json.dumps(body).encode()
                hdrs["Content-Type"] = "application/json"
            hdrs.update(headers or {})
            c.request(method, path, body=raw, headers=hdrs)
            r = c.getresponse()
            data = r.read()
            c.close()
            ctype = r.getheader("Content-Type", "")
            return r.status, (json.loads(data) if "json" in ctype else data.decode("utf-8", "replace")), r

        def post(self, path, body, **kw):
            h = {"X-Clawback-CSRF": app.csrf}
            h.update(kw.pop("headers", {}))
            return self.req("POST", path, body, headers=h, **kw)

        def get(self, path):
            return self.req("GET", path)

        def scan(self):
            st, data, _ = self.post("/api/scan", {"limit": 10000})
            assert st == 200, data
            for _ in range(200):
                s = self.get("/api/scan")[1]
                if s["status"] != "running":
                    return s
                time.sleep(0.05)
            raise AssertionError("scan did not finish")

    client = Client()
    client.app = app
    yield client
    httpd.shutdown()
    httpd.server_close()


# ------------------------------------------------------------------ bug 9
def test_wrong_host_header_is_refused(web):
    for host in ("evil.example", f"evil.example:{web.port}", "127.0.0.1", f"192.168.1.5:{web.port}", ""):
        status, body, _ = web.req("GET", "/api/state", headers={"Host": host})
        assert status == 421, host
    status, _, _ = web.req("POST", "/api/plan", {"action": "unsubscribe", "targets": [NEWS]},
                           headers={"Host": "evil.example", "X-Clawback-CSRF": web.csrf})
    assert status == 421
    assert web.req("GET", "/api/state", headers={"Host": f"localhost:{web.port}"})[0] == 200


def test_post_needs_csrf_json_and_same_origin(web):
    web.app.add_source(DemoSource())
    assert web.req("POST", "/api/scan", {"limit": 5})[0] == 403                                  # no token
    assert web.req("POST", "/api/scan", {"limit": 5}, headers={"X-Clawback-CSRF": "x" * 43})[0] == 403
    st, _, _ = web.req("POST", "/api/scan", raw=b"limit=5", headers={"X-Clawback-CSRF": web.csrf,
                                                                       "Content-Type": "application/x-www-form-urlencoded"})
    assert st == 415                                                                             # form posts refused
    st, _, _ = web.post("/api/scan", {"limit": 5}, headers={"Origin": "https://evil.example"})
    assert st == 403


def test_page_headers_and_token(web):
    status, page, r = web.get("/")
    assert status == 200 and web.csrf in page
    csp = r.getheader("Content-Security-Policy")
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.getheader("Access-Control-Allow-Origin") is None
    assert r.getheader("X-Frame-Options") == "DENY"
    assert web.get("/static/../server.py")[0] == 404
    status, strings, _ = web.get("/static/i18n/en.json")
    assert status == 200 and strings["app.tagline"].startswith("Claw back")


# ------------------------------------------------------------------ double confirmation over HTTP
def test_api_cannot_act_without_both_steps(web, unsub_server):
    web.app.add_source(DemoSource(base_url=unsub_server.base_url))
    web.scan()
    st, err, _ = web.post("/api/confirm", {"plan_id": "made-up", "count": 1, "digest": "0" * 64})
    assert st == 409
    st, plan, _ = web.post("/api/plan", {"action": "unsubscribe", "targets": [NEWS]})
    assert st == 200 and plan["count"] == 1 and unsub_server.requests == []
    for body in ({"plan_id": plan["id"]}, {"plan_id": plan["id"], "count": "yes", "digest": plan["digest"]},
                 {"plan_id": plan["id"], "count": plan["count"]}, {"plan_id": plan["id"], "count": plan["count"], "digest": "f" * 64}):
        assert web.post("/api/confirm", body)[0] == 409
    assert unsub_server.requests == []
    assert web.get("/api/audit")[1]["entries"] == []
    # A cancelled review can no longer be confirmed.
    web.post("/api/plan/cancel", {"plan_id": plan["id"]})
    assert web.post("/api/confirm", {"plan_id": plan["id"], "count": plan["count"], "digest": plan["digest"]})[0] == 409
    assert unsub_server.requests == []


def test_end_to_end_unsubscribe_and_savings(web, unsub_server):
    web.app.add_source(DemoSource(base_url=unsub_server.base_url))
    result = web.scan()
    assert result["status"] == "done" and result["result"]["messages"] > 400
    news = web.get("/api/senders?category=newsletter")[1]["senders"]
    assert any(s["address"] == NEWS and s["message_count"] == 90 for s in news)
    spam = web.get("/api/senders?category=spam")[1]["senders"]
    assert len(spam) == 3
    # Step 1 then step 2
    _, plan, _ = web.post("/api/plan", {"action": "unsubscribe", "targets": [NEWS, "news@weeklybyte.example"]})
    st, res, _ = web.post("/api/confirm", {"plan_id": plan["id"], "count": plan["count"], "digest": plan["digest"]})
    assert st == 200 and res["ok"] == 2
    posts = unsub_server.posts()
    assert len(posts) == 2 and all(p["body"] == b"List-Unsubscribe=One-Click" for p in posts)
    audit = web.get("/api/audit")[1]["entries"]
    assert {e["target"] for e in audit} == {NEWS, "news@weeklybyte.example"} and all(e["ok"] for e in audit)
    # Paid subscription: open the cancel page (two steps), then mark cancelled locally.
    subs = web.get("/api/subscriptions")[1]
    s = next(x for x in subs["subscriptions"] if x["vendor"].startswith("Streamora"))
    assert s["amount_display"] == "US$15.49"
    assert {p["currency"] for p in subs["paying"]} >= {"USD", "EUR", "INR", "JPY"}
    assert web.post("/api/subscription/status", {"id": s["id"], "status": "cancelled"})[0] == 200
    sv = web.get("/api/savings")[1]
    usd = next(m for m in sv["money"] if m["currency"] == "USD")
    assert usd["yearly"] == 185.88 and sv["emails"]["senders_silenced"] == 2
    st, svg, r = web.get("/api/card.svg")
    assert st == 200 and "US$185.88" in svg and "2 senders silenced" in svg
    for private in ("shopmart", "weeklybyte", "Streamora", "@"):
        assert private not in svg
    # Proof tab data exists for both unsubscribed senders
    pr = web.get("/api/proof")[1]["senders"]
    assert {p["address"] for p in pr} == {NEWS, "news@weeklybyte.example"}


def test_spam_sender_cannot_be_unsubscribed_through_api(web, unsub_server):
    web.app.add_source(DemoSource(base_url=unsub_server.base_url))
    web.scan()
    _, plan, _ = web.post("/api/plan", {"action": "unsubscribe", "targets": ["team@moonshot.example"]})
    assert plan["runnable"] == 0 and plan["steps"][0]["blocked"]
    assert web.post("/api/confirm", {"plan_id": plan["id"], "count": plan["count"], "digest": plan["digest"]})[0] == 409
    assert unsub_server.requests == []


def test_upload_takeout_streams_to_private_folder_and_scans(web, tmp_path, isolated_home):
    data = write_mbox(tmp_path / "t.mbox", mailbox()).read_bytes()
    st, res, _ = web.post("/api/upload?name=takeout.mbox", None, raw=data,
                          headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(data))})
    assert st == 200, res
    assert res["path"].startswith(str(isolated_home))
    assert web.scan()["status"] == "done"
    assert web.get("/api/state")[1]["sources"][res["account"]]["kind"] == "mbox"
    st, _, _ = web.post("/api/upload?name=evil.exe", None, raw=b"x", headers={"Content-Type": "application/octet-stream"})
    assert st == 400


def test_settings_and_keep_list_over_api(web):
    st, s, _ = web.post("/api/settings", {"home_currency": "INR", "region": "IN"})
    assert st == 200 and s["home_currency"] == "INR"
    assert web.post("/api/settings", {"home_currency": "XXX"})[0] == 400
    assert web.post("/api/keep", {"pattern": "bank.example"})[1]["patterns"] == ["@bank.example"]


def test_scan_progress_endpoints_are_protected_and_report_progress(web):
    st, _, _ = web.req("GET", "/api/scan", headers={"Host": "evil.example"})
    assert st == 421
    for path in ("/api/scan", "/api/scan/pause", "/api/scan/resume", "/api/scan/cancel"):
        st, _, _ = web.req("POST", path, {"since": "3m"})
        assert st == 403, path              # no CSRF header
    web.post("/api/connect", {"kind": "demo"})
    st, data, _ = web.post("/api/scan", {"since": "not a date"})
    assert st == 400 and "Scan window" in data["error"]
    done = web.scan()
    assert done["status"] == "done" and done["progress"]["done"] == done["progress"]["total"]
    assert done["progress"]["senders"] > 0 and done["result"]["partial"] is False
