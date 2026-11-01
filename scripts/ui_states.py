"""Edge-case states of the Clawback web UI, in headless Edge (Playwright).

scripts/ui_smoke.py walks the happy path. This script puts the real server
into the states a user can also end up in and checks what they see:

  * an empty mailbox (a real import of an empty .mbox file)
  * a scan running, then paused, then failed, then stopped early
  * very long sender names and paid subscriptions in many currencies
  * a read-only Google sign-in (asks once to sign in again, in place, no pop-up) and an .mbox import (no Delete)
  * the scan options as an inline panel (never a dialog)
  * each at 1280 px light (white), 1280 px dark (black) and 390 px phone width

For every state: no console errors, no request to any other server, no
horizontal scroll, and the expected words on screen. Screenshots go to the
output folder (state-*.png).
Usage: python scripts/ui_states.py . <outdir>   (needs: pip install playwright)
"""
import datetime
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

# Progress lines contain currency symbols; keep them printable when output is piped on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

repo, outdir = Path(sys.argv[1]), Path(sys.argv[2])
outdir.mkdir(parents=True, exist_ok=True)
home = Path(tempfile.mkdtemp(prefix="clawback-states-"))
os.environ["CLAWBACK_HOME"] = str(home)
sys.path.insert(0, str(repo))

from clawback.web.server import App, make_server, warm_up  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

app = App()
httpd, app = make_server(app, 0)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
warm_up()
URL = f"http://127.0.0.1:{app.port}/"
log, errors, foreign = [], [], []


def step(msg):
    log.append(msg)
    print("STEP", msg, flush=True)


def wait_scan():
    for _ in range(300):
        if app.job.snapshot()["status"] != "running":
            return app.job.snapshot()
        time.sleep(0.1)
    raise AssertionError("scan did not finish")


def settle(pg):
    # (polled with evaluate(): wait_for_function() needs eval, which the page's CSP blocks)
    for _ in range(160):
        if pg.evaluate("!document.querySelector('#toast.show') && document.getAnimations().every("
                       "a => a.playState !== 'running' || a.effect.getComputedTiming().iterations === Infinity)"):
            break
        pg.wait_for_timeout(50)
    else:
        raise AssertionError("animations did not settle within 8 s")
    pg.wait_for_timeout(50)


def no_hscroll(pg, where):
    width = pg.evaluate("document.documentElement.scrollWidth")
    view = pg.evaluate("window.innerWidth")
    assert width <= view + 2, f"{where}: horizontal scroll ({width} > {view})"


LONG_NAME = ("The Extraordinarily Long-Named International Weekly Digest of Absolutely Everything "
             "Happening in Gardening, Cooking and Travel")
LONG_ADDR = "newsletter-notifications-and-updates.weekly.digest@subdomain.very-long-company-name-example.example"
CURRENCIES = [("USD", "US$9.99"), ("EUR", "€12.50"), ("GBP", "£7.99"), ("INR", "₹149"), ("JPY", "JP¥1,200"),
              ("AUD", "A$19.99"), ("CAD", "CA$11.99"), ("BRL", "R$34.90"), ("CHF", "CHF 15.00")]


