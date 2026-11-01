"""Money parsing and formatting for many currencies and number formats.

Design rules (see docs/HEURISTICS.md for the reasoning):

* Never guess silently. Every parsed amount carries a ``certainty``:
  ``explicit`` (ISO code or an unambiguous symbol such as ``€``/``₹``/``A$``),
  ``inferred`` (an ambiguous symbol resolved by the sender's country domain,
  an ISO code elsewhere in the same message, or the user's region), or
  ``assumed`` (bare ``$`` with nothing else to go on: shown as "USD, assumed").
  A bare ``¥`` that cannot be resolved is kept as the pseudo-currency ``"¥?"``
  (JPY or CNY) and is never converted or added to anything.
* Different currencies are never added together. ``totals_by_currency`` keeps
  them apart; ``convert`` only works with rates the user supplied.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

# The ten most-traded currencies in the BIS Triennial Central Bank Survey
# (April 2025, share of global FX turnover), plus INR. See docs/HEURISTICS.md.
TOP_CURRENCIES = ["USD", "EUR", "JPY", "GBP", "CNY", "CHF", "AUD", "CAD", "HKD", "SGD"]
SUPPORTED = TOP_CURRENCIES + ["INR", "NZD", "SEK", "NOK", "DKK", "KRW", "MXN", "BRL", "ZAR", "PLN"]
AMBIGUOUS_YEN = "¥?"
ZERO_DECIMAL = {"JPY", "KRW"}

# Prefixed / symbol forms, matched longest first. ``None`` = ambiguous dollar.
_SYMBOLS: dict[str, str | None] = {
    "US$": "USD", "U.S.$": "USD", "AU$": "AUD", "A$": "AUD", "CA$": "CAD", "C$": "CAD",
    "HK$": "HKD", "SG$": "SGD", "S$": "SGD", "NZ$": "NZD", "MX$": "MXN", "R$": "BRL",
    "CN¥": "CNY", "RMB": "CNY", "元": "CNY", "JP¥": "JPY", "円": "JPY",
    "¥": AMBIGUOUS_YEN, "￥": AMBIGUOUS_YEN,
    "€": "EUR", "£": "GBP", "₹": "INR", "Rs.": "INR", "Rs": "INR", "₩": "KRW",
    "SFr.": "CHF", "Fr.": "CHF", "kr": None,  # "kr" (SEK/NOK/DKK) needs a code
    "$": None,
}
_DOLLAR_BY_TLD = {"ca": "CAD", "au": "AUD", "hk": "HKD", "sg": "SGD", "nz": "NZD", "us": "USD", "mx": "MXN", "br": "BRL"}
_YEN_BY_TLD = {"jp": "JPY", "cn": "CNY"}
_KRONA_BY_TLD = {"se": "SEK", "no": "NOK", "dk": "DKK"}
REGION_CURRENCY = {
    "US": "USD", "IN": "INR", "GB": "GBP", "UK": "GBP", "JP": "JPY", "CN": "CNY", "CH": "CHF",
    "AU": "AUD", "CA": "CAD", "HK": "HKD", "SG": "SGD", "NZ": "NZD", "SE": "SEK", "NO": "NOK",
    "DK": "DKK", "KR": "KRW", "MX": "MXN", "BR": "BRL", "ZA": "ZAR", "PL": "PLN",
    "DE": "EUR", "FR": "EUR", "ES": "EUR", "IT": "EUR", "NL": "EUR", "IE": "EUR", "AT": "EUR",
    "BE": "EUR", "PT": "EUR", "FI": "EUR", "GR": "EUR", "LU": "EUR", "EU": "EUR",
}

_DISPLAY_PREFIX = {"USD": "US$", "AUD": "A$", "CAD": "C$", "HKD": "HK$", "SGD": "S$", "NZD": "NZ$",
                   "EUR": "€", "GBP": "£", "INR": "₹", "JPY": "JP¥", "CNY": "CN¥", "KRW": "₩"}

_sym_alt = "|".join(re.escape(s) for s in sorted(_SYMBOLS, key=len, reverse=True))
_code_alt = "|".join(SUPPORTED)
# Number: digit groups separated by , . ' ’ or a (narrow) no-break/normal space
# that is followed by exactly three digits; optional 1–2 digit decimal part.
_NUM = r"(?:\d{1,3}(?:(?:[,.'’]|[   ](?=\d{3}(?!\d)))\d{2,3})+|\d+)(?:[.,]\d{1,2}(?!\d))?"
_TOKEN = rf"(?:(?<![A-Za-z])(?:{_sym_alt})|\b(?:{_code_alt})\b)"
_MONEY_RE = re.compile(
    rf"(?P<pre>{_TOKEN})\s?(?P<n1>{_NUM})(?:/-)?|(?<![\d.,])(?P<n2>{_NUM})\s?(?P<post>{_TOKEN})",
)
_CONTEXT_GOOD = re.compile(r"(?i)\b(total|amount|charged|charge|price|paid|payment|debit(?:ed)?|due|billed|bill|plan|subscription|fee|renew\w*|mandate|autopay)\b")
_CONTEXT_BAD = re.compile(r"(?i)\b(save|saving|off|discount|cashback|worth|up to|coupon|reward|was)\b\W*$")


@dataclass(frozen=True)
class Money:
    amount: float
    currency: str            # ISO 4217 code, or "¥?" when JPY/CNY cannot be told apart
    certainty: str           # explicit | inferred | assumed | ambiguous
    raw: str = ""

    @property
    def ambiguous(self) -> bool:
        return self.certainty == "ambiguous"


def parse_number(raw: str, currency: str | None = None) -> float | None:
    """Parse '1,234.56', '1.234,56', '1 234,56', "1'234.56", '1,23,456' and '¥1,200'."""
    s = raw.strip().replace(" ", " ").replace(" ", " ").replace("’", "'")
    seps = [(i, c) for i, c in enumerate(s) if c in ",.' "]
    if not seps:
        return float(s) if s.isdigit() else None
    last_i, last_c = seps[-1]
    tail = s[last_i + 1:]
    decimal = None
    if last_c in ",." and 1 <= len(tail) <= 2:
        decimal = last_i
    digits = "".join(c for i, c in enumerate(s) if c.isdigit() and (decimal is None or i < decimal))
    if not digits:
        return None
    value = float(digits)
    if decimal is not None:
        value += float("0." + tail)
    if currency in ZERO_DECIMAL and decimal is not None:
        # JPY/KRW prices have no minor unit; "¥1,20" is not a real price format.
        return None
    return round(value, 2)


def _tld(domain: str | None) -> str:
    return (domain or "").lower().rstrip(".").rsplit(".", 1)[-1]


def _resolve(token: str, text: str, domain: str | None, region: str | None) -> tuple[str, str]:
    up = token.upper()
    if up in SUPPORTED:
        return up, "explicit"
    cur = _SYMBOLS.get(token)
    if cur and cur != AMBIGUOUS_YEN:
        return cur, "explicit"
    codes_in_text = {c for c in re.findall(rf"\b({_code_alt})\b", text)}
    tld = _tld(domain)
    region_cur = REGION_CURRENCY.get((region or "").upper())
    if cur == AMBIGUOUS_YEN:
        if tld in _YEN_BY_TLD:
            return _YEN_BY_TLD[tld], "inferred"
        found = codes_in_text & {"JPY", "CNY"}
        if len(found) == 1:
            return found.pop(), "inferred"
        if region_cur in {"JPY", "CNY"}:
            return region_cur, "inferred"
        return AMBIGUOUS_YEN, "ambiguous"
    if token == "kr":
        if tld in _KRONA_BY_TLD:
            return _KRONA_BY_TLD[tld], "inferred"
        found = codes_in_text & {"SEK", "NOK", "DKK"}
        if len(found) == 1:
            return found.pop(), "inferred"
        return "kr?", "ambiguous"
    # Bare "$"
    if tld in _DOLLAR_BY_TLD:
        return _DOLLAR_BY_TLD[tld], "inferred"
    found = codes_in_text & {"USD", "CAD", "AUD", "HKD", "SGD", "NZD", "MXN"}
    if len(found) == 1:
        return found.pop(), "inferred"
    if region_cur in {"USD", "CAD", "AUD", "HKD", "SGD", "NZD", "MXN"}:
        return region_cur, "inferred" if region_cur != "USD" else "assumed"
    return "USD", "assumed"


def find_amounts(text: str, domain: str | None = None, region: str | None = None) -> list[tuple[Money, int]]:
    """Every money expression in ``text`` with a context score (higher = more billing-like)."""
    text = text or ""
    out: list[tuple[Money, int]] = []
    for m in _MONEY_RE.finditer(text):
        token = m.group("pre") or m.group("post")
        raw_num = m.group("n1") or m.group("n2")
        cur, certainty = _resolve(token, text, domain, region)
        value = parse_number(raw_num, cur)
        if value is None:
            continue
        before = text[max(0, m.start() - 60):m.start()]
        score = 1 + 2 * bool(_CONTEXT_GOOD.search(before + text[m.end():m.end() + 30])) - 3 * bool(_CONTEXT_BAD.search(before))
        out.append((Money(value, cur, certainty, m.group(0).strip()), score))
    return out


def parse_money(text: str, domain: str | None = None, region: str | None = None) -> Money | None:
    """The most billing-like amount in ``text`` (ties: the first one)."""
    found = find_amounts(text, domain, region)
    if not found:
        return None
    best = max(found, key=lambda x: x[1])  # max() keeps the first of equal scores
    return best[0] if best[1] > -1 else None


def _group_indian(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:]); head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def format_money(amount: float | None, currency: str) -> str:
    if amount is None:
        return "—"
    if currency == AMBIGUOUS_YEN:
        return f"¥{amount:,.0f} (JPY or CNY, unclear)"
    if currency == "kr?":
        return f"kr {amount:,.2f} (SEK/NOK/DKK, unclear)"
    neg = amount < 0
    amount = abs(amount)
    if currency in ZERO_DECIMAL:
        body = f"{round(amount):,}"
    elif currency == "INR":
        whole = int(round(amount * 100)) // 100
        cents = int(round(amount * 100)) % 100
        body = _group_indian(whole) + (f".{cents:02d}" if cents else "")
    else:
        body = f"{amount:,.2f}"
    prefix = _DISPLAY_PREFIX.get(currency)
    text = f"{prefix}{body}" if prefix else f"{currency} {body}"
    return ("-" if neg else "") + text


def totals_by_currency(items) -> dict[str, float]:
    """Sum (amount, currency) pairs per currency. Never mixes currencies."""
    totals: dict[str, float] = {}
    for amount, currency in items:
        if amount is None:
            continue
        totals[currency] = round(totals.get(currency, 0.0) + float(amount), 2)
    return totals


def convert(totals: dict[str, float], rates: dict, home: str) -> tuple[float, list[str]] | None:
    """Combine per-currency totals into ``home`` using USER-SUPPLIED rates only.

    ``rates`` = {"base": home, "date": "YYYY-MM-DD", "rates": {"USD": 83.1, ...}} where each
    rate is "units of home currency per one unit of that currency". Returns the estimate and a
    list of currencies that could not be converted (they are left out and reported).
    """
    if not rates or rates.get("base") != home:
        return None
    table = rates.get("rates") or {}
    total, missing = 0.0, []
    for cur, value in totals.items():
        if cur == home:
            total += value
        elif cur in table and isinstance(table[cur], (int, float)) and table[cur] > 0:
            total += value * float(table[cur])
        else:
            missing.append(cur)
    return round(total, 2), missing


def default_region_from_locale() -> tuple[str, str]:
    """(region, currency) from the OS locale, falling back to ("US", "USD")."""
    import locale
    names = []
    try:
        names.append(locale.getlocale()[0] or "")
    except (ValueError, TypeError):
        pass
    try:
        import ctypes  # Windows: GetUserDefaultLocaleName
        buf = ctypes.create_unicode_buffer(85)
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85):  # type: ignore[attr-defined]
            names.insert(0, buf.value)
    except (AttributeError, OSError):
        pass
    for name in names:
        m = re.search(r"[-_]([A-Za-z]{2})\b", name) or re.search(r"\(([A-Za-z ]+)\)", name)
        if m and len(m.group(1)) == 2:
            region = m.group(1).upper()
            if region in REGION_CURRENCY:
                return region, REGION_CURRENCY[region]
        for key, region in (("India", "IN"), ("United Kingdom", "GB"), ("Japan", "JP"), ("Canada", "CA"), ("Australia", "AU")):
            if key in name:
                return region, REGION_CURRENCY[region]
    return "US", "USD"
