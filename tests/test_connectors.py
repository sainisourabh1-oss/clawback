"""Provider presets, sign-in messages, TLS and folder discovery for IMAP.

What the user should see: picking "Yahoo" (or typing an @yahoo.co.uk
address) fills in the right servers; a refused sign-in says what to do and
links the provider's own app-password page; Outlook.com says plainly that
passwords no longer work there; a connection that fails before sign-in says
whether it was the name, the port, TLS or the certificate; and folders are
found by what the server says they are, in any account language.
"""
import imaplib
import json
import socket
import ssl
import threading
from importlib import resources

import pytest

import realmail as R
from fakes import FakeIMAP
from clawback import providers as P
from clawback.mailbox import choose_folders, connection_error, decode_folder, list_folders, open_imap
from clawback.scan import run_scan
from clawback.sources.base import AuthError, SourceError
from clawback.sources.imap import ImapSource


def _mail(n=3, tag="shop"):
    raws = [R.msg(f"{tag.title()} {i} <news{i}@{tag}{i}.example>", f"Deals {tag} {i}", "Deals. Unsubscribe.",
                  days_ago=1 + 10 * i, lu=f"<https://{tag}{i}.example/u>", one_click=True)
            for i in range(n)]
    return list(reversed(raws))


# ----------------------------------------------------------------- data file
def test_every_preset_cites_an_official_page_and_a_date():
    d = json.loads(resources.files("clawback").joinpath("data/providers.json").read_text(encoding="utf-8"))
    ids = {p["id"] for p in d["providers"]}
    assert {"outlook", "yahoo", "icloud", "aol", "zoho", "fastmail", "gmx", "yandex", "proton", "other"} <= ids
    for p in d["providers"]:
        if "imap" not in p:
            assert p["id"] == "other"
            continue
        assert p["sources"], p["id"]
        for src in p["sources"]:
            assert src["url"].startswith("https://") and src["checked_on"] == "2026-10-25" and src["says"]
        # A preset that wants an app password links to the provider's own page.
        if p["auth"] == "app_password":
            assert p["app_password"]["url"].startswith("https://"), p["id"]


def test_broken_data_is_refused_rather_than_half_used():
    good = P.data()
    bad = json.loads(json.dumps(good))
    bad["providers"][2]["imap"]["security"] = "none"
    with pytest.raises(ValueError, match="security"):
        P._validate(bad)
    bad = json.loads(json.dumps(good))
    bad["providers"][2]["sources"] = []
    with pytest.raises(ValueError, match="cite"):
        P._validate(bad)
    bad = json.loads(json.dumps(good))
    bad["connectors"][0]["label"] = "works great"
    with pytest.raises(ValueError, match="label"):
        P._validate(bad)


def test_labels_are_honest():
    assert P.label_of("gmail_api") == "verified"
    assert P.label_of("outlook_graph") == "experimental" and P.label_of("proton_bridge") == "experimental"
    assert P.label_of("imap_presets") == "fakes" and P.label_of("gmail_imap") == "fakes"
    assert P.label_text("fakes") == "Tested with fakes"
    # Exactly one connector is VERIFIED; nothing else may claim it.
    assert [c["id"] for c in P.connectors() if c["label"] == "verified"] == ["gmail_api"]


@pytest.mark.parametrize("address,expected", [
    ("someone@yahoo.com", "yahoo"), ("Someone@Yahoo.co.uk", "yahoo"), ("x@ymail.com", "yahoo"),
    ("x@icloud.com", "icloud"), ("x@me.com", "icloud"), ("x@aol.com", "aol"), ("x@zohomail.com", "zoho"),
    ("x@fastmail.fm", "fastmail"), ("x@gmx.com", "gmx"), ("x@yandex.ru", "yandex"), ("x@pm.me", "proton"),
    ("x@hotmail.com", "outlook"), ("x@outlook.com", "outlook"), ("x@gmail.com", "gmail"),
    ("x@mycompany.example", None), ("not-an-address", None), ("", None), ("x@yahoo", None),
])
def test_address_picks_the_preset(address, expected):
    p = P.for_address(address)
    assert (p and p["id"]) == expected


