"""Sign in with Microsoft (Outlook.com, Hotmail, Live, Microsoft 365). EXPERIMENTAL.

OAuth 2.0 authorization code flow with PKCE for a *public* desktop client, as
Microsoft documents for native apps: no client secret exists, the browser
returns to a one-shot listener on this computer (``http://localhost:<port>``;
Microsoft ignores the port when matching a localhost redirect URI), and the
code is exchanged for tokens at the token endpoint.

Sources (read 2026-10-25):
* https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow
  (authorize/token endpoints, PKCE S256, public clients send no secret,
  offline_access for refresh tokens, invalid_grant)
* https://learn.microsoft.com/en-us/entra/identity-platform/reply-url
  (updated 2026-06-15: "the port component ... is ignored for the purposes of
  matching a localhost redirect URI"; "The IPv6 loopback address ([::1]) isn't
  currently supported")
* https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference
  (preferred_username / email identify the signed-in mailbox)

The user registers their own app (client id). One sign-in asks for
SIGN_IN_SCOPES at once: Mail.Read to scan; Mail.ReadWrite, Mail.Send and
MailboxSettings.ReadWrite are used only for actions the user confirms.
Tokens are stored in ``<data folder>/tokens/outlook-<account>.json`` with
owner-only permissions, never uploaded and never logged.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from . import config
from .sources.base import AuthError, SourceError

AUTHORITY = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
G = "https://graph.microsoft.com/"
READ = G + "Mail.Read"
READWRITE = G + "Mail.ReadWrite"
SEND = G + "Mail.Send"
RULES = G + "MailboxSettings.ReadWrite"
# openid + email: an ID token naming the signed-in mailbox, so the account
# label is the real address without asking for User.Read. offline_access: a
# refresh token, so the next scan needs no sign-in.
BASE_SCOPES = ("openid", "email", "offline_access")
# Every Microsoft sign-in asks for all of these in one consent screen, so a
# cleanup later never needs a second sign-in. Mail.ReadWrite moves mail to
# Deleted Items / Archive / Junk and back (Graph has no "delete for good" in
# Clawback); Mail.Send sends an unsubscribe email you confirmed;
# MailboxSettings.ReadWrite creates the rule for a sender you block. Mail.Read
# is included so a refresh that asks for reading alone stays consented.
SIGN_IN_SCOPES = (G + "Mail.Read", G + "Mail.ReadWrite", G + "Mail.Send", G + "MailboxSettings.ReadWrite")
CONSENT_TIMEOUT = 300           # same as the Google sign-in: read the screen, do 2FA
TENANTS = {"common", "consumers", "organizations"}
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SKEW = 120                      # refresh this many seconds before expiry


def client_path() -> Path:
    return config.home() / "microsoft_client.json"


def load_client() -> dict | None:
    """{"client_id", "tenant"} saved by an earlier sign-in, else None."""
    try:
        d = json.loads(client_path().read_text(encoding="utf-8"))
        return d if valid_client_id(d.get("client_id", "")) else None
    except (OSError, ValueError):
        return None


def save_client(client_id: str, tenant: str = "common") -> None:
    p = client_path()
    p.write_text(json.dumps({"client_id": client_id, "tenant": tenant}, indent=2), encoding="utf-8")
    config._private(p)


def valid_client_id(value: str) -> bool:
    return bool(_GUID.match((value or "").strip()))


def token_path(account: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_.@" else "_" for c in account.lower()) or "account"
    return config.home() / "tokens" / f"outlook-{safe}.json"


def _short(scope: str) -> str:
    """Graph returns scopes with or without the resource prefix and in any
    case; compare by the last part, case-insensitive."""
    return scope.rsplit("/", 1)[-1].lower()


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge S256): RFC 7636; 64 random bytes give a
    86-character verifier, inside the 43-128 allowed."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def id_token_address(id_token: str | None) -> str | None:
    """The address in an ID token. It came straight from the token endpoint
    over TLS (not from the browser), so only its payload is read."""
    if not id_token or id_token.count(".") != 2:
        return None
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    addr = claims.get("email") or claims.get("preferred_username") or ""
    return addr.strip().lower() if "@" in addr else None


class _Listener:
    """One-shot loopback HTTP listener for the redirect. Binds 127.0.0.1 (and
    ::1 on the same port when possible, because a browser may try IPv6 first
    for "localhost"); accepts one request carrying our ``state``."""

    def __init__(self, state: str):
        self.state, self.result, self.done = state, None, threading.Event()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if q.get("state", [""])[0] != outer.state:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"This sign-in link is not the one Clawback started.")
                    return
                outer.result = {k: v[0] for k, v in q.items()}
                ok = "code" in outer.result
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(("Signed in. You can close this tab and go back to Clawback." if ok else
                                  "The sign-in did not finish. Go back to Clawback for details.").encode())
                outer.done.set()

            def log_message(self, *a):      # never log the code
                pass

        self.servers = [http.server.HTTPServer(("127.0.0.1", 0), H)]
        self.port = self.servers[0].server_address[1]
        try:
            class V6(http.server.HTTPServer):
                address_family = socket.AF_INET6
            self.servers.append(V6(("::1", self.port), H))
        except OSError:
            pass
        self.threads = []
        for s in self.servers:
            s.timeout = 0.2
            t = threading.Thread(target=self._loop, args=(s,), daemon=True)
            t.start()
            self.threads.append(t)

    def _loop(self, s):
        while not self.done.is_set():
            s.handle_request()

    def wait(self, timeout: float) -> dict | None:
        self.done.wait(timeout)
        self.done.set()
        for t in self.threads:          # each loop notices within one 0.2 s poll
            t.join(2)
        for s in self.servers:
            s.server_close()
        return self.result


class MicrosoftSignIn:
    """Tokens for one mailbox. ``authority`` and ``open_browser`` are
    replaceable so tests run against a fake identity server."""

    def __init__(self, client_id: str, account: str, tenant: str = "common",
                 authority: str = AUTHORITY, on_auth_url=None, open_browser=webbrowser.open,
                 timeout: float = CONSENT_TIMEOUT, clock=time.time, token_file: Path | None = None):
        client_id = (client_id or "").strip()
        if not valid_client_id(client_id):
            raise SourceError("The Application (client) ID looks like 00000000-0000-0000-0000-000000000000. "
                              "Copy it from your app's Overview page in the Microsoft Entra admin center.")
        if tenant not in TENANTS and not _GUID.match(tenant or ""):
            raise SourceError("Tenant must be common, consumers, organizations or a tenant ID")
        self.client_id, self.tenant, self.authority = client_id, tenant, authority.rstrip("/")
        self.account = (account or "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.account):
            raise SourceError("Enter your Microsoft email address")
        self.on_auth_url, self.open_browser, self.timeout, self.clock = on_auth_url, open_browser, timeout, clock
        self._token_file = token_file
        self._data: dict | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ storage
    @property
    def token_file(self) -> Path:
        return self._token_file or token_path(self.account)

    def _load(self) -> dict:
        if self._data is None:
            try:
                d = json.loads(self.token_file.read_text(encoding="utf-8"))
                self._data = d if d.get("client_id") == self.client_id else {}
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def _save(self, d: dict) -> None:
        self._data = d
        p = self.token_file
        p.parent.mkdir(parents=True, exist_ok=True)
        config._private(p.parent)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        config._private(tmp)
        tmp.replace(p)
        config._private(p)

    def forget(self) -> None:
        """Delete the stored tokens (disconnect on this computer)."""
        self._data = {}
        try:
            self.token_file.unlink()
        except FileNotFoundError:
            pass

    def granted(self) -> set[str]:
        return {_short(s) for s in self._load().get("scopes", [])}

    def has(self, scope: str) -> bool:
        return _short(scope) in self.granted()

    # ------------------------------------------------------------ tokens
    def _endpoint(self, what: str) -> str:
        return f"{self.authority}/{self.tenant}/oauth2/v2.0/{what}"

    def _post_token(self, form: dict) -> dict:
        data = urllib.parse.urlencode({"client_id": self.client_id, **form}).encode()
        req = urllib.request.Request(self._endpoint("token"), data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read() or b"{}")
            except ValueError:
                err = {}
            code, desc = err.get("error", ""), (err.get("error_description") or "").split("\r\n")[0]
            if code in {"invalid_grant", "interaction_required", "consent_required"}:
                raise AuthError("Microsoft no longer accepts the saved sign-in (it was revoked, expired or the "
                                "password changed). Sign in with Microsoft again.") from None
            if code in {"invalid_client", "unauthorized_client"} or "AADSTS700016" in desc:
                raise AuthError("Microsoft does not recognise this Application (client) ID for this kind of "
                                "account. Check the app registration (README: Outlook.com).") from None
            raise AuthError(f"Microsoft sign-in failed ({code or exc.code}): {desc}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise SourceError(f"Could not reach Microsoft sign-in: {getattr(exc, 'reason', exc)}") from None

    def _store(self, tok: dict, asked: set[str]) -> None:
        if not tok.get("access_token"):
            raise AuthError("Microsoft sign-in returned no access token")
        old = self._load()
        addr = id_token_address(tok.get("id_token"))
        if addr and addr != self.account:
            # Never file one mailbox's mail under another address: actions
            # are routed by this label.
            raise AuthError(f"You signed in to Microsoft as {addr}, but this connection is for {self.account}. "
                            "Sign in with the matching account, or connect again with that address.")
        scopes = (tok.get("scope") or "").split() or sorted(asked)
        d = {"client_id": self.client_id, "tenant": self.tenant, "account": self.account,
             "access_token": tok["access_token"],
             "refresh_token": tok.get("refresh_token") or old.get("refresh_token"),
             "expires_at": self.clock() + int(tok.get("expires_in") or 3600), "scopes": scopes}
        self._save(d)

    def access_token(self, scopes: set[str] | None = None, force_refresh: bool = False) -> str:
        """A valid access token covering ``scopes`` (READ by default),
        refreshing or signing in as needed."""
        want = {READ} | set(scopes or ())
        with self._lock:
            d = self._load()
            have = self.granted()
            if d.get("access_token") and {_short(s) for s in want} <= have and not force_refresh \
                    and d.get("expires_at", 0) - SKEW > self.clock():
                return d["access_token"]
            if d.get("refresh_token") and {_short(s) for s in want} <= have:
                tok = self._post_token({"grant_type": "refresh_token", "refresh_token": d["refresh_token"],
                                        "scope": " ".join(sorted(want | set(BASE_SCOPES)))})
                self._store(tok, want)
                return self._data["access_token"]
            return self._consent(want | set(SIGN_IN_SCOPES)
                                 | {G + s for s in have if s not in {"openid", "email", "offline_access", "profile"}})

    def _consent(self, want: set[str]) -> str:
        scope = " ".join(sorted(want | set(BASE_SCOPES)))
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        listener = _Listener(state)
        redirect = f"http://localhost:{listener.port}"
        params = {"client_id": self.client_id, "response_type": "code", "redirect_uri": redirect,
                  "response_mode": "query", "scope": scope, "state": state,
                  "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"}
        params["login_hint"] = self.account
        url = self._endpoint("authorize") + "?" + urllib.parse.urlencode(params)
        if self.on_auth_url:
            self.on_auth_url(url)
        try:
            self.open_browser(url)
        except Exception:   # noqa: BLE001 - the UI also shows the link
            pass
        result = listener.wait(self.timeout)
        if result is None:
            raise AuthError(f"No answer from the Microsoft sign-in within {max(1, round(self.timeout / 60))} minute(s). "
                            "If Microsoft showed an error page (for example AADSTS700016 or AADSTS50011), check the "
                            "Application (client) ID and that http://localhost is registered as a redirect URI under "
                            "'Mobile and desktop applications'. Then try again.")
        if "code" not in result:
            err, desc = result.get("error", ""), (result.get("error_description") or "").split("\r\n")[0]
            if err == "access_denied":
                raise AuthError("The Microsoft sign-in was cancelled or permission was not given. Nothing was changed.")
            raise AuthError(f"Microsoft sign-in failed ({err}): {desc}")
        tok = self._post_token({"grant_type": "authorization_code", "code": result["code"], "redirect_uri": redirect,
                                "code_verifier": verifier, "scope": scope})
        self._store(tok, want)
        return self._data["access_token"]
