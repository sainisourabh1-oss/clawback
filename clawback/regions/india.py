"""India: UPI AutoPay, card e-mandate / standing-instruction alerts, e-NACH,
and DPDP Act 2023 data-deletion requests.

Why this matters: many Indian subscriptions never send a receipt from the
merchant. The only trace is the bank's or UPI app's mandate alert. Under the
RBI e-mandate framework (consolidated as "Digital Payments - E-mandate
Framework, 2026", RBI press release 21 Apr 2026) issuers send a pre-debit
notification before each recurring debit, naming the merchant, amount and
date. Axis Bank's card page (checked 2026-10-24) states: "Customers will get a
pre-debit (SMS/Email) notification 24 hours prior to the debit." Those alerts
are what this module reads.

Fixtures are synthetic and modelled on that public format; no real alert is
included anywhere in this project.
"""
from __future__ import annotations

import re

from ..detect import Hit, base_domain, cycle_near, find_dated
from ..money import parse_money
from .base import PrivacyLaw, Region, RevokeGuide

# Sending domains of banks and UPI apps (RBI moved banks to *.bank.in in 2025-26;
# the older domains are kept because alerts still arrive from them).
SENDERS = {
    "hdfcbank.net", "hdfcbank.com", "hdfc.bank.in", "icicibank.com", "icici.bank.in", "sbi.co.in", "onlinesbi.sbi",
    "sbi.bank.in", "axisbank.com", "axis.bank.in", "kotak.com", "kotak.bank.in", "kotak811.com", "yesbank.in",
    "indusind.com", "idfcfirstbank.com", "pnb.co.in", "bankofbaroda.com", "canarabank.com", "unionbankofindia.co.in",
    "paytm.com", "paytmbank.com", "phonepe.com", "amazonpay.in", "npci.org.in", "bhimupi.org.in", "cred.club",
    "sihub.in", "mandatehq.com", "billdesk.com", "razorpay.com", "cashfree.com",
}
_MANDATE = re.compile(
    r"(?i)\b(?:upi[- ]?autopay|autopay|e-?mandate|standing instructions?|\bSI\b|e-?nach|nach mandate|"
    r"pre-?debit notification|recurring (?:payment|debit|mandate)|mandate (?:id|reference|registered|created|revoked|cancelled|paused)|UMN|UMRN)\b"
)
# "/" ends a name too: NACH debits read "towards Example Finance Ltd/12345678 with UMRN ...".
_STOP = r"(?=\s+(?:has|have|is|was|will|under|on|via|for|from|with|using|through|ending|UMN|UMRN|mandate|frequency)\b|[.,;:\n|()/]|$)"
_MERCHANT = [
    re.compile(r"(?i)\bmerchant(?:\s+name)?\s*[:\-]\s*([A-Za-z0-9&'.+ -]{2,40}?)\s*(?:$|\n|[|,;])", re.M),
    re.compile(r"(?i)\bpayee(?:\s+name)?\s*[:\-]\s*([A-Za-z0-9&'.+ -]{2,40}?)\s*(?:$|\n|[|,;])", re.M),
    re.compile(rf"(?i)\btowards\s+([A-Z0-9][A-Za-z0-9&'.+ -]{{1,40}}?){_STOP}"),
    re.compile(rf"(?i)\bin favou?r of\s+([A-Z0-9][A-Za-z0-9&'.+ -]{{1,40}}?){_STOP}"),
    re.compile(rf"(?i)\bmandate (?:for|to)\s+(?:(?:Rs\.?|INR|₹)\s?[\d,.]+\s+(?:towards|for|to)\s+)?([A-Z0-9][A-Za-z0-9&'.+ -]{{1,40}}?){_STOP}"),
    re.compile(rf"(?i)(?:Rs\.?|INR|₹)\s?[\d,.]+(?:/-)?\s+(?:for|to|towards)\s+([A-Z0-9][A-Za-z0-9&'.+ -]{{1,40}}?){_STOP}"),
    re.compile(rf"(?i)\b(?:debited|paid|charged) to\s+([A-Z0-9][A-Za-z0-9&'.+ -]{{1,40}}?){_STOP}"),
    # Card e-mandate registration: "Your payment for Example is registered for E-mandate".
    re.compile(rf"(?i)\b(?:payment|subscription) (?:for|to)\s+([A-Z0-9][A-Za-z0-9&'.+ -]{{1,40}}?){_STOP}"),
    # NACH mandate acceptance: "· Issued to Example Finance Ltd."
    re.compile(rf"(?i)\b(?:issued|registered) (?:to|in favou?r of)\s+([A-Z0-9][A-Za-z0-9&'.+ -]{{1,40}}?){_STOP}"),
]
# A mandate's "Maximum transaction amount: USD 150.00" (card e-mandate) or "The
# mandate has a value of Rs. X" (NACH) is a cap the merchant may debit up to,
# not the price.
_CAP = re.compile(r"(?i)\b(?:max(?:imum)?|upper limit|limit|up to|mandate has a value of)\b[^\n]{0,40}?"
                  r"(?:Rs\.?|INR|₹|USD|US\$|\$|EUR|€|GBP|£)\s?[\d,]+(?:\.\d+)?")
