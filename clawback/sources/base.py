"""Common source interface."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

from ..extract import MessageInfo


class SourceError(Exception):
    """A problem the user can fix; ``str(exc)`` is shown as-is."""


class AuthError(SourceError):
    pass


class ScanCancelled(SourceError):
    pass


Progress = Callable[[int, int | None], None]   # (done, total or None)


class Source:
    kind = "base"
    #: mailbox actions this source can perform. Unsubscribe by HTTPS (one-click
    #: POST or opening a page) never needs the mailbox, so every source has it.
    capabilities: set[str] = {"one-click", "link"}

    def __init__(self, account: str = "default"):
        self.account = account

    def iter_messages(self, limit: int | None = None, progress: Progress | None = None,
                      cancel: threading.Event | None = None, since=None, skip_ids=None) -> Iterator[MessageInfo]:
        """Newest first where the source allows it. ``since``: skip older mail.
        ``skip_ids``: ids already read by an earlier scan (may be ignored)."""
        raise NotImplementedError

    # Mailbox actions. Each returns data needed to undo it (or None).
    def send_email(self, to: str, subject: str, body: str) -> dict:
        raise SourceError(f"{self.kind} cannot send email")

    def trash(self, ids: list[str]) -> dict:
        raise SourceError(f"{self.kind} cannot move mail to trash")

    def untrash(self, ids: list[str]) -> None:
        raise SourceError(f"{self.kind} cannot restore mail")

    def archive(self, ids: list[str]) -> dict:
        raise SourceError(f"{self.kind} cannot archive mail")

    def unarchive(self, ids: list[str]) -> None:
        raise SourceError(f"{self.kind} cannot un-archive mail")

    def report_spam(self, ids: list[str]) -> dict:
        raise SourceError(f"{self.kind} cannot report spam")

    def unreport_spam(self, ids: list[str]) -> None:
        raise SourceError(f"{self.kind} cannot undo spam reports")

    def create_filter(self, address: str, mode: str) -> dict:
        raise SourceError(f"{self.kind} cannot create filters")

    def delete_filter(self, filter_id: str) -> None:
        raise SourceError(f"{self.kind} cannot delete filters")

    def fetch_body_links(self, ids: list[str], cancel: threading.Event | None = None) -> dict[str, str]:
        """{id: unsubscribe link found in that email's body ("" for none)} for
        sources that read only headers during a scan. Read-only; the links
        themselves are never fetched. Sources that already read every body
        (mbox, demo) have nothing more to read."""
        return {}

    def missing_permission(self, capability: str) -> str | None:
        """Human description of a permission the user must grant first, or None."""
        return None

    def grant(self, capability: str) -> None:
        """Ask the user for the extra permission (opens a consent screen)."""
        raise SourceError(f"{self.kind} has no extra permissions to grant")

    def can_clean_up(self) -> bool:
        """Every mailbox cleanup action this source supports is allowed now
        (no further sign-in needed)."""
        return not any(self.missing_permission(c) for c in ("trash", "block", "mailto") if c in self.capabilities)

    def describe(self) -> dict:
        can_delete = "trash" in self.capabilities
        return {"kind": self.kind, "account": self.account, "capabilities": sorted(self.capabilities),
                # the UI hides Delete when this connection cannot move mail at all,
                # and offers "Sign in again" once when it can but is not allowed yet
                "can_delete": can_delete, "needs_signin": can_delete and not self.can_clean_up()}


class SavedSource(Source):
    """An account opened from its saved results without a live connection
    (an app-password IMAP account - the password is never saved - or a file
    import). Its results, HTTPS unsubscribes and opening pages work; a new
    scan needs the account connected again."""
    kind = "saved"

    def __init__(self, account: str):
        super().__init__(account)
        self.capabilities = {"one-click", "link", "mailto-client"}

    def iter_messages(self, limit=None, progress=None, cancel=None, since=None, skip_ids=None):
        raise SourceError("This account is open from its saved results. To scan it again, connect it again "
                          "(Clawback never saves mail passwords) or import a newer export.")


class OfflineSource(Source):
    """Acts for an account whose mail was imported from a file (or when no
    live connection is needed): HTTPS unsubscribes, opening pages, and
    opening your mail app for mailto links. No mailbox changes."""
    kind = "offline"

    def __init__(self, account: str):
        super().__init__(account)
        self.capabilities = {"one-click", "link", "mailto-client"}

    def iter_messages(self, limit=None, progress=None, cancel=None, since=None, skip_ids=None):
        raise SourceError("This account was imported from a file: import a newer export to rescan")
