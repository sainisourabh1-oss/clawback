"""Synthetic emails modelled on patterns seen in a real-Gmail test (2026-10).

Nothing here is real mail. Sender addresses are the public sending addresses
of the organisations named (needed because the rules depend on their domains);
subjects, bodies, amounts, account numbers and IDs are invented. Each fixture
copies only the *shape* that tripped the classifier: the header layout, the
kind of unsubscribe host, the Authentication-Results result, the subject style.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)


def msg(from_: str, subject: str, body: str, *, days_ago: float = 1, lu: str | None = None, one_click: bool = False,
        auth: str | None = None, forged_auth: str | None = None, reply_to: str | None = None,
        list_id: str | None = None, precedence: str | None = None, mid: str | None = None,
        now: datetime = NOW) -> bytes:
    m = EmailMessage()
    if auth:
        m["Authentication-Results"] = auth
    if forged_auth:  # a lower, sender-written header that must be ignored
        m["Authentication-Results"] = forged_auth
    m["From"] = from_
    m["To"] = "you@mail.example"
    m["Subject"] = subject
    m["Date"] = format_datetime(now - timedelta(days=days_ago))
    m["Message-ID"] = mid or f"<{abs(hash((from_, subject, days_ago)))}@fixture.example>"
    if lu:
        m["List-Unsubscribe"] = lu
    if one_click:
        m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if reply_to:
        m["Reply-To"] = reply_to
    if list_id:
        m["List-Id"] = list_id
    if precedence:
        m["Precedence"] = precedence
    m.set_content(body)
    return m.as_bytes()


def ar(dkim_d: str | None = None, spf_from: str | None = None, dmarc: str = "pass", spf: str = "pass",
       dkim: str = "pass") -> str:
    parts = ["mx.google.com"]
    if dkim_d:
        parts.append(f"dkim={dkim} header.i=@{dkim_d} header.s=s1 header.b=AbCdEf12")
    if spf_from:
        parts.append(f"spf={spf} (google.com: domain of bounce@{spf_from} designates 192.0.2.10 as permitted sender) "
                     f"smtp.mailfrom=bounce@{spf_from}")
    parts.append(f"dmarc={dmarc} (p=REJECT sp=REJECT dis=NONE) header.from={dkim_d or spf_from or 'example.com'}")
    return "; ".join(parts)


# ---------------------------------------------------------------- D: important / transactional
def sbi_neft_alerts() -> list[bytes]:
    # Bank alerts from the new *.bank.in domain, replies to the older sbi.co.in
    # domain, SPF softfail through the bank's relay: scored "spam" before.
    return [msg("SBI <neftinfo.itps@alerts.sbi.bank.in>",
                f"NEFT transaction alert: Rs. {amt} credited to your A/c XX1234",
                f"Dear Customer, your A/c XX1234 has been credited with Rs. {amt} on 2{i}-10-2026 by NEFT "
                f"(UTR SBIN0000000{i}). Available balance: Rs. 1,00,000.00. This is a system generated alert.",
                days_ago=3 + 9 * i, auth=ar("alerts.sbi.bank.in", "alerts.sbi.co.in", spf="softfail"),
                reply_to="noreply@alerts.sbi.co.in")
            for i, amt in enumerate(("25,000.00", "4,200.00", "18,500.00"))]


def bobcard_statements() -> list[bytes]:
    # Card statements whose display name contains "Bank", unsubscribe host on a
    # numbered delivery-provider domain (usub.ftrans03.com): "spam" before.
    return [msg("Bank of Baroda Card <donotreply@mybobcard.co.in>",
                f"Your BOBCARD credit card statement for {mon} 2026",
                f"Dear Cardholder, your credit card statement for {mon} 2026 is attached. Total amount due: Rs. 3,210.00. "
                "Payment due date: 15 Nov 2026.",
                days_ago=5 + 30 * i, lu="<https://usub.ftrans03.com/u/q1w2e3r4t5>",
                auth=ar("mybobcard.co.in", "ftrans03.com"))
            for i, mon in enumerate(("September", "August"))]


def github_notifications(n: int = 6) -> list[bytes]:
    return [msg("Some Contributor <notifications@github.com>",
                f"Re: [octo-org/widgets] Fix crash on empty input (PR #{40 + i})",
                "@you approved this pull request.\n\nReply to this email directly, view it on GitHub, or unsubscribe.",
                days_ago=1 + i, lu=f"<mailto:unsub+{i}@reply.github.com>, <https://github.com/notifications/unsubscribe/abc{i}>",
                list_id="octo-org/widgets <widgets.octo-org.github.com>", precedence="list",
                auth=ar("github.com", "github.com"))
            for i in range(n)]


def government() -> list[bytes]:
    return [
        msg("Sampark <no-reply@sampark.gov.in>", "Message from your department: office order 12/2026",
            "Please find the office order attached. This is an auto-generated mail.", days_ago=4,
            precedence="bulk", auth=ar("sampark.gov.in", "sampark.gov.in")),
        msg("UIDAI <aadhaar@uidai.gov.in>", "Aadhaar authentication alert",
            "Your Aadhaar number XXXX XXXX 1234 was used for authentication on 20-10-2026. If this was not you, lock "
            "your biometrics.", days_ago=6, precedence="bulk", auth=ar("uidai.gov.in", "uidai.gov.in")),
    ]


def broker_mail() -> list[bytes]:
    return [
        msg("NSE <nse_alerts@nse.co.in>", "Trades executed at NSE on 21-Oct-2026",
            "Dear Investor, the following trades were executed in your account (client code XX99) on 21-Oct-2026 ...",
            days_ago=3, precedence="bulk", lu="<mailto:unsubscribe@nse.co.in>", auth=ar("nse.co.in", "nse.co.in")),
        msg("Angel One <contract.notes@angeltrade.in>", "Contract Note for 21-10-2026",
            "Dear Client, please find attached the contract note for trades on 21-10-2026.", days_ago=3,
            auth=ar("angeltrade.in", "angeltrade.in")),
        msg("Zerodha <no-reply-account-statement@reportsmailer.zerodha.net>", "Your monthly account statement - September 2026",
            "Dear Client, your funds and securities statement for September 2026 is attached.", days_ago=20,
            lu="<https://reportsmailer.zerodha.net/unsubscribe/x1>", auth=ar("reportsmailer.zerodha.net", "zerodha.net")),
    ]


def google_security() -> list[bytes]:
    return [
        msg("Google <no-reply@accounts.google.com>", "Security alert",
            "A new sign-in on Windows. We noticed a new sign-in to your Google Account on a Windows device. If this was "
            "you, you don't need to do anything.", days_ago=2, auth=ar("accounts.google.com", "accounts.google.com")),
        msg("Google <noreply-accounts@google.com>", "2-Step Verification turned on",
            "2-Step Verification is now on for your Google Account.", days_ago=40, auth=ar("google.com", "google.com")),
    ]


def bank_upi_alert() -> bytes:
    # Ordinary one-off UPI payment alert. The payee VPA contains "autopay":
    # before, this became a "VPA autopay" mandate at Rs 6,250/month.
    return msg("ICICI Bank <alerts@icici.bank.in>", "❗ You have done a UPI txn. Check details!",
               "Dear Customer,\nMoney sent! Rs.6250.00 to VPA autopay.billdesk@okicici from ICICI Bank A/c **1234 on "
               "21-10-26.\nUPI transaction reference number: 629400000001.\nNot you? Call 1800 000 0000 or SMS BLOCK UPI "
               "to 70000 00000.", days_ago=3, auth=ar("icici.bank.in", "icici.bank.in"))


def hdfc_marketing() -> list[bytes]:
    # Regulated-entity marketing: fine as marketing, NOT important, NOT spam.
    return [msg("HDFC Bank <information@mailers.hdfcbank.bank.in>", f"Pre-approved offer {i}: a personal loan at special rates",
                "Exclusive offer for you. Apply now. To stop receiving these emails, unsubscribe.",
                days_ago=2 + 7 * i, lu=f"<https://hdfc.netcorecloud.net/unsub/{i}>", one_click=True,
                auth=ar("mailers.hdfcbank.bank.in", "netcorecloud.net"))
            for i in range(3)]


def indian_marketing() -> list[bytes]:
    # Marketing from companies the user has an account with: "marketing", not spam.
    out = []
    for i in range(3):
        out.append(msg("BillDesk <offers@billdesk.com>", f"Pay bills, win cashback - offer {i}",
                       "Pay your bills on BillDesk and get cashback. Unsubscribe here.", days_ago=1 + 10 * i,
                       lu=f"<https://bd.netcorecloud.net/u/{i}>", one_click=True, auth=ar("billdesk.com", "netcorecloud.net")))
        out.append(msg("Angel One <marketing@angelone.in>", f"Open an F&O account in minutes ({i})",
                       "Start trading today. Unsubscribe.", days_ago=2 + 10 * i,
                       lu=f"<https://track.aclemails.com/unsub/{i}>", auth=ar("angelone.in", "aclemails.com")))
        out.append(msg("icash <offers@icashcard.in>", f"Recharge and save {10 + i}%",
                       "Top up your card today. Unsubscribe.", days_ago=3 + 10 * i,
                       lu=f"<https://u.aclmails.in/x/{i}>", auth=ar("icashcard.in", "aclmails.in")))
    return out


def phishing_bank() -> bytes:
    """Control: a look-alike bank 'security alert' that fails DMARC must stay spam."""
    return msg("HDFC Bank Security <alerts@hdfc-secure-verify.example>", "Security alert: your account will be suspended",
               "Verify your account now to avoid suspension: https://hdfc-secure-verify.example/login", days_ago=1,
               auth=ar("hdfc-secure-verify.example", "hdfc-secure-verify.example", dmarc="fail", dkim="fail"),
               reply_to="support@another-host.example")


def plain_newsletter() -> list[bytes]:
    """Control: an ordinary newsletter stays a newsletter."""
    return [msg("Weekly Digest <news@digest.example>", f"This week in gardening #{i}",
                "Ten tips for autumn bulbs. Unsubscribe.", days_ago=1 + 7 * i,
                lu=f"<https://digest.example/unsub/{i}>", one_click=True, auth=ar("digest.example", "digest.example"))
            for i in range(4)]


# ---------------------------------------------------------------- E: ESP unsubscribe hosts
def esp_senders() -> dict[str, bytes]:
    return {
        "pinterest": msg("Pinterest <ideas@explore.pinterest.com>", "Ideas you might love",
                         "New pins for you. Unsubscribe.", lu="<https://u.spmta.com/unsub/abc>", one_click=True,
                         auth=ar("explore.pinterest.com", "spmta.com")),
        "brevo": msg("Notion Press <news@notionpress.com>", "New releases this week",
                     "Books! Unsubscribe.", lu="<https://r.sendibm1.com/unsub/1>, <mailto:unsub-1@mailin.fr>",
                     one_click=True, auth=ar("notionpress.com", "sendibm1.com")),
        "lu-me": msg("Binance <do-not-reply@post.binance.com>", "Weekly market update",
                     "Markets. Unsubscribe.", lu="<https://list-unsubscribe.me/u/zz>", one_click=True,
                     auth=ar("post.binance.com", "post.binance.com")),
        # SendGrid's unsubscribe host (data/email_providers.json, sourced by DNS):
        # the link and the mailto both sit on it.
        "sendgrid-inbound": msg("Wallet News <news@wallet.example>", "What's new this month",
                                "News. Unsubscribe.", one_click=True,
                                lu="<https://unsubscribe-07.emailinboundprocessing.com/u/1>, "
                                   "<mailto:unsub-1@unsubscribe-07.emailinboundprocessing.com>",
                                auth=ar("wallet.example", "em1.sendgrid.net")),
        # An unknown host carrying both the link and the mailto: one warning, not two.
        "unknown-both": msg("Gadgets <news@gadgets.example>", "New gadgets", "Gadgets. Unsubscribe.",
                            lu="<https://unsub.oddhost.example/u/1>, <mailto:u1@lists.oddhost.example>",
                            auth=ar("gadgets.example", "gadgets.example")),
        "sbi-co-in-link": msg("SBI <offers@alerts.sbi.bank.in>", "Festive offers on SBI cards",
                              "Offers. Unsubscribe.", lu="<https://alerts.sbi.co.in/unsub/7>",
                              auth=ar("alerts.sbi.bank.in", "alerts.sbi.bank.in")),
        "dkim-signed-unknown-esp": msg("Shop <news@shop.example>", "Autumn sale",
                                       "Sale. Unsubscribe.", lu="<https://click.newesp.example/u/1>",
                                       auth=ar("newesp.example", "newesp.example")),
        "unknown-host": msg("Webinar <events@webinar.example>", "Join our webinar",
                            "Webinar. Unsubscribe.", lu="<https://track.clickfarm.example/u/1>",
                            auth=ar("webinar.example", "webinar.example")),
        "forged-lower-ar": msg("Promo <deals@promo.example>", "Deals", "Deals. Unsubscribe.",
                               lu="<https://evil-tracker.example/u/1>", auth=ar("promo.example", "promo.example"),
                               forged_auth=ar("evil-tracker.example", "evil-tracker.example")),
    }


# ---------------------------------------------------------------- F: paid-subscription detection
def amazon_nominations() -> list[bytes]:
    out = []
    for i, what in enumerate(("Kindle Deals", "Prime Reading")):
        for k in range(3):
            out.append(msg("Amazon.in <digital-no-reply@amazon.in>", f"Your {what} nomination will renew soon",
                           f"Your {what} nomination will renew automatically on {5 + k} Nov 2026. No action is needed.",
                           days_ago=10 + 91 * k + i))
    return out


def google_play() -> list[bytes]:
    out = []
    for k in range(3):  # Rs 169 on the 6th, monthly (the 27th plan: google_one_ground_truth)
        out.append(msg("Google Play <googleplay-noreply@google.com>", f"Your Google Play Order Receipt from {('Oct', 'Sept', 'Aug')[k]} 6, 2026",
                       "Order number: GPA.0000-0000-0000-0000\nItem: YouTube Premium (YouTube)\nAuto-renewing subscription\n"
                       "Price: ₹169.00/month\nIf you're in a free trial, your trial ends on the date shown in Google Play.",
                       days_ago=18 + 30.4 * k))
    out.append(msg("Google Store <googlestore-noreply@google.com>",
                   "Member benefit: Get ₹10,000 Google Store credit with purchase of Pixel 10 Pro",
                   "As a Google One member, get ₹10,000 store credit. Your Google One membership renews monthly. "
                   "Offer ends 31 Oct 2026.", days_ago=8))
    return out


def hostinger_trial() -> list[bytes]:
    return [
        msg("Hostinger <noreply@hostinger.com>", "Your Starter Business Email       Trial ends in 7 days",
            "Plan: Starter Business Email       Trial\nYour free trial ends on 31 Oct 2026. After the trial you will be "
            "charged ₹59.00/month unless you cancel.", days_ago=7),
        msg("Hostinger <noreply@hostinger.com>", "Your Starter Business Email Trial ends tomorrow",
            "Plan: Starter Business Email Trial\nYour free trial ends on 31 Oct 2026. After the trial you will be "
            "charged ₹59.00/month unless you cancel.", days_ago=1),
    ]


def google_one_ground_truth() -> list[bytes]:
    """Ground truth: the plan on the 27th is Google One 30 GB at Rs 59/month
    (receipt table layout, GST included). A Google One promo advertising a
    Rs 1,950 plan and Rs 10,000 store credit must not join it or set its price."""
    out = []
    for k, mon in enumerate(("Sept", "Aug", "Jul")):
        out.append(msg("Google Play <googleplay-noreply@google.com>", f"Your Google Play Order Receipt from {mon} 27, 2026",
                       "Thank you.\nYou've made a purchase from Google Play.\nOrder number: GPA.3333-3333-3333-3333\n"
                       "Order date: 27-Sep-2026\nItem Price\n30 GB (Google One) (by Google LLC) ₹59.00/month\n"
                       "Auto-renewing subscription\nTotal: ₹59.00/month (Includes GST of ₹9.00)\n"
                       "Payment method: UPI", days_ago=27 + 30.4 * k))
    out.append(msg("Google One <googleone-noreply@google.com>",
                   "Member benefit: Get ₹10,000 Google Store credit with purchase of Pixel 10 Pro",
                   "Upgrade to Google AI Pro (2 TB) for ₹1,950/month and get ₹10,000 Google Store credit when you buy "
                   "a Pixel 10 Pro. Your membership renews monthly. Terms apply. Unsubscribe.",
                   days_ago=9, lu="<https://google.com/u/x>"))
    return out


def loan_emi() -> list[bytes]:
    """Ground truth: a Rs 6,250 debit to Example Lending Bank is a LOAN EMI, not a
    subscription (and must not count as 'you're paying')."""
    return [msg("ICICI Bank <alerts@icici.bank.in>", "UPI AutoPay mandate executed",
                f"Dear Customer, Rs.6,250.00 has been debited from your A/c **1234 on 05-{m:02d}-2026 towards "
                "Example Lending Bank Ltd under your UPI AutoPay mandate (Loan EMI). UMN: lend123@icici.",
                days_ago=19 + 30.4 * i)
            for i, m in enumerate((10, 9, 8))]


def real_mandate() -> bytes:
    """Control: a genuine UPI AutoPay pre-debit notification is still detected."""
    return msg("ICICI Bank <alerts@icici.bank.in>", "Pre-debit notification for your UPI AutoPay mandate",
               "Dear Customer, Rs.149.00 will be debited from your A/c **1234 on 05-11-2026 towards Spotify India under "
               "your UPI AutoPay mandate. Frequency: Monthly. UMN: abc123@icici.", days_ago=2,
               auth=ar("icici.bank.in", "icici.bank.in"))
