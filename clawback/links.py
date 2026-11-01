"""Cancellation pages per vendor domain.

Built-in entries live in ``data/cancel_links.json``; each one was checked by
hand and records how and when (see the ``how`` / ``verified_on`` fields).
Users can add their own HTTPS links (``clawback link example.com URL``); a
user link must be on the same domain it is saved for.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path
from urllib.parse import urlparse

from . import config
from .detect import base_domain

NEUTRAL = {
    "www", "mail", "email", "billing", "payments", "pay", "invoice", "receipts", "notify", "notification",
    "no-reply", "noreply", "info", "team", "hello", "support", "accounts", "account", "help", "e", "em",
    "news", "updates", "members", "member",
}

_user_db: dict[str, str] = {}


@lru_cache(maxsize=1)
def builtin_entries() -> tuple[dict, ...]:
    text = resources.files("clawback").joinpath("data/cancel_links.json").read_text(encoding="utf-8")
    return tuple(json.loads(text)["links"])


def _builtin() -> dict[str, str]:
    return {e["domain"]: e["url"] for e in builtin_entries()}


BUILTIN = _builtin()


def entry_for_url(url: str) -> dict | None:
    return next((e for e in builtin_entries() if e["url"] == url), None)


def _user_path(path: Path | None = None) -> Path:
    return path or config.home() / "links.json"


def load_user_db(path: Path | None = None) -> None:
    global _user_db
    p = _user_path(path)
    if not p.exists():
        _user_db = {}
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read saved cancel links at {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Saved cancel links at {p} must contain a JSON object")
    _user_db = {str(k).lower(): str(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _extra_labels(domain: str, base: str) -> list[str]:
    if domain == base:
        return []
    return domain[: -len(base) - 1].split(".")


def _safe_link_for_domain(domain: str, url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        return (parsed.scheme == "https" and bool(host) and not parsed.username and not parsed.password
                and (host == domain or host.endswith("." + domain)))
    except (ValueError, AttributeError):
        return False


def cancel_link(domain: str) -> str | None:
    """Exact domain match, or a subdomain whose extra labels are neutral
    (billing.spotify.com -> spotify.com). aws.amazon.com deliberately does NOT
    match amazon.com: different product, different page. User links win but
    must point at their own domain; built-ins may use an official help domain."""
    d = (domain or "").lower().strip().rstrip(".")
    if d in _user_db and _safe_link_for_domain(d, _user_db[d]):
        return _user_db[d]
    if d in BUILTIN:
        return BUILTIN[d]
    for base, link in _user_db.items():
        if d.endswith("." + base) and all(x in NEUTRAL for x in _extra_labels(d, base)) and _safe_link_for_domain(base, link):
            return link
    for base, link in BUILTIN.items():
        if d.endswith("." + base) and all(x in NEUTRAL for x in _extra_labels(d, base)):
            return link
    return None


_GOOGLE_ONE = re.compile(r"(?i)\bgoogle one\b")


def find_cancel_link(domain: str, vendor: str = "", merchant: str = "") -> str | None:
    """Cancel page for a tracked subscription. Receipts from google.com are
    Google Play or Google One purchases; mandate alerts name a merchant whose
    domain we do not know, so they get no link (the revoke steps apply)."""
    if merchant:
        return None
    d = (domain or "").lower()
    if base_domain(d) == "google.com":
        return BUILTIN["one.google.com"] if _GOOGLE_ONE.search(vendor or "") else BUILTIN["play.google.com"]
    if base_domain(d) in {"claude.ai", "claude.com"}:
        return BUILTIN["anthropic.com"]
    if base_domain(d) == "chatgpt.com":
        return BUILTIN["openai.com"]
    for candidate in (d, base_domain(d)):
        link = cancel_link(candidate)
        if link:
            return link
    return None


def save_link(domain: str, url: str, path: Path | None = None) -> None:
    """Persist a user-supplied HTTPS cancellation URL after basic validation."""
    domain = domain.strip().lower().rstrip(".")
    if not domain or len(domain) > 253 or not re.fullmatch(r"[a-z0-9.-]+", domain):
        raise ValueError("Domain must be a valid hostname, e.g. example.com")
    labels = domain.split(".")
    if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in labels):
        raise ValueError("Domain must be a valid hostname, e.g. example.com")
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Cancel URL must be an HTTPS URL without embedded credentials")
    host = parsed.hostname.lower().rstrip(".")
    if host != domain and not host.endswith("." + domain):
        raise ValueError("Cancel URL host must be the saved domain or one of its subdomains")
    p = _user_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        db = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Saved cancel-link database is invalid JSON: {p}") from exc
    if not isinstance(db, dict):
        raise ValueError("Saved cancel-link database must contain a JSON object")
    db[domain] = url.strip()
    config.write_private(p, json.dumps(db, indent=2, sort_keys=True))


def email_settings_for(domain: str) -> dict | None:
    """Verified "turn off our emails here" guide for a company whose mail has
    no List-Unsubscribe header (data/email_settings.json), or None."""
    from .detect import base_domain
    text = resources.files("clawback").joinpath("data/email_settings.json").read_text(encoding="utf-8")
    return json.loads(text)["entries"].get(base_domain(domain or ""))
