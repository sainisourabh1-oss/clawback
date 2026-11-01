"""Important / transactional mail: bank and card alerts and statements,
government mail, broker contract notes, security and sign-in alerts, OTPs,
receipts, and work notifications (GitHub and similar).

Why this exists: in a real mailbox test, bank NEFT alerts and card statements
were labelled "spam" and GitHub, Aadhaar, NSE, broker and Google security mail
were labelled "newsletter", so the UI invited the user to unsubscribe from
them. These senders are now their own category: never suggested, shown with a
"Keep" default, and any action on them needs an explicit "I understand".

Scoring (per message; a sender is important when ANY of its messages scores
>= IMPORTANT_THRESHOLD and it has no identity-spoofing signal):

  3  government sending domain (.gov, .gov.in, .nic.in, .gov.uk, ...)
  3  transactional subject: OTP / verification code, security or sign-in
     alert, password or 2-step change, debited/credited/UPI/NEFT alert,
     account/card/demat statement, contract note, trades executed
  3  activity notification from a collaboration tool (notifications@ on
     GitHub/GitLab/Bitbucket/Atlassian/Linear/Slack, or a [owner/repo]
     thread subject)
  2  regulated finance domain (.bank.in, .fin.in, .bank) or market
     infrastructure (exchanges, depositories, registrars)
  3  account notice subject (_ACCOUNT_SUBJECT): order placed/delivered,
     refund, payment failed/declined/unsuccessful, bill, account data shared
     with an app, access turned off, sign-in requested, access code, email or
     identity verification, credit report, portfolio statement, broker
     square-off / trading-window notice, ... (not when the subject is a promo)
  2  receipt / invoice / booking / order confirmation subject
  2  account mailbox name: order-update@, failed-payments@, billing@,
     noreply-accounts@, accountalerts@, portfolio-update@, otp@, ...
  1  sender mailbox name like alerts@, statements@, contract.notes@, cbsalerts@
  1  no List-Unsubscribe header (transactional mail rarely has one)
 -2  your provider filed it under Promotions

Why "any message" rather than a share: the two errors are not symmetric. A
bank address wrongly marked important costs one extra tick ("I understand")
if you really want to act; a bank alert address wrongly marked "newsletter"
invites you to unsubscribe from fraud alerts. Why 3: one strong signal is
enough, a regulated domain alone (2) is not, so bank *marketing*
(information@mailers.hdfcbank.bank.in with List-Unsubscribe, Promotions tab)
stays "marketing" while its alerts address becomes important.
"""
from __future__ import annotations

import re

from .detect import PROMO_SUBJECT, base_domain

IMPORTANT_THRESHOLD = 3

GOV_SUFFIXES = (
    "gov", "mil", "gov.in", "nic.in", "gov.uk", "nhs.uk", "police.uk", "gov.au", "govt.nz", "gc.ca", "canada.ca",
    "europa.eu", "gouv.fr", "bund.de", "admin.ch", "gov.sg", "go.jp", "gov.hk", "gov.cn", "go.kr", "gov.tw",
    "gov.my", "gov.ph", "go.th", "go.id", "gov.vn", "gov.pk", "gov.bd", "gov.np", "gov.lk", "gov.br", "gob.mx",
    "gob.ar", "gov.co", "gob.pe", "gov.za", "gov.tr", "gov.sa", "gov.ae", "gov.il", "gov.eg", "gov.ng", "go.ke",
    "gov.pl", "gov.ua", "gov.ie",
)
REGULATED_SUFFIXES = ("bank.in", "fin.in", "bank", "insurance")
# Statutory market infrastructure: their mail is about your holdings, not marketing.
MARKET_INFRA = {"nse.co.in", "nseindia.com", "bseindia.com", "cdslindia.com", "nsdl.co.in", "camsonline.com",
                "kfintech.com", "npci.org.in", "sebi.gov.in", "mcxindia.com", "mcxccl.com"}
COLLAB = {"github.com", "gitlab.com", "bitbucket.org", "atlassian.net", "atlassian.com", "linear.app", "slack.com"}

_TXN_SUBJECT = re.compile(
    r"(?i)\b(?:otp|one[- ]time (?:password|passcode|pin)|verification code|security code|login code|sign[- ]?in code"
    r"|security (?:alert|notification)|new (?:sign[- ]?in|log[- ]?in|device)|sign[- ]?in (?:attempt|alert|from|on)"
    r"|password\b(?: for \S+)?(?: (?:has been|was|is))? (?:reset|changed|change|updated)|password (?:recovery|reset)|new password"
    r"|2-step verification|two[- ](?:factor|step)|account (?:recovery|was recovered)"
    r"|recovery (?:email|phone)|debited|credited|withdrawn|deposited|txn|transaction (?:alert|successful|declined|failed|notification|details)"
    r"|upi (?:txn|transaction|payment)|neft|rtgs|imps"
    r"|(?:account|card|e-?|bank|demat|monthly|annual|quarterly|consolidated|holdings?|ledger|margin|mutual fund|cas|"
    r"funds and securities|retention) ?statement|statement (?:for|of|period)"
    r"|contract notes?|trade confirmation|trades? executed|aadhaar|income tax|itr-?\d?|e-?filing)\b")
