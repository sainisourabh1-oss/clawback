"""Accounts saved on this computer, offered again on the next start.

Two places know about an account: a sign-in token in the data folder
(``tokens/<label>.json`` and the v1 ``token.json`` for the Gmail API,
``tokens/<label>.imap.json`` for Gmail over IMAP with Google sign-in,
``tokens/outlook-<address>.json`` for Microsoft) and saved scan results in
the database. An account with a token reconnects in one click with that
token; an account with results only (an app-password IMAP account - its
password is never saved - or a file import) opens its saved results, and
scanning it again needs the password or a newer export.

Nothing here reads a token's contents: only file names are listed.
"""
from __future__ import annotations

import re

from . import config, ms_oauth, tracker

KIND_LABEL = {"gmail": "Gmail (Google sign-in)", "gmail_imap": "Gmail (fast scan, Google sign-in)",
              "outlook": "Outlook / Microsoft 365", "imap": "Email over IMAP (app password)",
              "mbox": "Imported file", "demo": "Demo mailbox", "unknown": "Saved results"}


def _token_accounts() -> dict[str, dict]:
    out: dict[str, dict] = {}
    legacy = config.home() / "token.json"
    if legacy.is_file():
        out["default"] = {"kind": "gmail", "reconnect": {"kind": "gmail", "account": "default"}}
    folder = config.home() / "tokens"
    for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        name = f.stem
        if name.startswith(PENDING_PREFIX):
            continue                      # a sign-in still in progress
        if name.startswith("outlook-"):
            acct = name[len("outlook-"):]
            # the Microsoft app registration is saved apart from the token
            out[acct] = {"kind": "outlook",
                         "reconnect": {"kind": "outlook", "user": acct} if ms_oauth.load_client() else None}
        elif name.endswith(".imap"):
            acct = name[:-len(".imap")]
            # the full-access warning was accepted when this token was made
            out.setdefault(acct, {"kind": "gmail_imap", "reconnect": {"kind": "gmail_imap", "user": acct,
                                                                      "xoauth2": True, "accept_full_access": True}})
        else:
            out[name] = {"kind": "gmail", "reconnect": {"kind": "gmail", "account": name}}
    return out


def saved_accounts(allow_demo: bool = True) -> list[dict]:
    """Every account this computer knows, newest scan first:
    {account, kind, kind_label, address, reconnect (connect body or None),
    last_scan ({finished, messages, senders} or None)}."""
    found = _token_accounts()
    ids = tracker.identities()
    for acct in tracker.scanned_accounts():
        if acct not in found:
            kind = (ids.get(acct) or {}).get("kind") or "unknown"
            reconnect = {"kind": "demo"} if kind == "demo" and allow_demo else None
            found[acct] = {"kind": kind if kind in KIND_LABEL else "unknown", "reconnect": reconnect}
    rows = []
    for acct, d in found.items():
        last = tracker.last_scan(acct)
        rows.append({"account": acct, "kind": d["kind"], "kind_label": KIND_LABEL.get(d["kind"], KIND_LABEL["unknown"]),
                     "address": (ids.get(acct) or {}).get("address"), "reconnect": d["reconnect"], "last_scan": last})
    rows.sort(key=lambda r: ((r["last_scan"] or {}).get("finished") or "", r["account"]), reverse=True)
    return rows


# ------------------------------------------------------------------ names
# Inside, every account has a label: the address for IMAP/Outlook, a word
# such as "personal" or "default" for a Gmail sign-in made before 2.0.0,
# "import:<file>" for a file, "demo" for the demo. People see the mailbox's
# email address instead wherever it is known (Gmail: users.getProfile, kept
# in tracker.account_identity), else a sensible name. Labels and token files
# are never renamed: old databases and sign-ins keep working as they are.
DEMO_NAME = "Demo mailbox"
_UPLOAD_PREFIX = re.compile(r"^\d{9,}-")      # web uploads are saved as <unix time>-<file name>


