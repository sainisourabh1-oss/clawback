"""Real-Gmail finding F plus the operator's ground truth (2026-10-24).

Each test is modelled on a real pattern with synthetic data only:

* Google One: the plan is "30 GB (Google One)" at Rs 59/month from the
  receipt (Total line, GST included). A promo advertising Rs 1,950 and a
  Rs 10,000 store credit never joins it or sets its price.
* YouTube Premium Rs 169/month via Google Play is a real subscription.
* A Rs 6,250 debit to Example Lending Bank is a loan EMI: shown apart, never "paying".
* A one-off UPI payment whose payee VPA contains "autopay" is not a mandate.
* Amazon "nomination will renew" mails with no price are not subscriptions.
* Hostinger trial countdown reminders are one row with a clean plan name.
* Pinterest: sub-senders group under one company; no List-Unsubscribe gives
  a settings guide or Block, not a dead end.
* An unsubscribe is not offered as undoable.
"""
import pytest

import realmail as R
from helpers import write_mbox
from clawback import actions, oneclick, savings, tracker
from clawback.scan import run_scan
from clawback.sources.mbox import MboxSource
from clawback.web.server import _paying


def _scan(tmp_path, raws, account="gt"):
    src = MboxSource(write_mbox(tmp_path / f"{account}.mbox", raws), account=account)
    run_scan(src, region="IN")
    return src, [dict(s) for s in tracker.list_subscriptions(account=account)]


def test_google_one_price_comes_from_the_receipt_not_the_promo(tmp_path):
    _, subs = _scan(tmp_path, R.google_one_ground_truth())
    assert len(subs) == 1, [s["vendor"] for s in subs]
    s = subs[0]
    assert s["plan"] == "30 GB (Google One)"
    assert s["amount"] == 59.0 and s["currency"] == "INR" and s["cycle"] == "month"
    assert s["monthly_cost"] == 59.0
    assert s["tax_note"] == "Includes GST of ₹9.00"
    assert all(v not in (1950.0, 10000.0) for v in (s["amount"], s["monthly_cost"]))


def test_google_play_receipts_are_receipts_and_the_store_promo_is_ignored(tmp_path):
    _, subs = _scan(tmp_path, R.google_play())
    assert len(subs) == 1
    s = subs[0]
    assert s["amount"] == 169.0 and s["cycle"] == "month"
    assert "YouTube Premium" in s["vendor"] or s["plan"].startswith("YouTube Premium")
    evidence = [r["kind"] for r in tracker.connect().execute(
        "SELECT kind FROM evidence WHERE subscription_id=?", (s["id"],))]
    assert evidence and all(k == "receipt" for k in evidence)   # not "renewal,trial"


def test_loan_emi_is_its_own_bucket_and_never_counts_as_paying(tmp_path):
    _, subs = _scan(tmp_path, R.loan_emi() + [R.real_mandate()])
    loan = [s for s in subs if s["status"] == "loan"]
    assert len(loan) == 1 and loan[0]["amount"] == 6250.0
    assert "Example Lending" in loan[0]["vendor"]
    paying = _paying(subs)
    assert [(p["currency"], p["monthly"]) for p in paying] == [("INR", 149.0)]   # Spotify only
    assert savings.compute("gt")["money"] == []               # loans never "saved"


def test_upi_payment_to_an_autopay_vpa_is_not_a_mandate(tmp_path):
    _, subs = _scan(tmp_path, [R.bank_upi_alert()])
    assert subs == []
    assert not any("VPA" in s["vendor"] for s in subs)


def test_real_mandate_control_still_detected(tmp_path):
    _, subs = _scan(tmp_path, [R.real_mandate()])
    assert len(subs) == 1 and subs[0]["vendor"].startswith("Spotify") and subs[0]["amount"] == 149.0


def test_amazon_nominations_without_a_price_are_not_subscriptions(tmp_path):
    _, subs = _scan(tmp_path, R.amazon_nominations())
    assert subs == []


def test_hostinger_trial_reminders_are_one_row_with_a_clean_plan(tmp_path):
    _, subs = _scan(tmp_path, R.hostinger_trial())
    assert len(subs) == 1
    assert subs[0]["plan"] == "Starter Business Email Trial"
    assert "  " not in subs[0]["vendor"]


