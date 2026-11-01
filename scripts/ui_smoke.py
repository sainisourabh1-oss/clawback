"""Drive the real Clawback web UI in headless Edge (Playwright) end to end.

Starts the real server (clawback.web.server) in-process with the demo mailbox
whose one-click links point at a local HTTPS test server (trustme CA), so the
unsubscribe POSTs are real HTTP requests that we can count.

What it asserts is what the user sees: one click never acts; ticking senders
and pressing an action never opens a dialog, sheet or overlay; the action's
own buttons turn into an inline question with the count; Escape, a click
elsewhere or 8 s put them back; only Yes acts; Undo restores; the moving
background repaints about 8 times a second, stops in a hidden tab and is one
still frame under reduced motion; nothing scrolls sideways at 390 px; nothing is requested from any
other server.
Usage: pip install playwright ; python scripts/ui_smoke.py . docs/screenshots
(uses the installed Microsoft Edge; pass another channel by editing launch())
"""
import datetime
import os
import re
import socket
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
os.environ["CLAWBACK_HOME"] = tempfile.mkdtemp(prefix="clawback-ui-")
sys.path.insert(0, str(repo / "tests"))
sys.path.insert(0, str(repo))

from conftest import UnsubServer  # noqa: E402
from clawback import oneclick  # noqa: E402
from clawback.demo import DemoSource  # noqa: E402
from clawback.web.server import App, make_server, warm_up  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

srv = UnsubServer()
real = socket.getaddrinfo
socket.getaddrinfo = lambda host, port, *a, **k: real("127.0.0.1" if isinstance(host, str) and host.endswith(".example") else host, port, *a, **k)

app = App(http_post=lambda u: oneclick.post(u, context=srv.client_ctx, allow_private=True))
httpd, app = make_server(app, 0)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
warm_up()   # as `clawback ui` does before opening the browser
# The "Open the demo" button creates a DemoSource; make it point at the local test server.
orig_connect = app.connect


def connect(body):
    if body.get("kind") == "demo":
        return app.add_source(DemoSource(base_url=srv.base_url))
    return orig_connect(body)


app.connect = connect
URL = f"http://127.0.0.1:{app.port}/"
log = []
errors = []


def settle(pg):
    """Wait until finite animations (fades, the bar sliding in) have finished,
    so screenshots show the resting state. Endless animations (the connect
    illustration) are ignored here; the aurora is a canvas, not an animation."""
    # (polled with evaluate(): wait_for_function() needs eval, which the page's CSP blocks)
    for _ in range(160):
        if pg.evaluate("!document.querySelector('#toast.show') && document.getAnimations().every("
                       "a => a.playState !== 'running' || a.effect.getComputedTiming().iterations === Infinity)"):
            break
        pg.wait_for_timeout(50)
    else:
        raise AssertionError("animations did not settle within 8 s")
    pg.wait_for_timeout(50)


ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
NB = " "


def no_iso(text, where):
    """Dates on screen use one human format (27 Oct 2026); an ISO date means one slipped through."""
    m = ISO.search(text)
    assert not m, f"{where}: ISO date {m.group()!r} on screen: ...{text[max(0, m.start() - 60):m.end() + 20]!r}"


def gb_date(iso):
    d = datetime.date.fromisoformat(iso[:10])
    return f"{d.day}{NB}{d.strftime('%b')}{NB}{d.year}"


def api_get(pg, path):
    return pg.evaluate("p => fetch(p, {headers: {'X-Clawback-CSRF': document.querySelector('meta[name=csrf-token]').content}})"
                       ".then(r => r.json())", path)


# Fixed-position things the page may show; anything else fixed would be an overlay.
ALLOWED_FIXED = {"aurora", "actionbar", "toast"}


def no_popup(pg, where):
    """Nothing modal, nothing overlaid: no dialog element or role, and no fixed
    element other than the background, the selection bar and the toast."""
    n = pg.evaluate("document.querySelectorAll('dialog, [role=dialog], [role=alertdialog], [aria-modal]').length")
    assert n == 0, f"{where}: {n} dialog element(s)"
    fixed = pg.evaluate("""() => [...document.querySelectorAll('body *')].filter(e => getComputedStyle(e).position === 'fixed'
        && getComputedStyle(e).visibility !== 'hidden' && e.getClientRects().length).map(e => e.id || e.className.split(' ')[0])""")
    extra = [f for f in fixed if f not in ALLOWED_FIXED]
    assert not extra, f"{where}: overlay element(s) {extra}"