def test_settings_come_from_the_preset_and_user_overrides_win():
    s = P.imap_settings("yahoo", "me@yahoo.com")
    assert (s["host"], s["port"], s["security"], s["users"]) == ("imap.mail.yahoo.com", 993, "ssl", ["me@yahoo.com"])
    s = P.imap_settings(None, "me@icloud.com")
    assert s["host"] == "imap.mail.me.com" and s["users"] == ["me", "me@icloud.com"]
    s = P.imap_settings("proton", "me@proton.me", port="1144")
    assert (s["host"], s["port"], s["security"], s["connector"]) == ("127.0.0.1", 1144, "starttls", "proton_bridge")
    s = P.imap_settings("other", "me@x.example", host="mail.x.example", security="starttls")
    assert (s["port"], s["security"]) == (143, "starttls")
    with pytest.raises(P.PresetError, match="IMAP server"):
        P.imap_settings("other", "me@x.example")
    with pytest.raises(P.PresetError, match="between"):
        P.imap_settings("yahoo", "me@yahoo.com", port=70000)
    with pytest.raises(P.PresetError, match="number"):
        P.imap_settings("yahoo", "me@yahoo.com", port="nine")
    with pytest.raises(P.PresetError, match="Unknown"):
        P.imap_settings("nope", "me@yahoo.com")


def test_outlook_app_password_is_refused_up_front_with_the_reason():
    with pytest.raises(P.PresetError, match="16 September 2024") as exc:
        P.imap_settings("outlook", "me@hotmail.com")
    assert "Sign in with Microsoft" in str(exc.value)
    # Typing the Microsoft server by hand gets the same answer.
    with pytest.raises(P.PresetError, match="16 September 2024"):
        P.imap_settings(None, "me@live.com", host="outlook.office365.com")


# ----------------------------------------------------------------- sign-in
def _src(fake, host="imap.mail.yahoo.com", user="me@yahoo.com", password="wrong", **kw):
    return ImapSource(host, user, password, account=user, conn_factory=fake, **kw)


def test_wrong_password_names_the_provider_and_its_app_password_page():
    fake = FakeIMAP(_mail(), password="right")
    with pytest.raises(AuthError) as exc:
        list(_src(fake, provider="yahoo").iter_messages())
    text = str(exc.value)
    assert "Yahoo" in text and "https://help.yahoo.com/kb/SLN15241.html" in text and "AUTHENTICATIONFAILED" in text
    assert "wrong" not in text, "the password must never appear in a message"


def test_server_asking_for_an_app_password_is_explained():
    fake = FakeIMAP(_mail(), password="app", refusal=b"[AUTHENTICATIONFAILED] Please use an app password (see help)")
    with pytest.raises(AuthError, match="wants an app password"):
        list(_src(fake, host="imap.aol.com", user="me@aol.com", provider="aol").iter_messages())


def test_two_factor_not_enabled_is_part_of_the_advice():
    fake = FakeIMAP(_mail(), password="app")
    with pytest.raises(AuthError) as exc:
        list(_src(fake, host="imap.mail.me.com", user="me@icloud.com", provider="icloud").iter_messages())
    assert "two-factor authentication" in str(exc.value) and "support.apple.com" in str(exc.value)


def test_microsoft_rejecting_basic_auth_says_passwords_are_switched_off():
    fake = FakeIMAP(_mail(), password="never", refusal=b"LOGIN failed.")
    with pytest.raises(AuthError) as exc:
        list(_src(fake, host="outlook.office365.com", user="me@outlook.com").iter_messages())
    assert "16 September 2024" in str(exc.value) and "Sign in with Microsoft" in str(exc.value)


def test_icloud_tries_the_short_user_name_then_the_full_address():
    fake = FakeIMAP(_mail(), password="app", users={"me@icloud.com"})
    s = P.imap_settings("icloud", "me@icloud.com")
    src = _src(fake, host=s["host"], user="me@icloud.com", password="app", provider=s["provider"], users=s["users"])
    assert len(list(src.iter_messages())) == 3
    assert fake.logins == ["me", "me@icloud.com"]


def test_bridge_refusal_points_to_the_bridge_password():
    fake = FakeIMAP(_mail(), password="bridge-pw")
    with pytest.raises(AuthError, match="Mailbox details"):
        list(_src(fake, host="127.0.0.1", user="me@proton.me", provider="proton", port=1143,
                  security="starttls").iter_messages())


