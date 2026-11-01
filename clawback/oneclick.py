"""RFC 8058 one-click unsubscribe POST, with strict safety rules.

* HTTPS only; URLs with embedded user/password are refused.
* The host must not be a raw IP, punycode look-alike, URL shortener or local
  name, and every address it resolves to must be public (no LAN/loopback), so
  an email cannot make this tool poke your router or a local service.
* No cookies, no Authorization header, no Referer: the request carries nothing
  that identifies you beyond the URL the sender chose.
* The connection goes to the very address that was checked (the name is not
  looked up a second time), so a DNS answer that changes between the check
  and the connection (DNS rebinding) cannot point the request at your LAN.
  TLS still verifies the certificate against the host name.
* Redirects are followed manually, at most 3, and only to HTTPS URLs that pass
  the same checks. (RFC 8058 §3.2 says the POST "MUST NOT" need redirects, so
  most senders will not use them.)
* 15 s timeout per request. 2xx = accepted. Anything else is reported as
  failed with the reason, and nothing is retried automatically.
"""
from __future__ import annotations

import functools
import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from . import __version__
from .senders import suspicious_host

BODY = b"List-Unsubscribe=One-Click"
TIMEOUT = 15
MAX_REDIRECTS = 3
USER_AGENT = f"Clawback/{__version__} (RFC 8058 one-click unsubscribe)"


@dataclass
class PostResult:
    ok: bool
    status: int | None
    detail: str
    final_url: str


class UnsafeURL(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surface 3xx as HTTPError; we decide what to do with it


def check_url(url: str, allow_private: bool = False, resolver=None) -> str:
    """Return the host if ``url`` is safe to contact, else raise UnsafeURL."""
    return _checked(url, allow_private, resolver)[0]


def _checked(url: str, allow_private: bool = False, resolver=None) -> tuple[str, str]:
    """(host, the checked IP address to connect to), else raise UnsafeURL."""
    try:
        p = urlparse(url)
    except ValueError as exc:
        raise UnsafeURL(f"unparseable URL: {exc}") from exc
    if p.scheme != "https":
        raise UnsafeURL("not HTTPS")
    if p.username or p.password:
        raise UnsafeURL("URL contains a username/password")
    host = (p.hostname or "").lower().rstrip(".")
    why = suspicious_host(host)
    if why and not (allow_private and why in {"raw IP address instead of a domain", "local network host"}):
        raise UnsafeURL(why)
    try:
        infos = (resolver or socket.getaddrinfo)(host, p.port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        raise UnsafeURL(f"cannot resolve {host}: {exc}") from exc
    if not infos:
        raise UnsafeURL(f"cannot resolve {host}")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if not addr.is_global and not allow_private:
            raise UnsafeURL(f"{host} points to a private/local address ({addr})")
    return host, infos[0][4][0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to ``pinned_ip`` (already checked), with SNI and certificate
    verification for the URL's host name."""

    def __init__(self, *args, pinned_ip: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context: ssl.SSLContext, pinned_ip: str):
        super().__init__(context=context)
        self._pinned_ip = pinned_ip

    def https_open(self, req):
        return self.do_open(functools.partial(_PinnedHTTPSConnection, pinned_ip=self._pinned_ip), req,
                            context=self._context)


def post(url: str, context: ssl.SSLContext | None = None, allow_private: bool = False,
         timeout: float = TIMEOUT, resolver=None) -> PostResult:
    """Send the one-click POST. Never raises for network/HTTP problems."""
    context = context or ssl.create_default_context()
    method, data = "POST", BODY
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        try:
            _, ip = _checked(current, allow_private, resolver)
        except UnsafeURL as exc:
            return PostResult(False, None, f"refused: {exc}" + (" (after redirect)" if hop else ""), current)
        opener = urllib.request.OpenerDirector()
        for handler in (_PinnedHTTPSHandler(context, ip), _NoRedirect(),
                        urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
            opener.add_handler(handler)
        req = urllib.request.Request(current, data=data, method=method, headers={
            "User-Agent": USER_AGENT,
            **({"Content-Type": "application/x-www-form-urlencoded"} if data else {}),
        })
        try:
            with opener.open(req, timeout=timeout) as resp:
                resp.read(65536)
                return PostResult(200 <= resp.status < 300, resp.status, f"HTTP {resp.status}", current)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                nxt = urljoin(current, exc.headers["Location"])
                if hop == MAX_REDIRECTS:
                    return PostResult(False, exc.code, "too many redirects", nxt)
                if exc.code in (301, 302, 303):
                    method, data = "GET", None
                current = nxt
                continue
            return PostResult(False, exc.code, f"HTTP {exc.code} {exc.reason}", current)
        except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
            reason = getattr(exc, "reason", exc)
            return PostResult(False, None, f"network error: {reason}", current)
    return PostResult(False, None, "too many redirects", current)
