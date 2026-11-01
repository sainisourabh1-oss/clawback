"""Shared fixtures: isolated data folder, a local HTTPS unsubscribe server,
and DNS that resolves the demo's ``*.example`` hosts to it."""
from __future__ import annotations

import os

# Typer forces colour + terminal mode when GITHUB_ACTIONS/FORCE_COLOR is set,
# which puts ANSI codes into CliRunner output. Read at import time, so set it
# before anything imports typer.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
for _var in ("FORCE_COLOR", "PY_COLORS"):
    os.environ.pop(_var, None)

import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from clawback.demo import mailbox

DEMO_HOSTS = sorted({"unsubscribe.shopmart.example", "unsubscribe.weeklybyte.example", "unsubscribe.travelnow.example",
                     "unsubscribe.forum.example", "unsubscribe.fashionpicks.example", "www.gadgetstore.example",
                     "track.clickmetrics.example", "redirect.example", "plain.example", "slow.example", "fail.example"})


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAWBACK_HOME", str(tmp_path / "clawback-home"))
    return tmp_path / "clawback-home"


@pytest.fixture(autouse=True)
def no_real_imap(monkeypatch):
    """No test may sign in to a real mail server: a test that needs IMAP
    passes a fake conn_factory, patches open_imap itself, or runs a local
    server on 127.0.0.1."""
    import clawback.mailbox as mb
    real = mb.open_imap

    def refuse(host, *a, **kw):
        if host in {"127.0.0.1", "localhost"}:
            return real(host, *a, **kw)
        raise AssertionError(f"test tried to contact a real IMAP server ({host})")
    monkeypatch.setattr("clawback.sources.imap.open_imap", refuse)
    monkeypatch.setattr("clawback.mailbox.open_imap", refuse)


@pytest.fixture(autouse=True)
def virtual_time(monkeypatch):
    """Gmail quota pacing and backoff run on a virtual clock in tests, so a
    paced scan of minutes of real time finishes instantly. Tests that pass
    their own clock/sleep to GmailSource are unaffected."""
    import types
    from fakes import FakeClock
    import clawback.sources.gmail as gmail_mod
    clock = FakeClock()
    monkeypatch.setattr(gmail_mod, "time", types.SimpleNamespace(monotonic=clock, sleep=clock.sleep))
    return clock


class UnsubServer:
    def __init__(self):
        import trustme
        self.ca = trustme.CA()
        cert = self.ca.issue_cert(*DEMO_HOSTS, "localhost", "127.0.0.1")
        self.requests: list[dict] = []
        self.mode: dict[str, tuple] = {}   # path prefix -> (status, location)
        outer = self

        class H(BaseHTTPRequestHandler):
            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({"method": method, "host": self.headers.get("Host"), "path": self.path,
                                       "body": body, "headers": dict(self.headers)})
                status, location = outer.mode.get(self.headers.get("Host", "").split(":")[0], (200, None))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def do_POST(self):
                self._handle("POST")

            def do_GET(self):
                self._handle("GET")

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cert.configure_cert(ctx)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.client_ctx = ssl.create_default_context()
        self.ca.configure_trust(self.client_ctx)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return "https://{host}:%d" % self.port

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def unsub_server(monkeypatch):
    srv = UnsubServer()
    real = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if isinstance(host, str) and host.endswith(".example"):
            host = "127.0.0.1"
        return real(host, port, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    yield srv
    srv.close()


@pytest.fixture
def demo_raws(unsub_server):
    return mailbox(base_url=unsub_server.base_url)
