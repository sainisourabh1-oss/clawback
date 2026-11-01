"""Mail sources. Each yields ``extract.MessageInfo`` and declares which mailbox
actions it supports. Read-only access is always the default."""
from __future__ import annotations

from .base import Source, SourceError, AuthError, ScanCancelled  # noqa: F401