# ------------------------------------------------------------- Pinterest
def _pinterest(i, local, lu=None):
    return R.msg(f"Pinterest <{local}@explore.pinterest.com>", f"Ideas for you {local} {i}", "New pins for you.",
                 days_ago=1 + 3 * i, lu=lu, auth=R.ar("explore.pinterest.com", "explore.pinterest.com"))


@pytest.fixture
def pinterest(tmp_path):
    raws = [_pinterest(i, "ideas") for i in range(4)] + [_pinterest(i, "picks") for i in range(4)]
    src = MboxSource(write_mbox(tmp_path / "pin.mbox", raws), account="pin")
    run_scan(src, region="IN")
    return src


def test_pinterest_sub_senders_group_under_one_company(pinterest):
    rows = [r for r in tracker.list_senders("pin") if r["company"] == "pinterest.com"]
    assert len(rows) == 2 and {r["company_name"] for r in rows} == {"Pinterest"}


def test_no_list_unsubscribe_offers_settings_link_or_block(pinterest):
    rows = tracker.list_senders("pin")
    assert all(r["settings_guide"] and r["settings_guide"]["help"].startswith("https://help.pinterest.com/") for r in rows)
    plan = actions.build_plan(pinterest, "unsubscribe", ["pinterest.com"])
    assert len(plan.steps) == 2                                  # company target covers both addresses
    for step in plan.steps:
        assert step.blocked and "Pinterest" in step.blocked and "Block" in step.blocked
        assert step.data["guide"]["help"].startswith("https://help.pinterest.com/")


def test_unsubscribe_is_not_offered_as_undoable(tmp_path):
    raws = [R.msg("Digest <news@digest.example>", f"Issue {i}", "Tips. Unsubscribe.", days_ago=1 + 7 * i,
                  lu="<https://digest.example/unsub/1>", one_click=True, auth=R.ar("digest.example", "digest.example"))
            for i in range(4)]
    src = MboxSource(write_mbox(tmp_path / "u.mbox", raws), account="u")
    run_scan(src, region="US")
    plan = actions.build_plan(src, "unsubscribe", ["news@digest.example"])
    actions.confirm(src, plan.id, plan.count, plan.digest,
                    http_post=lambda url: oneclick.PostResult(True, 204, "HTTP 204", url))
    entry = [e for e in tracker.audit_entries("u") if e["action"] == "unsubscribe"][0]
    assert entry["ok"] and not entry.get("undo")
    assert "to resubscribe, use the sender's site" in str(entry["detail"])


def test_existing_database_is_migrated_to_accept_loans(tmp_path):
    import sqlite3
    from clawback.aggregate import Subscription
    p = tmp_path / "old.db"
    db = sqlite3.connect(p)
    db.execute(tracker._SUBS_SCHEMA.replace(",'loan'", "").replace(" tax_note TEXT NOT NULL DEFAULT '',", ""))
    db.execute("INSERT INTO subscriptions(account,domain,currency,vendor,status,created_at,updated_at) "
               "VALUES('a','x.example','INR','Old','confirmed','2026-01-01','2026-01-01')")
    db.commit(); db.close()
    sub = Subscription(vendor="Example Lending Bank", domain="icici.bank.in", amount=6250.0, currency="INR",
                       cycle="month", monthly_cost=6250.0, kinds={"loan"})
    tracker.upsert_detection(sub, path=p, account="a")
    rows = {r["vendor"]: r["status"] for r in tracker.list_subscriptions(p, "a")}
    assert rows == {"Old": "confirmed", "Example Lending Bank": "loan"}


def test_cli_list_shows_loans_apart_and_the_gst_note(tmp_path):
    from typer.testing import CliRunner
    from clawback.cli import app
    _scan(tmp_path, R.loan_emi() + R.google_one_ground_truth())
    out = CliRunner().invoke(app, ["list", "--account", "gt"]).output
    loans, _, table = out.partition("Paid subscriptions")
    assert "Loans & EMIs (not cancellable here" in loans and "6,250" in loans
    assert "6,250" not in table
    assert "(Includes" in table and "GST of" in table          # rich may wrap the note
