"""Compatibility wrapper around ``sources.gmail`` (v1 API: ``scan_gmail``)."""
from __future__ import annotations

from .detect import Hit, classify_message
from .extract import body_from_gmail_payload
from .sources.gmail import GmailSource, _gmail_query  # noqa: F401


def _body_from_payload(payload: dict) -> str:
    """Decoded visible text of a Gmail API payload (shared with IMAP/mbox)."""
    return body_from_gmail_payload(payload)


def scan_gmail(query: str = "newer_than:365d", limit: int = 500, account: str = "default") -> list[Hit]:
    """Classify recent Gmail messages (read-only)."""
    src = GmailSource(account=account, query=query)
    return [h for info in src.iter_messages(limit) if (h := classify_message(info))]
