"""Render every brand asset in docs/brand/ from source (original artwork, fictional data only).

Usage:
    pip install playwright pillow
    python scripts/render_brand.py            # uses the installed Microsoft Edge (or set CLAWBACK_BROWSER=chromium
                                              # after `python -m playwright install chromium`)

Inputs: the mark below, the Inter font bundled with the app (clawback/web/static/fonts, SIL OFL 1.1) and the
screenshots in docs/screenshots/ (made by scripts/ui_smoke.py from the built-in demo mailbox; every sender,
merchant and amount in them is fictional). Nothing is downloaded. See docs/brand/README.md for what each output
is for and where it is used.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "brand"
APP = OUT / "app"
SHOTS = ROOT / "docs" / "screenshots"
FONTS = ROOT / "clawback" / "web" / "static" / "fonts"
FONT = "Inter, 'Segoe UI', system-ui, -apple-system, Helvetica, Arial, sans-serif"

# Brand colours (the same tokens the web app uses). Since 2026-11-01 the app has two themes, pure black and
# pure white, so the assets are black (dark) and white (light) with the teal mark as the only colour.
TEAL = "#2dd4bf"       # mark tile
INK = "#0b1f2a"        # the mark's envelope outline
BLACK = "#000000"      # dark backgrounds (the app's dark page)
RAISED = "#161616"     # a card on black (the app's dark --surface)
ACCENT = "#0f766e"     # money figures on white
PAPER = "#f2f2f2"      # text on black (the app's dark --text)
MUTED_DARK = "#a8a8a8"
MUTED_LIGHT = "#525252"

# The mark: a pixel-art envelope on a strict 16x16 grid, hooked by three claws that pull it back.
# One character per pixel: t = tile (TEAL), k = outline (INK), w = paper (white), . = transparent.
# A plain, generic envelope (outline + V flap); no provider's logo shape. Every output is drawn from this grid:
# SVGs as one <rect> per horizontal run with shape-rendering="crispEdges", PNGs pixel by pixel and enlarged
# by whole numbers with nearest-neighbour, so the edges stay sharp at every size.
GRID = """
..tttttttttttt..
.tttttttttttttt.
ttttttttkttkttkt
tttttttkttkttktt
ttttttkttkttkttt
ttttttkttkttkttt
tkkkkkkkkkkkkttt
tkkwwwwwwwwkkttt
tkwkwwwwwwkwkttt
tkwwkwwwwkwwkttt
tkwwwkwwkwwwkttt
tkwwwwkkwwwwkttt
tkwwwwwwwwwwkttt
tkkkkkkkkkkkkttt
.tttttttttttttt.
..tttttttttttt..
""".strip().splitlines()
PALETTE = {"t": TEAL, "k": INK, "w": "#ffffff"}
assert len(GRID) == 16 and all(len(r) == 16 for r in GRID), "the mark must be exactly 16x16"


def mark() -> str:
    """SVG elements for the mark on a 16-unit grid (wrap in a transform to place and scale it)."""
    rects = []
    for y, row in enumerate(GRID):
        x = 0
        while x < 16:
            ch = row[x]
            run = 1
            while x + run < 16 and row[x + run] == ch:
                run += 1
            if ch in PALETTE:
                rects.append(f'<rect x="{x}" y="{y}" width="{run}" height="1" fill="{PALETTE[ch]}"/>')
            x += run
    return f'<g shape-rendering="crispEdges">{"".join(rects)}</g>'


def logo_svg(size: int = 64) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 16 16" '
            f'role="img" aria-label="Clawback logo"><title>Clawback</title>{mark()}</svg>')


def logo_png(size: int):
    """The mark as an RGBA image, exactly size x size; size must be a whole multiple of 16."""
    from PIL import Image
    assert size % 16 == 0, size
    im = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    for y, row in enumerate(GRID):
        for x, ch in enumerate(row):
            if ch in PALETTE:
                h = PALETTE[ch].lstrip("#")
                im.putpixel((x, y), tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (255,))
    return im.resize((size, size), Image.NEAREST)


def card_logo_group() -> str:
    """Drop-in replacement for clawback/card.py logo_group(): the same mark on that function's 64-unit grid."""
    return f'<g transform="scale(4)">{mark()}</g>'


def font_css() -> str:
    """@font-face rules for the bundled Inter (render time only; the SVG sources fall back to system fonts)."""
    rules = []
    for f in ("inter-latin.woff2", "inter-latin-ext.woff2"):
        rules.append(f'@font-face{{font-family:"Inter";font-weight:100 900;src:url("{(FONTS / f).as_uri()}") format("woff2")}}')
    return "".join(rules)


