"""Web UI quality checks that do not need a browser.

What the user sees: two themes, pure white and pure black, every text colour
meets WCAG 2.x AA contrast in both, every colour token the stylesheet uses exists, every string the page
asks for exists (no raw "empty.paid.title" on screen), the bundled font and
icons are served and shipped in the wheel, the page asks nothing of any other
server, and the local server answers quickly. scripts/ui_smoke.py checks the
same things in a real browser (requests, console, reduced motion, 390 px).
There is deliberately no size limit on the page: quality comes first.
"""
import fnmatch
import http.client
import re
import threading
import time
from importlib import resources
from pathlib import Path

import pytest

from clawback.web import server as server_mod
from clawback.web.server import STATIC, App, make_server

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = resources.files("clawback.web").joinpath("static")
# Loose sanity bound for the server: every first-screen request, warm, in a
# row. Measured ~12 ms on the dev machine; this only catches something badly
# wrong (a blocking call, a per-request database rebuild), not a budget.
SLOW_MS = 1000


def _text(name):
    return STATIC_DIR.joinpath(name).read_text("utf-8")


def _static_files():
    base = ROOT / "clawback" / "web" / "static"
    return {p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


@pytest.fixture
def served():
    httpd, app = make_server(App(), 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)

    def get(path, want_headers=False):
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{app.port}"})
        r = conn.getresponse()
        body = r.read()
        assert r.status == 200, (path, r.status)
        return (body, dict(r.getheaders())) if want_headers else body
    yield get
    conn.close()
    httpd.shutdown()
    httpd.server_close()


# ------------------------------------------------------------------ assets
def page_assets():
    """Everything the page loads: index.html references, CSS url()s, and
    what app.js fetches at boot."""
    html = _text("index.html")
    refs = set(re.findall(r'(?:href|src)="/static/([^"]+)"', html))
    refs |= set(re.findall(r'url\("([^"]+)"\)', _text("app.css")))
    refs |= set(re.findall(r'fetch\("/static/([^"]+)"\)', _text("app.js")))
    return refs


def test_every_file_in_static_is_served_and_every_served_file_exists():
    # A new asset must be listed in server.STATIC (or the browser gets a 404),
    # and STATIC must not name a file that is not there.
    assert _static_files() - {"index.html"} == set(STATIC)


def test_everything_the_page_loads_is_served(served):
    assert page_assets() <= set(STATIC), page_assets() - set(STATIC)
    for name in page_assets():
        body, headers = served("/static/" + name, want_headers=True)
        assert len(body) > 100, name
        assert headers["Content-Type"].split(";")[0] == STATIC[name], name


def test_bundled_fonts_are_real_woff2_with_their_licence(served):
    for name in ("fonts/inter-latin.woff2", "fonts/inter-latin-ext.woff2"):
        body, headers = served("/static/" + name, want_headers=True)
        assert body[:4] == b"wOF2", name
        assert headers["Content-Type"] == "font/woff2"
    assert "SIL Open Font License" in _text("fonts/OFL.txt")


def test_wheel_package_data_covers_every_static_file():
    # (tomllib is 3.11+; the package supports 3.10, so read the one list by hand)
    toml = (ROOT / "pyproject.toml").read_text("utf-8")
    section = toml[toml.index("[tool.setuptools.package-data]"):]
    entry = section[section.index("clawback = ["):]
    globs = re.findall(r'"([^"]+)"', entry[:entry.index("]")])
    missing = [f for f in _static_files() if not any(fnmatch.fnmatch("web/static/" + f, g) for g in globs)]
    assert not missing, f"not shipped in the wheel: {missing}"


def test_page_asks_nothing_of_any_other_server():
    # Privacy: no CDN, web font service, analytics or remote image. Links the
    # user may click (Google's app-password page) are fine; loads are not.
    html = _text("index.html")
    assert not re.search(r'(?:src|href)="(?:https?:)?//', html)
    css = _text("app.css")
    assert "@import" not in css
    assert not re.search(r"url\(\s*['\"]?(?:https?:)?//", css)
    js = _text("app.js") + _text("icons.js")
    for call in re.findall(r"\b(?:fetch|api)\(\s*([^,)]+)", js):
        literals = re.findall(r'"([^"]*)"', call)
        assert all(x.startswith("/") for x in literals) and (literals or call == "path"), call
    assert re.findall(r'x\.open\("POST",\s*"([^"]+)', js) == ["/api/upload?name="]
    assert "new WebSocket" not in js and "sendBeacon" not in js and "importScripts" not in js


def test_csp_allows_only_this_server(served):
    _, headers = served("/", want_headers=True)
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "font-src 'self'" in csp and "script-src 'self'" in csp
    assert "http" not in csp and "*" not in csp


# ------------------------------------------------------------------ strings
def _literal_keys(js):
    return {k for k in re.findall(r'\bt\("([a-zA-Z][\w.-]*)"', js) if not k.endswith(".")}


def test_every_string_the_page_asks_for_exists():
    strings = __import__("json").loads(_text("i18n/en.json"))
    js = _text("app.js")
    keys = _literal_keys(js)
    keys |= set(re.findall(r'data-i18n(?:-label)?="([\w.-]+)"', _text("index.html")))
    # keys built at run time
    for tab in ("paid", "newsletter", "spam", "important", "proof", "privacy", "activity"):
        keys.add("tabs." + tab)
    for kind in ("newsletter", "spam", "important", "paid", "proof", "privacy", "activity", "filter"):
        keys |= {f"empty.{kind}.title", f"empty.{kind}.body"}
    for mode in ("auto", "light", "dark"):
        keys.add("nav.theme." + mode)
    for action in ("unsubscribe", "archive", "trash", "block", "report_spam", "escalate", "open_cancel"):
        keys.add("actions." + action)
    for m in ("one-click", "mailto", "link", "none"):
        keys.add("method." + m)
    for v in ("pending", "honoured", "still_sending"):
        keys.add("verdict." + v)
    # the inline confirm's question, per action; the connect picker; permission chips
    for action in ("trash", "unsubscribe", "unsubscribe_trash", "archive", "block", "report_spam", "escalate",
                   "open_cancel", "send_request", "undo"):
        keys |= {"confirm.q." + action, "sheet.yes." + action, "sheet.title." + action, "toast." + action}
    for pick in ("gmail", "outlook", "imap", "file"):
        keys.add("connect.tab." + pick)
    keys |= {"connect.limits.file", "connect.limits.imap"}
    for perm in ("read", "move", "filter", "send"):
        keys |= {f"perm.{perm}.short", f"perm.{perm}.title", f"perm.{perm}.body", f"perm.ms.{perm}.body"}
    missing = sorted(k for k in keys if k not in strings)
    assert not missing, missing


def test_confirm_wording_is_two_clicks_and_never_asks_to_type():
    # Click 1 picks the action; click 2 is the inline Yes that replaced the
    # buttons, whose own words say the action and the count. Nothing to
    # type, and the words say so.
    s = __import__("json").loads(_text("i18n/en.json"))
    assert "Nothing happens until you confirm" in s["actions.safety"]
    assert "{emails}" in s["confirm.q.trash"] and "{emails}" in s["sheet.yes.trash"]
    assert "{emails}" in s["confirm.q.unsubscribe_trash"] and "{emails}" in s["sheet.yes.unsubscribe_trash"]
    assert "30 days" in s["sheet.line.trash.gmail"]
    assert "can't be undone" in s["sheet.line.unsubscribe"]
    assert "can't be undone" in s["toast.unsubscribe"], "the unsubscribe toast says it cannot be undone"
    for key, value in s.items():
        low = value.lower()
        assert "lifetime" not in low and "no ads" not in low and "guarantee" not in low, key
        assert "type " not in low or "type" in key, (key, value)        # never "type the phrase"
        assert "trash1" not in low and "unsubscribe 1" not in low, key
    js = _text("app.js")
    assert "phrase" not in js and "typed" not in js
    assert 'id: "confirm-no"' in js and "no.focus()" in js            # focus starts on Cancel: Enter alone never acts
    assert 'text: said' in js and 'const said = t("sheet.yes." + action, vars);' in js   # the Yes says "Yes, delete 120 emails"
    assert "const CONFIRM_MS = 8000;" in js                             # the question goes back by itself


def test_no_popups_anywhere():
    # The operator's rule: nothing opens after ticking senders or pressing an
    # action. No <dialog>, no showModal, no browser alert/confirm/prompt, no
    # window opened by script. (ui_smoke.py checks the same in a browser.)
    html, js = _text("index.html"), _text("app.js")
    code = re.sub(r"(?m)^\s*//.*$", "", re.sub(r"/\*.*?\*/", "", js, flags=re.S))   # comments may say what is not there
    assert "<dialog" not in html and "dialog" not in code
    for call in ("showModal", "window.open(", "alert(", "confirm(", "prompt("):
        assert call not in code, call
    assert "aria-modal" not in js + html


# ------------------------------------------------------------------ server speed (loose)
def test_server_answers_the_first_screen_quickly(served):
    paths = ["/"] + ["/static/" + a for a in sorted(page_assets())] + ["/api/state"]
    for p in paths:          # warm: imports, database, settings
        served(p)
    best = None
    for _ in range(3):
        t0 = time.perf_counter()
        for p in paths:
            served(p)
        ms = (time.perf_counter() - t0) * 1000
        best = ms if best is None else min(best, ms)
    assert best < SLOW_MS, f"first-screen requests took {best:.0f} ms"


def test_warm_up_takes_the_new_database_cost_off_the_first_request(served):
    server_mod.warm_up()      # what `clawback ui` runs before opening the browser
    t0 = time.perf_counter()
    served("/api/state")
    assert (time.perf_counter() - t0) * 1000 < SLOW_MS


# ------------------------------------------------------------------ colour
def _tokens(block):
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block))


