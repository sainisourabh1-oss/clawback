"""Mail provider presets and honest connector labels (data/providers.json).

A preset fills in what the user would otherwise copy from a help page: the
IMAP and SMTP servers, ports and TLS mode, and the provider's own page on app
passwords. Folder names are deliberately NOT part of a preset: they differ by
account language ("Spam", "Bulk", "Junk-E-Mail", "Спам"), so the IMAP code asks
the server for its special-use folders instead (RFC 6154).

Every value in the data file names the official page it came from and the day
it was read. ``label_of`` returns the connector's honest label: VERIFIED (run
against the real service), TESTED WITH FAKES, or EXPERIMENTAL.
"""
from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

SECURITY = {"ssl", "starttls"}
LABELS = ("verified", "fakes", "experimental")


@lru_cache(maxsize=1)
def data() -> dict:
    raw = resources.files("clawback").joinpath("data/providers.json").read_text(encoding="utf-8")
    d = json.loads(raw)
    _validate(d)
    return d


def _validate(d: dict) -> None:
    """Fail loudly at import of a broken data file rather than connect to a
    half-filled preset. Every preset with servers names at least one source."""
    ids = set()
    for p in d["providers"]:
        if p["id"] in ids:
            raise ValueError(f"duplicate provider id {p['id']}")
        ids.add(p["id"])
        for side in ("imap", "smtp"):
            if side in p:
                s = p[side]
                if not s.get("host") or not isinstance(s.get("port"), int) or s.get("security") not in SECURITY:
                    raise ValueError(f"{p['id']}.{side} needs host, port and security")
        if ("imap" in p) != bool(p.get("sources")):
            raise ValueError(f"{p['id']}: a preset with servers must cite its sources")
        for src in p.get("sources", []):
            if not src.get("url", "").startswith("https://") or not src.get("checked_on"):
                raise ValueError(f"{p['id']}: each source needs an https url and checked_on")
    for c in d["connectors"]:
        if c["label"] not in LABELS:
            raise ValueError(f"connector {c['id']} has unknown label {c['label']}")


def providers() -> list[dict]:
    return data()["providers"]


def get(provider_id: str) -> dict | None:
    return next((p for p in providers() if p["id"] == provider_id), None)


def for_address(address: str) -> dict | None:
    """The preset whose domain matches ``address`` (exact domain, or a listed
    prefix such as "yahoo." for yahoo.co.uk), else None."""
    domain = (address or "").rsplit("@", 1)[-1].strip().lower()
    if not domain or "@" not in (address or ""):
        return None
    for p in providers():
        if domain in p.get("domains", []):
            return p
    for p in providers():
        if any(domain.startswith(pre) for pre in p.get("domain_prefixes", [])):
            return p
    return None


def connectors() -> list[dict]:
    return data()["connectors"]


def connector(connector_id: str) -> dict:
    for c in connectors():
        if c["id"] == connector_id:
            return c
    raise KeyError(connector_id)


def label_of(connector_id: str) -> str:
    return connector(connector_id)["label"]


def label_text(label: str) -> str:
    """"Verified" / "Tested with fakes" / "Experimental" (UI and CLI wording)."""
    return data()["labels"][label]["text"]


def connector_for_provider(provider_id: str | None) -> str:
    p = get(provider_id or "") or {}
    return p.get("connector") or ("gmail_imap" if provider_id == "gmail" else "imap_presets")


def is_microsoft_host(host: str) -> bool:
    """IMAP hosts where Microsoft has switched password sign-in off for
    Outlook.com (16 Sep 2024): only XOAUTH2 works there."""
    h = (host or "").strip().lower().rstrip(".")
    # outlook.office365.com is the documented host; any other name under
    # Microsoft's mail domains gets the same advice (it only changes a message).
    return h == "outlook.office365.com" or h.endswith((".outlook.com", ".office365.com"))


def microsoft() -> dict:
    return data()["microsoft"]


def public_view() -> dict:
    """What the web page needs: presets (no secrets exist here), labels and
    connectors. Served by the local server; the page never fetches anything
    from the providers' sites itself."""
    d = data()
    return {"providers": d["providers"], "connectors": d["connectors"], "labels": d["labels"]}


def login_names(provider: dict | None, address: str) -> list[str]:
    """User names to try at sign-in. iCloud documents the part before the @
    first, then the full address; everyone else uses the full address."""
    address = (address or "").strip()
    if (provider or {}).get("imap_user") == "local_part_first" and "@" in address:
        return [address.split("@", 1)[0], address]
    return [address]


class PresetError(ValueError):
    """A preset that cannot be used for an app-password connection, with the
    reason in plain words (Outlook.com: password sign-in switched off)."""


def imap_settings(provider_id: str | None, address: str, host: str | None = None, port=None,
                  security: str | None = None) -> dict:
    """Server settings for an IMAP connection: the preset's values, each one
    overridable by what the user typed (an unusual data centre, Bridge on
    another port). "other" (or no preset) needs a host."""
    p = get(provider_id) if provider_id else for_address(address)
    if provider_id and p is None:
        raise PresetError(f"Unknown provider {provider_id}")
    if p and p.get("auth") == "oauth_only" and not host:
        raise PresetError(p["blocked"])
    imap = (p or {}).get("imap") or {}
    host = (host or imap.get("host") or "").strip()
    if not host:
        raise PresetError("Enter the IMAP server name (your provider's help pages list it)")
    security = security or imap.get("security") or "ssl"
    if security not in SECURITY:
        raise PresetError("Security must be SSL/TLS or STARTTLS")
    try:
        port = int(port or imap.get("port") or (993 if security == "ssl" else 143))
    except (TypeError, ValueError):
        raise PresetError("The port must be a number") from None
    if not 0 < port < 65536:
        raise PresetError("The port must be between 1 and 65535")
    if p and p.get("auth") == "oauth_only" and is_microsoft_host(host):
        raise PresetError(p["blocked"])
    return {"provider": p, "host": host, "port": port, "security": security,
            "users": login_names(p, address), "connector": connector_for_provider((p or {}).get("id"))}