# ----------------------------------------------------------------- folders
GMAIL_DE = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
    b'(\\All \\HasNoChildren) "/" "[Gmail]/Alle Nachrichten"',
    b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
    b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Papierkorb"',
    b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Gesendet"',
]
# Yandex in Russian: modified UTF-7 names, and the archive is its own folder.
YANDEX_RU = [
    b'(\\HasNoChildren \\Unmarked) "|" INBOX',
    b'(\\HasNoChildren \\Unmarked \\Archive) "|" "&BBAEQARFBDgEMg-"',
    b'(\\HasNoChildren \\Unmarked \\Junk) "|" "&BCEEPwQwBDw-"',
    b'(\\HasNoChildren \\Unmarked \\Trash) "|" "&BCMENAQwBDsENQQ9BD0ESwQ1-"',
    b'(\\HasNoChildren \\Unmarked \\Sent) "|" "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"',
]


def test_gmail_in_german_scans_alle_nachrichten_only():
    fake = FakeIMAP([], gmail=True, listing=GMAIL_DE, boxes={"INBOX": _mail(1), "[Gmail]/Alle Nachrichten": _mail(3),
                                                            "[Gmail]/Spam": _mail(2, "spam")})
    src = _src(fake, host="imap.gmail.com", user="me@gmail.com", password="pw")
    infos = list(src.iter_messages())
    assert set(fake.selected) == {"[Gmail]/Alle Nachrichten"} and len(infos) == 3
    assert src.folders == ["[Gmail]/Alle Nachrichten"]


def test_russian_folders_skip_spam_and_include_the_archive():
    boxes = {"INBOX": _mail(2), "&BBAEQARFBDgEMg-": _mail(1, "old"), "&BCEEPwQwBDw-": _mail(4, "spam")}
    fake = FakeIMAP([], listing=YANDEX_RU, boxes=boxes)
    src = _src(fake, host="imap.yandex.com", user="me@yandex.ru", password="pw")
    infos = list(src.iter_messages())
    assert list(dict.fromkeys(fake.selected)) == ["INBOX", "&BBAEQARFBDgEMg-"] and len(infos) == 3
    assert not any("spam" in i.from_addr for i in infos)
    assert decode_folder("&BBAEQARFBDgEMg-") == "Архив" and decode_folder("&BCEEPwQwBDw-") == "Спам"
    assert decode_folder("Tom &- Jerry") == "Tom & Jerry"


def test_list_parsing_handles_literals_nil_and_odd_flags():
    class C:
        def list(self):
            return "OK", [(b'(\\HasNoChildren \\JUNK) "/" {7}', b"Bulk Ma"), b'(\\Noselect) NIL "Shared"',
                          b'(\\All) "." "Archive \\"old\\""', None, b"garbage"]
    listed = list_folders(C())
    assert listed[0] == ("Bulk Ma", {"\\HasNoChildren", "\\Junk"})
    assert listed[1][0] == "Shared" and listed[2][0] == 'Archive "old"'
    folders, roles = choose_folders(listed)
    assert folders == ['Archive "old"'] and roles["junk"] == ["Bulk Ma"]


def test_no_list_at_all_falls_back_to_inbox_or_gmail_all_mail():
    assert choose_folders([], "imap.mail.yahoo.com")[0] == ["INBOX"]
    assert choose_folders([], "imap.gmail.com")[0] == ["[Gmail]/All Mail"]


def test_folders_typed_by_the_user_are_used_exactly():
    fake = FakeIMAP([], listing=YANDEX_RU, boxes={"INBOX": _mail(2), "Custom": _mail(1, "c")})
    src = _src(fake, host="imap.yandex.com", user="me@yandex.ru", password="pw", folders=["Custom"])
    assert len(list(src.iter_messages())) == 1 and set(fake.selected) == {"Custom"}


def test_scan_records_the_preset_account_and_discovered_folders():
    fake = FakeIMAP([], listing=YANDEX_RU, boxes={"INBOX": _mail(2), "&BBAEQARFBDgEMg-": _mail(1, "old")})
    src = _src(fake, host="imap.yandex.com", user="me@yandex.ru", password="pw", provider="yandex")
    result = run_scan(src, limit=None)
    assert result.account == "me@yandex.ru" and result.messages == 3
    assert src.describe()["provider"] == "yandex" and src.describe()["folders"] == ["INBOX", "&BBAEQARFBDgEMg-"]


