"""Price alerts must not cry wolf.

The three shapes below were seen on a real mailbox (values invented here):
an intro offer ending, a vendor with several different bills, and usage
receipts of varying size. Only a settled price that went up is a rise."""
from clawback import tracker


def _sub(db, vendor, prices, currency="INR"):
    cur = db.execute("INSERT INTO subscriptions(vendor, domain, status, currency, created_at, updated_at) VALUES(?,?, 'suspected', ?, '2026-01-01', '2026-01-01')",
                     (vendor, vendor.lower() + ".example", currency))
    sid = cur.lastrowid
    for i, amount in enumerate(prices):
        db.execute("INSERT INTO price_history(subscription_id, amount, currency, observed_on, source) VALUES(?,?,?,?, 'email scan')",
                   (sid, amount, currency, f"2026-{i + 1:02d}-10"))
    return sid


def _alerts(tmp_path, rows):
    path = tmp_path / "db.sqlite3"
    db = tracker.connect(path)
    for vendor, prices in rows:
        _sub(db, vendor, prices)
    db.commit(); db.close()
    return {a["vendor"]: a for a in tracker.price_increases(path)}


def test_intro_offer_ending_is_not_called_a_400_percent_rise(tmp_path):
    a = _alerts(tmp_path, [("Cloudbox", [11, 59])])["Cloudbox"]
    assert a["kind"] == "intro" and (a["old"], a["new"]) == (11, 59)


def test_several_bills_under_one_vendor_raise_no_alarm(tmp_path):
    assert _alerts(tmp_path, [("Phoneco", [2356, 232.36, 878.82])]) == {}


def test_usage_receipts_of_varying_size_raise_no_alarm(tmp_path):
    assert _alerts(tmp_path, [("Apico", [23.6, 5.9, 11.8, 5.9, 11.8, 23.6])]) == {}


def test_a_settled_price_going_up_is_a_rise(tmp_path):
    got = _alerts(tmp_path, [("Streamy", [149, 199]), ("Gymmo", [999, 999, 1299])])
    assert got["Streamy"]["kind"] == "rise" and got["Streamy"]["percent"] == 33.6
    assert got["Gymmo"]["kind"] == "rise" and (got["Gymmo"]["old"], got["Gymmo"]["new"]) == (999, 1299)


def test_a_price_that_went_down_or_stayed_is_quiet(tmp_path):
    assert _alerts(tmp_path, [("Cheaper", [199, 149]), ("Same", [149, 149])]) == {}


def test_currency_change_compares_only_the_latest_currency(tmp_path):
    path = tmp_path / "db.sqlite3"
    db = tracker.connect(path)
    sid = _sub(db, "Travel", [10], currency="EUR")
    for i, amount in enumerate([800, 900]):
        db.execute("INSERT INTO price_history(subscription_id, amount, currency, observed_on, source) VALUES(?,?,?,?, 'email scan')",
                   (sid, amount, "INR", f"2026-0{i + 5}-10"))
    db.commit(); db.close()
    (a,) = tracker.price_increases(path)
    assert a["currency"] == "INR" and (a["old"], a["new"]) == (800, 900) and a["kind"] == "rise"