def _block(css, head):
    start = css.index(head)
    return css[start:css.index("}", start)]


def _themes():
    css = _text("app.css")
    light = _tokens(_block(css, ":root {"))
    dark = _tokens(_block(css, ':root[data-theme="dark"] {'))
    auto_dark = _tokens(_block(css, ':root:not([data-theme="light"]) {'))
    assert dark == auto_dark, "the two dark blocks must stay identical"
    assert set(dark) == set(light), "dark must redefine every colour token: " + str(set(light) ^ set(dark))
    return {"light": light, "dark": dark}


def test_the_two_themes_are_pure_white_and_pure_black():
    # The operator's ask (2026-11-01): black and white, both themes. The page
    # is #ffffff or #000000, every surface and border is a neutral grey (no
    # blue-tinted slate left over), form controls and scrollbars follow
    # (color-scheme), and the moving background stays faint enough that the
    # page still reads as white or black at the brightest blob.
    css = _text("app.css")
    themes = _themes()
    assert themes["light"]["bg"] == "#ffffff" and themes["dark"]["bg"] == "#000000"
    for name, tok in themes.items():
        for key in ("bg", "surface", "surface-2", "text", "muted", "line", "line-strong"):
            r, g, b = (int(tok[key][i:i + 2], 16) for i in (1, 3, 5))
            assert r == g == b, (name, key, tok[key], "not a neutral grey")
        glass, blob_o = _motion_params(name)
        worst = max(_backdrops(tok, blob_o), key=lambda c: abs(_lum(c) - _lum(tok["bg"])))
        assert ratio(worst, tok["bg"]) < 1.35, (name, "aurora too strong", worst)
    assert re.search(r":root \{\s*color-scheme: light;", css)
    assert css.count("color-scheme: dark;") == 2      # the system-dark block and the picked-dark block
    html = _text("index.html")
    assert '<meta name="theme-color" content="#ffffff"' in html and '<meta name="theme-color" content="#000000"' in html


