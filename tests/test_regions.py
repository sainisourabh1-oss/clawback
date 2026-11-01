"""Goal I + regional modules. All alerts are synthetic, modelled on the public
format (merchant, amount, date, mandate reference); none is a real message."""
from datetime import datetime, timezone

import pytest

from clawback import regions
from clawback.aggregate import aggregate
from clawback.detect import classify_message
from clawback.extract import MessageInfo


def msg(frm, subject, body, labels=()):
    addr = frm.split("<")[-1].strip(">") if "<" in frm else frm
    return MessageInfo(source_id=subject, from_addr=addr, from_name="", domain=addr.split("@")[1], subject=subject,
                       date=datetime(2026, 10, 1, tzinfo=timezone.utc), body=body, labels=set(labels))


INDIA_ALERTS = [
    ("alerts@hdfcbank.net", "UPI AutoPay mandate created",
     "Dear Customer, UPI AutoPay mandate of Rs.199.00 towards SONYLIV has been successfully created. Frequency: Monthly. UMN: 9f8e7d@hdfcbank",
     "SONYLIV", 199.0, "month"),
    ("credit_cards@icicibank.com", "Pre-debit notification for your ICICI Bank Credit Card",
     "Rs 499.00 will be debited on 05 Nov 2026 from your ICICI Bank Credit Card XX1234 towards Spotify India under "
     "standing instruction. Mandate ID: IC1234567. To opt out, visit icici.mandatehq.com",
     "Spotify India", 499.0, None),
    ("donotreply@sbi.co.in", "e-Mandate registration successful",
     "Your e-mandate for INR 1,499.00 in favour of LIC of India has been registered. Frequency: Yearly. Mandate reference: SBI000123",
     "LIC of India", 1499.0, "year"),
    ("alerts@axis.bank.in", "Recurring payment debited",
     "Recurring payment of INR 129.00 debited to YouTube Premium on 01-10-2026 using your card ending 4321 under e-mandate.",
     "YouTube Premium", 129.0, None),
    ("noreply@kotak.com", "Standing Instruction executed",
     "Merchant Name: Swiggy One\nAmount: Rs. 299\nStanding Instruction ID: KMB778899\nFrequency: Quarterly",
     "Swiggy One", 299.0, "quarter"),
    ("no-reply@paytm.com", "Automatic payment set up",
     "Your UPI AutoPay of ₹149 for JioHotstar has been set up successfully. Frequency: Monthly", "JioHotstar", 149.0, "month"),
    ("noreply@phonepe.com", "AutoPay successful",
     "₹89 paid to Zomato Gold via UPI AutoPay. Mandate ID: PP0011", "Zomato Gold", 89.0, None),
    ("no-reply@amazonpay.in", "Your AutoPay is active",
     "Merchant: Audible India\nAmount: ₹199.00\nFrequency: Monthly\nUPI AutoPay mandate registered.", "Audible India", 199.0, "month"),
]


@pytest.mark.parametrize("frm,subject,body,merchant,amount,cycle", INDIA_ALERTS)
def test_indian_mandate_alerts_give_merchant_amount_and_cycle(frm, subject, body, merchant, amount, cycle):
    hit = classify_message(msg(frm, subject, body), region="IN")
    assert hit is not None and hit.region == "in"
    assert hit.merchant == merchant
    assert hit.money.amount == amount and hit.money.currency == "INR"
    assert hit.cycle == cycle


def test_pre_debit_date_becomes_the_renewal_date():
    hit = classify_message(msg(*INDIA_ALERTS[1][:3]), region="IN")
    assert hit.renewal_date.isoformat() == "2026-11-05"


def test_bank_alerts_become_one_subscription_per_merchant_not_per_bank():
    hits = [classify_message(msg(*a[:3]), region="IN") for a in INDIA_ALERTS[:2]]
    hits.append(classify_message(msg("alerts@hdfcbank.net", "Pre-debit notification",
                                     "Rs.199.00 will be debited on 10 Nov 2026 towards SONYLIV under UPI AutoPay. UMN: 9f8e7d@hdfcbank"), "IN"))
    subs = aggregate(hits)
    assert sorted(s.vendor for s in subs) == ["SONYLIV", "Spotify India"]
    sony = next(s for s in subs if s.vendor == "SONYLIV")
    assert sony.hits == 2 and sony.cycle == "month"


def test_revocation_alert_is_labelled():
    hit = classify_message(msg("alerts@hdfcbank.net", "UPI AutoPay mandate revoked",
                               "Your UPI AutoPay mandate for Rs.199.00 towards SONYLIV has been revoked."), "IN")
    assert hit.kind == "mandate-revoked"


def test_autopay_marketing_is_not_a_mandate():
    hit = classify_message(msg("offers@shop.example", "Set up AutoPay and save!", "Never miss a bill. Enable AutoPay today!"), "IN")
    assert hit is None


def test_ordinary_receipt_is_not_misread_as_a_card_alert():
    hit = classify_message(msg("info@account.netflix.com", "Your receipt",
                               "We charged $15.49 to your card ending 1234. Your subscription renews monthly. Recurring payment."), "US")
    assert hit.merchant == "" and hit.vendor == "Netflix" and hit.region == ""


def test_card_network_recurring_alert():
    hit = classify_message(msg("alerts@bank.example", "Recurring charge alert",
                               "A recurring payment of $9.99 to EXAMPLE STREAMING was made on your card ending 1234."), "US")
    assert hit.merchant == "EXAMPLE STREAMING" and hit.money.amount == 9.99 and hit.region == "cards"


def test_sepa_pre_notification_german_and_english():
    de = classify_message(msg("rechnung@fitness.example", "SEPA-Lastschrift Vorabankündigung",
                              "Wir werden am 05.11.2026 den Betrag von 29,90 € per SEPA-Lastschrift einziehen.\n"
                              "Mandatsreferenz: FIT-0042\nGläubiger-ID: DE98ZZZ09999999999\nMonatlicher Beitrag."), "DE")
    assert de and de.region == "eu" and de.money.amount == 29.9 and de.money.currency == "EUR"
    uk = classify_message(msg("billing@energy.example", "Your Direct Debit advance notice",
                              "We will collect £42.00 by Direct Debit on 1 December 2026. Service user number 123456. "
                              "This payment is covered by the Direct Debit Guarantee."), "GB")
    assert uk and uk.money.currency == "GBP" and uk.renewal_date.isoformat() == "2026-12-01"


def test_every_region_module_is_well_formed_and_sourced():
    codes = [r.code for r in regions.all_regions()]
    assert codes == ["in", "eu", "us", "cards"]
    for r in regions.all_regions():
        for g in r.revoke_guides:
            assert g.steps and g.provider
            if g.verified:
                assert g.source.startswith(("https://", "Directive")) and g.checked_on
    laws = {law.code: law for law in regions.privacy_laws()}
    assert set(laws) == {"dpdp", "gdpr", "uk-gdpr", "ccpa"}
    assert "Section 12" in laws["dpdp"].citation and "Article 17" in laws["gdpr"].citation
    assert "1798.105" in laws["ccpa"].citation
