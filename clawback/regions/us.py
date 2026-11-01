"""United States: CCPA/CPRA deletion + opt-out letters and cancellation context.

Status of the FTC "click-to-cancel" rule (checked 2026-10-24): the FTC's 2024
amendments to the Negative Option Rule were vacated by the U.S. Court of
Appeals for the Eighth Circuit on 8 July 2025 (Custom Communications, Inc. v.
FTC) before they took effect. The FTC opened a new advance notice of proposed
rulemaking on 11 March 2026 (comments closed 13 April 2026); no new rule has
been adopted. So this tool does NOT cite click-to-cancel as law. The federal
Restore Online Shoppers' Confidence Act (15 U.S.C. 8403) still requires
"simple mechanisms" to stop recurring online charges, and several states have
automatic-renewal laws.
"""
from __future__ import annotations

from .base import PrivacyLaw, Region, RevokeGuide

CCPA = PrivacyLaw(
    code="ccpa", name="California - CCPA as amended by the CPRA", region="us",
    citation="California Civil Code section 1798.105 (right to delete), section 1798.120 (right to opt out of sale or "
             "sharing), section 1798.130 (response within 45 days)",
    deadline="45 calendar days from receipt, extendable once by another 45 days with notice (Civ. Code 1798.130(a)(2)).",
    notes="Applies to California residents and to businesses covered by the CCPA. Others may still honour it voluntarily.",
    template=(
        "To the privacy team of {company},\n\n"
        "I am a California resident. My email address is {email}.\n\n"
        "1. Request to delete: under California Civil Code section 1798.105, please delete the personal information you "
        "have collected about me and direct your service providers and contractors to do the same.\n"
        "2. Request to opt out: under section 1798.120, do not sell or share my personal information.\n"
        "3. Please confirm completion. Under section 1798.130 you must respond within 45 days of receiving this request.\n\n"
        "Regards,\n{name}\n{date}\n"),
)

REVOKE_GUIDES = [
    RevokeGuide(
        "Recurring card charges (US)",
        ["Cancel with the merchant first and keep the confirmation.",
         "If charges continue after cancelling, dispute them with your card issuer and ask about blocking the merchant.",
         "Federal law (ROSCA, 15 U.S.C. 8403) requires sellers of online negative-option plans to provide a simple way to stop recurring charges."],
        source="15 U.S.C. 8403 (ROSCA); general guidance on disputes", checked_on="2026-10-24", verified=False),
]

REGION = Region(code="us", name="United States", revoke_guides=REVOKE_GUIDES, privacy_laws=[CCPA],
                description="CCPA/CPRA deletion and opt-out letters; recurring-charge guidance (no click-to-cancel rule in force).")
