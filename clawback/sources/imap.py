"""IMAP source (read-only scan) with optional SMTP sending for mailto unsubscribes."""
from __future__ import annotations

import smtplib
import ssl
import threading
from email.message import EmailMessage

from ..mailbox import check_login, fetch_raw, iter_messages, open_imap, xoauth2_string
from .base import Source, SourceError


def smtp_host_for(imap_host: str) -> str:
    """imap.gmail.com -> smtp.gmail.com; outlook.office365.com -> smtp.office365.com."""
    h = imap_host.lower()
    if h.startswith("imap."):
        return "smtp." + h[5:]
    if h == "outlook.office365.com":
        return "smtp.office365.com"
    return h


def default_folders(host: str) -> list[str]:
    """Gmail over IMAP: "[Gmail]/All Mail" holds every message except Spam and
    Trash (archived newsletters too), like the Gmail API scan; your own sent
    mail in it is skipped by its \\Sent label. Other providers: INBOX."""
    return ["[Gmail]/All Mail"] if host.lower().strip() in {"imap.gmail.com", "imap.googlemail.com"} else ["INBOX"]


class ImapSource(Source):
    kind = "imap"

    def __init__(self, host: str, user: str, password: str, folders: list[str] | None = None,
                 account: str | None = None, smtp_host: str | None = None, allow_send: bool = False,
                 conn_factory=None, smtp_factory=None, oauth_token=None, *, port: int = 993,
                 security: str = "ssl", cafile: str | None = None, provider: dict | str | None = None,
                 users: list[str] | None = None, smtp_port: int | None = None, smtp_security: str | None = None):
        """``password`` is an app password, held in memory only (never written
        anywhere); or pass ``oauth_token`` (a token string or a callable
        returning a fresh one) to sign in with XOAUTH2.

        ``folders``: scan exactly these. Left empty, the folders are chosen
        from the server's own special-use list after sign-in (see
        mailbox.choose_folders), so they are right in any account language.
        ``provider``: a preset (or its id) from providers.json; it supplies
        the help text for a refused sign-in and the SMTP server.
        ``security``: "ssl" or "starttls"; certificates are always checked
        (``cafile`` trusts one extra certificate, e.g. Proton Mail Bridge's)."""
        from .. import providers as prov
        super().__init__(account or user)
        if isinstance(provider, str):
            provider = prov.get(provider)
        self.provider = provider
        self.host, self.user, self._password = host, user, password
        self.port, self.security, self.cafile = int(port), security, cafile
        if security not in prov.SECURITY:
            raise ValueError("security must be ssl or starttls")
        self._folders = list(folders) if folders else None
        self.users = users
        self._oauth_token = oauth_token
        self.stats: dict = {}
        smtp = (provider or {}).get("smtp") or {}
        self.smtp_host = smtp_host or smtp.get("host") or smtp_host_for(host)
        # Port 465 (implicit TLS) was the only option before presets; keep it
        # as the default when nothing better is known.
        preset_port = smtp.get("port") if self.smtp_host == smtp.get("host") else None
        self.smtp_port = int(smtp_port or preset_port or 465)
        self.smtp_security = smtp_security or smtp.get("security") or ("starttls" if self.smtp_port == 587 else "ssl")
        self.allow_send = allow_send
        self._conn_factory = conn_factory or (lambda h: open_imap(h, self.port, self.security, self.cafile))
        self._smtp_factory = smtp_factory
        self.capabilities = {"one-click", "link"} | ({"mailto", "send"} if allow_send else set())

    @property
    def folders(self) -> list[str]:
        """The folders chosen by the user, else the ones the last scan picked
        from the server's list, else the usual default before any scan."""
        return self._folders or self.stats.get("folders") or default_folders(self.host)

    def iter_messages(self, limit: int | None = None, progress=None, cancel=None, since=None, skip_ids=None):
        stats: dict = {}
        try:
            yield from iter_messages(self.host, self.user, self._password, self._folders, limit, self._conn_factory,
                                     progress=progress, cancel=cancel, since=since, skip_ids=skip_ids, stats=stats,
                                     oauth_token=self._oauth_token, provider=self.provider, users=self.users,
                                     port=self.port)
        finally:
            self.bodies, self.reused, self.capped = stats.get("bodies"), stats.get("reused", 0), stats.get("capped", False)
            self.stats = stats

    def verify(self) -> None:
        """Check the sign-in now (nothing is read), so a wrong app password is
        reported at connect time instead of as an empty "finished" scan."""
        check_login(self.host, self.user, self._password, self._conn_factory, oauth_token=self._oauth_token,
                    provider=self.provider, users=self.users, port=self.port)

    def fetch_body_links(self, ids, cancel=None):
        from ..extract import from_bytes
        raws = fetch_raw(self.host, self.user, self._password, list(ids), self.folders, self._conn_factory,
                         oauth_token=self._oauth_token, provider=self.provider, users=self.users, port=self.port,
                         cancel=cancel)
        return {i: from_bytes(raw, i).body_unsub for i, raw in raws.items()}

    def _smtp(self):
        ctx = ssl.create_default_context(cafile=self.cafile) if self.cafile else ssl.create_default_context()
        if self._smtp_factory is not None:
            return self._smtp_factory(self.smtp_host, self.smtp_port, context=ctx, timeout=30)
        if self.smtp_security == "starttls":
            smtp = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)
            try:
                smtp.ehlo()
                smtp.starttls(context=ctx)     # refuses to continue in plain text if STARTTLS fails
                smtp.ehlo()
            except BaseException:
                smtp.close()
                raise
            return smtp
        return smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=ctx, timeout=30)

    def missing_permission(self, capability: str) -> str | None:
        if capability in {"mailto", "send"} and not self.allow_send:
            return "sending email over SMTP with the same app password (turn on 'Allow sending')"
        return None

    def send_email(self, to: str, subject: str, body: str) -> dict:
        if not self.allow_send:
            raise SourceError("Sending is off for this IMAP account")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.user, to, subject
        msg.set_content(body)
        try:
            with self._smtp() as smtp:
                if self._oauth_token is not None:
                    tok = self._oauth_token() if callable(self._oauth_token) else self._oauth_token
                    smtp.ehlo()
                    smtp.auth("XOAUTH2", lambda challenge=None: xoauth2_string(self.user, tok).decode(),
                              initial_response_ok=True)
                else:
                    smtp.login(self.user, self._password)
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise SourceError(f"{self.smtp_host} refused the sign-in for sending. Use the same app password as for "
                              f"reading. Server said: {exc.smtp_error.decode(errors='replace') if isinstance(exc.smtp_error, bytes) else exc}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise SourceError(f"Could not send through {self.smtp_host}:{self.smtp_port}: {exc}") from exc
        return {"message_id": None}

    def describe(self) -> dict:
        d = super().describe()
        d.update(host=self.host, port=self.port, security=self.security, folders=self.folders,
                 provider=(self.provider or {}).get("id"),
                 auth="xoauth2" if self._oauth_token is not None else "password")
        return d