def test_panels_stand_out_from_the_page_in_both_themes():
    # Cards, rows and the toast must not melt into a pure white or black page.
    dark = _themes()["dark"]
    glass, _ = _motion_params("dark")
    panel = _mix(dark["surface"], dark["bg"], glass)
    assert ratio(panel, dark["bg"]) >= 1.12, ("dark panel too close to black", panel)
    assert ratio(dark["surface-2"], dark["surface"]) >= 1.08
    for name, tok in _themes().items():
        glass, _ = _motion_params(name)
        panel = _mix(tok["surface"], tok["bg"], glass)
        assert ratio(tok["line"], panel) >= 1.2, (name, "panel border invisible")
        assert ratio(tok["line-strong"], tok["surface-2"]) >= 3, (name, "toast border")


def _lum(hex_):
    rgb = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT_PAIRS = [  # (foreground, background) as used on opaque surfaces in app.css
    # the toast is an opaque surface-2 card with a line-strong border: text and
    # its Undo button (accent-text) on surface-2 are checked below
    ("text", "bg"), ("text", "surface"), ("text", "surface-2"), ("text", "accent-soft"), ("text", "warn-bg"),
    ("muted", "bg"), ("muted", "surface"), ("muted", "surface-2"), ("muted", "accent-soft"),
    ("accent-text", "bg"), ("accent-text", "surface"), ("accent-text", "surface-2"), ("accent-text", "accent-soft"),
    ("accent-ink", "accent"),
    ("warn", "warn-bg"), ("warn", "surface"), ("bad", "bad-bg"), ("bad", "surface"), ("bad", "surface-2"),
    ("good", "good-bg"), ("good", "surface-2"), ("bad-ink", "bad"),
    # the savings card: a deep gradient from spot-1 to spot-2, money chips, and a light Save button
    ("spot-text", "spot-1"), ("spot-text", "spot-2"), ("spot-text", "spot-chip"), ("spot-muted", "spot-1"), ("spot-muted", "spot-2"),
    ("spot-accent", "spot-1"), ("spot-accent", "spot-2"), ("spot-1", "spot-text"),
    # avatar chips; tonal row buttons use accent-text on accent-soft (above)
] + [(f"av{i}-fg", f"av{i}-bg") for i in range(1, 7)]


