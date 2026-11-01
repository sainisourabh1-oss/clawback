"""Real-mailbox finding (2026-10-28): account mail was filed as "newsletter".

Order updates, failed-payment notices, "you shared account data with an app",
broker trading notices, portfolio statements and credit-report mail showed up
under Newsletters & marketing with Unsubscribe actions, and the "Select senders
not opened in 3 months" button bulk-selected them for unsubscribing. Gmail
files such mail under Updates, which counted as "bulk".

Every address, name, amount and id below is invented; only the SHAPE (mailbox
name, subject style, unsubscribe header or not, Gmail tab) copies the real case.
"""
from datetime import datetime, timedelta, timezone

import pytest

import realmail as R
from helpers import write_mbox
from clawback import insights, scan, tracker
from clawback.extract import MessageInfo
from clawback.important import account_mail_reason, is_important
from clawback.scan import run_scan
from clawback.senders import SenderSummary
from clawback.sources.mbox import MboxSource

NOW = datetime.now(timezone.utc)


def mail(from_, subject, *, days_ago=1.0, lu=None, tab="Updates", unread=True):
    raw = R.msg(from_, subject, "Hello.", days_ago=days_ago, lu=lu, now=NOW,
                mid=f"<{abs(hash((from_, subject, days_ago)))}@case.example>")
    labels = f"Inbox,Category {tab}," + ("Unread" if unread else "Opened")
    return b"X-Gmail-Labels: " + labels.encode() + b"\n" + raw


def three(from_, subjects, **kw):
    subjects = subjects if isinstance(subjects, list) else [subjects] * 3
    return [mail(from_, s, days_ago=2 + 9 * i, **kw) for i, s in enumerate(subjects)]


ACCOUNT_MAIL = {
    # security notice WITH a List-Unsubscribe header (to the account's communication preferences)
    "noreply-accounts@id.example": three("Ids <noreply-accounts@id.example>", [
        "You shared some Example Account data with Photo App", "You shared some Example Account data with Notes App",
        "You shared some Example Account data with Chat App"], lu="<https://myaccount.id.example/communication-prefs>"),
    "order-update@shop.example": three("Shop <order-update@shop.example>", [
        "Delivered: “Rubber Hex Dumbbell 5 kg...”", "Delivered: 1 item | Order # 111-2223334-4445556",
        "Delivered: “Stylus Pen for Tablets...”"]),
    "failed-payments@mail.aitool.example": three("AI Tool <failed-payments@mail.aitool.example>", [
        "$12.30 payment to Example Labs, Inc. was unsuccessful again",
        "$7.10 payment to Example Labs, Inc. was unsuccessful again", "$3.40 payment to Example Labs, Inc. was unsuccessful"]),
    "accountalerts@broker.example": three("Broker <accountalerts@broker.example>", [
        "Update: Revised Square-Off Timings Under the New Framework", "Extended Trading Window for Stock Derivatives on Expiry",
        "\U0001f514 New IPO Opens Soon - Pre Apply Today"]),
    "portfolio-update@broker2.example": three("Broker Two <portfolio-update@broker2.example>",
                                              "Your Equity Portfolio Performance - X0000000"),
    "creditview-noreply@bureau.example": three("Bureau <creditview-noreply@bureau.example>",
                                               "Information Regarding Your Credit Profile"),
    # a mostly-promotional sender that also sent one failed-payment notice: important (safe side)
    "offers@telco.example": [mail("Telco <offers@telco.example>", s, days_ago=2 + 9 * i, lu="<https://telco.example/u>",
                                  tab="Promotions") for i, s in enumerate(["50% off on new plans", "Weekend data bonanza",
                                                                           "Refer a friend and win"])]
                            + [mail("Telco <offers@telco.example>", "Payment declined: revise the payment for your bill",
                                    days_ago=5)],
}
PROMO = {
    "marketing@photos.example": three("Photos <marketing@photos.example>", [
        "Last chance to submit to the Photo Awards", "Help choose the photo of the year", "Photo Awards 2026"],
        lu="<https://photos.example/unsub>", tab="Promotions"),
    # promotional with an occasional storage notice: stays marketing (not security or payment)
    "member-noreply@cloud.example": three("Cloud <member-noreply@cloud.example>", [
        "Member benefit: Get 10,000 store credit with purchase of a phone", "Your Cloud storage is 86% full",
        "Don't leave these last 2 steps unfinished"], lu="<https://cloud.example/unsub>", tab="Promotions"),
    "invites@ads.example": three("Ads <invites@ads.example>", "[Webinar Invitation] Peak Season: Unlock more revenue",
                                 lu="<https://ads.example/unsub>"),
    # no List-Unsubscribe, Updates tab only, but plainly promotional subjects
    "hello@charts.example": three("Charts <hello@charts.example>", ["Up to 80% off ending soon",
                                  "Up to 80% off this summer", "Labour Day deal - ending soon"]),
}
SERVICE_NOTICE = "updates@api.example"   # Updates tab only, no List-Unsubscribe, not promotional