def step(msg):
    log.append(msg)
    print("STEP", msg, flush=True)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    ctx = browser.new_context(viewport={"width": 1280, "height": 860}, color_scheme="light", accept_downloads=True, locale="en-GB")
    # Privacy: every network request any page makes must go to this local
    # server. (Edge's own edge:// pages, e.g. the downloads bubble, are the
    # browser, not the page, and make no network requests.)
    foreign = []

    def watch(c):
        c.on("request", lambda r: foreign.append(r.url)
             if r.url.split(":", 1)[0] in {"http", "https", "ws", "wss", "ftp"} and not r.url.startswith(URL) else None)
    watch(ctx)
    page = ctx.new_page()
    page.on("console", lambda m: errors.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    # First-screen time: navigation start -> the connect heading in the DOM,
    # recorded in the page by a MutationObserver (no polling delay), fresh
    # browser context each run (no cache). Run 0 warms Edge up and is dropped.
    # Reported, with only a loose sanity bound: there is no size or time budget.
    first = []
    for i in range(6):
        c = browser.new_context(viewport={"width": 1280, "height": 860})
        watch(c)
        pg = c.new_page()
        pg.add_init_script("new MutationObserver((m, o) => { if (document.querySelector('#view h1')) {"
                           " window.__firstScreen = performance.now(); o.disconnect(); } })"
                           ".observe(document, {subtree: true, childList: true});")
        pg.goto(URL)
        pg.wait_for_selector("#view h1")
        if i:
            first.append(pg.evaluate("window.__firstScreen"))
        c.close()
    first.sort()
    assert first[-1] < 3000, f"first screen took over 3 s: {first}"
    step(f"first screen in {first[len(first) // 2]:.0f} ms median, {first[-1]:.0f} ms max (5 fresh contexts)")

    page.goto(URL)
    expect(page.get_by_role("heading", name="Where is your email?")).to_be_visible()
    expect(page.locator("#flow li[aria-current=step]")).to_have_text("Connect")
    # one screen: the hero and the whole connect card fit a 1280x860 window without scrolling
    bottom = page.evaluate("Math.max(document.querySelector('.c-pick').getBoundingClientRect().bottom, document.querySelector('.c-hero').getBoundingClientRect().bottom)")
    assert bottom <= 860, f"connect screen needs scrolling (bottom at {bottom}px)"
    expect(page.locator("#pick-gmail")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#gmail-go")).to_be_visible()
    expect(page.locator("#gmail-perms .perm-chips > li")).to_have_count(4)
    settle(page)
    page.screenshot(path=str(outdir / "01-connect.png"))
    no_popup(page, "connect")
    step(f"connect: one screen at 1280x860 (bottom {bottom:.0f}px), Gmail chosen first with 'Sign in with Google' and the 4 "
         "permission chips asked once; demo one click away")
    # Presets: the address picks the provider; server details stay folded; labels are honest.
    assert page.locator(".conn-label[data-label=verified]").count() == 1, "only the Gmail API read path is Verified"
    page.locator("#pick-imap").click()
    expect(page.locator("#pane-imap")).to_be_visible()
    expect(page.locator("#limits-imap")).to_contain_text("Deleting over IMAP isn't built yet")   # said before connecting
    expect(page.locator("#pane-gmail")).to_be_hidden()
    page.locator("#imap-user").fill("someone@yahoo.co.uk")
    expect(page.locator("#imap-provider")).to_have_value("yahoo")
    expect(page.locator("#imap-help")).to_contain_text("Make an app password on Yahoo Mail's page")
    expect(page.locator("#imap-help")).to_contain_text("Tested with fakes")
    expect(page.locator("#imap-host")).to_be_hidden()
    assert page.locator("#imap-host").input_value() == "imap.mail.yahoo.com"
    page.locator("#imap-user").fill("someone@icloud.com")
    expect(page.locator("#imap-help")).to_contain_text("two-factor authentication")
    page.locator("#imap-user").fill("someone@hotmail.com")
    expect(page.locator("#imap-blocked")).to_contain_text("16 September 2024")
    expect(page.locator("#imap-go")).to_be_disabled()
    page.locator("#imap-help").get_by_role("button", name="Use Sign in with Microsoft instead").click()
    expect(page.locator("#pane-outlook")).to_be_visible()
    expect(page.locator("#pick-outlook")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#ms-user")).to_have_value("someone@hotmail.com")
    expect(page.locator("#ms-user")).to_be_focused()
    expect(page.locator("#ms-go")).to_be_visible()
    page.locator("#pick-imap").click()
    page.locator("#imap-user").fill("someone@proton.me")
    expect(page.locator("#imap-help")).to_contain_text("Experimental")
    page.locator("#imap-tech summary").click()
    assert (page.locator("#imap-host").input_value(), page.locator("#imap-port").input_value(),
            page.locator("#imap-security").input_value()) == ("127.0.0.1", "1143", "starttls")
    page.locator("#imap-help details.tech summary").click()
    no_iso(page.locator("#imap-help").inner_text(), "preset sources")
    settle(page)
    page.screenshot(path=str(outdir / "14-connect-presets.png"))
    page.locator("#imap-provider").select_option("other")
    expect(page.locator("#imap-host")).to_be_visible()
    expect(page.locator("#imap-host")).to_have_value("")
    page.goto(URL)
    expect(page.get_by_role("heading", name="Where is your email?")).to_be_visible()
    step("connect presets: @yahoo.co.uk picks Yahoo (app-password link, label, servers folded); iCloud mentions 2FA; "
         "@hotmail.com is blocked with the 16 Sep 2024 reason and switches to the Outlook form with the address filled; "
         "Proton fills 127.0.0.1:1143 STARTTLS and is Experimental; Other unfolds empty server fields; exactly one Verified label")
    page.evaluate("document.fonts.ready")
    font = page.evaluate("[...document.fonts].filter(f => f.family.replace(/\"/g, '') === 'Inter' && f.status === 'loaded').length")
    family = page.evaluate("getComputedStyle(document.body).fontFamily")
    assert font >= 1 and family.lstrip('"').startswith("Inter"), (font, family)
    motion = page.evaluate("document.getAnimations().length")
    assert motion >= 1, "the connect illustration should animate when motion is allowed"
    step(f"bundled font loaded from this server ({font} face(s) loaded; body font {family.split(',')[0]}); {motion} animations running")
    page.keyboard.press("Tab")
    focused = page.evaluate("[document.activeElement.className, getComputedStyle(document.activeElement).outlineStyle, getComputedStyle(document.activeElement).outlineWidth]")
    assert focused[0] == "skip" and focused[1] == "solid" and focused[2] == "2px", focused
    step("keyboard: first Tab reaches the skip link with a visible 2 px focus outline")

    page.get_by_role("button", name="Open the demo").click()
    expect(page.get_by_role("tab", name="Newsletters & ads")).to_be_visible(timeout=20000)
    expect(page.locator("#flow li[aria-current=step]")).to_have_text("Clean up")
    assert page.locator("#scan-hint").count() == 0, "the demo must not claim fast rescans"
    # The first view is the list: three tabs, the rest behind "More"; one summary card.
    assert page.get_by_role("tab").all_inner_texts() == ["Newsletters & ads\n9", "Spam-like\n3", "Paid\n8"], \
        page.get_by_role("tab").all_inner_texts()
    expect(page.locator(".glance")).to_contain_text("436 emails from 9 senders")
    expect(page.locator(".glance")).to_contain_text("Subscriptions (8)")
    expect(page.locator(".glance")).to_contain_text("3 senders")
    expect(page.locator("#banner")).to_be_hidden()   # nothing saved yet: no banner to read past
    # at a glance: summary, the tabs and the first sender rows with their buttons all in the first 860 px
    rows_in_view = page.evaluate("[...document.querySelectorAll('li.srow')].filter(r => r.getBoundingClientRect().bottom <= innerHeight).length")
    assert rows_in_view >= 8, f"only {rows_in_view} sender rows visible without scrolling"
    step(f"demo scan finished: flow at 'Clean up'; three tabs + More; one summary card (436 emails from 9 senders, "
         f"subscriptions, spam-like); no empty savings banner; {rows_in_view} of 9 rows visible at 1280x860 without scrolling")
    upcoming = api_get(page, "/api/insights")["upcoming"]
    assert upcoming, "the demo has upcoming renewals"
    page.locator("#heads-up summary").click()
    heads = page.locator("#heads-up").inner_text()
    for u in upcoming:
        assert gb_date(u["date"]) in heads, (gb_date(u["date"]), heads)
    page.locator("#heads-up summary").click()
    no_iso(page.locator("#view").inner_text(), "results")
    expect(page.locator(".r-head .last")).to_contain_text(re.compile(r"Last scan \d{1,2}\s[A-Z][a-z]{2}\s\d{4}, "))   # dates keep together with no-break spaces
    step(f"dates: folded heads-up shows {gb_date(upcoming[0]['date']).replace(NB, ' ')} (same format as the header), no ISO dates on the results")

    # Scan again: options open inline under the heading (no dialog); the demo has
    # no quick update (it is not a live mailbox): the whole demo, from a date, or the newest N.
    page.locator("#scan-again").click()
    sp = page.locator("#scan-panel")
    expect(sp.get_by_role("heading", name="Scan Demo mailbox again")).to_be_visible()
    no_popup(page, "scan options")
    radios = sp.locator("input[name=scan-mode]")
    expect(radios).to_have_count(3)
    expect(sp.locator("#scan-mode-full")).to_be_checked()
    expect(sp.locator("#scan-mode-full")).to_be_focused()
    expect(sp).to_contain_text("Whole mailbox")
    expect(sp).to_contain_text("From a date")
    expect(sp).to_contain_text("Your newest emails")
    expect(sp.locator("#scan-estimate")).to_contain_text("Estimated time")
    page.keyboard.press("ArrowDown")
    expect(sp.locator("#scan-mode-since")).to_be_checked()
    expect(sp.locator("#scan-desc")).to_contain_text("All mail since the date you choose")
    settle(page)
    page.screenshot(path=str(outdir / "20-scan-options.png"))
    page.keyboard.press("Escape")
    expect(page.locator("#scan-panel")).to_have_count(0)
    expect(page.locator("#scan-again")).to_be_focused()
    assert api_get(page, "/api/scan")["status"] != "running", "Escape must not start a scan"
    step("Scan again: inline panel (no dialog) offers whole demo / from a date / newest N with a time estimate; arrow keys "
         "move between options; Escape closes it, refocuses Scan again and starts nothing")

    demo = app.source_for("demo")

    def row(pg, addr):
        return pg.locator(f"li.srow[data-address='{addr}']")

    def trashed(pg, addr):
        return next(s for s in api_get(pg, "/api/senders?category=newsletter")["senders"] if s["address"] == addr)["trashed_count"]

    # Sorted by most emails, each row shows its count and its own actions.
    counts = [int(x.replace(",", "")) for x in page.locator("li.srow .count b").all_inner_texts()]
    assert counts == sorted(counts, reverse=True) and counts[0] == 120, counts
    first = row(page, "digest@forum.example")
    for name in ("Unsubscribe from Community Forum", "Delete all 120 emails from Community Forum", "Keep Community Forum (never suggest it)",
                 "More actions for Community Forum"):
        expect(first.get_by_role("button", name=name)).to_be_visible()
    step(f"newsletters sorted by most emails first ({counts[:4]}...); each row: count, Unsubscribe, Delete all, Keep, More")

    # SAFETY: one click never deletes. Click 1 turns the row's buttons into an
    # inline "Yes, delete 120 emails" + Cancel; Enter (focus is on Cancel),
    # Escape, a click elsewhere and 8 s without a touch all put the buttons
    # back and leave the mailbox alone.
    before = list(demo.simulated)
    del_name = "Delete all 120 emails from Community Forum"
    first.get_by_role("button", name=del_name).click()
    yes = first.locator("#confirm-yes")
    expect(yes).to_be_visible()
    no_popup(page, "row confirm")
    expect(first.locator(".cf-q")).to_have_text("Delete 120 emails?")
    expect(yes).to_have_text("Yes, delete 120 emails")
    expect(first.locator("#confirm-no")).to_have_text("Cancel")
    expect(yes).to_have_accessible_name("Yes, delete 120 emails")
    expect(yes).to_have_attribute("data-count", "1")
    expect(first).to_contain_text("Demo mailbox: nothing real is deleted (simulated).")
    expect(first).to_contain_text("Undo appears right after")
    expect(first.locator("#confirm-no")).to_be_focused()
    expect(page.locator("#flow li[aria-current=step]")).to_have_text("Confirm")
    assert first.locator("input[type=text]").count() == 0 and "type the" not in first.inner_text().lower(), "nothing to type"
    expect(first.get_by_role("button", name=del_name)).to_have_count(0)   # the buttons became the question
    settle(page)
    page.screenshot(path=str(outdir / "03-review.png"))
    # the same moment in a 900 px window, where the row's name and its Yes sit close together (feature strip)
    page.set_viewport_size({"width": 900, "height": 640})
    first.scroll_into_view_if_needed()
    settle(page)
    page.screenshot(path=str(outdir / "03-review-strip.png"))
    # at this width every row's buttons still sit right of its email count, never on top of it
    overlap = page.evaluate("""() => [...document.querySelectorAll('li.srow')].filter(r => {
        const c = r.querySelector(':scope > .count'), b = r.querySelector(':scope > .acts button');
        return c && b && b.getBoundingClientRect().left < c.getBoundingClientRect().right - 1; }).length""")
    assert overlap == 0, f"{overlap} rows at 900 px have buttons over the count"
    page.set_viewport_size({"width": 1280, "height": 860})
    assert demo.simulated == before and trashed(page, "digest@forum.example") == 0, "click 1 must not delete"
    page.keyboard.press("Enter")                   # Enter alone presses Cancel
    expect(first.locator("#confirm-yes")).to_have_count(0)
    expect(first.get_by_role("button", name=del_name)).to_be_focused()
    page.keyboard.press("Enter")                   # Enter on the button arms it again (click 1), nothing more
    expect(first.locator("#confirm-yes")).to_be_visible()
    page.keyboard.press("Escape")
    expect(first.locator("#confirm-yes")).to_have_count(0)
    expect(first.get_by_role("button", name=del_name)).to_be_focused()
    first.get_by_role("button", name=del_name).click()
    expect(first.locator("#confirm-yes")).to_be_visible()
    page.locator(".r-head h1").click()            # a click anywhere else
    expect(first.locator("#confirm-yes")).to_have_count(0)
    first.get_by_role("button", name=del_name).click()
    expect(first.locator("#confirm-yes")).to_be_visible()
    t0 = time.monotonic()
    expect(first.locator("#confirm-yes")).to_have_count(0, timeout=12000)   # nobody touched it: back by itself
    waited = time.monotonic() - t0
    assert 7.0 <= waited <= 10.5, f"auto-revert after {waited:.1f} s"
    expect(first.get_by_role("button", name=del_name)).to_be_visible()
    first.get_by_role("checkbox").focus()
    page.keyboard.press("Enter")                   # Enter on the list does nothing either
    no_popup(page, "Enter on a row")
    assert demo.simulated == before and trashed(page, "digest@forum.example") == 0 and len(srv.posts()) == 0
    step(f"one click never deletes: Delete all turns into '[Yes, delete 120 emails] [Cancel]' in the row (no dialog, no overlay; "
         f"focus on Cancel); Enter, Escape, a click elsewhere and {waited:.1f} s untouched each put the buttons back; "
         "0 trash calls, 0 POSTs; nothing to type")

    # Click 2 deletes (to Trash); the toast offers Undo, which puts it back.
    first.get_by_role("button", name=del_name).click()
    first.locator("#confirm-yes").click()
    toast = page.locator("#toast.show")
    expect(toast).to_contain_text("Deleted 120 emails from Community Forum.")
    expect(toast.get_by_role("button", name="Undo")).to_be_visible()
    assert demo.simulated[len(before):] == [("trash", (120,))], demo.simulated[len(before):]
    expect(first.get_by_text("120 deleted")).to_be_visible()
    assert trashed(page, "digest@forum.example") == 120
    no_popup(page, "after delete")
    page.screenshot(path=str(outdir / "04-done.png"))
    toast.get_by_role("button", name="Undo").click()
    expect(page.locator("#toast.show")).to_contain_text("Undone: 1 put back.")
    assert demo.simulated[-1] == ("untrash", (120,)), demo.simulated[-1]
    assert trashed(page, "digest@forum.example") == 0
    expect(row(page, "digest@forum.example").get_by_text("120 deleted")).to_have_count(0)
    step("click 2 (Yes, delete 120 emails) moved 120 emails to Trash (1 trash call), row shows '120 deleted', no dialog; the toast's Undo "
         "ran untrash for the same 120 and cleared the marker")

    # Multi-select: tick 3; only the bar appears (no dialog, no sheet).
    for addr in ("deals@shopmart.example", "news@weeklybyte.example", "offers@travelnow.example"):
        row(page, addr).get_by_role("checkbox").check()
        no_popup(page, "after ticking " + addr)
    bar = page.locator(".actionbar")
    expect(bar.locator(".count-pill")).to_have_text("3 selected · 161 emails")
    expect(bar.locator(".safety")).to_contain_text("Nothing happens until you confirm")
    for name in ("Unsubscribe", "Delete all", "Unsubscribe + delete all", "Keep"):
        expect(bar.get_by_role("button", name=name, exact=True)).to_be_visible()
    settle(page)
    page.screenshot(path=str(outdir / "02-newsletters.png"))
    step("ticked 3 newsletters: only the bar appears ('3 selected · 161 emails', Unsubscribe / Delete all / "
         "Unsubscribe + delete all / Keep); no dialog or overlay after any tick")

    # A link and a mailto on one unknown domain: one short warning, the why folded.
    wrow = row(page, "events@webinarhub.example")
    fold = wrow.locator("details.flags")
    expect(fold).to_have_count(1)
    expect(fold.locator("summary")).to_have_text("⚠ Unsubscribe goes to an unknown domain (clickmetrics.example)")
    assert wrow.inner_text().count("clickmetrics.example") == 1, wrow.inner_text()  # explanation still folded
    fold.locator("summary").click()
    expect(fold.locator("p")).to_contain_text("isn't the sender (webinarhub.example)")
    fold.locator("summary").click()
    step("unknown unsubscribe domain: one short warning line per sender (link + mailto merged), details folded")

    # No List-Unsubscribe header, link only in the email body: "Link in email",
    # its domain and a Copy button; Unsubscribe opens it only after Yes.
    brow = row(page, "club@bookclub.example")
    expect(brow.get_by_text("Link in email")).to_be_visible()
    expect(brow.locator(".via .addr")).to_have_text("bookclub.example")
    expect(brow.get_by_role("button", name="Copy the unsubscribe link from Book Club's email")).to_be_visible()
    brow.get_by_role("button", name="Unsubscribe from Book Club").click()
    expect(brow.locator(".cf-q")).to_have_text("Unsubscribe from Book Club?")
    expect(brow).to_contain_text("A web page opens; you finish there.")
    expect(brow).to_contain_text("This can't be undone from Clawback.")
    brow.get_by_text("What exactly happens").click()
    expect(brow).to_contain_text("Open the unsubscribe link from Book Club's email (bookclub.example)")
    brow.locator("#confirm-no").click()
    expect(brow.locator("#confirm-yes")).to_have_count(0)
    step("body-only unsubscribe link: row shows 'Link in email' + domain + Copy; the inline question says a page opens and "
         "that unsubscribe can't be undone; details fold open in place; cancelled with Cancel")

    bar.get_by_role("button", name="Unsubscribe + delete all", exact=True).click()
    expect(bar.locator(".cf-q")).to_have_text("Unsubscribe + delete 161 emails?")
    expect(bar.locator("#confirm-yes")).to_have_text("Yes, unsubscribe + delete 161 emails")
    page.keyboard.press("Escape")
    bar.get_by_role("button", name="Unsubscribe", exact=True).click()
    expect(bar.locator(".cf-q")).to_have_text("Unsubscribe from 3 senders?")
    expect(bar.locator("#confirm-yes")).to_have_text("Yes, unsubscribe from 3 senders")
    no_popup(page, "bulk confirm")
    # Plain words first; per-sender detail under "What exactly happens", protocol detail folded further.
    assert "HTTPS POST" not in bar.inner_text(), "jargon must stay folded"
    bar.get_by_text("What exactly happens").click()
    expect(bar.locator(".exact .step")).to_have_count(3)
    step1 = bar.locator(".exact .step").first
    expect(step1).to_contain_text("Ask Daily Deals to unsubscribe you automatically (one-click), contacting unsubscribe.shopmart.example")
    expect(step1.get_by_text("RFC 8058")).to_be_hidden()
    expect(bar.locator(".exact details.tech")).to_have_count(3)
    step1.get_by_text("Technical details").click()
    expect(step1.get_by_text("RFC 8058")).to_be_visible()
    expect(step1.locator("details.tech")).to_contain_text("https://unsubscribe.shopmart.example")
    no_iso(bar.inner_text(), "bulk confirm")
    page.wait_for_timeout(8500)                    # an open fold keeps the question while it is read
    expect(bar.locator("#confirm-yes")).to_be_visible()
    settle(page)
    page.screenshot(path=str(outdir / "24-bulk-confirm.png"))
    assert len(srv.posts()) == 0, "nothing may be sent before click 2"
    step("bulk: 'Unsubscribe + delete all' asks 'Unsubscribe + delete 161 emails?'; Unsubscribe asks 'Unsubscribe from 3 "
         "senders?' in the bar itself; plain lines, 3 per-sender steps folded, RFC 8058/URL folded deeper; an open fold keeps "
         "it past 8 s; 0 POSTs")
    bar.locator("#confirm-yes").click()
    expect(page.locator("#toast.show")).to_contain_text("Unsubscribed from 3 senders. This can't be undone", timeout=20000)
    expect(page.locator("#toast.show").get_by_role("button", name="Undo")).to_have_count(0)
    posts = srv.posts()
    assert len(posts) == 3 and all(x["body"] == b"List-Unsubscribe=One-Click" for x in posts), posts
    expect(page.locator("#banner")).to_contain_text("You've clawed back your inbox")
    step(f"confirmed: {len(posts)} real RFC 8058 POSTs received by the local HTTPS test server; toast says it can't be undone "
         "and has no Undo; savings card appears ('You've clawed back your inbox')")

    with page.expect_download() as dl:
        page.locator("#banner").get_by_role("button", name="Save shareable card").click()
    card = outdir / "05-savings-card.png"
    dl.value.save_as(card)
    step(f"savings card PNG downloaded ({card.stat().st_size} bytes)")

    # Per-row action, by keyboard: the row's own Unsubscribe asks in that row
    # for that one sender, and leaves other ticks alone.
    row(page, "digest@forum.example").get_by_role("checkbox").check()
    frow = row(page, "style@fashionpicks.example")
    frow.get_by_role("button", name="Unsubscribe from Fashion Picks").focus()
    page.keyboard.press("Enter")
    expect(frow.locator(".cf-q")).to_have_text("Unsubscribe from Fashion Picks?")
    expect(frow.locator("#confirm-no")).to_be_focused()
    assert len(srv.posts()) == 3, "nothing may be sent before click 2"
    page.keyboard.press("Shift+Tab")
    expect(frow.locator("#confirm-yes")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#toast.show")).to_contain_text("Unsubscribed from Fashion Picks.", timeout=20000)
    new = srv.posts()[3:]
    assert len(new) == 1 and new[0]["host"].startswith("unsubscribe.fashionpicks.example"), new
    expect(row(page, "digest@forum.example").get_by_role("checkbox")).to_be_checked()
    expect(page.locator(".actionbar .count-pill")).to_have_text("1 selected · 120 emails")
    # The row's "more" menu: opens by keyboard, Escape closes it and returns focus.
    more = page.get_by_role("button", name="More actions for The Weekly Byte")
    more.focus()
    page.keyboard.press("Enter")
    menu = page.locator(".menu:not([hidden])")
    expect(menu.get_by_role("button", name="Block future mail from The Weekly Byte")).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(menu.get_by_role("button", name="Archive all 26 emails from The Weekly Byte")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator(".menu:not([hidden])")).to_have_count(0)
    expect(more).to_be_focused()
    row(page, "digest@forum.example").get_by_role("checkbox").uncheck()
    step("per-row Unsubscribe by keyboard: asks in the row for 1 sender, Cancel focused, Shift+Tab then Enter on Yes sent "
         "exactly 1 POST to that sender; the other ticked sender stayed selected; the row's More menu opens with Enter, "
         "arrows move, Escape closes and refocuses")

    # Unsubscribe + delete all in one go, from a row's menu: one question, one Yes.
    trow = row(page, "offers@travelnow.example")
    expect(trow.get_by_text("Unsubscribed")).to_be_visible()
    search = page.get_by_role("searchbox", name="Search senders")
    search.fill("recipe")
    expect(page.locator("li.srow")).to_have_count(1)
    rb = row(page, "hello@recipebox.example")
    rb.get_by_role("button", name="More actions for Recipe Box").click()
    page.get_by_role("button", name="Unsubscribe from Recipe Box and delete all 30 emails").click()
    expect(rb.locator(".cf-q")).to_have_text("Unsubscribe + delete 30 emails?")
    no_popup(page, "menu action confirm")
    n_before = len(demo.simulated)
    rb.locator("#confirm-yes").click()
    expect(page.locator("#toast.show, #result").first).to_contain_text("deleted 30 emails", timeout=20000)
    ops = [op for op, _ in demo.simulated[n_before:]]
    assert "trash" in ops, demo.simulated[n_before:]
    if page.locator("#result").count():
        no_popup(page, "result box")
        page.locator("#result-close").click()
        expect(page.locator("#result")).to_have_count(0)
    search.fill("")
    expect(page.locator("li.srow")).to_have_count(9)
    step(f"search 'recipe' narrows to 1 row; its menu's 'Unsubscribe + delete all' asked once in the row "
         f"('Unsubscribe + delete 30 emails?') and did both ({ops})")

    page.get_by_role("tab", name="Paid").click()
    expect(page.get_by_text("Streamora: Standard")).to_be_visible()
    page.get_by_role("combobox", name="Status Streamora: Standard").select_option("cancelled")
    page.get_by_role("combobox", name="Status Tunewave").select_option("cancelled")
    page.get_by_role("combobox", name="Status Cloudlocker").select_option("cancelled")
    for part in ("US$185.88/yr", "€131.88/yr", "₹2,628/yr"):
        expect(page.locator("#banner")).to_contain_text(part, timeout=10000)
    banner = page.locator("#banner").inner_text()
    assert "US$185.88/yr" in banner and "€131.88/yr" in banner and "₹2,628/yr" in banner, banner
    page.get_by_role("tab", name="Paid").click()
    page.evaluate("window.scrollTo(0, 0)")  # a sticky header would be drawn mid-page otherwise
    settle(page)
    page.screenshot(path=str(outdir / "06-paid-and-savings.png"))
    with page.expect_download() as dl2:
        page.locator("#banner").get_by_role("button", name="Save shareable card").click()
    dl2.value.save_as(outdir / "10-savings-card-money.png")
    step("marked 3 subscriptions cancelled; savings card shows US$185.88/yr · €131.88/yr · ₹2,628/yr separately (never summed)")

    cric = page.get_by_text("How to stop this mandate").first
    cric.click()
    expect(page.get_by_text("Google Pay (UPI AutoPay)").first).to_be_visible()
    no_iso(page.locator("#panel").inner_text(), "paid tab with revoke guides open")
    step("UPI mandate row shows sourced revoke guides")
    # A cancel page the user saved for this company (demo companies have no built-in one).
    page.evaluate("""() => fetch('/api/link', {method: 'POST', headers: {'Content-Type': 'application/json',
        'X-Clawback-CSRF': document.querySelector('meta[name=csrf-token]').content},
        body: JSON.stringify({domain: 'streamora.example', url: 'https://streamora.example/account/cancel'})})""")
    page.reload()
    page.get_by_role("tab", name="Paid").click()
    page.get_by_role("button", name="Open the cancel page for Streamora: Standard").click()
    prow = page.locator("tr", has=page.get_by_text("Streamora: Standard"))
    expect(prow.locator(".cf-q")).to_have_text("Open the cancel page?")
    expect(prow).to_contain_text("Clawback never cancels for you")
    no_popup(page, "paid confirm")
    prow.locator("#confirm-no").click()
    expect(prow.locator("#confirm-yes")).to_have_count(0)
    step("paid row: its own 'Cancel page' button asks 'Open the cancel page?' in the row (never cancels for you); cancelled")

    page.get_by_role("tab", name="Spam-like").click()
    srow = row(page, "team@moonshot.example")
    srow.get_by_role("checkbox").check()
    bar = page.locator(".actionbar")
    assert bar.get_by_role("button", name="Unsubscribe").count() == 0, "spam tab must not offer unsubscribe"
    assert page.locator("#panel [data-act=unsubscribe]").count() == 0, "spam rows must not offer unsubscribe"
    assert page.locator("#panel [data-act=unsubscribe_trash]").count() == 0
    expect(srow.get_by_role("button", name=re.compile(r"^Delete all \d+ emails from "))).to_be_visible()
    expect(srow.get_by_role("button", name=re.compile(r"^Block future mail from "))).to_be_visible()
    bar.get_by_role("button", name="Block future mail").click()
    expect(bar.locator(".cf-q")).to_have_text(re.compile(r"^Block .+\?$"))
    bar.locator("#confirm-yes").click()
    expect(page.locator("#toast.show")).to_contain_text("Blocked")
    expect(page.locator("#toast.show").get_by_role("button", name="Undo")).to_be_visible()
    step("spam tab: no Unsubscribe anywhere (rows offer Delete all and Block); Block asked in the bar; toast with Undo")

    page.get_by_role("button", name="More", exact=True).click()
    page.locator(".menu:not([hidden])").get_by_role("button", name="Did it work?").click()
    expect(page.get_by_role("tab", name="Did it work?")).to_be_visible()
    expect(page.locator("#panel").get_by_text("deals@shopmart.example")).to_be_visible()
    page.evaluate("document.querySelectorAll('#panel details').forEach(d => d.open = true)")
    no_iso(page.locator("#panel").inner_text(), "proof tab with timelines open")
    page.evaluate("document.querySelectorAll('#panel details').forEach(d => d.open = false)")
    page.evaluate("window.scrollTo(0, 0)")  # a sticky header would be drawn mid-page otherwise
    page.mouse.move(1, 1)
    settle(page)
    page.screenshot(path=str(outdir / "07-proof.png"))
    step("More menu opens 'Did it work?', which lists unsubscribed senders with verdicts")

    page.get_by_role("button", name="More", exact=True).click()
    page.locator(".menu:not([hidden])").get_by_role("button", name="Privacy").click()
    page.locator("#pemail").fill("me@mail.example")
    page.locator("#pname").fill("A. User")
    page.locator("#law").select_option("dpdp")
    page.get_by_role("button", name="Create draft").click()
    expect(page.locator("pre.letter").first).to_contain_text("Section 12")
    settle(page)
    page.screenshot(path=str(outdir / "25-privacy.png"))
    step("privacy (under More): DPDP draft created (not sent)")

    page.get_by_role("button", name="More", exact=True).click()
    page.locator(".menu:not([hidden])").get_by_role("button", name="History").click()
    no_iso(page.locator("#panel").inner_text(), "history tab")
    undo_btn = page.locator("#panel").get_by_role("button", name="Undo").first
    undo_btn.click()
    expect(page.locator("#panel .cf-q").first).to_have_text("Undo 1?")
    no_popup(page, "history undo")
    page.locator("#panel #confirm-no").click()
    expect(page.locator("#panel #confirm-yes")).to_have_count(0)
    settle(page)
    page.screenshot(path=str(outdir / "26-history.png"))
    step("history (under More) lists entries with Undo, which also asks first, in its row; no ISO dates on paid, proof, history")

    # keyboard: arrow keys move between the tabs shown
    page.get_by_role("tab", name="History").focus()
    page.keyboard.press("ArrowLeft")
    expect(page.get_by_role("tab", name="Paid")).to_be_focused()
    step("keyboard: ArrowLeft moves focus/selection across tabs")

    # Settings: a page in place of the results, not a dialog; Back returns.
    page.locator("#open-settings").click()
    expect(page.get_by_role("heading", name="Settings")).to_be_visible()
    expect(page.locator("#set-region")).to_be_focused()
    no_popup(page, "settings")
    settle(page)
    page.screenshot(path=str(outdir / "23-settings.png"))
    page.locator("#settings-back").click()
    expect(page.get_by_role("tab", name="Paid")).to_be_visible()
    expect(page.locator("#open-settings")).to_be_focused()
    step("settings: opens as a page (no dialog), region focused; Back returns to the results and refocuses the button")

    page.get_by_role("tab", name="Newsletters & ads").click()
    page.emulate_media(color_scheme="dark")
    page.evaluate("window.scrollTo(0, 0)")
    settle(page)
    page.screenshot(path=str(outdir / "08-dark.png"))
    step("dark mode rendered")

    # The moving background: a small canvas repainted about 8 times a second
    # (not 60), stretched over the window; it stops while the tab is hidden.
    def frames(pg):
        return int(pg.evaluate("document.getElementById('aurora-c').dataset.frames || '0'"))

    size = page.evaluate("[document.getElementById('aurora-c').width, document.getElementById('aurora-c').height]")
    assert size == [1280 // 16 + 1, 860 // 16 + 1 + (1 if 860 % 16 else 0)], size
    f0, t0 = frames(page), time.monotonic()
    page.wait_for_timeout(3000)
    fps = (frames(page) - f0) / (time.monotonic() - t0)
    assert 5 <= fps <= 9, f"aurora repaints {fps:.1f} times a second"
    page.evaluate("Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});"
                  "document.dispatchEvent(new Event('visibilitychange'))")
    hidden0 = frames(page)
    page.wait_for_timeout(1500)
    hidden = frames(page) - hidden0
    assert hidden == 0, f"{hidden} repaints while the tab was hidden"
    page.evaluate("Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});"
                  "document.dispatchEvent(new Event('visibilitychange'))")
    back = frames(page)
    page.wait_for_timeout(1000)
    assert frames(page) - back >= 4, "the aurora did not start again when the tab came back"
    loops = page.evaluate("document.getAnimations().filter(a => a.effect.getComputedTiming().iterations === Infinity).length")
    assert loops == 0, f"{loops} endless CSS animations on the results page"
    step(f"aurora: canvas {size[0]}x{size[1]} px for a 1280x860 window, {fps:.1f} repaints a second; 0 while the tab is "
         "hidden, running again after; no endless CSS animation on the results page")

    # Reduced motion: nothing animates (aurora, illustration, fades, the bar).
    rm = browser.new_context(viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
    watch(rm)
    rp = rm.new_page()
    app.active = None
    rp.goto(URL)
    expect(rp.get_by_role("heading", name="Where is your email?")).to_be_visible()
    running = rp.evaluate("document.getAnimations().length")
    app.active = "demo"
    rp.goto(URL)
    expect(rp.get_by_role("tab", name="Paid")).to_be_visible()
    rp.get_by_role("tab", name="Newsletters & ads").click()
    rp.locator("li.srow[data-address='digest@forum.example'] input[type=checkbox]").check()
    rp.locator(".actionbar").get_by_role("button", name="Delete all", exact=True).click()
    expect(rp.locator(".actionbar #confirm-yes")).to_be_visible()
    running += rp.evaluate("document.getAnimations().length")
    still0 = int(rp.evaluate("document.getElementById('aurora-c').dataset.frames || '0'"))
    rp.wait_for_timeout(1500)
    still = int(rp.evaluate("document.getElementById('aurora-c').dataset.frames || '0'")) - still0
    assert running == 0 and still == 0 and still0 >= 1, f"{running} animations, {still} aurora repaints with prefers-reduced-motion: reduce"
    rp.locator(".actionbar #confirm-no").click()
    rm.close()
    step(f"reduced motion: 0 animations on the connect screen, the results and the armed bar; the aurora is one still "
         f"frame, 0 repaints in 1.5 s (normal mode: {motion} animation(s) on connect)")

    mobile = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="light", is_mobile=True, locale="en-GB")
    watch(mobile)
    m = mobile.new_page()
    m.on("console", lambda msg: errors.append(f"mobile console {msg.type}: {msg.text}") if msg.type in ("error", "warning") else None)
    m.goto(URL)
    expect(m.get_by_role("tab", name="Paid")).to_be_visible()
    settle(m)
    m.screenshot(path=str(outdir / "11-mobile-top.png"))
    m.get_by_role("tab", name="Paid").click()
    settle(m)
    m.screenshot(path=str(outdir / "09-mobile.png"))
    width = m.evaluate("document.documentElement.scrollWidth")
    assert width <= 392, f"horizontal scroll on mobile: {width}"
    step(f"mobile 390px: no horizontal scroll (scrollWidth={width})")
    m.locator("#scan-again").click()
    expect(m.locator("#scan-panel #scan-start")).to_be_visible()
    m.locator("#scan-mode-last").check()
    expect(m.locator("#scan-last")).to_be_visible()
    settle(m)
    assert m.evaluate("document.documentElement.scrollWidth") <= 392
    m.screenshot(path=str(outdir / "21-mobile-scan-options.png"))
    m.keyboard.press("Escape")
    expect(m.locator("#scan-panel")).to_have_count(0)
    step("mobile scan options: newest-N field and Start scan fit at 390 px; closed without scanning")
    # Tab row: arrow keys reach every shown tab; "More" keeps a name when its word is hidden;
    # the page itself never scrolls sideways.

    def fully_visible(name):
        return m.evaluate("""n => { const t = document.getElementById('tab-' + n).getBoundingClientRect(),
            l = document.querySelector('[role=tablist]').getBoundingClientRect(); return t.left >= l.left - 1 && t.right <= l.right + 1; }""", name)
    m.get_by_role("tab", name="Paid").focus()
    for name in ("newsletter", "spam", "paid"):
        m.keyboard.press("ArrowRight")
        expect(m.locator(f"#tab-{name}")).to_be_focused()
        assert fully_visible(name), f"tab {name} not scrolled into view"
    expect(m.get_by_role("button", name="More", exact=True)).to_be_visible()
    assert m.evaluate("document.documentElement.scrollWidth") <= 392
    m.locator("#heads-up summary").click()
    for u in upcoming:
        assert gb_date(u["date"]) in m.locator("#heads-up").inner_text()
    step("mobile tab row: arrow keys scroll each tab fully into view; 'More' keeps its name with the word hidden; dates use no-break spaces")
    m.get_by_role("tab", name="Newsletters & ads").click()
    mrow = m.locator("li.srow[data-address='digest@forum.example']")
    mrow.get_by_role("button", name="Delete all 120 emails from Community Forum").click()
    expect(mrow.locator("#confirm-yes")).to_be_in_viewport()
    expect(mrow.locator("#confirm-no")).to_be_in_viewport()
    no_popup(m, "mobile row confirm")
    assert m.evaluate("document.documentElement.scrollWidth") <= 392
    settle(m)
    m.screenshot(path=str(outdir / "12-mobile-review.png"))
    mrow.locator("#confirm-no").click()
    for addr in ("digest@forum.example", "deals@shopmart.example"):
        m.locator(f"li.srow[data-address='{addr}'] input[type=checkbox]").check()
    expect(m.locator(".actionbar .count-pill")).to_be_in_viewport()
    no_popup(m, "mobile after ticking")
    assert m.evaluate("document.documentElement.scrollWidth") <= 392
    settle(m)
    m.screenshot(path=str(outdir / "22-mobile-selected.png"))
    m.locator(".actionbar").get_by_role("button", name="Delete all", exact=True).click()
    expect(m.locator(".actionbar #confirm-yes")).to_be_in_viewport()
    expect(m.locator(".actionbar .cf-q")).to_have_text("Delete 210 emails?")
    expect(m.locator(".actionbar #confirm-yes")).to_have_text("Yes, delete 210 emails")
    expect(m.locator(".actionbar #confirm-no")).to_be_in_viewport()
    assert m.evaluate("document.documentElement.scrollWidth") <= 392
    settle(m)
    m.screenshot(path=str(outdir / "27-mobile-bulk-confirm.png"))
    m.locator(".actionbar #confirm-no").click()
    step("mobile: the row and the bar ('Yes, delete 210 emails') show the Yes and Cancel without scrolling; no dialog; "
         "no sideways scroll")
    mc = mobile.new_page()
    app.active = None   # back to the connect screen for a mobile check
    mc.goto(URL)
    expect(mc.get_by_role("heading", name="Where is your email?")).to_be_visible()
    width = mc.evaluate("document.documentElement.scrollWidth")
    assert width <= 392, f"horizontal scroll on mobile connect: {width}"
    settle(mc)
    mc.screenshot(path=str(outdir / "13-mobile-connect.png"))
    app.active = "demo"
    step(f"mobile connect view: no horizontal scroll (scrollWidth={width})")

    # All accounts: a second (fictional) mailbox, combined with the first.
    app.add_source(DemoSource(account="demo-work", base_url=srv.base_url))
    app.start_scan(limit=100000)
    for _ in range(300):
        if app.job.snapshot()["status"] != "running":
            break
        time.sleep(0.1)
    page.emulate_media(color_scheme="light")
    page.goto(URL)
    page.locator("#account").select_option("__all__")
    expect(page.get_by_role("heading", name="All accounts")).to_be_visible()
    page.get_by_role("tab", name="Newsletters & ads").click()
    expect(page.locator(".in-acct").first).to_contain_text("In: Demo mailbox, Demo mailbox (demo-work)")
    assert page.get_by_role("button", name="Scan again").count() == 0, "the All view has no single scan button"
    news = api_get(page, "/api/senders?category=newsletter")["senders"]
    one = next(r for r in news if r["address"] == "digest@forum.example")
    single = api_get(page, "/api/senders?category=newsletter&account=demo")["senders"]
    one_single = next(r for r in single if r["address"] == "digest@forum.example")
    assert one["message_count"] == 2 * one_single["message_count"] and one["mailboxes"] == 2, (one, one_single)
    row(page, "digest@forum.example").get_by_role("checkbox").check()
    abar = page.locator(".actionbar")
    abar.get_by_role("button", name="Delete all", exact=True).click()
    expect(abar.locator(".cf-q")).to_have_text("Delete 240 emails?")
    expect(abar.locator("#confirm-yes")).to_have_attribute("data-count", "2")
    abar.get_by_text("What exactly happens").click()
    expect(abar.locator(".step")).to_have_count(2)
    expect(abar.locator(".step .in-acct")).to_have_count(2)
    expect(abar.locator(".step .in-acct").first).to_contain_text("Account: Demo mailbox")
    settle(page)
    page.screenshot(path=str(outdir / "15-all-accounts-review.png"))
    abar.locator("#confirm-no").click()
    # Clear the selection so the floating action bar does not cover the overview in the screenshot.
    row(page, "digest@forum.example").get_by_role("checkbox").uncheck()
    expect(page.locator(".actionbar")).to_have_count(0)
    page.evaluate("window.scrollTo(0, 0)")
    settle(page)
    page.screenshot(path=str(outdir / "16-all-accounts.png"))
    no_iso(page.locator("#view").inner_text(), "all accounts")
    page.locator("#account").select_option("demo")
    expect(page.get_by_role("heading", name="Demo mailbox", exact=True)).to_be_visible()
    assert page.locator("#account option").all_inner_texts() == ["All accounts", "Demo mailbox", "Demo mailbox (demo-work)"], \
        page.locator("#account option").all_inner_texts()
    # demo-work was scanned last; demo's own "Last scan" line must still show.
    expect(page.locator(".r-head .last")).to_have_text(re.compile(r"Last scan .+ · 480 emails from 24 senders"))
    step("all accounts: two fictional mailboxes combined (counts added across mailboxes), rows tagged 'In: Demo mailbox, Demo mailbox (demo-work)'; "
         "Delete all for one sender in both asks once in the bar: 'Delete 240 emails?' (count 2, one step per account); cancelled; "
         "back on demo its own Last scan line still shows")

    # Hero screenshot for the README: the newsletter list, light theme
    page.emulate_media(color_scheme="light")
    page.get_by_role("tab", name="Newsletters & ads").click()
    page.set_viewport_size({"width": 1280, "height": 860})
    page.evaluate("window.scrollTo(0, 0)")
    page.mouse.move(1, 1)
    settle(page)
    page.screenshot(path=str(outdir / "hero.png"))

    # Wide screen (1920) in both themes, and the phone in dark: the layout must stay
    # centred and readable, never stretched, and nothing may scroll sideways.
    page.set_viewport_size({"width": 1920, "height": 1080})
    for scheme, name in (("light", "17-wide.png"), ("dark", "18-wide-dark.png")):
        page.emulate_media(color_scheme=scheme)
        page.evaluate("window.scrollTo(0, 0)")
        settle(page)
        assert page.evaluate("document.documentElement.scrollWidth") <= 1920
        page.screenshot(path=str(outdir / name))
    md = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, locale="en-GB")
    watch(md)
    mp = md.new_page()
    mp.goto(URL)
    expect(mp.get_by_role("tab", name="Paid")).to_be_visible()
    settle(mp)
    assert mp.evaluate("document.documentElement.scrollWidth") <= 392
    mp.screenshot(path=str(outdir / "19-mobile-dark.png"))
    app.active = None
    mp.goto(URL)
    expect(mp.get_by_role("heading", name="Where is your email?")).to_be_visible()
    settle(mp)
    assert mp.evaluate("document.documentElement.scrollWidth") <= 392
    mp.screenshot(path=str(outdir / "28-mobile-connect-dark.png"))
    app.active = "demo"
    md.close()
    step("1920 px light and dark, 390 px dark (results and connect): no horizontal scroll")

    # Rough CPU cost of the moving background: total CPU time of every Edge
    # process (browser, GPU, renderer) over 10 s with the results page idle,
    # motion on vs. reduced motion, nothing else open. Headless Edge may
    # composite in software, so this is an upper bound for a real GPU.
    for c in (ctx, mobile):
        c.close()
    cdp = browser.new_browser_cdp_session()

    def cpu_time():
        return sum(x.get("cpuTime", 0) for x in cdp.send("SystemInfo.getProcessInfo")["processInfo"])
    cost = {}
    for label, extra in (("motion", {}), ("reduced", {"reduced_motion": "reduce"})):
        c = browser.new_context(viewport={"width": 1280, "height": 860}, color_scheme="dark", **extra)
        watch(c)
        pg = c.new_page()
        pg.goto(URL)
        expect(pg.get_by_role("tab", name="Paid")).to_be_visible()
        settle(pg)
        pg.wait_for_timeout(1000)
        t0, c0 = time.monotonic(), cpu_time()
        pg.wait_for_timeout(10000)
        cost[label] = (cpu_time() - c0) / (time.monotonic() - t0)
        c.close()
    assert cost["motion"] - cost["reduced"] < 0.20, f"the moving background costs {cost['motion'] - cost['reduced']:.0%} of a core"
    step(f"CPU (all Edge processes, 10 s idle on results, 1280x860 dark): motion {cost['motion']:.1%} of one core, "
         f"reduced motion {cost['reduced']:.1%}; difference {cost['motion'] - cost['reduced']:.1%}")
    browser.close()

httpd.shutdown()
srv.close()
print("CONSOLE/PAGE ERRORS:", errors if errors else "none")
print("REQUESTS TO OTHER SERVERS:", sorted(set(foreign)) if foreign else "none")
if errors or foreign:
    sys.exit(1)
print("ALL UI STEPS PASSED:", len(log))