def _mix(a, b, wa):
    """Colour a laid over colour b at opacity wa (0..1), as color-mix(in srgb) does."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * wa + y * (1 - wa)):02x}" for x, y in zip(ca, cb))


def _motion_params(theme):
    css = _text("app.css")
    block = _block(css, ":root {") if theme == "light" else _block(css, ':root[data-theme="dark"] {')
    glass = float(re.search(r"--glass:\s*([\d.]+)%", block).group(1)) / 100
    blob_o = float(re.search(r"--blob-o:\s*([\d.]+)", block).group(1))
    return glass, blob_o


def _backdrops(tok, blob_o):
    """Every colour the moving background can put behind a panel: the plain
    page, each blob at full strength, and each pair of blobs overlapping."""
    blobs = [tok[f"blob-{i}"] for i in range(1, 5)]
    out = [tok["bg"]] + [_mix(b, tok["bg"], blob_o) for b in blobs]
    out += [_mix(b2, _mix(b1, tok["bg"], blob_o), blob_o) for b1 in blobs for b2 in blobs if b1 != b2]
    return out


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_on_glass_over_the_moving_background_meets_wcag_aa(theme):
    # Panels are translucent (--glass) over the aurora, so what is behind the
    # text moves. Check the worst case: each text colour on a panel over each
    # blob and each pair of overlapping blobs at full strength, plus the
    # header (70 % surface), the footer pill, a selected row, a row with its
    # confirm armed and the toast.
    tok = _themes()[theme]
    glass, blob_o = _motion_params(theme)
    low = []
    for under in _backdrops(tok, blob_o):
        panel = _mix(tok["surface"], under, glass)
        surfaces = {"glass": panel, "header": _mix(tok["surface"], under, .70),
                    "selected row": _mix(tok["accent-soft"], panel, .85), "armed row": _mix(tok["warn-bg"], panel, .55),
                    "toast": tok["surface-2"]}
        for where, bg in surfaces.items():
            for fg in ("text", "muted", "accent-text"):
                r = ratio(tok[fg], bg)
                if r < 4.5:
                    low.append((where, fg, under, round(r, 2)))
    assert not low, f"{theme}: below 4.5:1 on glass {low[:6]}"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_colours_meet_wcag_aa(theme):
    tok = _themes()[theme]
    low = [(f, b, round(ratio(tok[f], tok[b]), 2)) for f, b in TEXT_PAIRS if ratio(tok[f], tok[b]) < 4.5]
    assert not low, f"{theme}: below 4.5:1 {low}"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_focus_ring_and_control_borders_are_visible(theme):
    # WCAG 1.4.11: focus indicators and input/button borders need 3:1.
    tok = _themes()[theme]
    for bg in ("bg", "surface", "surface-2"):
        assert ratio(tok["focus"], tok[bg]) >= 3, (theme, "focus", bg)
        assert ratio(tok["line-strong"], tok[bg]) >= 3, (theme, "line-strong", bg)
    assert ratio(tok["accent"], tok["surface"]) >= 3, (theme, "primary button edge")
    # inside the savings spotlight the focus ring is spot-accent
    for bg in ("spot-1", "spot-2"):
        assert ratio(tok["spot-accent"], tok[bg]) >= 3, (theme, "spotlight focus", bg)


def test_every_css_variable_used_is_defined():
    css = re.sub(r"/\*.*?\*/", "", _text("app.css"), flags=re.S)
    defined = set(re.findall(r"--([\w-]+)\s*:", css))
    used = set(re.findall(r"var\(--([\w-]+)", css))
    assert not used - defined, used - defined


def test_reduced_motion_turns_every_animation_off():
    css = _text("app.css")
    block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    block = block[:block.index("\n}\n")]
    assert "animation: none !important" in block and "transition: none !important" in block
    assert "*, *::before, *::after" in block


def test_background_motion_is_cheap_and_pauses():
    # The aurora is painted by app.js into a small canvas (1 px per 16 CSS px)
    # 8 times a second instead of 60, stops while the tab is hidden, paints
    # one still frame under reduced motion, and sits behind everything
    # without catching clicks. (ui_smoke.py measures the frame rate and the
    # CPU cost in a browser.)
    css, js, html = _text("app.css"), _text("app.js"), _text("index.html")
    assert '<canvas id="aurora-c"' in html and 'aria-hidden="true"' in html.split('<canvas id="aurora-c"')[0][-80:]
    assert "@keyframes drift" not in css, "no CSS-animated blobs left over"
    aurora = css[css.index(".aurora {"):]
    aurora = aurora[:aurora.index("}")]
    assert "pointer-events: none" in aurora and "z-index: -1" in aurora
    assert "const AURORA_STEP = 16, AURORA_MS = 125;" in js
    run = js[js.index("function auroraRun()"):]
    run = run[:run.index("\n}\n")]
    assert 'if (STILL.matches || document.visibilityState !== "visible") return;' in run
    assert "setTimeout(auroraRun, AURORA_MS)" in run and "requestAnimationFrame" not in run
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in js
    assert not re.search(r"(?<![-\w])filter:\s*blur", css), "no filter blur on anything"
    assert len(re.findall(r"(?<![-\w])backdrop-filter:\s*blur", css)) == 2, "backdrop blur only on the header and the bar"


# ------------------------------------------------------------------ dates
def test_dates_use_one_unambiguous_format():
    # Every date on screen goes through day()/when(): day, short month name,
    # year, joined by no-break spaces (no "27/10" vs "10/27" confusion, no
    # "2026-10-\n27" on a phone). Browser checks: ui_smoke.py / ui_states.py.
    js = _text("app.js")
    assert 'const DATE_FMT = { day: "numeric", month: "short", year: "numeric" }' in js
    assert r's.replace(/\s/g, "\u00a0")' in js
    calls = re.findall(r"toLocale\w*String\(([^)]*)\)", js)
    assert calls and all(c.startswith("LOCALE, ") for c in calls), calls
    assert len(re.findall(r"toLocaleDateString\(", js)) == 1, "dates only through day()"
    # every {date} handed to a string is formatted first
    code = re.sub(r"(?m)^\s*//.*$", "", js)       # comments may mention dates
    for value in re.findall(r"\bdate: ([^,}]+)", code):
        if value.strip() == "rdate.value":      # the rates date the user typed, sent to the server, not shown
            continue
        assert "day(" in value or "when(" in value or value.strip() == '""', value
    # a bare calendar date is parsed as a local day, not UTC midnight
    assert r"/^(\d{4})-(\d{2})-(\d{2})$/" in js


def test_review_shows_plain_words_and_folds_technical_details():
    from clawback.actions import Step
    assert "details" in Step.__dataclass_fields__
    js = _text("app.js")
    assert 'h("details", { class: "tech" }, h("summary", { text: t("review.technical") })' in js


def test_illustrations_use_no_inline_styles():
    # The page's CSP is style-src 'self': an inline style="" in the SVG markup
    # is blocked by the browser (and logged as an error), so the art would not
    # animate as designed. Delays and colours come from classes in app.css.
    from importlib import resources
    js = resources.files("clawback.web").joinpath("static/icons.js").read_text(encoding="utf-8")
    assert "style=" not in js
    css = resources.files("clawback.web").joinpath("static/app.css").read_text(encoding="utf-8")
    for cls in set(__import__("re").findall(r'class="([^"]+)"', js.replace('\\"', '"'))):
        for name in cls.split():
            assert f".{name}" in css, f"class {name} used in icons.js has no rule in app.css"
