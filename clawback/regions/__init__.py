"""Regional modules.

A region module is a Python module in this package exposing a ``REGION``
object (see ``base.Region``). It can contribute:

* ``detect(info)`` – recognise regional recurring-payment notices (bank mandate
  alerts, direct-debit notices) and return a ``detect.Hit`` with the merchant;
* ``revoke_steps`` – written, sourced steps for revoking such mandates;
* ``privacy_laws`` – data-deletion letter templates with correct citations;
* ``cancel_links`` – verified regional cancellation pages.

To add a region, copy ``_template.py``, fill it in, add it to ``MODULES`` below
and add tests with synthetic fixtures (never real mail).
"""
from __future__ import annotations

from importlib import import_module

MODULES = ["india", "eu", "us", "cards"]
_loaded = None


def all_regions():
    global _loaded
    if _loaded is None:
        _loaded = [import_module(f"{__name__}.{name}").REGION for name in MODULES]
    return _loaded


def get(code: str):
    for r in all_regions():
        if r.code == code:
            return r
    return None


def detect_mandate(info, region: str | None = None):
    """First regional mandate/direct-debit detection for this message, if any."""
    for r in all_regions():
        hit = r.detect(info)
        if hit is not None:
            hit.region = r.code
            return hit
    return None


def privacy_laws():
    out = []
    for r in all_regions():
        out.extend(r.privacy_laws)
    return out


def cancel_links() -> dict:
    links = {}
    for r in all_regions():
        links.update(r.cancel_links)
    return links