_RECEIPT_SUBJECT = re.compile(
    r"(?i)\b(?:receipt|invoice|order (?:confirmation|confirmed|shipped|delivered)|booking (?:confirmation|confirmed)"
    r"|e-?ticket|itinerary|payment (?:received|successful|confirmation|confirmed|failed))\b")
_ALERT_LOCAL = re.compile(
    r"(?i)(?:^|[._+-])(?:alerts?|statements?|e-?statements?|contract[._-]?notes?|account[._-]?statements?|otp|"
    r"transactions?|txn|security|instaalerts?|noreply-accounts|cards?alerts?)(?:$|[._+-])"
    # one-word compounds: accountalerts@, cbsalerts.sbi@
    r"|^[a-z]{2,12}alerts?(?:$|[._+-])")
# Mailbox names that only send account mail: orders, payments, billing,
# account notices. Worth 2: with no unsubscribe header (+1) that is enough on
# its own; with an unsubscribe header it still needs a transactional subject.
_ACCOUNT_LOCAL = re.compile(
    r"(?i)^(?:no-?reply[._-])?(?:accounts?[._-]?(?:alerts?|updates?|security|protection)(?:[._-]no-?reply)?|accountalerts"
    r"|account-security-noreply|noreply-accounts|failed[._-]?payments?|payments?[._-]?(?:updates?|reports?|failed)"
    r"|billing(?:[._-][a-z]+)?|order[._-]?(?:updates?|confirm\w*|status)|auto-?confirm|orders?|shipment[._-]?\w*"
    r"|portfolio[._-]?updates?|e-?bills?|e-?statements?|otp|security)$")
# Account notices that are not covered by _TXN_SUBJECT: orders and deliveries,
# failed or declined payments, bills, account data shared with an app, access
# turned off, credit reports, portfolio statements, broker trading notices.
# Real-mailbox finding: these were all "newsletter" (Gmail files most of them
# under Updates, which made them "bulk"), so the bulk-select "not opened"
# button offered to unsubscribe from security notices and failed-payment mail.
_ACCOUNT_SUBJECT = re.compile(
    r"(?i)(?:^|\s)(?:delivered|ordered|shipped|dispatched|out for delivery|arriving (?:today|tomorrow)|refunded)\s*:"
    r"|\b(?:your (?:\w+ ){0,2}order (?:from|of|at|has|is|was|#|no\.?|number)|order (?:#|no\.?\s*:?|number|id)\s*:?\s*\w*\d"
    r"|received your order|ready for dispatch|out for delivery|(?:has been|was) (?:shipped|dispatched|delivered)"
    r"|you (?:bought|purchased)|new sale of|thank you for your (?:order|purchase)"
    r"|refund (?:initiated|issued|processed|has been|of)|your refund"
    r"|payment (?:declined|failed|unsuccessful|failure|reminder|due|overdue|pending|notification|is late)"
    r"|payment (?:to|of|for) .{1,60}? (?:was |is |has been )?(?:unsuccessful|declined|failed)(?: again)?"
    r"|(?:card|payment|transaction) (?:was |has been )?declined|failed billing|billing (?:failed|failure|problem|issue)"
    r"|(?:update|revise|fix) (?:your )?payment|invalid payment|confirm your \S+ payment|royalty payment"
    r"|bill for your|your (?:\w+ ){0,2}bill (?:is|has|for)|e-?bill\b|bill (?:generated|summary)"
    r"|shared (?:some )?(?:of your )?(?:\w+ ){0,2}account data|access to your (?:\w+ ){0,2}account"
    r"|(?:access|account|api|service|project|subscription)\b.{0,30}?\b(?:is|has been|was) (?:turned off|paused|on hold|deactivated|disabled)"
    r"|account (?:on hold|is on hold)|out of credits|used (?:all|\d{1,3}%) (?:of )?(?:your |available )?credits"
    r"|(?:single-use|access|authentication|auth|temporary|one-time|login) (?:code|token)|your code\b"
    r"|sign[- ]?in to .{1,40}? requested|sign[- ]?in request|accessed from a new|new ip address|incorrect (?:m?pin|password)"
    r"|verify your (?:identity|email(?: address)?)|confirm your email(?: address)?|email (?:id |address )?verification"
    r"|identity (?:has been |was )?verified|(?:verification|kyc) (?:successful|completed|approved|is complete|complete)"
    r"|re-?kyc|kyc (?:due|verification|update)"
    r"|bank (?:account|detail)s?\b.{0,30}?\b(?:updated?|updation|confirmed|removed|added|changed)|removed your bank accounts?"
    r"|credit (?:report|profile|enquiry|inquiry)|credit score (?:is generated|has been generated|through|report)|cibil|enquiry alert|access alert|(?:debit|credit) card\b.{0,25}?\bdispatch"
    r"|dispatch of .{1,30}? card|transaction (?:confirmation|started|reference|completed|receipt)|transactions? in your"
    r"|your (?:\w+ ){0,2}transaction \d{4,}|deposit advice|fixed deposit"
    r"|portfolio (?:performance|statement|summary|valuation|report)|holdings? (?:report|summary)"
    r"|loan (?:summary|statement|interest rate|account (?:no|number)|a/c)|policy (?:copy|document|schedule)"
    r"|square[- ]?off|trading window|f&o ban|margin (?:call|shortfall|penalty)|statement from|insurance proposal"
    r"|application (?:id|number|no\.?)\b|verify your (?:\w+ ){0,2}account|account verification"
    r"|domains? (?:has |have )?(?:expire[sd]?|cancel(?:s|led|ed)?)\b|cancell?ation notice|to resume your"
    r"|subscription (?:has )?(?:ended|expired|cancell?ed)|cancell?ation (?:request )?(?:is )?confirmed"
    r"|will (?:soon )?be deleted|deletion of (?:your )?(?:data|account)"
    r"|delivery status notification|undeliverable|mail delivery (?:failed|subsystem))")
