"""Shared types for regional modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class RevokeGuide:
    """How to stop a recurring mandate in one app/bank.

    ``verified`` is True only when the steps were checked against the
    ``source`` URL on ``checked_on``; otherwise the UI labels them
    "general guidance".
    """
    provider: str
    steps: list[str]
    source: str = ""
    checked_on: str = ""
    verified: bool = False


@dataclass
class PrivacyLaw:
    code: str            # e.g. "dpdp", "gdpr", "uk-gdpr", "ccpa"
    name: str
    region: str
    citation: str        # the exact provisions relied on
    deadline: str        # what the law says about response time, honestly stated
    template: str        # str.format fields: {company}, {email}, {name}, {date}
    notes: str = ""


@dataclass
class Region:
    code: str
    name: str
    detect: Callable = field(default=lambda info: None)
    revoke_guides: list[RevokeGuide] = field(default_factory=list)
    privacy_laws: list[PrivacyLaw] = field(default_factory=list)
    cancel_links: dict = field(default_factory=dict)
    description: str = ""
