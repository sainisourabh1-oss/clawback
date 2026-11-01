"""EU / UK: SEPA Direct Debit pre-notifications, UK Direct Debit notices, and
GDPR / UK GDPR erasure + marketing-objection letters."""
from __future__ import annotations

import re

from ..detect import Hit, cycle_near, find_dated, vendor_from_domain
from ..money import parse_money
from .base import PrivacyLaw, Region, RevokeGuide

_SEPA = re.compile(
    r"(?i)\b(?:sepa(?:[- ]core)?(?: direct debit| lastschrift)?|lastschrift(?:mandat)?|mandatsreferenz|gl(?:ä|ae)ubiger-?(?:identifikationsnummer|id)|"
    r"pr[ée]l[èe]vement sepa|r[ée]f[ée]rence unique (?:de|du) mandat|\bRUM\b|domiciliation|addebito diretto|adeudo directo|incasso)\b")
_UK_DD = re.compile(r"(?i)\b(?:direct debit (?:instruction|guarantee|confirmation|mandate|notice|advance notice)|service user number|\bSUN\b|DDI)\b")
_PRENOTE = re.compile(r"(?i)\b(?:pre-?notification|advance notice|we will collect|will be (?:collected|debited|taken)|vorabank(?:ü|ue)ndigung|wird .* abgebucht)\b")
_CREDITOR = re.compile(r"(?im)\b(?:creditor(?: name)?|gl(?:ä|ae)ubiger(?:name)?|cr[ée]ancier|payee)\s*[:\-]\s*([A-Za-z0-9&'.+ -]{2,40}?)\s*$")
_NEXT = r"\b(?:on|am|le|collect(?:ed|ion)? on|debit(?:ed)? on|abgebucht am|due on)\b"


def detect(info) -> Hit | None:
    text = f"{info.subject}\n{info.body}"
    sepa, uk = _SEPA.search(text), _UK_DD.search(text)
    if not (sepa or uk):
        return None
    money = parse_money(text, info.domain, "EU" if sepa else "GB")
    if money is None or not (_PRENOTE.search(text) or re.search(r"(?i)\bmandate\b|\bmandat\b", text)):
        return None
    creditor = _CREDITOR.search(text)
    idx = text.find(money.raw)
    return Hit(
        vendor=(creditor.group(1).strip() if creditor else vendor_from_domain(info.domain)), domain=info.domain,
        subject=(info.subject or "")[:160], amount=money.raw, kind="mandate", confidence=0.8,
        evidence="SEPA direct debit notice" if sepa else "UK Direct Debit notice",
        cycle=cycle_near(text, idx, idx + len(money.raw)), money=money,
        renewal_date=find_dated(info.body or "", _NEXT), recurring=True,
        merchant=creditor.group(1).strip() if creditor else "", region="eu",
    )


REVOKE_GUIDES = [
    RevokeGuide(
        "SEPA Direct Debit (EU/EEA)",
        ["Cancel the contract with the company, and tell your bank to stop collections for that mandate reference "
         "(most banking apps list direct-debit mandates).",
         "For an authorised SEPA Core direct debit you can ask your bank for a refund within eight weeks of the debit "
         "(PSD2, Directive (EU) 2015/2366, Article 76).",
         "For a debit you never authorised, tell your bank without undue delay, at the latest within 13 months (PSD2 Article 71)."],
        source="Directive (EU) 2015/2366 (PSD2), Articles 71 and 76", checked_on="2026-10-24", verified=True),
    RevokeGuide(
        "UK Direct Debit",
        ["Cancel the Direct Debit in your banking app or by contacting your bank or building society, and tell the company.",
         "The Direct Debit Guarantee entitles you to a full and immediate refund from your bank if an error is made in a payment."],
        source="general guidance based on the Direct Debit Guarantee wording printed on UK Direct Debit notices", verified=False),
]

_GDPR_TEMPLATE = (
    "To the Data Protection Officer of {company},\n\n"
    "I am writing about the personal data you hold about me, linked to the email address {email}.\n\n"
    "1. Objection to direct marketing: under Article 21(2) and (3) of the {law}, please stop processing my personal "
    "data for direct marketing, including profiling related to it.\n"
    "2. Erasure: under Article 17(1) of the {law}, please erase my personal data without undue delay, where no "
    "exemption in Article 17(3) applies.\n"
    "3. Please confirm what you have done, and tell me about any data you keep and why (Article 12(4) if you refuse).\n\n"
    "Under Article 12(3) you must respond without undue delay and in any event within one month of receiving this "
    "request (extendable by two further months where necessary, in which case please tell me why).\n\n"
    "Regards,\n{name}\n{date}\n")

GDPR = PrivacyLaw(
    code="gdpr", name="EU - General Data Protection Regulation", region="eu",
    citation="Regulation (EU) 2016/679 (GDPR): Article 17 (right to erasure), Article 21(2)-(3) (objection to direct "
             "marketing), Article 12(3)-(4) (response time and refusals)",
    deadline="Without undue delay and within one month; extendable by two further months (Art. 12(3)).",
    notes="If the company does not respond you can complain to your national data protection authority (Art. 77).",
    template=_GDPR_TEMPLATE.replace("{law}", "General Data Protection Regulation (EU) 2016/679"))
UK_GDPR = PrivacyLaw(
    code="uk-gdpr", name="UK - UK GDPR and Data Protection Act 2018", region="eu",
    citation="UK GDPR (retained Regulation (EU) 2016/679 as amended), Article 17 and Article 21(2)-(3), Article 12(3)",
    deadline="Without undue delay and within one month; extendable by two further months (Art. 12(3)).",
    notes="If the company does not respond you can complain to the Information Commissioner's Office (ICO).",
    template=_GDPR_TEMPLATE.replace("{law}", "UK GDPR"))

REGION = Region(code="eu", name="EU / UK", detect=detect, revoke_guides=REVOKE_GUIDES, privacy_laws=[GDPR, UK_GDPR],
                description="SEPA and UK Direct Debit notices; GDPR / UK GDPR erasure and marketing-objection letters.")