# ----------------------------------------------------------------- connecting
def test_connection_errors_are_plain_words():
    assert "was not found" in str(connection_error(socket.gaierror(11001, "x"), "imap.typo.example"))
    assert "did not answer" in str(connection_error(socket.timeout(), "h", 993))
    assert "refused the connection" in str(connection_error(ConnectionRefusedError(), "h", 993))
    bridge = P.get("proton")
    assert "Is Proton Mail Bridge running" in str(connection_error(ConnectionRefusedError(), "127.0.0.1", 1143, bridge))
    assert "STARTTLS" in str(connection_error(imaplib.IMAP4.error("STARTTLS extension not supported"), "h", 143))
    tls = ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")
    assert "port and the security" in str(connection_error(tls, "h", 143))


def test_failed_connection_surfaces_as_a_source_error():
    def factory(host):
        raise socket.gaierror(11001, "getaddrinfo failed")
    with pytest.raises(SourceError, match="imap.typo.example was not found"):
        list(ImapSource("imap.typo.example", "me@x.example", "pw", conn_factory=factory).iter_messages())


class _TinyImapServer:
    """A local IMAP greeting + CAPABILITY/STARTTLS/LOGOUT responder, enough
    to prove the real TLS code path: implicit TLS or STARTTLS, with a
    certificate from a private test CA the system does not trust."""

    def __init__(self, mode, starttls=True):
        import trustme
        self.ca = trustme.CA()
        cert = self.ca.issue_cert("127.0.0.1")
        self.ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        cert.configure_cert(self.ctx)
        self.mode, self.starttls = mode, starttls
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.cafile = None
        threading.Thread(target=self._serve, daemon=True).start()

    def write_ca(self, path):
        self.ca.cert_pem.write_to_path(str(path))
        return str(path)

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        try:
            if self.mode == "ssl":
                c = self.ctx.wrap_socket(c, server_side=True)
            f = c.makefile("rwb")
            f.write(b"* OK tiny ready\r\n"); f.flush()
            while True:
                line = f.readline()
                if not line:
                    return
                tag, cmd = line.split(b" ", 2)[:2]
                cmd = cmd.strip().upper()
                if cmd == b"CAPABILITY":
                    caps = b"IMAP4rev1" + (b" STARTTLS" if self.mode == "starttls" and self.starttls else b"")
                    f.write(b"* CAPABILITY " + caps + b"\r\n" + tag + b" OK done\r\n"); f.flush()
                elif cmd == b"STARTTLS":
                    f.write(tag + b" OK begin TLS\r\n"); f.flush()
                    c = self.ctx.wrap_socket(c, server_side=True)
                    f = c.makefile("rwb")
                elif cmd == b"LOGOUT":
                    f.write(b"* BYE\r\n" + tag + b" OK bye\r\n"); f.flush()
                    return
                else:
                    f.write(tag + b" BAD unknown\r\n"); f.flush()
        except (OSError, ssl.SSLError, ValueError):
            return
        finally:
            c.close()

    def close(self):
        self.sock.close()


@pytest.mark.parametrize("mode", ["ssl", "starttls"])
def test_untrusted_certificate_is_refused_and_a_trusted_one_connects(mode, tmp_path):
    srv = _TinyImapServer(mode)
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            open_imap("127.0.0.1", srv.port, mode, timeout=5)
        conn = open_imap("127.0.0.1", srv.port, mode, cafile=srv.write_ca(tmp_path / "ca.pem"), timeout=5)
        assert isinstance(conn.sock, ssl.SSLSocket) and conn.sock.version() in {"TLSv1.2", "TLSv1.3"}
        conn.logout()
    finally:
        srv.close()


def test_untrusted_certificate_through_a_scan_explains_what_to_do():
    srv = _TinyImapServer("starttls")
    try:
        src = ImapSource("127.0.0.1", "me@proton.me", "pw", port=srv.port, security="starttls", provider="proton")
        with pytest.raises(SourceError, match="Export TLS certificates"):
            list(src.iter_messages())
    finally:
        srv.close()


def test_server_without_starttls_is_never_used_in_plain_text():
    srv = _TinyImapServer("starttls", starttls=False)
    try:
        src = ImapSource("127.0.0.1", "me@x.example", "pw", port=srv.port, security="starttls")
        with pytest.raises(SourceError, match="does not offer STARTTLS"):
            list(src.iter_messages())
    finally:
        srv.close()


def test_nothing_listening_at_the_bridge_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    src = ImapSource("127.0.0.1", "me@proton.me", "pw", port=port, security="starttls", provider="proton")
    with pytest.raises(SourceError, match="Is Proton Mail Bridge running"):
        list(src.iter_messages())


