"""Goal J: zero-setup import of Google Takeout / mbox / .eml (read-only, streaming)."""
import io
import tarfile
import time
import zipfile
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from helpers import write_mbox
from clawback import tracker
from clawback.demo import mailbox
from clawback.scan import run_scan
from clawback.sources.base import SourceError
from clawback.sources.mbox import MboxSource, split_mbox


def raw(frm="News <news@letters.example>", subject="Hello", body="Hi there", labels=None, extra=None):
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"] = frm, subject, "Tue, 06 Oct 2026 09:00:00 +0000"
    m["Message-ID"] = f"<{abs(hash((frm, subject, body)))}@x.example>"
    if labels is not None:
        m["X-Gmail-Labels"] = labels
    for k, v in (extra or {}).items():
        m[k] = v
    m.set_content(body)
    return bytes(m)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAWBACK_HOME", str(tmp_path / "home"))
    return tmp_path


def test_from_lines_inside_bodies_do_not_split_messages(tmp_path):
    body = "Line one\nFrom here on, prices change.\n>From quoted\nend"
    p = write_mbox(tmp_path / "a.mbox", [raw(body=body), raw(subject="Second")])
    infos = list(MboxSource(p).iter_messages())
    assert [i.subject for i in infos] == ["Hello", "Second"]
    assert "From here on, prices change." in infos[0].body
    assert ">From quoted" in infos[0].body  # mboxrd: exactly one '>' removed


def test_crlf_export_is_parsed(tmp_path):
    p = write_mbox(tmp_path / "crlf.mbox", [raw(), raw(subject="Two")], crlf=True)
    assert [i.subject for i in MboxSource(p).iter_messages()] == ["Hello", "Two"]


def test_takeout_labels_give_read_state_and_skip_spam_trash_sent(tmp_path):
    msgs = [raw(subject="unread", labels="Inbox,Unread,Category Promotions"),
            raw(subject="read", labels="Inbox,Opened"),
            raw(subject="spam", labels="Spam,Unread"),
            raw(subject="trash", labels="Trash"),
            raw(subject="mine", labels="Sent")]
    src = MboxSource(write_mbox(tmp_path / "t.mbox", msgs))
    infos = {i.subject: i for i in src.iter_messages()}
    assert set(infos) == {"unread", "read"}
    assert infos["unread"].unread is True and infos["read"].unread is False
    assert "CATEGORY_PROMOTIONS" in infos["unread"].labels and infos["unread"].is_bulk
    assert src.skipped == 3


def test_plain_mbox_without_labels_has_unknown_read_state(tmp_path):
    info = next(MboxSource(write_mbox(tmp_path / "p.mbox", [raw()])).iter_messages())
    assert info.unread is None


def test_takeout_zip_and_tgz_and_eml_folder(tmp_path):
    mbox = write_mbox(tmp_path / "All mail Including Spam and Trash.mbox", [raw(), raw(subject="Two")])
    z = tmp_path / "takeout-20261024T000000Z-001.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(mbox, "Takeout/Mail/All mail Including Spam and Trash.mbox")
        zf.writestr("Takeout/archive_browser.html", "<html></html>")
    assert len(list(MboxSource(z).iter_messages())) == 2
    t = tmp_path / "takeout.tgz"
    with tarfile.open(t, "w:gz") as tf:
        tf.add(mbox, "Takeout/Mail/All mail.mbox")
    assert len(list(MboxSource(t).iter_messages())) == 2
    folder = tmp_path / "emls"
    folder.mkdir()
    (folder / "a.eml").write_bytes(raw(subject="eml one"))
    (folder / "sub").mkdir()
    (folder / "sub" / "b.eml").write_bytes(raw(subject="eml two"))
    assert sorted(i.subject for i in MboxSource(folder).iter_messages()) == ["eml one", "eml two"]


def test_zip_without_mail_gives_a_helpful_error(tmp_path):
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Takeout/Drive/file.txt", "x")
    with pytest.raises(SourceError, match="select 'Mail'"):
        list(MboxSource(z).iter_messages())


def test_huge_attachment_is_truncated_not_loaded(tmp_path):
    m = EmailMessage()
    m["From"], m["Subject"] = "Shop <billing@shop.example>", "Your receipt"
    m.set_content("Subscription renews monthly. Total US$9.99")
    m.add_attachment(b"\0" * 5_000_000, maintype="application", subtype="octet-stream", filename="big.bin")
    infos = list(MboxSource(write_mbox(tmp_path / "big.mbox", [bytes(m), raw(subject="after")])).iter_messages())
    assert [i.subject for i in infos] == ["Your receipt", "after"]
    assert "US$9.99" in infos[0].body


def test_split_is_streaming(tmp_path):
    class Lines(io.BytesIO):
        reads = 0

        def __iter__(self):
            while True:
                line = self.readline()
                if not line:
                    return
                Lines.reads += 1
                yield line
    data = write_mbox(tmp_path / "s.mbox", [raw(subject=str(i)) for i in range(3)]).read_bytes()
    gen = split_mbox(Lines(data))
    next(gen)
    assert Lines.reads < data.count(b"\n")  # first message yielded before the file was fully read


def test_large_export_is_fast_and_counts_correctly(tmp_path):
    msgs = [raw(frm=f"Sender {i % 50} <s{i % 50}@bulk.example>", subject=f"Issue {i}",
                extra={"List-Unsubscribe": f"<https://bulk.example/u/{i % 50}>"}, labels="Inbox,Unread") for i in range(5000)]
    p = write_mbox(tmp_path / "large.mbox", msgs)
    start = time.time()
    result = run_scan(MboxSource(p, account="big"), limit=10**9)
    assert result.messages == 5000
    rows = tracker.list_senders("big")
    assert len(rows) == 50 and all(r["message_count"] == 100 for r in rows)
    assert time.time() - start < 60


def test_demo_mailbox_import_end_to_end(tmp_path):
    now = datetime.now(timezone.utc)
    p = write_mbox(tmp_path / "demo.mbox", mailbox(now))
    result = run_scan(MboxSource(p, account="me"), region="US")
    senders = {r["address"]: r for r in tracker.list_senders("me")}
    assert senders["deals@shopmart.example"]["category"] == "newsletter"
    assert senders["deals@shopmart.example"]["unread_count"] == 86
    assert senders["deals@shopmart.example"]["one_click"] == 1
    assert senders["team@moonshot.example"]["category"] == "spam"
    assert senders["asha@mail.example"]["category"] == "other"
    currencies = {s.currency for s in result.subscriptions}
    assert {"USD", "EUR", "INR", "JPY", "AUD", "GBP"} <= currencies
    # Rescanning the same export must not double-count.
    run_scan(MboxSource(p, account="me"), region="US")
    assert tracker.get_sender("me", "deals@shopmart.example")["message_count"] == 90


def test_v1_data_is_copied_once_into_the_clawback_folder(tmp_path):
    from clawback import config
    old, new = tmp_path / ".unsubscribe", tmp_path / ".clawback"
    old.mkdir(); new.mkdir()
    (old / "links.json").write_text('{"example.com": "https://example.com/account"}', encoding="utf-8")
    (old / "token.json").write_text("{}", encoding="utf-8")
    assert sorted(config.import_legacy(old, new)) == ["links.json", "token.json"]
    assert (new / "links.json").read_text(encoding="utf-8").startswith('{"example.com"')
    assert (old / "links.json").exists()               # old folder untouched
    assert config.import_legacy(old, new) == []         # never overwrites / repeats
