"""Regression tests for the ten bugs found in the v1.0.2 review.

Each test asserts what the user would see (number of rows, the price shown,
the cycle shown, whether a sender is listed as a paid subscription).
"""
from datetime import datetime, timezone
import email.message

from clawback.aggregate import aggregate
from clawback.detect import Hit, classify, classify_message
from clawback.extract import from_bytes
from clawback.mailbox import iter_messages, scan as imap_scan
from clawback.money import parse_money, format_money


def dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


# ---- bug 1: month abbreviations split one subscription into many rows
def test_bug1_google_play_receipts_with_abbreviated_months_are_one_row():
    body = "Item: YouTube Premium (YouTube)\nAuto-renewing subscription\nTotal: ₹129.00/month"
    hits = [classify(f"Your Google Play Order Receipt from {m} 5, 2026", body, "google.com") for m in ("Oct", "Nov", "Sept")]
    for h, d in zip(hits, (dt(2026, 10, 5), dt(2026, 11, 5), dt(2026, 9, 5))):
        h.date = d
    subs = aggregate(hits)
    assert len(subs) == 1
    assert subs[0].hits == 3
    assert subs[0].monthly_cost == 129.0 and subs[0].currency == "INR"


# ---- bug 2: generic vendor subjects merged different products at the max price
def test_bug2_apple_receipts_keep_icloud_and_music_separate():
    icloud = classify("Your receipt from Apple.", "iCloud+ with 50 GB of storage\nMonthly\nRenews 12 Nov 2026\nTotal ₹75.00", "email.apple.com")
    music = classify("Your receipt from Apple.", "Apple Music (Monthly)\nRenews 3 Nov 2026\nTotal ₹119.00", "email.apple.com")
    subs = aggregate([icloud, music])
    assert len(subs) == 2
    shown = {s.vendor: s.monthly_cost for s in subs}
    assert any("iCloud" in v for v in shown) and any("Apple Music" in v for v in shown)
    assert sorted(shown.values()) == [75.0, 119.0]


def test_bug2_stripe_receipts_for_two_merchants_are_separate():
    a = classify("Your receipt from Acme Analytics #2231-0042", "Pro plan\nBilled monthly\nAmount paid $29.00", "stripe.com")
    b = classify("Your receipt from Beta Notes #9921-1100", "Starter plan\nBilled monthly\nAmount paid $8.00", "stripe.com")
    assert len(aggregate([a, b])) == 2


# ---- bug 3: IMAP read only text/plain and left encoded subjects undecoded
def _html_only_receipt() -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "Netflix <info@account.netflix.com>"
    msg["Subject"] = "=?UTF-8?B?WW91ciBOZXRmbGl4IHJlY2VpcHQ=?="  # "Your Netflix receipt"
    msg["Date"] = "Mon, 05 Oct 2026 10:00:00 +0000"
    msg.set_content("<html><body><p>Your subscription renews monthly.</p><p>Amount: $15.49 USD</p></body></html>", subtype="html")
    return bytes(msg)


from fakes import FakeIMAP  # noqa: E402


def test_bug3_imap_html_only_receipt_and_encoded_subject():
    fake = FakeIMAP([_html_only_receipt()])
    infos = list(iter_messages("imap.example", "u", "p", ["INBOX"], 10, conn_factory=fake))
    assert infos[0].subject == "Your Netflix receipt"
    assert "renews monthly" in infos[0].body and "<p>" not in infos[0].body
    assert infos[0].unread is False and fake.selected_readonly is True
    hits = list(imap_scan("imap.example", "u", "p", ["INBOX"], 10, conn_factory=fake))
    assert len(hits) == 1 and hits[0].vendor == "Netflix" and hits[0].money.amount == 15.49


