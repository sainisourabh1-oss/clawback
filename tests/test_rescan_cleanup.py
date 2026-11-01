"""What a rescan with a newer classifier must show (patterns from a real
read-only Gmail rescan, 2026-10-25; all names, amounts and dates invented).

* Rows an older classifier created, which the current one no longer finds
  (a promo price taken as the plan price, a one-off payment taken as a
  mandate), disappear on the next scan unless the user already acted on them.
* Trial countdown reminders a few days apart do not get a "weekly" billing
  cycle from their spacing.
* A bank's account notice that mentions AutoPay but debits nothing is not a
  mandate ("Unknown merchant, 0.00").
"""
import sqlite3

import realmail as R
from helpers import write_mbox
from clawback import config, tracker
from clawback.scan import run_scan
from clawback.sources.mbox import MboxSource


def _receipts():
    return [R.msg("Streamly <billing@streamly.example>", f"Your Streamly receipt {k}",
                  "Streamly Basic\nAuto-renewing subscription\nTotal: ₹249.00/month", days_ago=5 + 30.4 * k)
            for k in range(3)]


def _stale(db, account, vendor, domain, key, amount, status="suspected", notes=""):
    cur = db.execute("INSERT INTO subscriptions(account,vendor,domain,identity_key,amount,currency,cycle,monthly_cost,"
                     "status,notes,created_at,updated_at) VALUES(?,?,?,?,?,'INR','month',?,?,?,'2026-10-01','2026-10-01')",
                     (account, vendor, domain, key, amount, amount, status, notes))
    db.execute("INSERT INTO evidence(subscription_id,subject,kind,confidence,observed_at) VALUES(?,?,?,?,?)",
               (cur.lastrowid, "old subject", "receipt", 0.8, "2026-10-01"))
    return cur.lastrowid


def test_rows_from_an_older_classifier_are_dropped_unless_the_user_acted(tmp_path):
    tracker.connect().close()                       # create the schema in this test's data folder
    db = sqlite3.connect(config.db_path())
    db.execute("PRAGMA foreign_keys=ON")
    promo = _stale(db, "acct", "Streamly: Premium", "streamly.example", "plan:premium|month", 1999.0)
    vpa = _stale(db, "acct", "VPA autopay", "bank.example", "merchant:vpa autopay|month", 12345.0)
    kept = _stale(db, "acct", "Old gym", "gym.example", "old gym|month", 900.0, status="confirmed")
    noted = _stale(db, "acct", "Old club", "club.example", "old club|month", 300.0, notes="ask about refund")
    other = _stale(db, "other-acct", "VPA autopay", "bank.example", "merchant:vpa autopay|month", 12345.0)
    db.commit()
    db.close()

    run_scan(MboxSource(write_mbox(tmp_path / "a.mbox", _receipts()), account="acct"), region="IN")

    rows = {r["id"]: dict(r) for r in tracker.list_subscriptions()}
    assert promo not in rows and vpa not in rows                   # what the user would have seen is gone
    assert kept in rows and noted in rows and other in rows        # reviewed, annotated or another account: untouched
    current = [r for r in rows.values() if r["account"] == "acct" and r["domain"] == "streamly.example"]
    assert [(r["amount"], r["cycle"]) for r in current] == [(249.0, "month")]
    assert not any(r["amount"] in (1999.0, 12345.0) for r in rows.values() if r["account"] == "acct"
                   and r["status"] == "suspected")
    db = sqlite3.connect(config.db_path())
    assert db.execute("SELECT COUNT(*) FROM evidence WHERE subscription_id IN (?,?)", (promo, vpa)).fetchone()[0] == 0
    db.close()


def test_a_second_scan_keeps_what_the_first_found(tmp_path):
    # Retiring must not remove rows that stored detections still support
    # (an incremental rescan reads nothing new).
    path = write_mbox(tmp_path / "a.mbox", _receipts())
    run_scan(MboxSource(path, account="acct"), region="IN")
    first = [(r["vendor"], r["amount"]) for r in tracker.list_subscriptions(account="acct")]
    run_scan(MboxSource(path, account="acct"), region="IN")
    assert [(r["vendor"], r["amount"]) for r in tracker.list_subscriptions(account="acct")] == first != []


def test_trial_countdown_reminders_do_not_get_a_weekly_cycle(tmp_path):
    raws = [R.msg("Mailhost <team@mailhost.example>", subject, "Your free Business Email trial ends soon. "
                  "Keep your mailbox for ₹468.00.", days_ago=d)
            for subject, d in (("Your free Business Email trial ends in 7 days", 8),
                               ("Your free Business Email trial ends tomorrow", 1))]
    run_scan(MboxSource(write_mbox(tmp_path / "t.mbox", raws), account="t"), region="IN")
    subs = [dict(s) for s in tracker.list_subscriptions(account="t")]
    assert len(subs) == 1
    assert subs[0]["cycle"] == "unknown" and subs[0]["monthly_cost"] is None   # not "₹2,028 a month"


