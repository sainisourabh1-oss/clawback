"""Template for a new regional module. Copy to <code>.py, fill in, add the
module name to ``MODULES`` in regions/__init__.py, and add tests with
synthetic fixtures (never real mail). Every legal citation and every revoke
step must name its source; mark ``verified=False`` for general guidance."""
from __future__ import annotations

from .base import PrivacyLaw, Region, RevokeGuide  # noqa: F401


def detect(info):
    """Return a detect.Hit (with ``merchant`` set) for regional recurring-payment
    notices, or None. ``info`` is an extract.MessageInfo."""
    return None


REGION = Region(code="xx", name="Example region", detect=detect, revoke_guides=[], privacy_laws=[])