THEMES = {
    "dark": dict(bg1=BLACK, bg2=BLACK, text=PAPER, muted=MUTED_DARK, chip_bg=RAISED, chip_stroke="#2dd4bf",
                 chip_text="#e6e6e6", card="#ffffff", card_stroke="#ffffff", border="#2e2e2e"),
    "light": dict(bg1="#ffffff", bg2="#ffffff", text="#0a0a0a", muted=MUTED_LIGHT, chip_bg="#ffffff",
                  chip_stroke="#0f766e", chip_text="#0f5e58", card="#ffffff", card_stroke="#d4d4d4", border="#d4d4d4"),
}


def banner_svg(theme: str = "dark") -> str:
    """1600x480 README banner. Text sizes are chosen for GitHub's ~880 px README column (x0.55):
    the smallest text (24 px) still shows at about 13 px."""
    t = THEMES[theme]
    chips = ["Safe unsubscribe", "Paid-subscription finder", "Runs on your computer"]
    chip_svg, x = [], 96
    for c in chips:
        w = 44 + len(c) * 12.6
        chip_svg.append(f'<rect x="{x}" y="352" width="{w:.0f}" height="52" rx="26" fill="{t["chip_bg"]}" '
                        f'stroke="{t["chip_stroke"]}" stroke-opacity=".55" stroke-width="1.5"/>'
                        f'<text x="{x + w / 2:.0f}" y="386" text-anchor="middle" font-size="24" font-weight="500" '
                        f'fill="{t["chip_text"]}">{c}</text>')
        x += w + 16
    rows = [("Streamora", "US$185.88/yr"), ("Tunewave", "€131.88/yr"), ("CloudLocker", "₹2,628/yr")]
    card = [f'<rect x="1108" y="64" width="428" height="352" rx="28" fill="{t["card"]}" stroke="{t["card_stroke"]}" stroke-width="1.5"/>',
            f'<text x="1144" y="118" font-size="24" font-weight="600" fill="{ACCENT}">You\'ve clawed back</text>']
    y = 178
    for name, amt in rows:
        card.append(f'<text x="1144" y="{y}" font-size="26" fill="#0a0a0a">{name}</text>'
                    f'<text x="1500" y="{y}" font-size="28" font-weight="700" text-anchor="end" fill="{ACCENT}">{amt}</text>'
                    f'<line x1="1144" x2="1500" y1="{y + 24}" y2="{y + 24}" stroke="#e0e0e0" stroke-width="1.5"/>')
        y += 68
    card.append(f'<text x="1144" y="{y + 12}" font-size="22" fill="#525252">Estimates · demo data</text>')
    border = (f'<rect x=".75" y=".75" width="1598.5" height="478.5" rx="31.25" fill="none" stroke="{t["border"]}" stroke-width="1.5"/>'
              if t["border"] else "")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="480" viewBox="0 0 1600 480" role="img" aria-label="Clawback: claw back your inbox and your money">
<title>Clawback: claw back your inbox and your money</title>
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{t["bg1"]}"/><stop offset="1" stop-color="{t["bg2"]}"/></linearGradient>
<style>text{{font-family:{FONT}}}</style></defs>
<rect width="1600" height="480" rx="32" fill="url(#g)"/>{border}
<g transform="translate(96 56) scale(7)">{mark()}</g>
<text x="234" y="148" font-size="80" font-weight="800" fill="{t["text"]}" letter-spacing="-2">Clawback</text>
<text x="96" y="250" font-size="48" font-weight="700" fill="{t["text"]}" letter-spacing="-.5">Claw back your inbox and your money.</text>
<text x="96" y="302" font-size="28" fill="{t["muted"]}">Private, local email cleanup. Nothing happens until you confirm twice.</text>
{''.join(chip_svg)}
{''.join(card)}
</svg>"""


def social_html() -> str:
    """1280x640 social preview. All text sits inside x 72..664, y 72..568: clear of the 40 px edge margin
    GitHub suggests and of the side crop some sites apply to make a 1.91:1 card. Sized to stay readable when
    the card is shown ~400 px wide (x0.31: the 30 px chips still show at about 9 px, the title at 28 px)."""
    hero = (SHOTS / "hero.png").as_uri()
    chips = "".join(f"<span>{c}</span>" for c in ("Safe unsubscribe", "Subscription finder", "Local only"))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{font_css()}
html,body{{margin:0;width:1280px;height:640px;overflow:hidden;font-family:{FONT}}}
body{{background:{BLACK};color:{PAPER};position:relative}}
.left{{position:absolute;left:72px;top:84px;width:600px}}
.brand{{display:flex;align-items:center;gap:26px}} .brand svg{{width:128px;height:128px;flex:none}}
.name{{font-size:96px;font-weight:800;letter-spacing:-3px;line-height:1}}
.tag{{font-size:58px;font-weight:700;line-height:1.12;margin-top:52px;letter-spacing:-1px}}
.chips{{display:flex;flex-wrap:wrap;gap:12px;margin-top:44px}}
.chips span{{font-size:28px;font-weight:600;color:#e6e6e6;background:{RAISED};border:1.5px solid rgba(45,212,191,.55);
  border-radius:999px;padding:8px 20px}}
.shot{{position:absolute;left:720px;top:64px;width:900px;border-radius:20px;overflow:hidden;box-shadow:0 30px 80px rgba(0,0,0,.55);
  border:1px solid rgba(255,255,255,.18);transform:perspective(1600px) rotateY(-10deg);transform-origin:left center}}
.shot img{{width:900px;display:block}}
</style></head><body>
<div class="shot"><img src="{hero}" alt=""></div>
<div class="left"><div class="brand">{logo_svg(128)}<div class="name">Clawback</div></div>
<div class="tag">Claw back your inbox and your money.</div>
<div class="chips">{chips}</div></div>
</body></html>"""


