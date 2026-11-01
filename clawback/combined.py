"""The "All accounts" view: results of every connected account in one list.

Two account labels can be one mailbox: Gmail through the API (label
"default" unless named) and the same Gmail over IMAP (label = the address).
They are grouped as one mailbox when

* the scans recorded the same mailbox address (tracker.account_identity:
  Gmail's users.getProfile, or the IMAP/Outlook sign-in address), or
* they share Gmail message ids (16 hex digits: the API id and IMAP's
  X-GM-MSGID are the same number). Random 64-bit ids do not collide by
  chance, and IMAP ids of other providers ("INBOX:12") are never compared.

Within one mailbox a message is counted once (by its id). Across different
mailboxes the same sender's counts are added, because those are different
emails. Money stays per currency and is never summed across currencies.
Actions are split per account (actions.build_group_plan) and every part runs
through the normal two-step confirmation.
"""
from __future__ import annotations

import json
import re

from . import savings as savings_mod
from . import tracker

ALL = "__all__"
_GMAIL_ID = re.compile(r"^[0-9a-f]{16}$")
_STATUS_RANK = {"cancelled": 0, "confirmed": 1, "loan": 2, "suspected": 3, "dismissed": 4}


def groups(path=None) -> list[list[str]]:
    """Accounts grouped by mailbox, each group sorted, groups in a stable order."""
    accts = [a for a in tracker.accounts(path)]
    ids = tracker.identities(path)
    parent = {a: a for a in accts}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_addr: dict[str, str] = {}
    for a in accts:
        addr = (ids.get(a) or {}).get("address") or (a.lower() if "@" in a else None)
        if addr:
            if addr in by_addr:
                union(a, by_addr[addr])
            else:
                by_addr[addr] = a
    db = tracker.connect(path)
    try:
        pairs = db.execute(
            "SELECT DISTINCT x.account, y.account, x.source_id FROM messages x JOIN messages y "
            "ON x.source_id = y.source_id AND x.account < y.account WHERE length(x.source_id) = 16").fetchall()
    finally:
        db.close()
    for a, b, sid in pairs:
        if a in parent and b in parent and _GMAIL_ID.match(sid):
            union(a, b)
    out: dict[str, list[str]] = {}
    for a in accts:
        out.setdefault(find(a), []).append(a)
    return sorted((sorted(g) for g in out.values()), key=lambda g: g[0])


def mailbox_of(path=None) -> dict[str, list[str]]:
    """account -> the accounts that are the same mailbox (itself included)."""
    return {a: g for g in groups(path) for a in g}


def label(group: list[str], path=None) -> str:
    ids = tracker.identities(path)
    for a in group:
        addr = (ids.get(a) or {}).get("address")
        if addr:
            return addr
    return next((a for a in group if "@" in a), group[0])


def _distinct_counts(accounts: list[str], path=None) -> dict[str, tuple[int, int]]:
    """sender -> (messages, unread), each message id counted once."""
    q = ",".join("?" * len(accounts))
    db = tracker.connect(path)
    try:
        rows = db.execute(
            f"SELECT sender, COUNT(*), SUM(COALESCE(u, 0)) FROM (SELECT sender, source_id, MAX(unread) AS u "
            f"FROM messages WHERE account IN ({q}) GROUP BY sender, source_id) GROUP BY sender", accounts).fetchall()
    finally:
        db.close()
    return {r[0]: (r[1], r[2] or 0) for r in rows}


def senders(category: str | None = None, path=None) -> list[dict]:
    merged: dict[str, dict] = {}
    for g in groups(path):
        rows = [r for a in g for r in tracker.list_senders(a, category, path=path)]
        if not rows:
            continue
        counts = _distinct_counts(g, path) if len(g) > 1 else {}
        by_addr: dict[str, list[dict]] = {}
        for r in rows:
            by_addr.setdefault(r["address"], []).append(r)
        for addr, rs in by_addr.items():
            best = dict(max(rs, key=lambda r: (r["message_count"], r["account"])))
            if addr in counts:
                best["message_count"], best["unread_count"] = counts[addr]
            best["accounts"] = sorted({r["account"] for r in rs})
            best["mailboxes"] = 1
            if addr not in merged:
                merged[addr] = best
                continue
            m = merged[addr]                  # a different mailbox: different emails, add them up
            m["message_count"] += best["message_count"]
            m["unread_count"] += best["unread_count"]
            m["trashed_count"] = (m.get("trashed_count") or 0) + (best.get("trashed_count") or 0)
            m["accounts"] = sorted(set(m["accounts"]) | set(best["accounts"]))
            m["mailboxes"] += 1
            for k in ("first_seen",):
                if best.get(k) and (not m.get(k) or best[k] < m[k]):
                    m[k] = best[k]
            for k in ("last_seen",):
                if best.get(k) and (not m.get(k) or best[k] > m[k]):
                    m[k] = best[k]
    out = list(merged.values())
    out.sort(key=lambda r: (-r["message_count"], r["address"]))
    return out


def subscriptions(path=None) -> list[dict]:
    """One row per subscription per mailbox; the same subscription seen
    through two connections of one mailbox is listed once."""
    out = []
    for g in groups(path):
        seen: dict[tuple, dict] = {}
        for a in g:
            for r in tracker.list_subscriptions(path, a):
                d = dict(r)
                key = (d["domain"], d["currency"], d["identity_key"])
                d["accounts"] = [a]
                old = seen.get(key)
                if old is None:
                    seen[key] = d
                    continue
                old["accounts"] = sorted(set(old["accounts"]) | {a})
                if (_STATUS_RANK.get(d["status"], 9), -d["hits"]) < (_STATUS_RANK.get(old["status"], 9), -old["hits"]):
                    d["accounts"] = old["accounts"]
                    seen[key] = d
        out.extend(seen.values())
    return out


def savings(path=None, now=None, settings=None) -> dict:
    subs = {s["id"] for s in subscriptions(path)}
    keys = set()
    for g in groups(path):
        seen = set()
        for a in g:
            for s in tracker.list_senders(a, path=path):
                if s["address"] not in seen:
                    seen.add(s["address"])
                    keys.add((a, s["address"]))
    return savings_mod.compute(None, now=now, path=path, settings=settings, subscription_ids=subs, sender_keys=keys)


def route(address: str, accounts: list[str], live: dict, capability: str | None = None, path=None) -> list[str]:
    """Which accounts an action on ``address`` goes to: one per mailbox (the
    connected one that can do it, else the first), never twice to one
    mailbox."""
    chosen = []
    for g in groups(path):
        mine = [a for a in g if a in accounts]
        if not mine:
            continue

        def score(a):
            src = live.get(a)
            return (src is None, capability is not None and src is not None and capability not in src.capabilities, a)
        chosen.append(min(mine, key=score))
    return chosen


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)