# "Frequency: Monthly", or NACH's "At MNTH frequency" (NPCI codes MNTH, QURT, YRLY).
_FREQ = re.compile(r"(?i)\bfrequency\s*[:\-]?\s*(daily|weekly|fortnightly|monthly|mnth|quarterly|qurt|half[- ]yearly|"
                   r"yearly|annually|yrly|as presented|one[- ]time)\b|\b(mnth|qurt|yrly|monthly|quarterly|yearly)\s+frequency\b")
_FREQ_MAP = {"weekly": "week", "monthly": "month", "mnth": "month", "qurt": "quarter", "yrly": "year", "quarterly": "quarter", "half-yearly": "half-year", "half yearly": "half-year",
             "yearly": "year", "annually": "year"}
_REVOKED = re.compile(r"(?i)\b(?:revoked|cancelled|canceled|deactivated|paused|suspended)\b")
_NEXT_DEBIT = r"\b(?:will be debited|debit(?:ed)? on|debit date|scheduled (?:for|on)|due on|next debit)\b"


# UPI IDs (VPAs), email addresses and URLs are removed before looking for
# mandate wording: a payee VPA such as "autopay.billdesk@okhdfcbank" in an
# ordinary one-off UPI payment alert once produced a "VPA autopay" mandate of
# a large monthly amount (real case).
_TOKENS = re.compile(r"(?i)\b[\w.+-]+@[\w.-]+\b|https?://\S+|\bwww\.\S+")
_VPA_PAYEE = re.compile(r"(?i)\bVPA\s+[\w.+-]+@[\w.-]+\s+([A-Z][A-Z0-9&'. -]{2,40}?)(?=\s+on\b|[.,;\n(]|$)")
# Loan EMIs and repayments to banks/NBFCs are not subscriptions: they go to a
# separate "Loans & EMIs" bucket that is never in "you're paying" or savings.
_LOAN = re.compile(r"(?i)\b(?:loan|emi|equated monthly|repayment|instal+ments?|home finance|housing finance|"
                   r"overdue|credit card (?:bill|dues)|card dues|bill payment towards)\b")
_LENDER = re.compile(r"(?i)\b(?:bank|finance|finserv|financial services|capital|credit|fincorp|nbfc|lending|loans?)\b")
_NOT_MERCHANT = {"your account", "the merchant", "merchant", "you", "your", "the payee", "payee"}


def _merchant(text: str) -> str:
    for pat in _MERCHANT:
        m = pat.search(text)
        if m:
            name = " ".join(m.group(1).split()).strip(" -.'")
            if name.lower().startswith("vpa"):
                continue  # a UPI ID is not a merchant name
            if re.search(r"[A-Za-z]{2}", name) and name.lower() not in _NOT_MERCHANT:
                return name[:40]
    m = _VPA_PAYEE.search(text)   # "to VPA x@y MERCHANT NAME on ..." -> the registered payee name
    if m:
        return " ".join(m.group(1).split()).strip(" -.'")[:40]
    return ""


def detect(info) -> Hit | None:
    text = f"{info.subject}\n{info.body}"
    if not _MANDATE.search(_TOKENS.sub(" ", text)):
        return None
    known_sender = (base_domain(info.domain) in SENDERS or info.domain in SENDERS
                    or info.domain.endswith((".bank.in", ".fin.in")))
    money = parse_money(_CAP.sub(" ", text), info.domain, "IN")
    if money is not None and money.amount <= 0:
        money = None  # "Current transaction amount: 0.00" (a registration) charges nothing
    merchant = _merchant(_TOKENS.sub(lambda m: m.group(0) if "@" in m.group(0) else " ", text))
    if not merchant and known_sender and money is not None:
        merchant = ""  # genuine mandate wording from a bank, payee not named
    elif not merchant or (money is None and not known_sender):
        # Needs the merchant AND an INR amount, or a known bank/UPI sender:
        # generic "set up AutoPay!" marketing has neither.
        return None
    if money is not None and money.currency != "INR" and not known_sender:
        return None
    freq = _FREQ.search(text)
    cycle = _FREQ_MAP.get((freq.group(1) or freq.group(2)).lower()) if freq else None
    if cycle is None and money is not None:
        idx = text.find(money.raw)
        cycle = cycle_near(text, idx, idx + len(money.raw))
    revoked = bool(_REVOKED.search(info.subject or ""))
    kind = "mandate-revoked" if revoked else "mandate"
    if not revoked and (_LOAN.search(_TOKENS.sub(" ", text)) or (merchant and _LENDER.search(merchant))):
        kind = "loan"
    vendor = (merchant.title() if merchant.isupper() or merchant.islower() else merchant) or "Unknown merchant"
    return Hit(
        vendor=vendor, domain=info.domain,
        subject=(info.subject or "")[:160], amount=money.raw if money else None, kind=kind,
        confidence=0.85 if known_sender else 0.7, evidence="mandate alert" + (" (bank/UPI sender)" if known_sender else ""),
        cycle=cycle, plan="", recurring=True, money=money, renewal_date=find_dated(info.body or "", _NEXT_DEBIT),
        merchant=merchant, region="in",
    )