# Feature strip: one focused crop per step, so the important part of each screen is large enough to read.
# Boxes are (left, top, right, bottom) in docs/screenshots pixels (1280 px wide captures); each box is widened
# to the panel's 16:10 shape around its centre. Re-check them when the screenshots' layout changes.
PANEL_W, PANEL_H = 736, 460
STEPS = [
    ("02-newsletters.png", (20, 85, 1260, 860), "Pick the senders you no longer want"),
    ("03-review-strip.png", (20, 110, 880, 640), "Two clicks: pick an action, then say yes"),
    ("04-done.png", (20, 225, 840, 860), "Done, with Undo right there"),
    ("06-paid-and-savings.png", (20, 112, 1220, 860), "See what you save, per currency"),
]


def crop_steps(tmp: Path) -> list[tuple[Path, str]]:
    from PIL import Image
    out = []
    for i, (name, (l, t, r, b), cap) in enumerate(STEPS, 1):
        im = Image.open(SHOTS / name).convert("RGB")
        w, h = r - l, b - t
        aspect = PANEL_W / PANEL_H
        if w / h < aspect:
            w = round(h * aspect)
        else:
            h = round(w / aspect)
        cx, cy = (l + r) / 2, (t + b) / 2
        left = int(min(max(cx - w / 2, 0), im.width - w))
        top = int(min(max(cy - h / 2, 0), im.height - h))
        box = (left, top, left + min(w, im.width), top + min(h, im.height))
        crop = im.crop(box)
        p = tmp / f"step-{i}.png"
        crop.save(p)
        out.append((p, cap))
    return out


def strip_html(cells: list[tuple[Path, str]], theme: str) -> str:
    dark = theme == "dark"
    bg, card, text, ring = ((BLACK, RAISED, PAPER, "#2e2e2e") if dark
                            else ("#ffffff", "#ffffff", "#0a0a0a", "#d4d4d4"))
    figs = "".join(f'<figure><img src="{p.as_uri()}" alt=""><figcaption><b>{i}</b>{cap}</figcaption></figure>'
                   for i, (p, cap) in enumerate(cells, 1))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{font_css()}
html,body{{margin:0;font-family:{FONT};background:{bg};color:{text}}}
.wrap{{display:grid;grid-template-columns:repeat(2,{PANEL_W}px);gap:32px;padding:32px;width:1600px;box-sizing:border-box}}
figure{{margin:0;background:{card};border-radius:20px;padding:16px;border:1.5px solid {ring}}}
img{{width:{PANEL_W - 32}px;height:{round((PANEL_W - 32) * PANEL_H / PANEL_W)}px;border-radius:12px;display:block;object-fit:cover}}
figcaption{{font-size:30px;font-weight:700;margin:16px 4px 4px;display:flex;align-items:center;gap:14px;letter-spacing:-.3px}}
figcaption b{{flex:none;width:44px;height:44px;border-radius:50%;background:{TEAL};color:{BLACK};font-size:24px;
  display:grid;place-items:center}}
</style></head><body><div class="wrap">{figs}</div></body></html>"""


def frame_html(img: Path, cap: str, n: int) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{font_css()}
html,body{{margin:0;width:960px;height:640px;overflow:hidden;font-family:{FONT};background:{BLACK};color:{PAPER}}}
img{{position:absolute;left:24px;top:24px;width:912px;height:524px;object-fit:cover;border-radius:14px}}
.cap{{position:absolute;left:24px;right:24px;bottom:22px;font-size:30px;font-weight:700;display:flex;gap:14px;align-items:center}}
.cap b{{width:44px;height:44px;border-radius:50%;background:{TEAL};color:{BLACK};font-size:24px;display:grid;place-items:center}}
.cap span.n{{margin-left:auto;font-size:20px;font-weight:500;color:{MUTED_DARK};display:flex;align-items:center;gap:10px}}
.cap span.n svg{{width:32px;height:32px}}
</style></head><body><img src="{img.as_uri()}" alt=""><div class="cap"><b>{n}</b>{cap}<span class="n">{logo_svg(32)}Clawback · demo data</span></div></body></html>"""