@pytest.fixture
def scanned(tmp_path):
    raws = [r for group in (ACCOUNT_MAIL, PROMO) for rs in group.values() for r in rs]
    raws += three(f"API <{SERVICE_NOTICE}>", ["Model Small is being decommissioned",
                                             "REMINDER: Model Large is being decommissioned", "Model Mini is being decommissioned"])
    run_scan(MboxSource(write_mbox(tmp_path / "acct.mbox", raws), account="acct"), region="IN")
    return {s["address"]: s for s in tracker.list_senders("acct")}


@pytest.mark.parametrize("address", sorted(ACCOUNT_MAIL))
def test_account_mail_is_not_in_newsletters(scanned, address):
    s = scanned[address]
    assert s["category"] == "important", s
    assert s["important"], "the UI must be able to say why"


@pytest.mark.parametrize("address", sorted(ACCOUNT_MAIL))
def test_not_opened_never_selects_account_mail(scanned, address):
    picked = {x["address"] for x in insights.not_opened("acct")}
    assert address not in picked


@pytest.mark.parametrize("address", sorted(PROMO))
def test_promotions_stay_newsletters_and_are_suggested(scanned, address):
    assert scanned[address]["category"] == "newsletter", scanned[address]
    assert address in {x["address"] for x in insights.not_opened("acct")}


def test_updates_tab_alone_does_not_make_a_newsletter(scanned):
    # Gmail's Updates tab is "confirmations, receipts, bills and statements": with no
    # unsubscribe header and nothing promotional the sender is "other" (never suggested).
    assert scanned[SERVICE_NOTICE]["category"] == "other"
    assert SERVICE_NOTICE not in {x["address"] for x in insights.not_opened("acct")}


def _stale_newsletter(address, subjects, lu=()):
    """A sender stored by an older classifier as "newsletter", all unread."""
    rows = [(f"{address}-{i}", address, (NOW - timedelta(days=3 + 10 * i)).isoformat(), s, 1)
            for i, s in enumerate(subjects)]
    tracker.record_messages("old", rows, cv=scan.CLASSIFIER_VERSION - 1)
    tracker.upsert_senders("old", [SenderSummary(address=address, domain=address.split("@")[1], category="newsletter",
                                                 list_unsubscribe=list(lu), bulk=True, subjects=subjects)])


def test_not_opened_skips_account_mail_even_when_categorised_newsletter():
    _stale_newsletter("noreply@billing.example", ["Payment Declined for Invoice #900001"] * 3)
    _stale_newsletter("news@digest.example", ["This week in gardening #1", "Autumn bulbs", "Ten tips"],
                      lu=["https://digest.example/u"])
    assert {s["address"] for s in tracker.list_senders("old", "newsletter")} == {"noreply@billing.example",
                                                                                   "news@digest.example"}
    assert [x["address"] for x in insights.not_opened("old")] == ["news@digest.example"]


def test_rescan_moves_stale_account_senders_out_of_newsletters(tmp_path):
    # Old classifier filed it as newsletter; this rescan does not read its mail again
    # (older than the window), yet it must leave Newsletters.
    _stale_newsletter("order-update@shop.example", ["Delivered: “Rubber Hex Dumbbell...”"] * 3)
    _stale_newsletter("news@digest.example", ["This week in gardening #1"] * 3, lu=["https://digest.example/u"])
    assert scan.reads_everything("gmail", "old"), "rows from an older classifier version must be read again"
    run_scan(MboxSource(write_mbox(tmp_path / "new.mbox", PROMO["marketing@photos.example"]), account="old"))
    cats = {s["address"]: s["category"] for s in tracker.list_senders("old")}
    assert cats["order-update@shop.example"] == "important"
    assert cats["news@digest.example"] == "newsletter"
    assert cats["marketing@photos.example"] == "newsletter"


@pytest.mark.parametrize("subject", [
    "Your Code - 12345", "Your password has been changed", "Sign in to Example Wallet requested at 2026 January 16",
    "Bill for your Example Wi-Fi 0000000000 - October'26", "[Action needed] Your Example API access is turned off",
    "Example Cloud - Payment Declined for Invoice #900002", "Information Regarding Your Credit Profile",
    "Your Example order from Burger Place", "Refund initiated for connection having Order Number NO0000",
    "Alert: Incorrect mPIN attempt - 1 Attempt Remaining", "Your Example subscription has ended",
])
def test_account_subjects_score_important_even_with_unsubscribe_header(subject):
    info = MessageInfo("1", "news@company.example", "Company", "company.example", subject, None,
                       list_unsubscribe=["https://company.example/u"])
    assert is_important(info)[0], subject
    assert account_mail_reason("news@company.example", [subject])


@pytest.mark.parametrize("subject", [
    "Up to 80% off ending soon", "Member benefit: Get 10,000 store credit with purchase", "See your latest credit score (free)",
    "Your vibe has arrived", "[Webinar Invitation] Peak Season", "Welcome to Example", "Your storage is 86% full",
    "This week in gardening #3", "Complete your order today and save 20%",
])
def test_marketing_subjects_with_unsubscribe_header_are_not_important(subject):
    info = MessageInfo("1", "news@company.example", "Company", "company.example", subject, None,
                       list_unsubscribe=["https://company.example/u"])
    assert not is_important(info)[0], (subject, is_important(info))
    assert account_mail_reason("news@company.example", [subject]) is None, subject
