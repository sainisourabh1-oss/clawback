"""Shareable savings card (SVG, 1200x630). Contains ONLY totals: no sender,
vendor, address or date that could identify you. The web UI converts it to
PNG in the browser (canvas), so no image library is needed."""
from __future__ import annotations

from functools import lru_cache
from html import escape
from importlib import resources

from .money import format_money


def svg(summary: dict) -> str:
    money = summary.get("money", [])
    emails = summary.get("emails", {})
    lines = []
    for m in money[:3]:
        lines.append(f"{format_money(m['yearly'], m['currency'])} / year")
    if len(money) > 3:
        lines.append(f"+ {len(money) - 3} more currencies")
    silenced = emails.get("senders_silenced", 0)
    per_month = emails.get("per_month", 0)
    headline = "Saved by cancelling subscriptions (estimate)"
    if not lines:
        headline = "Inbox cleanup (estimate)"
        lines.append(f"{silenced} sender{'s' if silenced != 1 else ''} silenced")
    rows = []
    y = 300
    for i, text in enumerate(lines):
        size = 64 if i == 0 else 44
        rows.append(f'<text x="80" y="{y}" class="big" font-size="{size}">{escape(text)}</text>')
        y += size + 22
    sub = f"{silenced} sender{'s' if silenced != 1 else ''} silenced · about {per_month} fewer emails a month"
    if not money:
        sub = f"About {per_month} fewer emails a month"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630" role="img" aria-label="Clawback savings card">
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0b1f2a"/><stop offset="1" stop-color="#123b3a"/></linearGradient>
<style>text{{font-family:'Segoe UI',system-ui,-apple-system,Helvetica,Arial,sans-serif;fill:#f2f7f6}}.big{{font-weight:700}}.muted{{fill:#a9c9c4}}</style></defs>
<rect width="1200" height="630" fill="url(#bg)"/>
<g transform="translate(80 72) scale(1.5)">{logo_group()}</g>
<text x="200" y="128" font-size="46" class="big">Clawback</text>
<text x="80" y="220" font-size="30" class="muted">{escape(headline)}</text>
{''.join(rows)}
<text x="80" y="{y + 30}" font-size="32">{escape(sub)}</text>
<text x="80" y="590" font-size="22" class="muted">Estimates from my own email history · local, privacy-first email cleanup</text>
</svg>"""


@lru_cache(maxsize=1)
def logo_group() -> str:
    """The Clawback mark (a 16 x 16 pixel envelope with three claw strokes) on
    a 64-unit grid. Read from the web app's own logo.svg so there is one source."""
    svg = resources.files("clawback.web").joinpath("static/logo.svg").read_text(encoding="utf-8")
    inner = svg[svg.index("<g"):svg.rindex("</g>") + 4]
    return f'<g transform="scale(4)">{inner}</g>'


def logo_svg(size: int = 64) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 64 64" role="img" '
            f'aria-label="Clawback logo">{logo_group()}</svg>')
