"""A fake Microsoft identity platform + Microsoft Graph mail API on 127.0.0.1.

Real HTTP, so the code under test runs its real urllib paths: the authorize
redirect to the loopback listener, the PKCE check at the token endpoint,
refresh-token rotation, revocation, Graph paging, 401/403/429 answers and the
mail calls with their permission checks. Behaviour follows the Microsoft
pages cited in clawback/ms_oauth.py and clawback/sources/outlook.py.
"""
from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import http.server
import itertools
import json
import threading
import urllib.parse
from email.utils import parsedate_to_datetime

G = "https://graph.microsoft.com/"
WELL_KNOWN = ("inbox", "archive", "sentitems", "drafts", "outbox", "deleteditems", "junkemail")


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return enc({"alg": "none"}) + "." + enc(claims) + ".sig"


def short(scope: str) -> str:
    return scope.rsplit("/", 1)[-1].lower()


class FakeMicrosoft:
    def __init__(self, address="me@outlook.com", client_id="11111111-2222-3333-4444-555555555555",
                 consent="approve", signed_in_as=None, no_archive=False, omit_headers=False):
        self.address, self.client_id = address, client_id
        self.consent = consent                 # "approve" | "deny"
        self.signed_in_as = signed_in_as or address
        self.folders = {wk: f"F-{wk}" for wk in WELL_KNOWN if not (no_archive and wk == "archive")}
        self.omit_headers = omit_headers       # list answers without internetMessageHeaders
        self.messages: list[dict] = []         # newest first
        self.raw: dict[str, bytes] = {}
        self.codes: dict[str, dict] = {}
        self.tokens: dict[str, set[str]] = {}  # access token -> scopes (short)
        self.refresh: dict[str, set[str]] = {} # refresh token -> scopes
        self.requests: list[tuple[str, str]] = []
        self.authorize_params: list[dict] = []
        self.token_forms: list[dict] = []
        self.sent: list[dict] = []
        self.rules: dict[str, dict] = {}
        self.throttle = 0                      # answer this many Graph calls with 429 first
        self.counter = itertools.count(1)
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None, headers=None):
                data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if body is not None and not isinstance(body, bytes):
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer._route(self, "GET")

            def do_POST(self):
                outer._route(self, "POST")

            def do_DELETE(self):
                outer._route(self, "DELETE")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.graph = self.base + "/v1.0"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------ mailbox
    def add(self, raw: bytes, folder="inbox", read=True, headers=True) -> str:
        mid = f"AAMk-immutable-{next(self.counter)}="
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
        date = parsedate_to_datetime(msg["Date"])
        frm = email.utils.parseaddr(msg["From"] or "")
        m = {"id": mid, "internetMessageId": msg["Message-ID"] or f"<{mid}@fake>", "subject": msg["Subject"] or "",
             "from": {"emailAddress": {"name": frm[0], "address": frm[1]}},
             "receivedDateTime": date.astimezone(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "isRead": read, "parentFolderId": self.folders[folder], "conversationId": "conv-" + mid}
        if headers:
            m["internetMessageHeaders"] = [{"name": k, "value": str(v)} for k, v in msg.items()]
        self.messages.append(m)
        self.messages.sort(key=lambda x: x["receivedDateTime"], reverse=True)
        self.raw[mid] = raw
        return mid

    def revoke_all(self):
        self.tokens.clear()
        self.refresh.clear()

    def expire_access_tokens(self):
        self.tokens.clear()

    def folder_of(self, mid) -> str:
        fid = next(m["parentFolderId"] for m in self.messages if m["id"] == mid)
        return next(k for k, v in self.folders.items() if v == fid)

    # ------------------------------------------------------------ routing
    def _scopes_of(self, h) -> set[str] | None:
        auth = h.headers.get("Authorization", "")
        return self.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None

    def _route(self, h, method):
        u = urllib.parse.urlparse(h.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.requests.append((method, h.path))
        parts = u.path.strip("/").split("/")
        if len(parts) == 4 and parts[1:3] == ["oauth2", "v2.0"]:
            return self._authorize(h, q) if parts[3] == "authorize" else self._token(h)
        if parts[0] != "v1.0":
            return h._send(404, {"error": {"code": "NotFound"}})
        scopes = self._scopes_of(h)
        if scopes is None:
            return h._send(401, {"error": {"code": "InvalidAuthenticationToken", "message": "Access token has expired or is not yet valid."}})
        if h.headers.get("Prefer") != 'IdType="ImmutableId"':
            return h._send(400, {"error": {"code": "TestNeedsImmutableIds"}})
        if self.throttle:
            self.throttle -= 1
            return h._send(429, {"error": {"code": "TooManyRequests"}}, {"Retry-After": "3"})
        body = None
        if method == "POST":
            n = int(h.headers.get("Content-Length") or 0)
            body = json.loads(h.rfile.read(n) or b"{}")
        p = parts[1:]
        need = lambda *s: bool(scopes & {x.lower() for x in s})
        forbidden = lambda: h._send(403, {"error": {"code": "ErrorAccessDenied", "message": "Access is denied. Check credentials and try again."}})
        if p[:2] == ["me", "mailFolders"] and len(p) == 3 and method == "GET":
            if not need("mail.read", "mail.readwrite"):
                return forbidden()
            fid = self.folders.get(p[2].lower())
            return h._send(200, {"id": fid}) if fid else h._send(404, {"error": {"code": "ErrorFolderNotFound", "message": "The specified folder could not be found in the store."}})
        if p == ["me", "messages"] and method == "GET":
            if not need("mail.read", "mail.readwrite"):
                return forbidden()
            items = list(self.messages)
            flt = q.get("$filter", "")
            if flt.startswith("receivedDateTime ge "):
                cut = flt.split(" ge ", 1)[1]
                items = [m for m in items if m["receivedDateTime"] >= cut]
            assert q.get("$orderby") == "receivedDateTime desc"
            assert "internetMessageHeaders" in q.get("$select", "")
            top, skip = int(q.get("$top", 10)), int(q.get("$skip", 0))
            page = items[skip:skip + top]
            if self.omit_headers:
                page = [{k: v for k, v in m.items() if k != "internetMessageHeaders"} for m in page]
            out = {"value": page}
            if skip + top < len(items):
                nq = dict(q, **{"$skip": str(skip + top)})
                out["@odata.nextLink"] = self.graph + "/me/messages?" + urllib.parse.urlencode(nq)
            return h._send(200, out)
        if p[:2] == ["me", "messages"] and len(p) >= 3:
            mid = urllib.parse.unquote(p[2])
            m = next((x for x in self.messages if x["id"] == mid), None)
            if m is None:
                return h._send(404, {"error": {"code": "ErrorItemNotFound", "message": "The specified object was not found in the store."}})
            if len(p) == 3 and method == "GET":
                return h._send(200, m) if need("mail.read", "mail.readwrite") else forbidden()
            if p[3] == "$value" and method == "GET":
                return h._send(200, self.raw[mid], {"Content-Type": "text/plain"}) if need("mail.read", "mail.readwrite") else forbidden()
            if p[3] == "move" and method == "POST":
                if not need("mail.readwrite"):
                    return forbidden()
                dest = self.folders.get(body["destinationId"].lower(), body["destinationId"])
                if dest not in self.folders.values():
                    return h._send(404, {"error": {"code": "ErrorFolderNotFound"}})
                m["parentFolderId"] = dest
                return h._send(201, m)
        if p == ["me", "sendMail"] and method == "POST":
            if not need("mail.send"):
                return forbidden()
            self.sent.append(body)
            return h._send(202)
        if p[:4] == ["me", "mailFolders", "inbox", "messageRules"]:
            if not need("mailboxsettings.readwrite"):
                return forbidden()
            if method == "POST":
                rid = f"rule-{next(self.counter)}"
                self.rules[rid] = body
                return h._send(201, {"id": rid, **body})
            if method == "DELETE" and len(p) == 5:
                return h._send(204) if self.rules.pop(p[4], None) else h._send(404, {"error": {"code": "ErrorItemNotFound"}})
        return h._send(400, {"error": {"code": "BadRequest", "message": f"unhandled {method} {u.path}"}})

    def _authorize(self, h, q):
        self.authorize_params.append(q)
        if q.get("client_id") != self.client_id:
            # Microsoft shows an error page and never redirects back.
            return h._send(400, b"AADSTS700016: Application with identifier was not found in the directory.",
                           {"Content-Type": "text/plain"})
        assert q["response_type"] == "code"
        assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
        assert q["redirect_uri"].startswith("http://localhost:")
        redirect = q["redirect_uri"]
        if self.consent == "deny":
            loc = redirect + "?" + urllib.parse.urlencode({"error": "access_denied", "error_description": "The user has denied access to the scope requested by the client application.", "state": q["state"]})
        else:
            code = f"code-{next(self.counter)}"
            self.codes[code] = {"challenge": q["code_challenge"], "redirect": redirect,
                                "scopes": {short(s) for s in q["scope"].split()}}
            loc = redirect + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]})
        h._send(302, b"", {"Location": loc})

    def _issue(self, scopes: set[str]) -> dict:
        at, rt = f"at-{next(self.counter)}", f"rt-{next(self.counter)}"
        self.tokens[at] = set(scopes)
        self.refresh[rt] = set(scopes)
        graph = sorted(G + s for s in scopes if s not in {"openid", "email", "offline_access", "profile"})
        return {"token_type": "Bearer", "access_token": at, "refresh_token": rt, "expires_in": 3600,
                "scope": " ".join(graph + ["openid", "email"]),
                "id_token": jwt({"email": self.signed_in_as, "preferred_username": self.signed_in_as})}

    def _token(self, h):
        n = int(h.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(h.rfile.read(n).decode()).items()}
        self.token_forms.append(form)
        assert "client_secret" not in form, "a public client never sends a secret"
        bad = lambda: h._send(400, {"error": "invalid_grant", "error_description": "AADSTS70000: The provided grant has expired or been revoked.\r\nTrace ID: x"})
        if form.get("client_id") != self.client_id:
            return h._send(400, {"error": "unauthorized_client", "error_description": "AADSTS700016: Application not found."})
        if form["grant_type"] == "authorization_code":
            c = self.codes.pop(form.get("code", ""), None)
            if c is None or form.get("redirect_uri") != c["redirect"]:
                return bad()
            digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            if digest != c["challenge"]:
                return h._send(400, {"error": "invalid_grant", "error_description": "AADSTS501481: The Code_Verifier does not match the code_challenge."})
            return h._send(200, self._issue(c["scopes"]))
        if form["grant_type"] == "refresh_token":
            scopes = self.refresh.pop(form.get("refresh_token", ""), None)   # rotated: one use
            if scopes is None:
                return bad()
            asked = {short(s) for s in form.get("scope", "").split()}
            if not asked - {"openid", "email", "offline_access", "profile"} <= scopes:
                return h._send(400, {"error": "invalid_grant", "error_description": "AADSTS65001: The user or administrator has not consented."})
            return h._send(200, self._issue(scopes))
        return h._send(400, {"error": "unsupported_grant_type"})


def browser():
    """Plays the user's browser: opens the sign-in URL and follows the
    redirect back to the loopback listener (urllib follows the 302)."""
    import urllib.request
    opened = []

    def open_(url):
        opened.append(url)
        with urllib.request.urlopen(url, timeout=10) as r:
            r.read()
        return True
    open_.opened = opened
    return open_
