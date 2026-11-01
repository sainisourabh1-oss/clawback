"""Card-network recurring-charge alerts, any country.

Many card issuers email an alert when a merchant charges a card on file
("A recurring payment of $9.99 to EXAMPLE STREAMING was made on your card
ending 1234"). These often reveal subscriptions whose receipts went to a
different address. Detection needs: a card reference ("card ending"/"xx1234"),
recurring wording, an amount, and a merchant name.
"""
from __future__ import annotations

import re

from ..detect import Hit, cycle_near
from ..money import parse_money
from .base import Region

_CARD = re.compile(r"(?i)\b(?:card|account)\s+(?:ending(?: in| with)?|no\.?|number)\s*[x*•.\s]*\d{4}\b|\b[x*]{2,}\d{4}\b")
_RECUR = re.compile(r"(?i)\b(?:recurring (?:payment|charge|transaction)|subscription (?:charge|payment)|card on file|automatic (?:payment|renewal)|standing order)\b")
_MERCHANT = re.compile(r"(?i)\b(?:to|at|from|merchant[:\s]+)\s+([A-Z0-9][A-Z0-9&'.* -]{2,40}?)(?=\s+(?:on|was|has|for|using|with)\b|[.,;\n])")


def detect(info) -> Hit | None:
    text = f"{info.subject}\n{info.body}"
    if not (_CARD.search(text) and _RECUR.search(text)):
        return None
    money = parse_money(text, info.domain)
    m = _MERCHANT.search(info.body or "")
    if money is None or not m:
        return None
    merchant = " ".join(m.group(1).split()).strip(" *.-")
    if not re.search(r"[A-Za-z]{2}", merchant) or re.match(r"(?i)(?:your|the|my|our|this|a|an)\b", merchant):
        return None
    idx = text.find(money.raw)
    return Hit(vendor=merchant.title(), domain=info.domain, subject=(info.subject or "")[:160], amount=money.raw,
               kind="mandate", confidence=0.75, evidence="card recurring-charge alert", money=money,
               cycle=cycle_near(text, idx, idx + len(money.raw)), recurring=True, merchant=merchant, region="cards")


REGION = Region(code="cards", name="Card networks (any country)", detect=detect,
                description="Issuer alerts for recurring card charges.")
