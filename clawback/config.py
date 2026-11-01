"""Local paths and user settings. Everything lives in one private folder.

Set ``CLAWBACK_HOME`` to use another folder (tests do this).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .money import REGION_CURRENCY, SUPPORTED, default_region_from_locale


LEGACY_FILES = ("subscriptions.sqlite3", "links.json", "token.json", "client_secret.json")


def home() -> Path:
    env = os.environ.get("CLAWBACK_HOME")
    p = Path(env) if env else Path.home() / ".clawback"
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
        _private(p)
        if not env:
            import_legacy(Path.home() / ".unsubscribe", p)
    return p


def import_legacy(old: Path, new: Path) -> list[str]:
    """One-time copy of v1 data (database, saved links, OAuth files)."""
    import shutil
    copied = []
    if not old.is_dir():
        return copied
    for name in LEGACY_FILES:
        src, dst = old / name, new / name
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            _private(dst)
            copied.append(name)
    if copied:
        (new / "IMPORTED_FROM_UNSUBSCRIBE_V1.txt").write_text(
            "Copied from " + str(old) + ": " + ", ".join(copied) + "\n", encoding="utf-8")
    return copied


def db_path() -> Path:
    return home() / "subscriptions.sqlite3"


def token_path(account: str = "default") -> Path:
    safe = "".join(c if c.isalnum() or c in "-_.@" else "_" for c in account) or "default"
    if safe == "default":
        legacy = home() / "token.json"   # v1 location
        return legacy
    return home() / "tokens" / f"{safe}.json"


def client_secret_path() -> Path:
    return home() / "client_secret.json"


def settings_path() -> Path:
    return home() / "settings.json"


DEFAULTS = {
    "region": None,              # filled from the OS locale on first read
    "home_currency": None,
    "show_converted_total": False,
    "rates": None,               # {"base": "INR", "date": "YYYY-MM-DD", "rates": {"USD": 83.2}}
    "grace_days_one_click": 2,
    "grace_business_days_other": 10,
    "inactive_months": 3,
}


def load_settings() -> dict:
    data = dict(DEFAULTS)
    try:
        stored = json.loads(settings_path().read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            data.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, json.JSONDecodeError):
        pass
    if not data["region"] or not data["home_currency"]:
        region, currency = default_region_from_locale()
        data["region"] = data["region"] or region
        data["home_currency"] = data["home_currency"] or currency
    return data


def save_settings(changes: dict) -> dict:
    data = load_settings()
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError(f"Unknown setting: {key}")
        if key == "home_currency" and value not in SUPPORTED:
            raise ValueError(f"Unsupported currency: {value}")
        if key == "region" and value not in REGION_CURRENCY:
            raise ValueError(f"Unknown region code: {value}")
        if key in {"grace_days_one_click", "grace_business_days_other", "inactive_months"}:
            if not isinstance(value, int) or not 0 < value <= 60:
                raise ValueError(f"{key} must be a whole number between 1 and 60")
        if key == "rates" and value is not None:
            validate_rates(value)
        if key == "show_converted_total":
            value = bool(value)
        data[key] = value
    p = settings_path()
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    _private(p)
    return data


def validate_rates(value: dict) -> None:
    from datetime import date
    if not isinstance(value, dict) or value.get("base") not in SUPPORTED:
        raise ValueError("Rates need a 'base' currency code")
    try:
        date.fromisoformat(str(value.get("date")))
    except ValueError as exc:
        raise ValueError("Rates need a 'date' (YYYY-MM-DD) so totals can be labelled") from exc
    rates = value.get("rates")
    if not isinstance(rates, dict) or not rates:
        raise ValueError("Rates need a 'rates' object, e.g. {\"USD\": 83.2}")
    for code, rate in rates.items():
        if code not in SUPPORTED or not isinstance(rate, (int, float)) or rate <= 0:
            raise ValueError(f"Invalid rate for {code!r}")


def _private(p: Path) -> None:
    try:
        p.chmod(0o600)
    except OSError:
        pass
