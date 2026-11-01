"""Goals C and K: savings estimates per currency; privacy footprint and drafts."""
from datetime import datetime, timedelta, timezone

import pytest

from clawback import config, privacy, savings, tracker
from clawback.actions import PlanError, build_plan, confirm
from clawback.demo import DemoSource
from clawback.scan import run_scan

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def scanned():
    src = DemoSource(now=NOW)
    run_scan(src, region="US")
    return src


def sub_id(vendor_prefix):
    return next(r["id"] for r in tracker.list_subscriptions() if r["vendor"].startswith(vendor_prefix))


def test_savings_are_per_currency_and_never_summed():
    scanned()
    tracker.set_status(sub_id("Streamora"), "cancelled", when=NOW - timedelta(days=61))   # US$15.49/mo
    tracker.set_status(sub_id("Cloudlocker"), "cancelled", when=NOW - timedelta(days=30.44))  # ₹219/mo
    tracker.set_status(sub_id("Pixelforge"), "cancelled", when=NOW)                     # A$239.88/yr
    s = savings.compute(now=NOW, settings={**config.load_settings(), "home_currency": "INR"})
    by = {m["currency"]: m for m in s["money"]}
    assert set(by) == {"USD", "INR", "AUD"}
    assert s["money"][0]["currency"] == "INR"                      # home currency first
    assert by["USD"]["yearly"] == 185.88 and by["USD"]["so_far"] == pytest.approx(31.04, abs=0.05)
    assert by["INR"]["display"]["yearly"] == "₹2,628"
    assert by["AUD"]["monthly"] == 19.99 and by["AUD"]["so_far"] == 0
    assert s["converted"] is None                                  # never combined without opt-in
    assert "Estimates" in s["label"]


def test_converted_total_only_with_opt_in_and_dated_user_rates():
    scanned()
    tracker.set_status(sub_id("Streamora"), "cancelled", when=NOW)
    tracker.set_status(sub_id("Kotoba"), "cancelled", when=NOW)
    settings = {**config.load_settings(), "home_currency": "INR", "show_converted_total": True,
                "rates": {"base": "INR", "date": "2026-10-01", "rates": {"USD": 84.0}}}
    s = savings.compute(now=NOW, settings=settings)
    conv = s["converted"]
    assert conv["yearly"] == round(185.88 * 84.0, 2) and conv["missing"] == ["JPY"]
    assert "2026-10-01" in conv["label"] and "Estimate" in conv["label"]


def test_rates_need_a_date():
    with pytest.raises(ValueError, match="date"):
        config.save_settings({"rates": {"base": "INR", "rates": {"USD": 84}}})


def test_emails_avoided_estimate_after_unsubscribe():
    run_scan(DemoSource(now=NOW - timedelta(days=61)), region="US")     # mailbox seen until 61 days ago
    tracker.set_sender_status("demo", "deals@shopmart.example", "unsubscribed", method="one-click", when=NOW - timedelta(days=60.88))
    s = savings.compute(now=NOW)
    rate = tracker.get_sender("demo", "deals@shopmart.example")["monthly_rate"]
    assert s["emails"]["senders_silenced"] == 1 and s["emails"]["per_month"] == round(rate)
    assert s["emails"]["avoided_so_far"] == round(rate * 2)               # two quiet months
    # Mail that still arrives after unsubscribing is subtracted from the estimate.
    tracker.record_messages("demo", [(f"late{i}", "deals@shopmart.example", (NOW - timedelta(days=i)).isoformat(), "x", 1) for i in range(5)])
    assert savings.compute(now=NOW)["emails"]["avoided_so_far"] == round(rate * 2 - 5)


def test_footprint_lists_companies_by_email_volume():
    scanned()
    fp = privacy.footprint("demo")
    assert fp[0]["domain"] == "forum.example" and fp[0]["emails"] == 120
    assert any(e["domain"] == "streamora.example" and "paid" in e["categories"] for e in fp)