def _plain_name(account: str, ident: dict | None) -> str:
    address = (ident or {}).get("address")
    if address:
        return address
    if "@" in account:
        return account.lower()
    if account.startswith("import:"):
        return _UPLOAD_PREFIX.sub("", account[len("import:"):]) or account
    if (ident or {}).get("kind") == "demo" or account == "demo":
        return DEMO_NAME if account == "demo" else f"{DEMO_NAME} ({account})"
    return account


def _weight(account: str) -> tuple:
    """Which of several labels of one mailbox is the main one: the one with
    the most saved emails (the real history), then the latest scan."""
    last = tracker.last_scan(account) or {}
    return (last.get("messages") or 0, last.get("finished") or "", account)


def display_names(accounts) -> dict[str, str]:
    """label -> what to show. Two labels of one mailbox (the same address
    signed in twice before 2.0.0 reused accounts) both stay reachable: the
    main one shows the plain address, the other "address (label)"."""
    ids = tracker.identities()
    accounts = sorted(set(accounts))
    plain = {a: _plain_name(a, ids.get(a)) for a in accounts}
    groups: dict[str, list[str]] = {}
    for a, n in plain.items():
        groups.setdefault(n.lower(), []).append(a)
    out = dict(plain)
    for labels in groups.values():
        if len(labels) > 1:
            main = max(labels, key=_weight)
            for a in labels:
                if a != main:
                    out[a] = f"{plain[a]} ({a})"
    return out


def label_for_address(address: str) -> str | None:
    """The existing account of this mailbox address, if any: a signed-in
    address must reopen its account (data, sign-in, history) instead of
    starting a new one. Several labels of one mailbox: the main one."""
    address = (address or "").strip().lower()
    if not address:
        return None
    ids = tracker.identities()
    known = set(tracker.scanned_accounts()) | set(_token_accounts()) | set(ids)
    same = [a for a in known if (ids.get(a) or {}).get("address") == address or a.lower() == address]
    if not same:
        return None
    gmail = [a for a in same if _token_accounts().get(a, {}).get("kind") == "gmail"
             or (ids.get(a) or {}).get("kind") == "gmail"]
    return max(gmail or same, key=_weight)


def account_label(address: str) -> str:
    """Label for a connection made with an email address (IMAP, Gmail fast
    scan): the existing account of that mailbox, else the address itself
    (lower case, so "Me@Mail.example" and "me@mail.example" are one account)."""
    return label_for_address(address) or address.strip().lower()


PENDING_PREFIX = ".signin-"


def pending_token_path():
    """Where a new Google sign-in writes its token until the address is
    known (a dot name, so the saved-accounts list never shows it)."""
    import secrets
    return config.home() / "tokens" / f"{PENDING_PREFIX}{secrets.token_hex(6)}.json"


def adopt_gmail_signin(src) -> str:
    """After a new Google sign-in (token at a pending path): ask Google which
    address it is (users.getProfile, read-only), give the source the label of
    that mailbox's existing account (or the address itself for a new one),
    move the fresh token there and remember the address. Returns the label.

    Moving the token over an older token of the same mailbox is safe: the
    sign-in asked Google to include every scope granted before
    (include_granted_scopes), and the old one is never needed again."""
    import os
    from .sources.base import SourceError
    pending = src.token_file
    try:
        address = (src.mailbox_address() or "").strip().lower()
    except Exception as exc:  # noqa: BLE001 - reported to the user below
        address, why = "", f" Technical details: {type(exc).__name__}: {exc}"
    else:
        why = ""
    if "@" not in address:
        try:
            pending.unlink()
        except OSError:
            pass
        raise SourceError("You signed in, but Google did not say which Gmail address it was, so nothing was "
                          "saved. Try signing in again." + why)
    label = label_for_address(address) or address
    target = config.token_path(label)
    if pending.exists() and pending != target:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(pending, target)
        config._private(target)
    src.account, src.token_file = label, target
    tracker.record_identity(label, "gmail", address)
    return label