def test_bug3_imap_and_gmail_produce_identical_detection():
    import base64
    from clawback.extract import from_gmail_message
    raw = _html_only_receipt()
    imap_info = from_bytes(raw, "1")
    html = "<html><body><p>Your subscription renews monthly.</p><p>Amount: $15.49 USD</p></body></html>"
    gmail_info = from_gmail_message({"id": "g1", "payload": {"mimeType": "text/html", "headers": [
        {"name": "From", "value": "Netflix <info@account.netflix.com>"},
        {"name": "Subject", "value": "=?UTF-8?B?WW91ciBOZXRmbGl4IHJlY2VpcHQ=?="}],
        "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()}}})
    a, b = classify_message(imap_info), classify_message(gmail_info)
    assert (a.vendor, a.money, a.cycle, a.kind) == (b.vendor, b.money, b.cycle, b.kind)


# ---- bug 4: substrings such as "industrial" matched "trial"
def test_bug4_industrial_is_not_a_trial():
    hit = classify("Your invoice", "Industrial supplies order. Subscription fee $40.00 per month.", "billing.supplier.com")
    assert hit is not None and hit.kind != "trial"


def test_bug4_trialogue_and_renewable_words_do_not_match():
    hit = classify("Renewable energy digest", "Trialogue on industrial policy. Read more.", "news.energy.org")
    assert hit is None


# ---- bug 5: billing cycle detection too loose
def test_bug5_new_year_receipt_is_not_yearly():
    hit = classify("Happy New Year receipt", "Thanks for your payment of $9.99. Your subscription renews automatically.", "shop.example.com")
    assert hit.cycle is None
    sub = aggregate([hit])[0]
    assert sub.cycle == "unknown" and sub.monthly_cost is None


def test_bug5_stray_month_word_does_not_make_price_monthly():
    hit = classify("Your receipt", "Amount charged: $120.00 for your subscription. " + "x" * 200 + " See you next month!", "app.example.com")
    assert hit.cycle is None


def test_bug5_price_with_per_year_is_yearly_and_normalised():
    hit = classify("Your receipt", "Subscription renewed. Total $120.00 per year.", "app.example.com")
    sub = aggregate([hit])[0]
    assert sub.cycle == "year" and sub.monthly_cost == 10.0


def test_bug5_cycle_inferred_from_receipt_spacing_is_labelled():
    hits = []
    for month in (7, 8, 9, 10):
        h = classify("Payment received", "Thanks! Amount: €9.99 for your subscription.", "pay.example.de")
        h.date = dt(2026, month, 3)
        hits.append(h)
    sub = aggregate(hits)[0]
    assert sub.cycle == "month" and sub.cycle_source == "receipt spacing"


# ---- bug 6: newsletter footers became "paid subscriptions"
def test_bug6_newsletter_footer_is_not_a_paid_subscription():
    body = ("This week in design: 10 links.\nYou are receiving this because you subscribed to our newsletter.\n"
            "Manage your subscription | Update your membership preferences | Unsubscribe")
    assert classify("Design Weekly #212", body, "news.designweekly.com", is_bulk=True) is None


def test_bug6_marketing_price_in_bulk_mail_is_not_paid():
    body = "Big sale! Annual membership only $49 per year. Start your free trial today. Unsubscribe"
    assert classify("Members save 50% this weekend", body, "deals.gym.com", is_bulk=True) is None


def test_bug6_real_receipt_with_list_unsubscribe_is_still_found():
    body = "Your membership renewed. Amount charged: $39.00 monthly. Manage your subscription"
    assert classify("Membership renewed", body, "billing.citygym.com", is_bulk=True) is not None


# ---- bug 10 (currency part): "$" was always USD
def test_bug10_dollar_variants():
    assert parse_money("Total CA$12.99").currency == "CAD"
    assert parse_money("Total A$12.99").currency == "AUD"
    assert parse_money("Total S$12.99").currency == "SGD"
    assert parse_money("Total HK$88").currency == "HKD"
    assert parse_money("Total US$5").currency == "USD"
    m = parse_money("Total $12.99", domain="store.example.ca")
    assert (m.currency, m.certainty) == ("CAD", "inferred")
    m = parse_money("Total $12.99", domain="example.com")
    assert (m.currency, m.certainty) == ("USD", "assumed")


def test_bug10_unresolvable_yen_is_marked_ambiguous_and_never_summed():
    m = parse_money("Total ¥1,200", domain="example.com")
    assert m.certainty == "ambiguous"
    assert "unclear" in format_money(m.amount, m.currency)
    assert parse_money("Total ¥1,200", domain="shop.example.co.jp").currency == "JPY"
    assert parse_money("合計 ¥88 CNY").currency == "CNY"