def test_bank_account_notice_with_zero_amount_is_not_a_mandate(tmp_path):
    raw = R.msg("Example Bank Alerts <alerts@examplebank.bank.in>", "View: Account update for your Example Bank A/c",
                "Dear Customer, your UPI AutoPay preferences were updated. Amount: $0.00. No action is needed.",
                days_ago=3, auth=R.ar("examplebank.bank.in", "examplebank.bank.in"))
    run_scan(MboxSource(write_mbox(tmp_path / "b.mbox", [raw, R.real_mandate()]), account="b"), region="IN")
    subs = [dict(s) for s in tracker.list_subscriptions(account="b")]
    assert [s["vendor"] for s in subs] == ["Spotify India"]               # the real mandate only
    assert not any(s["vendor"] == "Unknown merchant" for s in subs)


def test_card_e_mandate_registration_names_the_merchant_and_no_price(tmp_path):
    # A registration says "Current transaction amount: 0.00" and a cap
    # ("Maximum transaction amount"). The cap is not the price; the merchant
    # is named in "Your payment for X is registered".
    raw = R.msg("Example Bank <alerts@examplebank.bank.in>", "View: Account update for your Example Bank A/c",
                "Dear Customer,\nYour payment for Cloudnote is registered for E-mandate (Auto payment) using your "
                "Example Bank Debit Card ending 0000.\nE-mandate Details:\nCurrent transaction amount: USD 0.00\n"
                "Maximum transaction amount: USD 150.00\nFrequency: As Presented\nStart date: 01/03/2026\n",
                days_ago=200, auth=R.ar("examplebank.bank.in", "examplebank.bank.in"))
    run_scan(MboxSource(write_mbox(tmp_path / "e.mbox", [raw]), account="e"), region="IN")
    subs = [dict(s) for s in tracker.list_subscriptions(account="e")]
    assert [(s["vendor"], s["amount"]) for s in subs] == [("Cloudnote", None)]   # not "Examplebank US$150.00"


def test_nach_debit_with_umrn_to_a_lender_is_a_loan(tmp_path):
    raws = [R.msg("Example Bank <alerts@examplebank.bank.in>", "Account update for your Example Bank A / c",
                  f"Dear Customer,\nRs.4321.00 has been debited from Example Bank Account Number XXXXXXXX0000 towards "
                  f"Example Credit Co Ltd/87654321 with UMRN EXMP0000000000000001 on 04-{m:02d}-2026.\n",
                  days_ago=21 + 30.4 * i)
            for i, m in enumerate((10, 9, 8))]
    run_scan(MboxSource(write_mbox(tmp_path / "n.mbox", raws), account="n"), region="IN")
    subs = [dict(s) for s in tracker.list_subscriptions(account="n")]
    assert [(s["vendor"], s["amount"], s["status"]) for s in subs] == [("Example Credit Co Ltd", 4321.0, "loan")]


def test_one_row_per_merchant_across_bank_domains_and_the_mandate_value_is_a_cap(tmp_path):
    # The bank moved its alerts from examplebank.net to examplebank.bank.in;
    # the NACH acceptance mail names the lender and "a value of" (a cap).
    accepted = R.msg("Example Bank <nachalerts@examplebank.net>", "Example Bank NACH Debit Mandate Update",
                     "Dear Sir/Madam,\nWe have Accepted your Example Bank NACH Debit Mandate with\n"
                     "· UMRN EXMP0000000000000001\n· Issued to Example Credit Co Ltd.\n"
                     "· The mandate has a value of Rs. 9999.00 and\n· At MNTH frequency.\n", days_ago=330)
    debits = [R.msg(f"Example Bank <nachalerts@{dom}>", "Account update for your Example Bank A / c",
                    f"Dear Customer,\nRs.4321.00 has been debited from Example Bank Account Number XXXXXXXX0000 "
                    f"towards Example Credit Co Ltd/87654321 with UMRN EXMP0000000000000001 on 04-{m:02d}-2026.\n",
                    days_ago=d)
              for dom, m, d in (("examplebank.net", 1, 290), ("examplebank.net", 2, 260),
                                ("examplebank.bank.in", 9, 51), ("examplebank.bank.in", 10, 21))]
    run_scan(MboxSource(write_mbox(tmp_path / "m.mbox", [accepted] + debits), account="m"), region="IN")
    subs = [dict(s) for s in tracker.list_subscriptions(account="m")]
    assert [(s["vendor"], s["amount"], s["cycle"], s["status"]) for s in subs] == [
        ("Example Credit Co Ltd", 4321.0, "month", "loan")]                  # one row, never the 9,999 cap
    assert subs[0]["domain"] == "examplebank.bank.in"                         # the bank's current domain