REVOKE_GUIDES = [
    RevokeGuide(
        "Google Pay (UPI AutoPay)",
        ["Open Google Pay and tap your profile picture (top right).", "Tap Autopay.",
         "In the 'Live' tab, tap the mandate.", "Choose 'Cancel autopay' (or 'Pause autopay' with dates).",
         "Enter your UPI PIN, then check the mandate's status in the 'Live' tab.",
         "Loan-related mandates cannot be cancelled in Google Pay: contact the merchant."],
        source="https://support.google.com/pay/india/answer/10840624", checked_on="2026-10-24", verified=True),
    RevokeGuide(
        "ICICI Bank cards (standing instructions / e-mandates)",
        ["Find the Mandate ID in the registration or pre-debit SMS/email.",
         "If it starts with 'IC', use Mandate HQ: https://icici.mandatehq.com/login ; otherwise SI Hub: https://www.sihub.in/managesi/icici",
         "Log in with your card number and view or change the mandate there.",
         "'Opt-out' or 'Pause' only halts it temporarily; the bank says it cannot cancel on your behalf."],
        source="https://www.icici.bank.in/personal-banking/cards/merchant-standing-instructions", checked_on="2026-10-24", verified=True),
    RevokeGuide(
        "Axis Bank cards (standing instructions / e-mandates)",
        ["Go to SI Hub: https://www.sihub.in/managesi/axisbank (register, then cancel or modify the mandate), or",
         "Mandate HQ: https://axis.mandatehq.com (enter the card number, confirm the OTP, pick the mandate, cancel or modify).",
         "You can also opt out through the link in the pre-debit notification."],
        source="https://www.axis.bank.in/important-links/credit-card/recurring-payments", checked_on="2026-10-24", verified=True),
    RevokeGuide(
        "PhonePe (UPI AutoPay)",
        ["Tap your profile picture on the home screen.", "Under Payment Management, tap AutoPay.",
         "Pick the mandate and choose to remove it; confirm with your UPI PIN if asked.",
         "You can also cancel from the merchant's own app or website."],
        source="PhonePe help page 'How do I modify or delete an AutoPay...' (seen via search 2026-10-24; the page now redirects)",
        checked_on="2026-10-24", verified=False),
    RevokeGuide(
        "Paytm, BHIM, Amazon Pay and other UPI apps",
        ["Revoke the mandate in the SAME UPI app where you approved it (look for AutoPay / Automatic Payments / Mandates).",
         "Pick the mandate, choose Cancel/Revoke (Pause is only temporary) and confirm with your UPI PIN."],
        source="general guidance (no official help page could be verified)", verified=False),
    RevokeGuide(
        "HDFC, SBI, Kotak and other bank cards",
        ["Use the card e-mandate portal named in the bank's registration or pre-debit alert (often SI Hub or Mandate HQ), "
         "or the mandates section of the bank's app/net banking.", "If you cannot find it, call the number on the back of your card."],
        source="general guidance (bank pages could not be verified)", verified=False),
    RevokeGuide(
        "Always also",
        ["Cancel the plan with the merchant too: revoking the mandate stops future debits but may not end the subscription.",
         "Revoking does not refund past debits."],
        source="general guidance", verified=False),
]

DPDP = PrivacyLaw(
    code="dpdp", name="India - Digital Personal Data Protection Act, 2023", region="in",
    citation="Section 12 (right to correction and erasure of personal data) and Section 6(4) (withdrawal of consent) "
             "of the Digital Personal Data Protection Act, 2023; Section 13 (grievance redressal)",
    deadline="The Act does not fix a number of days in Section 12; the DPDP Rules, 2025 (notified 13-14 Nov 2025) bring "
             "most Data Fiduciary obligations into force 18 months after publication (around May 2027). Until then the "
             "company may say the right is not yet enforceable.",
    notes="Send it to the company's Data Protection Officer or grievance officer (see its privacy notice).",
    template=(
        "To the Data Protection Officer / Grievance Officer of {company},\n\n"
        "I am a Data Principal under the Digital Personal Data Protection Act, 2023. My email address is {email}.\n\n"
        "1. I withdraw any consent I have given for processing my personal data for marketing or other purposes "
        "(Section 6(4)).\n"
        "2. I request erasure of my personal data that you hold, other than data you are required by law to retain "
        "(Section 12(3)).\n"
        "3. Please confirm in writing when this is done, and tell me which data, if any, you retain and under which law.\n\n"
        "If you do not respond, I may approach your grievance redressal mechanism (Section 13) and then the Data "
        "Protection Board of India.\n\n"
        "Regards,\n{name}\n{date}\n"),
)

REGION = Region(
    code="in", name="India", detect=detect, revoke_guides=REVOKE_GUIDES, privacy_laws=[DPDP],
    description="UPI AutoPay, card e-mandate / standing-instruction and e-NACH alerts; DPDP Act 2023 requests.",
)