def test_bad_security_value_is_refused():
    with pytest.raises(ValueError):
        ImapSource("h", "u", "p", security="none")


# ----------------------------------------------------------------- sending
class FakeSMTP:
    instances: list = []

    def __init__(self, host, port, context=None, timeout=None, password="app"):
        self.host, self.port, self.context, self.sent, self.password = host, port, context, [], password
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        import smtplib
        if password != self.password:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        self.user = user

    def send_message(self, msg):
        self.sent.append(msg)


def test_smtp_uses_the_preset_server_and_port():
    FakeSMTP.instances.clear()
    src = ImapSource("imap.mail.me.com", "me@icloud.com", "app", provider="icloud", allow_send=True,
                     smtp_factory=FakeSMTP)
    assert (src.smtp_host, src.smtp_port, src.smtp_security) == ("smtp.mail.me.com", 587, "starttls")
    src.send_email("unsub@list.example", "unsubscribe", "Please remove me")
    smtp = FakeSMTP.instances[-1]
    assert (smtp.host, smtp.port, smtp.user) == ("smtp.mail.me.com", 587, "me@icloud.com")
    assert smtp.sent[0]["To"] == "unsub@list.example"


def test_smtp_refusal_is_explained_without_the_password():
    src = ImapSource("imap.aol.com", "me@aol.com", "wrongpw", provider="aol", allow_send=True, smtp_factory=FakeSMTP)
    with pytest.raises(SourceError) as exc:
        src.send_email("u@list.example", "unsubscribe", "x")
    assert "refused the sign-in for sending" in str(exc.value) and "wrongpw" not in str(exc.value)


def test_without_a_preset_smtp_keeps_the_old_default():
    src = ImapSource("imap.example.org", "me@example.org", "pw")
    assert (src.smtp_host, src.smtp_port, src.smtp_security) == ("smtp.example.org", 465, "ssl")


def test_sending_stays_off_unless_allowed():
    src = ImapSource("imap.mail.yahoo.com", "me@yahoo.com", "pw", provider="yahoo", smtp_factory=FakeSMTP)
    with pytest.raises(SourceError, match="Sending is off"):
        src.send_email("u@list.example", "unsubscribe", "x")


# ----------------------------------------------------------------- web + CLI
def test_web_connect_fills_in_the_preset_from_the_address(monkeypatch):
    # connect now signs in once before the account replaces the one on screen;
    # a local fake server answers (no real mail server is contacted)
    from fakes import FakeIMAP
    fake = FakeIMAP([])
    monkeypatch.setattr("clawback.sources.imap.open_imap", lambda host, *a, **kw: fake)
    from clawback.web.server import ApiError, App
    app = App()
    src = app.connect({"kind": "imap", "user": "me@yahoo.co.uk", "password": " app-pw "})
    assert (src.host, src.port, src.security, src.account) == ("imap.mail.yahoo.com", 993, "ssl", "me@yahoo.co.uk")
    assert src._password == "app-pw" and src._folders is None
    src = app.connect({"kind": "imap", "provider": "proton", "user": "me@proton.me", "password": " bridge pw ", "port": 1150})
    assert (src.host, src.port, src.security, src._password) == ("127.0.0.1", 1150, "starttls", " bridge pw ")
    with pytest.raises(ApiError, match="16 September 2024"):
        app.connect({"kind": "imap", "user": "me@hotmail.com", "password": "x"})
    with pytest.raises(ApiError, match="IMAP server"):
        app.connect({"kind": "imap", "user": "me@company.example", "password": "x"})
    with pytest.raises(ApiError, match="email address"):
        app.connect({"kind": "imap", "host": "imap.x.example", "user": "me", "password": "x"})
    with pytest.raises(ApiError, match="app password"):
        app.connect({"kind": "imap", "user": "me@aol.com", "password": "   "})
    with pytest.raises(ApiError, match="certificate file"):
        app.connect({"kind": "imap", "provider": "proton", "user": "me@proton.me", "password": "x",
                     "cafile": "C:/no/such/cert.pem"})


def test_cli_outlook_address_is_sent_to_microsoft_sign_in():
    from typer.testing import CliRunner
    from clawback.cli import app
    r = CliRunner().invoke(app, ["scan", "--user", "me@outlook.com"])
    assert r.exit_code == 2 and "16 September 2024" in r.output