def render(browser, html: str, path: Path, w: int, h: int, full_page: bool = False, transparent: bool = False):
    tmp = OUT / f"_render-{path.stem}.html"
    tmp.write_text(html, encoding="utf-8", newline="\n")
    try:
        pg = browser.new_page(viewport={"width": w, "height": h})
        pg.goto(tmp.as_uri())
        pg.evaluate("document.fonts.ready")
        pg.wait_for_timeout(200)
        pg.screenshot(path=str(path), full_page=full_page, omit_background=transparent)
        pg.close()
    finally:
        tmp.unlink()


def svg_page(svg: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{font_css()}html,body{{margin:0;background:transparent}}'
            f'svg{{display:block}}</style></head><body>{svg}</body></html>')


def main():
    import tempfile

    from PIL import Image
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    APP.mkdir(parents=True, exist_ok=True)
    (OUT / "logo.svg").write_text(logo_svg(512) + "\n", encoding="utf-8", newline="\n")
    for size in (16, 32, 64, 128, 256, 512):
        logo_png(size).save(OUT / f"logo-{size}.png", optimize=True)
    # Ready-to-copy files for the web app (see docs/brand/app/README.md for where each one goes).
    (APP / "logo.svg").write_text(logo_svg(64) + "\n", encoding="utf-8", newline="\n")
    (APP / "favicon.svg").write_text(logo_svg(32) + "\n", encoding="utf-8", newline="\n")
    for size in (16, 32, 48, 192):
        logo_png(size).save(APP / f"favicon-{size}.png", optimize=True)
    # .ico with exact 16/32/48 frames (each a whole-number enlargement of the grid, no resampling blur).
    logo_png(48).save(APP / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)],
                      append_images=[logo_png(16), logo_png(32)])
    (APP / "card_logo_group.svg").write_text(
        '<!-- Paste-in body for clawback/card.py logo_group(): the mark on that function\'s 64-unit grid. -->\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">{card_logo_group()}</svg>\n',
        encoding="utf-8", newline="\n")
    for theme in THEMES:
        (OUT / f"banner-{theme}.svg").write_text(banner_svg(theme) + "\n", encoding="utf-8", newline="\n")
    channel = os.environ.get("CLAWBACK_BROWSER", "msedge")
    with sync_playwright() as p, tempfile.TemporaryDirectory() as td:
        b = p.chromium.launch(channel=None if channel == "chromium" else channel, headless=True)
        for theme in THEMES:
            render(b, svg_page(banner_svg(theme)), OUT / f"banner-{theme}.png", 1600, 480, transparent=True)
        render(b, social_html(), OUT / "social-preview.png", 1280, 640)
        cells = crop_steps(Path(td))
        for theme in THEMES:
            render(b, strip_html(cells, theme), OUT / f"feature-strip-{theme}.png", 1600, 900, full_page=True)
        frames = []
        for i, (img, cap) in enumerate(cells, 1):
            fp = Path(td) / f"frame-{i}.png"
            render(b, frame_html(img, cap, i), fp, 960, 640)
            frames.append(Image.open(fp).convert("RGB"))
        b.close()
    # Short looping GIF of the same four steps, ~2 s per frame. One palette built from all four frames, so no
    # frame loses its colours and nothing flickers between frames.
    sheet = Image.new("RGB", (frames[0].width, frames[0].height * len(frames)))
    for i, f in enumerate(frames):
        sheet.paste(f, (0, i * f.height))
    pal = sheet.quantize(colors=224, method=Image.Quantize.MEDIANCUT)
    gif = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    gif[0].save(OUT / "flow.gif", save_all=True, append_images=gif[1:], duration=2000, loop=0, optimize=True)
    for f in sorted(list(OUT.iterdir()) + list(APP.iterdir())):
        if f.suffix in {".png", ".gif", ".svg", ".ico"}:
            extra = ""
            if f.suffix in {".png", ".gif"}:
                with Image.open(f) as im:
                    extra = f" {im.width}x{im.height}"
            print(f"{f.relative_to(OUT).as_posix()}{extra} {f.stat().st_size:,} bytes")


if __name__ == "__main__":
    sys.exit(main())