_NOTIFY_LOCAL = re.compile(r"(?i)^(?:notifications?|notify)$")
_THREAD_SUBJECT = re.compile(r"^\s*(?:re:\s*)?\[[^\]\s]+/[^\]\s]+\]|\(#\d+\)|\bPR #\d+\b|\bissue #\d+\b", re.I)


def _suffix_match(domain: str, suffixes) -> str | None:
    d = (domain or "").lower()
    for s in suffixes:
        if d == s or d.endswith("." + s):
            return s
    return None


def signals(info) -> list[tuple[int, str]]:
    """(weight, reason) pairs for one message."""
    out: list[tuple[int, str]] = []
    domain = (info.domain or "").lower()
    local = (info.from_addr or "").split("@")[0]
    subject = info.subject or ""
    gov = _suffix_match(domain, GOV_SUFFIXES)
    if gov:
        out.append((3, f"government sender (.{gov})"))
    m = _TXN_SUBJECT.search(subject)
    if m:
        out.append((3, f"transactional subject ({m.group(0).strip()!r})"))
    base = base_domain(domain)
    if _NOTIFY_LOCAL.match(local) and (base in COLLAB or _THREAD_SUBJECT.search(subject)):
        out.append((3, "activity notification (work/collaboration)"))
    reg = _suffix_match(domain, REGULATED_SUFFIXES)
    if reg:
        out.append((2, f"regulated finance domain (.{reg})"))
    elif base in MARKET_INFRA:
        out.append((2, "stock exchange / depository / registrar"))
    if not m and not PROMO_SUBJECT.search(subject):
        a = _ACCOUNT_SUBJECT.search(subject)
        if a:
            m = a
            out.append((3, f"account notice ({a.group(0).strip()!r})"))
    if not m and _RECEIPT_SUBJECT.search(subject):
        out.append((2, "receipt or confirmation"))
    if _ACCOUNT_LOCAL.match(local):
        out.append((2, f"account mailbox ({local}@)"))
    elif _ALERT_LOCAL.search(local):
        out.append((1, f"alert mailbox ({local}@)"))
    if not info.list_unsubscribe:
        out.append((1, "no unsubscribe header"))
    if "CATEGORY_PROMOTIONS" in (info.labels or set()):
        out.append((-2, "filed under Promotions"))
    return out


def score(info) -> tuple[int, list[str]]:
    sig = signals(info)
    return sum(w for w, _ in sig), [t for w, t in sig if w > 0]


def is_important(info) -> tuple[bool, list[str]]:
    total, reasons = score(info)
    return total >= IMPORTANT_THRESHOLD, reasons


def account_mail_reason(address: str, subjects) -> str | None:
    """Why a sender must never be bulk-suggested for unsubscribing, or None.

    Defence in depth for "not opened in N months" and similar suggestions: a
    sender that ever sent a security, payment, order, statement or other
    account notice (any stored subject), or whose mailbox name only sends
    account mail, is left out even when it is categorised "newsletter"
    (for example a scan made by an older classifier, or a mixed sender whose
    account notice scored below the threshold because of a Promotions label).
    """
    local, _, domain = (address or "").lower().rpartition("@")
    if _suffix_match(domain, GOV_SUFFIXES):
        return "government sender"
    if _ACCOUNT_LOCAL.match(local) or _ALERT_LOCAL.search(local):
        return f"account mailbox ({local}@)"
    for subject in subjects or ():
        subject = subject or ""
        m = _TXN_SUBJECT.search(subject) or _RECEIPT_SUBJECT.search(subject)
        if m is None and not PROMO_SUBJECT.search(subject):
            m = _ACCOUNT_SUBJECT.search(subject)
        if m:
            return f"account notice ({m.group(0).strip()!r})"
    return None