def test_draft_letters_cite_the_right_law_and_never_send():
    src = scanned()
    d = privacy.draft("demo", "shopmart.example", "dpdp", "A. User", "me@mail.example")
    assert "Section 12" in d["body"] and "Section 6(4)" in d["body"] and d["status"] == "draft"
    g = privacy.draft("demo", "shopmart.example", "gdpr", "A. User", "me@mail.example", to_address="privacy@shopmart.example")
    assert "Article 17" in g["body"] and "Article 21" in g["body"] and "one month" in g["body"]
    c = privacy.draft("demo", "shopmart.example", "ccpa", "A. User", "me@mail.example")
    assert "1798.105" in c["body"] and "45 days" in c["body"]
    assert "Not legal advice" in privacy.DISCLAIMER
    # Sending: needs a recipient, then BOTH steps.
    plan = build_plan(src, "send_request", [d["id"]])
    assert "recipient" in plan.steps[0].blocked
    plan = build_plan(src, "send_request", [g["id"]])
    assert plan.count == 1 and src.simulated == []
    with pytest.raises(PlanError):
        confirm(src, plan.id, "yes", plan.digest)
    confirm(src, plan.id, plan.count, plan.digest)
    assert src.simulated[0][0] == "send" and privacy.get(g["id"])["status"] == "sent"


def test_unknown_law_rejected():
    with pytest.raises(ValueError):
        privacy.draft("demo", "x.example", "made-up-act", "n", "me@mail.example")


def test_app_password_and_message_bodies_never_reach_the_disk(isolated_home):
    # README "Privacy": app passwords are held in memory only, and the local
    # database keeps metadata, never message bodies. Scan a fake IMAP server
    # whose mail carries a marker in the body, then read every byte Clawback
    # wrote: neither the password nor the body text may be there.
    from fakes import FakeIMAP
    from realmail import msg
    from clawback.sources.imap import ImapSource

    secret, marker = "app-pw-SECRET-4417", "BODYMARKER-9c2e"
    raws = [msg(f"Shop {i} <news@shop{i}.example>", f"Weekly deals {i}",
                f"Hello! {marker} Your receipt: total US$9.99 per month. Unsubscribe any time.",
                days_ago=1 + i, lu=f"<https://shop{i}.example/u>", one_click=True) for i in range(5)]
    src = ImapSource("imap.mail.example", "me@mail.example", secret, account="me@mail.example",
                     conn_factory=FakeIMAP(raws, password=secret))
    run_scan(src, limit=None, region="US")
    assert tracker.connect().execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 5
    written = [p for p in isolated_home.rglob("*") if p.is_file()]
    assert any(b"news@shop0.example" in p.read_bytes() for p in written), "the check must see what was saved"
    for p in written:
        data = p.read_bytes()
        assert secret.encode() not in data, f"app password found in {p.name}"
        assert marker.encode() not in data, f"message body text found in {p.name}"


def test_private_folder_stays_usable(tmp_path, monkeypatch):
    """The data folder must be owner-only but still enterable (0700, not 0600),
    or on Linux/macOS the database next to it cannot be opened."""
    import os
    import stat
    from clawback import config, tracker
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("CLAWBACK_HOME", str(home))
    tracker.connect().close()
    assert (home / "subscriptions.sqlite3").is_file()
    if os.name == "posix":
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        assert stat.S_IMODE((home / "subscriptions.sqlite3").stat().st_mode) == 0o600
    config._private(home)
    assert home.is_dir()


def test_secret_files_are_owner_only_from_the_first_byte(tmp_path, monkeypatch):
    """Tokens, the Microsoft client file, settings and saved links are created
    0600 (not written world-readable and chmod-ed afterwards), in a 0700
    folder, and replace the old file whole."""
    import os
    import stat
    from clawback import config
    seen = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **kw):
        seen.append((str(path), flags, mode))
        return real_open(path, flags, mode, *a, **kw)
    monkeypatch.setattr(os, "open", spy)
    target = tmp_path / "tokens" / "someone@example.com.json"
    config.write_private(target, '{"refresh_token": "x"}')
    config.write_private(target, '{"refresh_token": "y"}')
    assert target.read_text(encoding="utf-8") == '{"refresh_token": "y"}'
    assert [p for p in target.parent.iterdir()] == [target]          # no temp file left behind
    assert len(seen) == 2 and all(mode == 0o600 and flags & os.O_CREAT and flags & os.O_EXCL for _, flags, mode in seen)
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_every_secret_write_goes_through_write_private():
    """No module writes a token, client or settings file with plain write_text."""
    from pathlib import Path
    import clawback
    root = Path(clawback.__file__).parent
    for name in ("config.py", "ms_oauth.py", "links.py", "saved.py", "sources/gmail.py"):
        src = (root / name).read_text(encoding="utf-8")
        # the one exception is the v1-import note, which holds no secret
        bad = [line for line in src.splitlines() if ".write_text(" in line and "IMPORTED_FROM" not in line]
        assert bad == [], (name, bad)