def long_and_many(route):
    """Serve real data with a very long sender and many currencies mixed in."""
    resp = route.fetch()
    data = resp.json()
    path = route.request.url.split("/api/", 1)[1]
    if path.startswith("senders?category=newsletter") and data["senders"]:
        data["senders"][0]["display_name"] = LONG_NAME
        data["senders"][0]["address"] = LONG_ADDR
    elif path.startswith("subscriptions"):
        data["paying"] = [{"currency": c, "display": d, "monthly": 1} for c, d in CURRENCIES]
        if data["subscriptions"]:
            data["subscriptions"][0]["vendor"] = LONG_NAME
    elif path.startswith("savings"):
        data["money"] = [{"currency": c, "yearly": 1, "so_far": 1, "display": {"yearly": d, "so_far": d}} for c, d in CURRENCIES]
    route.fulfill(response=resp, body=json.dumps(data), headers={**resp.headers, "content-type": "application/json"})


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)

    def context(width=1280, height=900, scheme="light", mobile=False, **extra):
        c = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, is_mobile=mobile,
                                **{"locale": "en-GB", **extra})
        c.on("request", lambda r: foreign.append(r.url)
             if r.url.split(":", 1)[0] in {"http", "https", "ws", "wss"} and not r.url.startswith(URL) else None)
        pg = c.new_page()
        pg.on("console", lambda m: errors.append(f"{width}px console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        return c, pg

    views = [("desktop", dict()), ("dark", dict(scheme="dark")), ("mobile", dict(width=390, height=844, mobile=True))]

    # 1. Empty mailbox: a real import of an empty .mbox file.
    empty = home / "empty.mbox"
    empty.write_bytes(b"")
    app.connect({"kind": "mbox", "path": str(empty)})
    app.start_scan(limit=100000)
    snap = wait_scan()
    assert snap["status"] == "done", snap
    for name, kw in views:
        c, pg = context(**kw)
        pg.goto(URL)
        expect(pg.get_by_role("tab", name="Newsletters & ads")).to_be_visible()
        expect(pg.get_by_text("No newsletters or marketing senders found")).to_be_visible()
        expect(pg.locator(".glance")).to_have_count(0)      # no row of zeros
        expect(pg.locator("#banner")).to_be_hidden()        # no savings claim, nothing to read past
        no_hscroll(pg, "empty " + name)
        # the two themes are pure white and pure black, page and browser chrome alike
        want = "rgb(0, 0, 0)" if kw.get("scheme") == "dark" else "rgb(255, 255, 255)"
        got = pg.evaluate("getComputedStyle(document.documentElement).backgroundColor")
        assert got == want, (name, got)
        settle(pg)
        pg.screenshot(path=str(outdir / f"state-empty-{name}.png"), full_page=True)
        pg.get_by_role("tab", name="Paid").click()
        expect(pg.get_by_text("No paid subscriptions found")).to_be_visible()
        for tab, words in (("Did it work?", "Nothing to check yet"), ("History", "No actions yet")):
            pg.get_by_role("button", name="More", exact=True).click()
            pg.locator(".menu:not([hidden])").get_by_role("button", name=tab).click()
            expect(pg.get_by_text(words)).to_be_visible()
        c.close()
    step(f"empty mailbox ({snap['result']['messages']} emails): friendly empty states on 4 tabs (two reached through More), "
         "no glance line of zeros, no savings banner; page rgb(255, 255, 255) light, rgb(0, 0, 0) dark; desktop/dark/390 px")

    # The demo gives the next states real data behind them.
    app.connect({"kind": "demo"})
    app.start_scan(limit=100000)
    wait_scan()

    # Dates: a calendar date ("2026-10-27") is the same day in every time zone,
    # and the format follows the browser's language without ever being numeric-only.
    up = None
    for tz, loc in (("America/Los_Angeles", "en-GB"), ("Pacific/Kiritimati", "en-GB"), ("America/Los_Angeles", "en-US")):
        c, pg = context(timezone_id=tz, locale=loc)
        pg.goto(URL)
        expect(pg.get_by_role("tab", name="Paid")).to_be_visible()
        up = pg.evaluate("fetch('/api/insights', {headers: {'X-Clawback-CSRF': document.querySelector('meta[name=csrf-token]').content}})"
                         ".then(r => r.json())")["upcoming"][0]
        d = datetime.date.fromisoformat(up["date"][:10])
        want = (f"{d.day}NBSP{d:%b}NBSP{d.year}" if loc == "en-GB" else f"{d:%b}NBSP{d.day},NBSP{d.year}").replace("NBSP", chr(0xa0))
        pg.locator("#heads-up summary").click()
        heads = pg.locator("#heads-up").inner_text()
        assert want in heads, (tz, loc, want, heads)
        c.close()
    step(f"dates: {up['date']} shows as the same calendar day in Los Angeles and Kiritimati (en-GB '27 Oct 2026' style, en-US 'Oct 27, 2026' style)")

    # 2. Scan running, then paused (the job state the server reports while a scan runs).
    app.job.update(status="running", account="demo", done=1240, total=3000, paused=False, kind="gmail", hint="scan.hint.firstApi",
                   progress={"line": "1,240 of 3,000 emails read · about 6 min left"})
    for name, kw in views:
        c, pg = context(**kw)
        pg.goto(URL)
        expect(pg.get_by_role("heading", name="Scanning Demo mailbox")).to_be_visible()
        expect(pg.locator(".pct")).to_have_text("41%")
        expect(pg.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "41")
        expect(pg.get_by_text("Reading your mail")).to_be_visible()
        expect(pg.get_by_role("button", name="Pause")).to_be_visible()
        expect(pg.locator("#flow li[aria-current=step]")).to_have_text("Scan")
        no_hscroll(pg, "running " + name)
        pg.screenshot(path=str(outdir / f"state-scan-running-{name}.png"))
        app.job.update(paused=True, progress={"line": "Paused at 1,240 of 3,000 emails"})
        expect(pg.get_by_role("button", name="Resume")).to_be_visible(timeout=5000)
        expect(pg.locator(".scan-state .badge")).to_have_text("Paused")
        expect(pg.locator(".scan-card.paused")).to_have_count(1)
        pg.screenshot(path=str(outdir / f"state-scan-paused-{name}.png"))
        app.job.update(paused=False, progress={"line": "1,240 of 3,000 emails read · about 6 min left"})
        c.close()
    step("scan running: 41% with progressbar value, 'Reading your mail', Pause; paused: 'Paused' chip, Resume button; desktop/dark/390 px")

    # 2b. Partial results before any date is known (Gmail's priority pass, a
    # file import): a count, never a "…" placeholder; then the real date.
    preview = {"partial": True, "messages": 36, "scanned_back_to": None,
               "note": "Likely subscriptions and newsletters first, then everything else", "categories": {"newsletter": 1},
               "senders": [{"address": "news@shop.example", "name": "Shop News", "category": "newsletter", "count": 3,
                            "last_seen": "2026-10-20T10:00:00+00:00"}], "subscriptions": []}
    for name, kw in views:
        app.job.preview = dict(preview)
        app.job.update(preview_seq=1)
        c, pg = context(**kw)
        pg.goto(URL)
        head = pg.locator("p.badge.warn", has_text="Partial results")
        expect(head).to_contain_text("Partial results: 36 emails read so far", timeout=5000)
        expect(head).to_contain_text("read first")
        assert "…" not in pg.locator("#view").inner_text(), "placeholder ellipsis on the scan screen"
        no_hscroll(pg, "partial no date " + name)
        if name == "desktop":
            settle(pg)
            pg.screenshot(path=str(outdir / "state-partial-nodate-desktop.png"))
            app.job.preview = dict(preview, scanned_back_to="2026-10-01", note="", messages=480)
            app.job.update(preview_seq=2)
            expect(pg.locator("p.badge.warn")).to_contain_text("Partial: scanned back to 1 Oct 2026 (480 emails so far)",
                                                              timeout=5000)
            app.job.preview = dict(preview, note="", messages=12)
            app.job.update(preview_seq=3)
            expect(pg.locator("p.badge.warn")).to_have_text(
                "Partial results: 12 emails read so far. Results keep growing until the scan finishes.", timeout=5000)
        c.close()
    app.job.preview = None
    app.job.update(preview_seq=0)
    step("partial results with no date yet: '36 emails read so far' (+ why during the priority pass), no '…'; "
         "then 'scanned back to 1 Oct 2026'; a file import's count line; desktop/dark/390 px")

    # 3. Scan failed: the error is shown where the user looks, with a way forward.
    error = "IMAP login failed: [AUTHENTICATIONFAILED] Invalid credentials (Failure)"
    app.job.update(status="error", error=error, finished="2026-10-25T10:00:00+00:00", hint=None)
    for name, kw in views:
        c, pg = context(**kw)
        pg.goto(URL)
        alert = pg.locator("#scan-failed")
        expect(alert).to_be_visible()
        expect(alert).to_contain_text(error)
        expect(alert).to_contain_text("nothing in your mailbox was changed")
        expect(alert.get_by_role("button", name="Try again")).to_be_visible()
        expect(alert.get_by_role("button", name="Use another mailbox")).to_be_visible()
        no_hscroll(pg, "failed " + name)
        settle(pg)
        pg.screenshot(path=str(outdir / f"state-scan-failed-{name}.png"))
        if name == "desktop":
            alert.get_by_role("button", name="Use another mailbox").click()
            expect(pg.get_by_role("heading", name="Where is your email?")).to_be_visible()
            pg.get_by_role("button", name="Back to results").click()
            expect(pg.locator("#scan-failed")).to_be_visible()
        c.close()
    step("scan failed: alert with the error text, 'nothing in your mailbox was changed', Try again + Use another mailbox (and back); desktop/dark/390 px")

    # 4. Scan stopped early (Stop pressed): results marked as possibly incomplete.
    app.job.update(status="done", error=None, result={"messages": 800, "senders": 12, "partial": True, "reused": 0})
    c, pg = context()
    pg.goto(URL)
    expect(pg.locator("#scan-partial")).to_contain_text("may be incomplete")
    c.close()
    app.job.update(result={"messages": 470, "senders": 23, "partial": False, "reused": 0})
    step("scan stopped early: 'may be incomplete' notice on the results")

    # 4b. A scan killed in an earlier session (the process vanished, so it could
    # not say why): the reason recorded at the next start-up is shown.
    from clawback import tracker  # noqa: E402
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)).isoformat(timespec="seconds")
    sid = tracker.begin_scan(app.active, "gmail", old, None)
    tracker.close_abandoned()
    saved = dict(app.job.snapshot())
    app.job.update(status="idle", account=None)
    c, pg = context()
    pg.goto(URL)
    alert = pg.locator("#scan-failed")
    expect(alert).to_contain_text("Clawback was closed or stopped")
    expect(alert.get_by_role("button", name="Try again")).to_be_visible()
    settle(pg)
    pg.screenshot(path=str(outdir / "state-scan-killed-desktop.png"))
    c.close()
    db = tracker.connect()
    db.execute("DELETE FROM scans WHERE id=?", (sid,))
    db.commit()
    db.close()
    app.job.update(status=saved["status"], account=saved["account"])
    step("earlier session closed mid-scan: the failed-scan alert explains it (closed or stopped, nothing changed) with Try again")

    # 5. Very long names and many currencies.
    for name, kw in views:
        c, pg = context(**kw)
        pg.route("**/api/senders?category=newsletter", long_and_many)
        pg.route("**/api/subscriptions", long_and_many)
        pg.route("**/api/savings", long_and_many)
        pg.goto(URL)
        pg.get_by_role("tab", name="Newsletters & ads").click()
        expect(pg.get_by_text(LONG_NAME).first).to_be_visible()
        for _, d in CURRENCIES:
            expect(pg.locator(".glance")).to_contain_text(d)
            expect(pg.locator("#banner")).to_contain_text(d + "/yr")
        # never summed: one chip per currency, no total line (no rates entered)
        expect(pg.locator("#banner .money-chip")).to_have_count(len(CURRENCIES))
        assert "≈" not in pg.locator("#banner").inner_text()
        # savings came only from cancellations here: no "0 senders silenced" line
        assert "0 senders silenced" not in pg.locator("#banner").inner_text(), f"{name}: zero email line shown"
        no_hscroll(pg, "long/many " + name)
        table_w = pg.evaluate("document.querySelector('ul.senders').scrollWidth")
        panel_w = pg.evaluate("document.getElementById('panel').clientWidth")
        assert table_w <= panel_w + 2, f"{name}: sender list wider than its panel ({table_w} > {panel_w})"
        settle(pg)
        pg.screenshot(path=str(outdir / f"state-long-names-{name}.png"), full_page=(name != "mobile"))
        pg.get_by_role("tab", name="Paid").click()
        expect(pg.get_by_text(LONG_NAME).first).to_be_visible()
        no_hscroll(pg, "long paid " + name)
        c.close()
    step(f"long sender/vendor names wrap inside the list; {len(CURRENCIES)} currencies shown separately in the glance line and banner, no total; desktop/dark/390 px")

    # 6. A new account whose first scan failed (sign-in refused) never replaces
    # the account on screen, and says so there.
    from clawback.sources.base import AuthError, Source  # noqa: E402

    class Refused(Source):
        kind = "imap"

        def iter_messages(self, *a, **kw):
            raise AuthError("Mail Example refused the sign-in. Use an app password from https://mail.example/apppasswords. "
                            "Server said: [AUTHENTICATIONFAILED] Invalid credentials (Failure)")
            yield  # noqa: unreachable - makes this a generator

    keep = app.active
    app.add_source(Refused("new@mail.example"))
    app.start_scan(limit=10)
    wait_scan()
    assert app.active == keep and "new@mail.example" not in app.sources, (app.active, list(app.sources))
    for name, kw in views:
        c, pg = context(**kw)
        pg.goto(URL)
        alert = pg.locator("#connect-failed")
        expect(alert).to_contain_text("Couldn't scan new@mail.example")
        expect(alert).to_contain_text("still looking at Demo mailbox")
        # plain reason first with the fix as a real link; the server's words folded away
        expect(alert.get_by_role("link", name="mail.example/apppasswords")).to_have_attribute("href", "https://mail.example/apppasswords")
        tech = alert.locator("details.tech")
        expect(tech).not_to_have_attribute("open", "")
        expect(alert.locator(".err-reason")).not_to_contain_text("AUTHENTICATIONFAILED")
        expect(tech.locator("code")).to_be_hidden()
        tech.locator("summary").click()
        expect(tech.locator("code")).to_have_text("Server said: [AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        expect(pg.get_by_role("heading", name="Demo mailbox", exact=True)).to_be_visible()
        no_hscroll(pg, "dropped " + name)
        settle(pg)
        pg.screenshot(path=str(outdir / f"state-connect-failed-{name}.png"))
        c.close()
    step("failed first scan of a new account: back on the account with results, alert names the failed account, "
         "reason with a real link first, server text under Technical details; desktop/dark/390 px")

    # 7. Connected but never scanned: "Not scanned yet", never a row of zeros.
    app.add_source(Source("fresh@mail.example"))
    for name, kw in views:
        c, pg = context(**kw)
        pg.goto(URL)
        card = pg.locator("#not-scanned")
        expect(card).to_contain_text("Not scanned yet")
        expect(card.get_by_role("button", name="Scan now")).to_be_visible()
        expect(pg.locator(".glance")).to_have_count(0)
        expect(pg.locator("#connect-failed")).to_have_count(0)     # a new connection clears the old alert
        no_hscroll(pg, "not scanned " + name)
        settle(pg)
        pg.screenshot(path=str(outdir / f"state-not-scanned-{name}.png"))
        c.close()
    app.sources.pop("fresh@mail.example")
    app.active = keep
    step("connected, never scanned: 'Not scanned yet' with Scan now, no line of zeros; desktop/dark/390 px")

    # 8. Clawback stops while the page is open (terminal closed, process killed):
    # a persistent message says so and how to start it again, the stale screen
    # cannot be used, and the page reloads itself once Clawback is back. A real
    # separate process is killed, so its connections really close.
    import socket
    import subprocess
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port2 = s.getsockname()[1]
    child = ("import sys\nfrom clawback.web.server import App, make_server\nfrom clawback.demo import DemoSource\n"
             "app = App(); httpd, app = make_server(app, int(sys.argv[1]))\n"
             "if sys.argv[2] == 'scanning':\n"
             "    app.add_source(DemoSource(account='gone-test'))\n"
             "    app.job.update(status='running', account='gone-test', kind='gmail', done=40, total=300,\n"
             "                   progress={'line': '40 of 300 emails read'})\n"
             "print('READY', flush=True)\nhttpd.serve_forever()\n")

    def launch(mode):
        proc = subprocess.Popen([sys.executable, "-c", child, str(port2), mode], cwd=str(repo), stdout=subprocess.PIPE,
                                env={**os.environ, "PYTHONPATH": str(repo)}, text=True)
        assert proc.stdout.readline().strip() == "READY"
        return proc

    def stop(proc):
        # a venv's python.exe on Windows is a launcher with the real interpreter
        # as its child: end the whole tree, as closing the terminal would
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
        proc.wait()

    proc = launch("scanning")
    URL2 = f"http://127.0.0.1:{port2}/"
    pages = []
    for name, kw in views:
        c = browser.new_context(viewport={"width": kw.get("width", 1280), "height": kw.get("height", 900)},
                                color_scheme=kw.get("scheme", "light"), is_mobile=kw.get("mobile", False), locale="en-GB")
        c.on("request", lambda r: foreign.append(r.url)
             if r.url.split(":", 1)[0] in {"http", "https", "ws", "wss"} and not r.url.startswith(URL2) else None)
        pg = c.new_page()
        # the refused requests themselves are expected here; anything else is not
        pg.on("console", lambda m, n=name: errors.append(f"gone {n} console {m.type}: {m.text}")
              if m.type in ("error", "warning") and "ERR_CONNECTION_REFUSED" not in m.text else None)
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.goto(URL2)
        expect(pg.get_by_role("heading", name="Scanning")).to_be_visible()
        pages.append((name, c, pg))
    stop(proc)
    for name, c, pg in pages:
        box = pg.locator("#offline")
        expect(box).to_be_visible(timeout=10000)
        expect(box).to_have_attribute("role", "alert")
        expect(box).to_contain_text("Clawback stopped running on this computer")
        expect(box.locator("code")).to_have_text("clawback ui")
        expect(box).to_contain_text("The scan that was running stopped with it")
        expect(box).to_contain_text("continues from there")
        expect(box).to_contain_text("reloads by itself")
        assert pg.evaluate("document.getElementById('view').inert"), "the stale screen must not be usable"
        assert "Failed to fetch" not in pg.locator("body").inner_text(), "raw fetch error shown"
        assert pg.evaluate("document.activeElement.id") == "offline", "focus goes to the message"
        no_hscroll(pg, "server gone " + name)
        settle(pg)
        pg.screenshot(path=str(outdir / f"state-server-gone-{name}.png"))
    # still stopped a few seconds later: the message stays, no toast replaces it
    pages[0][2].wait_for_timeout(4500)
    expect(pages[0][2].locator("#offline")).to_be_visible()
    expect(pages[0][2].locator("#toast.show")).to_have_count(0)
    proc = launch("idle")
    for name, c, pg in pages:
        expect(pg.get_by_role("heading", name="Where is your email?")).to_be_visible(timeout=15000)
        expect(pg.locator("#offline")).to_have_count(0)
    # A click on a page whose screen was idle (no poll running) also finds the stop.
    name, c, pg = pages[0]
    pg.get_by_role("button", name="Open the demo").scroll_into_view_if_needed()
    stop(proc)
    pg.get_by_role("button", name="Open the demo").click()
    expect(pg.locator("#offline")).to_be_visible(timeout=10000)
    expect(pg.locator("#offline")).not_to_contain_text("The scan that was running")
    expect(pg.locator("#toast.show")).to_have_count(0)
    proc = launch("idle")
    expect(pg.get_by_role("heading", name="Where is your email?")).to_be_visible(timeout=15000)
    stop(proc)
    for name, c, pg in pages:
        c.close()
    step("Clawback stopped (process killed): persistent alert 'stopped running on this computer', 'clawback ui', "
         "scan progress kept, stale screen inert, no 'Failed to fetch'; reloads itself when Clawback is back; "
         "an idle page finds out on the next click; desktop/dark/390 px")

    # 9. Accounts are shown by email address, never by internal labels. Saved
    # here: a Gmail sign-in from before 2.0.0 labelled "personal", an
    # accidental second sign-in of the same mailbox ("default", the v1 token
    # file), and an app-password account (results only). Invented addresses.
    from clawback import config  # noqa: E402
    from clawback.senders import SenderSummary  # noqa: E402
    from clawback.sources.base import SavedSource  # noqa: E402
    pat = "pat.example@gmail.example"
    for label, n, kind, addr in (("personal", 40, "gmail", pat), ("default", 5, "gmail", pat),
                                 ("me@imap.example", 7, "imap", "me@imap.example")):
        tracker.upsert_senders(label, [SenderSummary(address="news@shop.example", domain="shop.example",
                                                     message_count=n, category="newsletter", bulk=True)])
        tracker.record_messages(label, [(f"{label}-{i}", "news@shop.example", "2026-10-01T10:00:00+00:00", "Hi", 0)
                                        for i in range(n)], cv=1)
        sid = tracker.begin_scan(label, kind, tracker.now_iso(), None)
        tracker.finish_scan(sid, None, messages=n)
        tracker.record_identity(label, kind, addr)
    (config.home() / "tokens").mkdir(exist_ok=True)
    (config.home() / "tokens" / "personal.json").write_text("{}", encoding="utf-8")   # contents never read
    (config.home() / "token.json").write_text("{}", encoding="utf-8")
    keep_active, keep_sources = app.active, dict(app.sources)
    app.active = None
    for name, kw in views:
        c, pg = context(**kw)
        pg.goto(URL)
        expect(pg.get_by_role("heading", name="Where is your email?")).to_be_visible()
        rows = pg.locator(".saved-row")
        names = [x.strip() for x in rows.locator("strong").all_inner_texts()]
        assert pat in names and f"{pat} (default)" in names and "me@imap.example" in names, names
        assert "Demo mailbox" in names and "personal" not in names and "default" not in names and "demo" not in names, names
        assert names.index(f"{pat} (default)") == names.index(pat) + 1, names     # the copy sits under its main
        copy = rows.filter(has_text=f"{pat} (default)")
        expect(copy).to_contain_text(f"A second saved copy of {pat}")
        expect(rows.filter(has=pg.locator("strong", has_text=pat)).first.get_by_role("button")).to_have_attribute(
            "aria-label", re.compile(re.escape(pat)))
        # no account name to type for Google sign-in: it is the address
        expect(pg.locator("#gmail-account")).to_have_count(0)
        expect(pg.locator("#gmail-same-note")).to_contain_text("An address used here before reopens its saved results")
        no_hscroll(pg, "saved by address " + name)
        pg.locator(".saved-accounts").scroll_into_view_if_needed()
        settle(pg)
        pg.screenshot(path=str(outdir / f"state-saved-accounts-{name}.png"))
        c.close()
    # an account opened under its old label is headed and listed by its address
    app.add_source(SavedSource("personal"))
    c, pg = context()
    pg.goto(URL)
    expect(pg.get_by_role("heading", name=pat, exact=True)).to_be_visible()
    opts = pg.locator("#account option").all_inner_texts()
    assert pat in opts and "personal" not in opts and "Demo mailbox" in opts, opts
    settle(pg)
    pg.screenshot(path=str(outdir / "state-account-by-address-desktop.png"))
    c.close()
    app.sources.clear()
    app.sources.update(keep_sources)
    app.active = keep_active
    step("accounts by email address: saved list, picker and results heading show the address (old labels "
         "'personal'/'default' never shown), the second copy of one mailbox is marked, no label field for Google "
         "sign-in; desktop/dark/390 px")

    # 10. Scan options for a live mailbox: the first scan, then "Scan again"
    # (quick update, fresh full scan, since a date, the newest N), each with a
    # time estimate; after a classifier upgrade, why this one scan is slower.
    # The mailbox is the demo's mail behind a Gmail-like (incremental) source.
    from clawback.demo import DemoSource  # noqa: E402
    from clawback.scan import CLASSIFIER_VERSION  # noqa: E402

    from clawback.progress import checkpoint  # noqa: E402

    class LiveDemo(DemoSource):
        kind = "gmail"
        # The demo reads its 480 emails in well under the page's 1 s poll, so a
        # running scan would be over before the page could show it: while
        # ``gate`` is clear the scan waits (pause and cancel still work), as a
        # real Gmail read waits on Google's quota.
        gate = threading.Event()
        gate.set()

        def mailbox_address(self):
            return self.account

        def iter_messages(self, limit=None, progress=None, cancel=None, since=None, skip_ids=None):
            while not self.gate.wait(0.05):
                checkpoint(cancel)
            yield from super().iter_messages(limit, progress, cancel, since, skip_ids)

    lee = "lee.example@gmail.example"
    keep_active, keep_sources = app.active, dict(app.sources)
    app.job.update(status="idle", account=None, result=None)
    app.add_source(LiveDemo(account=lee))
    c, pg = context()
    pg.goto(URL)
    pg.locator("#scan-now").click()
    dlg = pg.locator("#scan-panel")
    expect(dlg.get_by_role("heading", name=f"Scan {lee}")).to_be_visible()
    expect(dlg.get_by_role("radio")).to_have_count(3)                    # first scan: nothing to update yet
    expect(dlg.get_by_role("radio", name="Standard scan")).to_be_checked()
    expect(dlg.get_by_role("radio", name="Standard scan")).to_be_focused()
    expect(dlg.locator("#scan-estimate")).to_contain_text("10 to 15 minutes")
    settle(pg)
    pg.screenshot(path=str(outdir / "state-scan-first-desktop.png"))
    dlg.locator("#scan-last").fill("20")                               # typing picks its option
    expect(dlg.get_by_role("radio", name="Your newest emails")).to_be_checked()
    expect(dlg.locator("#scan-estimate")).to_have_text("Estimated time: a few seconds. Reads up to 20 emails.")
    dlg.get_by_role("radio", name="Standard scan").check()
    dlg.locator("#scan-start").click()
    expect(pg.locator("#scan-scope")).to_have_text(re.compile(r"^That scan covered emails since \d+ \w+ \d{4}\.$"),
                                                   timeout=20000)
    assert tracker.last_scan(lee)["mode"] == "full"
    # Scan again: all four, quick update first and recommended
    pg.locator("#scan-again").click()
    dlg = pg.locator("#scan-panel")
    expect(dlg.get_by_role("heading", name=f"Scan {lee} again")).to_be_visible()
    expect(dlg.get_by_role("radio")).to_have_count(4)
    expect(dlg.get_by_role("radio", name="Quick update Recommended")).to_be_checked()
    expect(dlg.locator("#scan-estimate")).to_have_text("Estimated time: a few seconds. Only mail Clawback has not read yet is read.")
    pg.keyboard.press("ArrowDown")                                     # radios move with the arrow keys
    expect(dlg.get_by_role("radio", name="Fresh full scan")).to_be_checked()
    expect(dlg.locator("#scan-estimate")).to_contain_text("Based on the 480 emails saved from earlier scans")
    dlg.locator("#scan-since").fill("2099-01-01")
    expect(dlg.get_by_role("radio", name="From a date")).to_be_checked()
    expect(dlg.locator("#scan-choice-error")).to_have_text("Choose a date that is not in the future.")
    dlg.locator("#scan-start").click()
    expect(dlg).to_be_visible()                                        # nothing started
    assert app.job.snapshot()["mode"] == "full"
    dlg.locator("#scan-since").fill("2026-09-01")
    expect(dlg.locator("#scan-choice-error")).to_be_hidden()
    expect(dlg.locator("#scan-estimate")).to_contain_text("a few seconds")
    settle(pg)
    pg.screenshot(path=str(outdir / "state-scan-again-desktop.png"))
    pg.keyboard.press("Escape")
    expect(pg.locator("#scan-panel")).to_have_count(0)
    expect(pg.locator("dialog")).to_have_count(0)                      # inline, never a pop-up
    assert app.job.snapshot()["mode"] == "full", "Escape must not start a scan"
    pg.locator("#scan-again").click()
    dlg = pg.locator("#scan-panel")
    dlg.locator("#scan-last").fill("20")
    expect(dlg.locator("#scan-estimate")).to_have_text("Estimated time: a few seconds. Only mail Clawback has not read yet is read.")
    dlg.locator("#scan-start").click()
    expect(pg.locator("#scan-scope")).to_have_text("That scan covered your newest 20 emails. "
                                                   "Results also include what earlier scans saved.", timeout=20000)
    assert tracker.last_scan(lee)["mode"] == "last" and tracker.last_scan(lee)["last_n"] == 20
    c.close()
    # the picker itself in the dark theme and at phone width: the "newest N" choice picked
    for name, kw in views[1:]:
        c, pg = context(**kw)
        pg.goto(URL)
        pg.locator("#scan-again").click()
        dlg = pg.locator("#scan-panel")
        expect(dlg.get_by_role("radio")).to_have_count(4)
        dlg.locator("#scan-last").fill("1000")
        expect(dlg.get_by_role("radio", name="Your newest emails")).to_be_checked()
        expect(dlg.locator("#scan-estimate")).to_contain_text("Estimated time:")
        no_hscroll(pg, "scan options " + name)
        box = dlg.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= pg.viewport_size["width"] + 1, f"scan panel off screen: {box}"
        settle(pg)
        pg.screenshot(path=str(outdir / f"state-scan-again-{name}.png"))
        pg.keyboard.press("Escape")
        c.close()
    # after an upgrade of the sorting rules, the saved mail was read by an older version
    for name, kw in views:
        db = tracker.connect()                     # (again for each view: the desktop one rescans)
        db.execute("UPDATE messages SET cv=? WHERE account=?", (CLASSIFIER_VERSION - 1, lee))
        db.commit()
        db.close()
        c, pg = context(**kw)
        pg.goto(URL)
        pg.locator("#scan-again").click()
        dlg = pg.locator("#scan-panel")
        expect(dlg.locator("#scan-reread")).to_contain_text("This one scan takes longer")
        expect(dlg.locator("#scan-reread")).to_contain_text("10 to 15 minutes for 3,000 emails")
        expect(dlg.locator("#scan-estimate")).to_contain_text("Based on the")
        no_hscroll(pg, "scan options " + name)
        settle(pg)
        pg.screenshot(path=str(outdir / f"state-scan-options-upgrade-{name}.png"))
        if name == "desktop":
            LiveDemo.gate.clear()
            dlg.locator("#scan-start").click()                            # quick update: reads it all once, and says why
            expect(pg.get_by_role("heading", name="Scanning")).to_be_visible(timeout=10000)
            expect(pg.locator("#scan-hint")).to_contain_text("This version of Clawback sorts mail better")
            expect(pg.locator("#scan-hint")).to_contain_text("After this, quick updates only read new mail")
            assert app.job.snapshot()["hint"] == "scan.hint.upgradeApi" and app.job.snapshot()["mode"] == "quick"
            settle(pg)
            pg.screenshot(path=str(outdir / "state-scan-upgrade-running-desktop.png"))
            LiveDemo.gate.set()
            # once done, the line says what the next scans do (all mail was read: none reused)
            expect(pg.locator("#scan-hint")).to_have_text("Next scans only read new mail, so they usually take seconds.",
                                                          timeout=20000)
            for _ in range(300):
                if app.job.snapshot()["status"] != "running":
                    break
                time.sleep(0.1)
            pg.goto(URL)
            expect(pg.locator("#scan-scope")).to_contain_text("(a quick update: only mail not read before was read)")
        c.close()
    app.sources.clear()
    app.sources.update(keep_sources)
    app.active = keep_active
    step("scan options: first scan offers standard / since a date / newest N with the first-scan estimate; newest 20 "
         "ran and the results say 'your newest 20 emails'; Scan again offers quick (recommended) / full / since / "
         "newest with estimates, arrow keys, a future date refused, Escape starts nothing; after a sorting upgrade "
         "the inline panel (no pop-up) and the running scan say why this one is slower; desktop/dark/390 px")

    # 9. Cleanup permissions. A Google sign-in saved by an older version can
    # only read: the list works, Delete says once what is missing, one sign-in
    # (each permission explained first) asks for everything, and it is never
    # asked again. A fake Google mailbox stands in: no real account is touched.
    import mailbox as stdlib_mailbox  # noqa: E402
    from clawback.demo import DemoSource, mailbox as demo_mail  # noqa: E402
    from clawback.sources import gmail as gmail_src  # noqa: E402

    class ReadOnlyGoogle(DemoSource):
        kind = "gmail"

        def __init__(self, account):
            super().__init__(account)
            self.grants = 0

        def missing_permission(self, capability):
            if self.grants or capability not in {"trash", "archive", "spam", "block", "mailto", "send"}:
                return None
            return gmail_src.REAUTH_WHY

        def grant(self, capability=None):
            self.grants += 1

    keep_sources, keep_active = dict(app.sources), app.active
    ro = ReadOnlyGoogle("reader@mail.example")
    app.add_source(ro)
    app.start_scan(limit=100000)
    assert wait_scan()["status"] == "done"
    app.active = ro.account
    for name, kw in views:
        ro.grants = 0
        c, pg = context(**kw)
        pg.goto(URL)
        note = pg.locator("#needs-signin")
        expect(note).to_contain_text("Cleanup needs one more permission")
        expect(note.get_by_role("button", name="Sign in again to allow cleanup")).to_be_visible()
        no_hscroll(pg, "read-only " + name)
        settle(pg)
        pg.screenshot(path=str(outdir / f"state-readonly-{name}.png"))
        row = pg.locator("li.srow[data-address='digest@forum.example']")
        delete = "Delete all 120 emails from Community Forum"
        row.get_by_role("button", name=delete).click()
        # In the row, no pop-up: it says what is missing; no Yes until cleanup is allowed.
        expect(row.locator(".cf-q")).to_have_text("Needs permission")
        expect(row.locator(".cf-why")).to_contain_text("only allows reading")
        expect(row.locator("#confirm-yes")).to_have_count(0)
        expect(row.locator("#confirm-grant")).to_have_text("Allow cleanup")
        expect(row.locator("#confirm-no")).to_be_focused()
        expect(pg.locator("dialog")).to_have_count(0)
        no_hscroll(pg, "permission in the row " + name)
        settle(pg)
        pg.screenshot(path=str(outdir / f"state-permission-{name}.png"))
        assert ro.grants == 0 and ro.simulated == [], "nothing may happen before Allow cleanup"
        if name == "desktop":
            row.locator("#confirm-grant").click()
            # one sign-in, then straight back to the same row with its Yes; still nothing deleted
            expect(row.locator("#confirm-yes")).to_have_text("Yes, delete 120 emails")
            expect(pg.locator("#needs-signin")).to_have_count(0)
            assert ro.grants == 1 and ro.simulated == [], (ro.grants, ro.simulated)
            row.locator("#confirm-no").click()
            pg.reload()
            expect(pg.locator("li.srow").first).to_be_visible()
            expect(pg.locator("#needs-signin")).to_have_count(0)
            pg.locator("li.srow[data-address='digest@forum.example']").get_by_role("button", name=delete).click()
            expect(pg.locator("#confirm-yes")).to_have_text("Yes, delete 120 emails")
            expect(pg.locator("#confirm-grant")).to_have_count(0)
            pg.locator("#confirm-no").click()
            assert ro.grants == 1 and ro.simulated == []
            # the notice's own button asks for the same one sign-in (fresh read-only account)
            ro.grants = 0
            pg.reload()
            pg.locator("#needs-signin").get_by_role("button", name="Sign in again to allow cleanup").click()
            expect(pg.locator("#needs-signin")).to_have_count(0)
            assert ro.grants == 1 and ro.simulated == []
        else:
            row.locator("#confirm-no").click()
            expect(row.get_by_role("button", name=delete)).to_be_visible()
        c.close()
    step("read-only Google sign-in: one notice 'Cleanup needs one more permission' with 'Sign in again to allow cleanup'; "
         "Delete all turns the row into 'Needs permission' + Allow cleanup + Cancel (no Yes, no pop-up); one "
         "sign-in brings back the same row with 'Yes, delete 120 emails'; after a reload nothing asks again; the notice's "
         "button does the same one sign-in; nothing deleted throughout; desktop/dark/390 px")

    # 10. A mailbox imported from a file (Google Takeout .mbox) cannot move
    # mail: no Delete anywhere, and one line says why and what would allow it.
    box_path = home / "takeout.mbox"
    box = stdlib_mailbox.mbox(str(box_path))
    for raw in demo_mail(None, "https://{host}"):
        box.add(raw)
    box.flush()
    box.close()
    app.connect({"kind": "mbox", "path": str(box_path)})
    app.start_scan(limit=100000)
    assert wait_scan()["status"] == "done"
    for name, kw in views:
        c, pg = context(**kw)
        pg.goto(URL)
        expect(pg.locator("li.srow").first).to_be_visible()
        expect(pg.locator("#no-delete")).to_have_text("Imported from a file: Clawback can unsubscribe but can't delete. "
                                                      "To delete too, connect the account with Google or Microsoft sign-in.")
        for act in ("trash", "unsubscribe_trash", "archive", "block"):
            expect(pg.locator(f"[data-act={act}]")).to_have_count(0)
        expect(pg.get_by_text("Delete all")).to_have_count(0)
        pg.locator("li.srow input[type=checkbox]").first.check()
        bar = pg.locator(".actionbar")
        expect(bar.get_by_role("button", name="Unsubscribe", exact=True)).to_be_visible()
        expect(bar.get_by_role("button", name=re.compile("delete", re.I))).to_have_count(0)
        no_hscroll(pg, "mbox " + name)
        settle(pg)
        pg.screenshot(path=str(outdir / f"state-mbox-{name}.png"))
        c.close()
    app.sources.clear()
    app.sources.update(keep_sources)
    app.active = keep_active
    step("imported .mbox: no Delete all / Unsubscribe + delete / Archive / Block on rows or in the bar; Unsubscribe "
         "stays; one visible line says the import can unsubscribe but not delete, and to connect with Google or Microsoft sign-in to delete; desktop/dark/390 px")

    browser.close()

httpd.shutdown()
print("CONSOLE/PAGE ERRORS:", errors if errors else "none")
print("REQUESTS TO OTHER SERVERS:", sorted(set(foreign)) if foreign else "none")
if errors or foreign:
    sys.exit(1)
print("ALL STATE CHECKS PASSED:", len(log))
