"""Privacy footprint (which companies have your address) and data-deletion
request DRAFTS. Drafting never sends anything; sending goes through the
double-confirmed ``send_request`` action.

NOT LEGAL ADVICE: the templates cite the provisions they rely on, but whether
a law applies to a given company depends on facts this tool cannot know.
"""
from __future__ import annotations

from datetime import date

from . import regions, tracker
from .detect import base_domain, vendor_from_domain

DISCLAIMER = ("Not legal advice. These templates cite the provisions they rely on, but whether a law applies to a "
              "company (and to you) depends on facts this tool cannot check. Find the company's privacy contact in "
              "its privacy notice.")


def footprint(account: str | None = None, path=None) -> list[dict]:
    """Companies (by base domain) that have emailed this account."""
    by: dict[str, dict] = {}
    for s in tracker.list_senders(account, path=path):
        d = base_domain(s["domain"])
        if not d or d == "unknown.invalid":
            continue
        e = by.setdefault(d, {"domain": d, "company": vendor_from_domain(d), "emails": 0, "senders": 0,
                              "first_seen": s["first_seen"], "last_seen": s["last_seen"], "categories": set()})
        e["emails"] += s["message_count"]; e["senders"] += 1; e["categories"].add(s["category"])
        if s["first_seen"] and (not e["first_seen"] or s["first_seen"] < e["first_seen"]):
            e["first_seen"] = s["first_seen"]
        if s["last_seen"] and (not e["last_seen"] or s["last_seen"] > e["last_seen"]):
            e["last_seen"] = s["last_seen"]
    out = []
    for e in by.values():
        e["categories"] = sorted(e["categories"])
        out.append(e)
    return sorted(out, key=lambda e: -e["emails"])


def laws() -> list[dict]:
    return [{"code": law.code, "name": law.name, "region": law.region, "citation": law.citation,
             "deadline": law.deadline, "notes": law.notes} for law in regions.privacy_laws()]


def draft(account: str, domain: str, law_code: str, your_name: str, your_email: str, to_address: str = "",
          company: str | None = None, path=None, today: date | None = None) -> dict:
    law = next((x for x in regions.privacy_laws() if x.code == law_code), None)
    if law is None:
        raise ValueError(f"Unknown law {law_code!r}; choose one of: " + ", ".join(x.code for x in regions.privacy_laws()))
    if not your_email or "@" not in your_email:
        raise ValueError("Enter the email address the company knows you by")
    to_address = (to_address or "").strip()
    if to_address and ("@" not in to_address or " " in to_address):
        raise ValueError("Recipient must be an email address")
    company = (company or vendor_from_domain(domain)).strip()[:80]
    body = law.template.format(company=company, email=your_email.strip(), name=(your_name or "").strip(),
                               date=(today or date.today()).isoformat())
    subject = f"Data deletion request ({law.name.split(' - ')[0]}) - {your_email.strip()}"
    db = tracker.connect(path)
    try:
        cur = db.execute("""INSERT INTO privacy_requests(account, company, domain, law, to_address, subject, body, status, created_at)
                            VALUES(?,?,?,?,?,?,?,?,?)""", (account, company, base_domain(domain), law.code, to_address,
                                                           subject, body, "draft", tracker.now_iso()))
        db.commit()
        rid = cur.lastrowid
    finally:
        db.close()
    return get(rid, path)


def update_recipient(rid: int, to_address: str, path=None) -> dict:
    to_address = to_address.strip()
    if "@" not in to_address or " " in to_address:
        raise ValueError("Recipient must be an email address")
    db = tracker.connect(path)
    try:
        db.execute("UPDATE privacy_requests SET to_address=? WHERE id=? AND status='draft'", (to_address, rid))
        db.commit()
    finally:
        db.close()
    return get(rid, path)


def get(rid: int, path=None) -> dict:
    db = tracker.connect(path)
    try:
        row = db.execute("SELECT * FROM privacy_requests WHERE id=?", (rid,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise ValueError(f"No request {rid}")
    return dict(row)


def requests(account: str | None = None, path=None) -> list[dict]:
    db = tracker.connect(path)
    try:
        q, args = "SELECT * FROM privacy_requests", ()
        if account:
            q += " WHERE account=?"; args = (account,)
        return [dict(r) for r in db.execute(q + " ORDER BY id DESC", args)]
    finally:
        db.close()
