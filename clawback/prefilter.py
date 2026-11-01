"""Which messages need their full body?

Scans read headers (plus Gmail's short snippet) for every message, and the
full body only for billing candidates, because a body costs three times as
much Gmail quota as headers (see sources/pacing.py) and most mail is decided
by headers alone: sender grouping, newsletter/marketing, important mail and
spam signals all use headers only.

A message is a candidate when its subject (or, for non-bulk mail, its
snippet) has any billing/charge/mandate word, or a regional module's mandate
cue. The word list is deliberately wider than the classifier's own hints
(``detect.SUBJECT_HINTS``): a candidate that turns out not to be a charge
costs one extra fetch, while a missed candidate is a subscription you never
see. Bulk mail (List-Unsubscribe etc.) needs a billing word in the SUBJECT,
matching the classifier's own rule that bulk mail needs a billing subject.
"""
from __future__ import annotations

import re

from .detect import SUBJECT_HINTS

_WIDE = re.compile(
    r"(?i)\b(?:receipts?|invoices?|orders?|payments?|paid|charged?|charges|billing|billed|bill|subscriptions?|subscribed|"
    r"renew\w*|trials?|membership|autopay|auto-?pay|auto-?debit|e-?mandate|mandates?|pre-?debit|standing instructions?|"
    r"direct debit|sepa|e-?nach|recurring|debited|purchase[ds]?|plan|upgrade[ds]?|downgrade[ds]?|price|pricing|"
    r"tax invoice)\b")
_HINTS = [re.compile(p, re.I) for p in SUBJECT_HINTS]
# The single words of _WIDE and SUBJECT_HINTS as Gmail search terms (Gmail
# matches whole words, so plurals and verb forms are listed). Only used to
# COUNT likely body fetches for the Gmail API time estimate, never to decide.
SUBJECT_SEARCH_WORDS = (
    "receipt", "receipts", "invoice", "invoices", "order", "orders", "payment", "payments", "paid", "charge",
    "charged", "charges", "billing", "billed", "bill", "subscription", "subscriptions", "subscribed", "renew",
    "renews", "renewal", "renewed", "renewing", "trial", "trials", "membership", "autopay", "mandate", "mandates",
    "emandate", "debit", "debited", "predebit", "sepa", "recurring", "purchase", "purchased", "purchases", "plan",
    "upgrade", "upgraded", "upgrades", "downgrade", "downgraded", "downgrades", "price", "pricing",
)


def needs_body(info) -> bool:
    subject = info.subject or ""
    if any(p.search(subject) for p in _HINTS) or _WIDE.search(subject):
        return True
    if info.is_bulk:
        return False
    return bool(_WIDE.search(info.body or ""))  # body here is only the snippet
