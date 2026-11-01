"""Every outgoing or mailbox-changing action goes through this module.

HARD RULE: nothing is unsubscribed, sent, deleted, archived, filtered, reported
or opened without TWO separate user steps:

1. ``build_plan`` - the user picked items and an action (click 1). This only
   produces a plan: per sender the exact method, the host or address that
   will be contacted, and how many emails are affected. Nothing is contacted.
2. ``confirm`` - a SEPARATE call (click 2, on a button that names the action
   and the count, e.g. "Yes, delete 37 emails") that must quote the plan's
   id, its digest (a hash of exactly what was shown) and the number of
   senders the user saw on that button (``count``). Nothing is typed; a
   stale or different count is refused. Only ``confirm`` can create the
   private ``_Confirmed`` token that ``_execute`` requires.

There is no "yes to all", no default-yes, no auto mode and no bypass flag.
Plans expire after ``PLAN_TTL`` and can be confirmed once.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from . import links as links_mod
from . import oneclick, tracker
from .detect import base_domain
from .senders import best_method, check_link, host_of, mailto_parts, suspicious_host
from .sources.base import SourceError

PLAN_TTL = timedelta(minutes=15)

ACTIONS = {
    # action: (verb for logs and the CLI question, needs mailbox capability)
    "unsubscribe": ("UNSUBSCRIBE", None),
    # Unsubscribe and move every email from the sender to Trash, in one go.
    # A sender with no way to unsubscribe (or a spam-like one) is only deleted.
    "unsubscribe_trash": ("UNSUBSCRIBE+DELETE", "trash"),
    "archive": ("ARCHIVE", "archive"),
    "trash": ("TRASH", "trash"),
    "block": ("BLOCK", "block"),
    "report_spam": ("REPORT", "spam"),
    "escalate": ("ESCALATE", "block"),
    "open_cancel": ("OPEN", None),
    "send_request": ("SEND", "send"),
    "undo": ("UNDO", None),
}


class PlanError(Exception):
    """The plan cannot be built or confirmed; message is user-facing."""


@dataclass
class Step:
    target: str
    label: str
    method: str
    contact: str = ""          # host for HTTPS, address for mailto, "" for mailbox-only
    url: str = ""
    emails_affected: int = 0
    effects: list[str] = field(default_factory=list)   # plain language: what will happen, and who is contacted
    details: list[str] = field(default_factory=list)   # the technical detail behind each effect (protocol, full URL)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # informational, e.g. "handled by Brevo"
    blocked: str = ""        # non-empty = this step will NOT run, with the reason
    permission: str = ""       # missing permission description, if any
    data: dict = field(default_factory=dict)
    # blocked until the user ticks a box: "sender" (unknown link domain) or
    # "important" (looks like account mail). A field, so the UI receives it.
    ack: str = ""


@dataclass
class Plan:
    id: str
    account: str
    action: str
    created_at: str
    expires_at: str
    count: int                 # senders/items that will run: what the confirm button names
    digest: str
    steps: list[Step]
    options: dict
    dry_run: bool = False

    @property
    def runnable(self) -> list[Step]:
        return [s for s in self.steps if not s.blocked]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["runnable"] = len(self.runnable)
        d["emails"] = sum(s.emails_affected for s in self.runnable)
        d["permissions"] = sorted({s.permission for s in self.runnable if s.permission})
        return d


# Undo exists only for actions Clawback can reverse itself.
REVERSIBLE_OPS = {"untrash", "unarchive", "unreport_spam", "delete_filter"}


class _Confirmed:
    """Proof that ``confirm`` validated a plan. Only ``confirm`` can make one."""
    __slots__ = ("plan",)
    _key = object()

    def __init__(self, plan: Plan, key):
        if key is not _Confirmed._key:
            raise PermissionError("Actions can only run through confirm()")
        self.plan = plan


def _digest(account, action, steps, options, dry_run) -> str:
    canon = json.dumps({"account": account, "action": action, "dry_run": dry_run, "options": options,
                        "steps": [asdict(s) for s in steps]}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _sender_step(source, account: str, s: dict, action: str, options: dict, path) -> Step:
    address = s["address"]
    count = s["message_count"]
    step = Step(target=address, label=s.get("display_name") or address, method=action, emails_affected=count)
    if s.get("kept"):
        step.blocked = "on your keep-list (remove it from the keep-list first)"
        return step
    important = s.get("important") or []
    if important:
        why = "; ".join(important[:3])
        if address not in set(options.get("acknowledged_important", [])):
            step.blocked = (f"important sender ({why}). Clawback keeps bank and card alerts, statements, security, "
                            "government and work mail by default: acting on it could hide alerts you need. "
                            "Tick 'I understand, include it anyway' to include it.")
            step.ack = "important"
            return step
        step.warnings.append(f"important sender ({why}): you chose to include it anyway")
    if action == "unsubscribe_trash":
        return _unsubscribe_trash_step(source, account, s, options, path, step)
    if action == "unsubscribe":
        if s["category"] == "spam":
            step.blocked = ("looks like spam: clicking its unsubscribe link would confirm your address is "
                            "read. Use Block or Report spam instead.")
            return step
        m = best_method(type("S", (), {"list_unsubscribe": s["unsubscribe"], "one_click": bool(s["one_click"]),
                                     "body_unsub": s.get("body_unsub") or ""}))
        if m is None:
            guide = links_mod.email_settings_for(s["domain"])
            if guide:
                step.blocked = (f"its emails have no unsubscribe header. Turn them off in {guide['service']}'s settings "
                                f"({guide['help']}), or use Block to filter them out of your inbox.")
                step.data["guide"] = guide
            else:
                step.blocked = ("its emails have no unsubscribe header, so there is nothing to click. Use Block to "
                                "filter them out of your inbox (undo available).")
            step.ack = ""
            return step
        method, uri = m
        step.method, step.url = method, uri
        signed = set(s.get("signed") or [])
        verdict = check_link(s["domain"], uri, signed)
        if verdict and verdict[0] == "unsafe":
            step.blocked = verdict[1] + " - not contacted. Use Block instead."
            return step
        if verdict and verdict[0] == "info":
            step.notes.append(verdict[1])
        if verdict and verdict[0] == "warn":
            step.warnings.append(verdict[1] + " (possible phishing)")
            if address not in set(options.get("acknowledged", [])):
                step.blocked = (verdict[1] + ". Tick 'I checked this sender' to include it.")
                step.ack = "sender"
                return step
        if method == "one-click":
            step.contact = host_of(uri)
            step.effects.append(f"Ask {step.label} to unsubscribe you automatically (one-click), contacting {step.contact}")
            step.details.append(f"HTTPS POST with the body List-Unsubscribe=One-Click (RFC 8058) to {uri}")
        elif method == "mailto":
            to, subject, _ = mailto_parts(uri)
            step.contact = to
            if "mailto" in source.capabilities:
                step.permission = source.missing_permission("mailto") or ""
                step.effects.append(f"Send an unsubscribe email from your account to {to}")
                step.details.append(f"Email to {to}, subject {subject!r}, from the mailto: link {uri}")
            else:
                step.method = "mailto-client"
                step.effects.append(f"Open your mail app with an unsubscribe email to {to}; you press Send")
                step.details.append(f"Opens the mailto: link {uri}")
        elif method == "body-link":
            # Found in the email body, not in a header: Clawback never fetches
            # it; the browser opens it only after this plan is confirmed.
            step.contact = host_of(uri)
            step.effects.append(f"Open the unsubscribe link from {step.label}'s email ({step.contact}) in your browser; "
                                "you finish there")
            step.details.append(f"Link found in the newest email that was read (no List-Unsubscribe header): {uri}")
        else:
            step.contact = host_of(uri)
            step.effects.append(f"Open {step.label}'s unsubscribe page on {step.contact} in your browser; you finish there")
            step.details.append(f"Web page: {uri}")
        for extra in ("archive", "trash"):
            if options.get(f"also_{extra}"):
                if extra not in source.capabilities:
                    step.warnings.append(f"{source.kind} cannot {extra} mail; skipped")
                else:
                    step.permission = step.permission or (source.missing_permission(extra) or "")
                    step.effects.append(f"{extra.title()} {count} existing email(s) from {address}")
                    step.details.append("Gmail: " + ("removes the Inbox label from those emails; undo available" if extra == "archive"
                                                     else "moves those emails to Trash, which Gmail empties after 30 days; undo available"))
        if options.get("also_block"):
            if "block" not in source.capabilities:
                step.warnings.append(f"{source.kind} cannot create filters; skipped")
            else:
                step.permission = step.permission or (source.missing_permission("block") or "")
                step.effects.append(f"Keep future mail from {address} out of your inbox")
                step.details.append(f"Gmail: creates a filter (from:{address}, skip the Inbox); undo deletes it")
        return step
    cap = ACTIONS[action][1]
    if cap not in source.capabilities:
        step.blocked = _not_possible(source, cap)
        return step
    step.permission = source.missing_permission(cap) or ""
    if action == "archive":
        step.effects.append(f"Archive {count} email(s) from {address}: they leave your Inbox but stay in your mailbox (undo available)")
        step.details.append("Gmail: removes the Inbox label from those emails")
    elif action == "trash":
        step.effects.append(f"Move {count} email(s) from {address} to Trash (restorable for 30 days; undo available)")
        step.details.append("Gmail: moves those emails to Trash; Gmail deletes Trash after 30 days")
    elif action == "block":
        mode = options.get("block_mode", "archive")
        if mode not in {"archive", "trash"}:
            raise PlanError("block_mode must be archive or trash")
        step.effects.append(f"From now on, mail from {address} is {'archived' if mode == 'archive' else 'moved to Trash'} automatically")
        step.details.append(f"Gmail: creates a filter (from:{address}, {'skip the Inbox' if mode == 'archive' else 'delete it'}); undo deletes the filter")
    elif action == "report_spam":
        step.effects.append(f"Report {count} email(s) from {address} as spam (moves them to Spam; undo available)")
        step.details.append("Gmail: adds the Spam label to those emails")
    elif action == "escalate":
        step.permission = step.permission or (source.missing_permission("spam") or "")
        step.effects.append(f"From now on, mail from {address} goes to Trash automatically")
        step.effects.append(f"Report {count} email(s) from {address} as spam")
        step.details.append(f"Gmail: creates a filter (from:{address}, delete it) and adds the Spam label to those emails")
    return step


def _not_possible(source, cap: str) -> str:
    """Why a mailbox action cannot run through this connection, in words."""
    if source.kind in {"mbox", "offline", "saved"}:
        return ("this mailbox was imported from a file (or opened from saved results), so Clawback cannot change "
                "the mail in it. Connect the account with Google or Microsoft sign-in to delete or move mail")
    if source.kind == "imap":
        return ("this connection (IMAP with an app password) can unsubscribe but not move mail. Connect with Google "
                "or Microsoft sign-in to delete or move mail")
    return f"not possible with a {source.kind} connection"


def _unsubscribe_trash_step(source, account: str, s: dict, options: dict, path, base: Step) -> Step:
    """Unsubscribe and move every email from the sender to Trash. A sender
    that cannot (or should not) be unsubscribed from - no unsubscribe link,
    spam-like, unsafe link - is only deleted, and the step says so."""
    address, count = s["address"], s["message_count"]
    if "trash" not in source.capabilities:
        base.blocked = _not_possible(source, "trash")
        return base
    opts = {k: v for k, v in options.items() if not k.startswith("also_")}
    opts["also_trash"] = True
    step = _sender_step(source, account, s, "unsubscribe", opts, path)
    if not step.blocked or step.ack:
        step.data["also_trash"] = True
        return step
    # Unsubscribing is not possible: delete only, and say why in plain words.
    only = Step(target=address, label=base.label, method="trash", emails_affected=count, warnings=list(base.warnings))
    only.permission = source.missing_permission("trash") or ""
    if s["category"] == "spam":
        only.notes.append("This sender looks like spam, so Clawback does not unsubscribe (that would tell the "
                          "spammer your address is read); its emails are only deleted.")
    else:
        only.notes.append(f"There is no safe way to unsubscribe from {base.label}, so its emails are only deleted. "
                          "Block keeps future mail out of your inbox.")
    only.effects.append(f"Move {count} email(s) from {address} to Trash (restorable for 30 days; undo available)")
    only.details.append("Unsubscribe not possible: " + step.blocked)
    only.data["trash_only"] = True
    return only


def build_plan(source, action: str, targets: list, options: dict | None = None, dry_run: bool = False,
               path=None, now: datetime | None = None) -> Plan:
    """Step 1 of 2. Describe exactly what would happen; contact nothing."""
    if action not in ACTIONS:
        raise PlanError(f"Unknown action {action!r}")
    options = dict(options or {})
    if action == "unsubscribe_trash":   # it already deletes; extra "also_" options would act twice
        options = {k: v for k, v in options.items() if not k.startswith("also_")}
    targets = list(dict.fromkeys(str(t) for t in targets if str(t).strip()))
    if not targets:
        raise PlanError("Select at least one item first")
    account = source.account
    steps: list[Step] = []
    if action in {"unsubscribe", "unsubscribe_trash", "archive", "trash", "block", "report_spam", "escalate"}:
        senders = {s["address"]: s for s in tracker.list_senders(account, path=path)}
        expanded: list[str] = []
        for t in targets:
            if t in senders:
                expanded.append(t)
                continue
            # A company ("pinterest.com") means every address of that organisation
            # (discover@, explore@, inspire@ ...), each still its own step.
            org = [a for a, s in senders.items() if "@" not in t and base_domain(s["domain"]) == base_domain(t.lower())]
            if not org:
                raise PlanError(f"Unknown sender {t!r} (scan first)")
            expanded.extend(sorted(org))
        for t in dict.fromkeys(expanded):
            steps.append(_sender_step(source, account, senders[t], action, options, path))
    elif action == "open_cancel":
        links_mod.load_user_db()
        for t in targets:
            sub = tracker.get_subscription(int(t), path)
            if sub is None or sub["account"] != account:
                raise PlanError(f"Unknown subscription {t!r}")
            url = links_mod.find_cancel_link(sub["domain"], sub["vendor"], sub["merchant"])
            step = Step(target=str(sub["id"]), label=sub["vendor"], method="open-page")
            if url:
                step.url, step.contact = url, host_of(url)
                step.effects.append(f"Open {step.label}'s cancellation page on {step.contact} in your browser so you can cancel there")
                step.details.append(f"Web page: {url}")
            else:
                step.blocked = "no verified cancellation page known; check the provider's account settings"
            steps.append(step)
    elif action == "send_request":
        for t in targets:
            req = _privacy_request(int(t), account, path)
            step = Step(target=str(req["id"]), label=req["company"], method="email", contact=req["to_address"])
            if not req["to_address"] or "@" not in req["to_address"]:
                step.blocked = "no recipient address: enter the company's privacy contact first"
            elif req["status"] != "draft":
                step.blocked = "already sent"
            elif "send" not in source.capabilities:
                step.method = "mailto-client"
                step.effects.append(f"Open your mail app with this request to {req['to_address']}; you press Send")
            else:
                step.permission = source.missing_permission("send") or ""
                step.effects.append(f"Send your {req['law'].upper()} request to {req['to_address']} from your account")
            steps.append(step)
    elif action == "undo":
        for t in targets:
            entry = tracker.audit_entry(int(t), path)
            step = Step(target=str(t), label=f"#{t} {entry['action'] if entry else '?'}", method="undo")
            if entry is None or entry["account"] != account:
                raise PlanError(f"Unknown log entry {t!r}")
            if not entry["undo"] or entry["undone_at"] or not entry["ok"]:
                step.blocked = "nothing to undo for this entry"
            else:
                op = entry["undo"].get("op")
                step.effects.append({"untrash": "Restore emails from Trash", "unarchive": "Move emails back to the Inbox",
                                     "unreport_spam": "Move emails out of Spam", "delete_filter": "Delete the filter Clawback created",
                                     "resubscribe_note": "Mark the sender active again (re-subscribing is up to you)"}.get(op, op))
                step.data = {"entry": entry["id"]}
            steps.append(step)
    runnable = [s for s in steps if not s.blocked]
    created = now or datetime.now(timezone.utc)
    plan = Plan(id=secrets.token_urlsafe(18), account=account, action=action,
                created_at=created.isoformat(timespec="seconds"), expires_at=(created + PLAN_TTL).isoformat(timespec="seconds"),
                count=len(runnable), digest=_digest(account, action, steps, options, dry_run), steps=steps, options=options,
                dry_run=dry_run)
    db = tracker.connect(path)
    try:
        # (the plans table's "phrase" column now keeps the confirmed count)
        db.execute("INSERT INTO plans(id, account, action, created_at, expires_at, digest, phrase, status, payload) VALUES(?,?,?,?,?,?,?,?,?)",
                   (plan.id, account, action, plan.created_at, plan.expires_at, plan.digest, str(plan.count), "pending",
                    json.dumps(plan.to_dict())))
        db.commit()
    finally:
        db.close()
    return plan


def _load_plan(plan_id: str, path=None) -> tuple[Plan, str]:
    db = tracker.connect(path)
    try:
        row = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise PlanError("Unknown plan: review the selection again")
    d = json.loads(row["payload"])
    steps = [Step(**s) for s in d["steps"]]
    # a plan stored by an older version (typed phrase) has no count: it can only expire
    count = d["count"] if "count" in d else -1
    plan = Plan(d["id"], d["account"], d["action"], d["created_at"], d["expires_at"], count, d["digest"],
                steps, d["options"], d.get("dry_run", False))
    return plan, row["status"]


def get_plan(plan_id: str, path=None) -> dict:
    row = _group_row(plan_id, path)
    if row is not None:
        d = json.loads(row["payload"])
        d["status"] = row["status"]
        return d
    plan, status = _load_plan(plan_id, path)
    d = plan.to_dict()
    d["status"] = status
    return d


def cancel_plan(plan_id: str, path=None) -> None:
    row = _group_row(plan_id, path)
    ids = [plan_id] + ([g["plan_id"] for g in json.loads(row["payload"])["group"]] if row is not None else [])
    db = tracker.connect(path)
    try:
        for pid in ids:
            db.execute("UPDATE plans SET status='cancelled' WHERE id=? AND status='pending'", (pid,))
        db.commit()
    finally:
        db.close()


def same_count(given, expected: int) -> bool:
    """The count quoted by the confirm click equals the plan's. A bool or an
    empty value is never a count (True would otherwise equal 1)."""
    if isinstance(given, bool) or given is None:
        return False
    try:
        return int(str(given).strip()) == expected and expected >= 0
    except ValueError:
        return False


def confirm(source, plan_id: str, count, digest: str, path=None, now: datetime | None = None,
            http_post=None, opener=None) -> dict:
    """Step 2 of 2. Validate the confirmed count and the digest, then run the plan once."""
    plan, status = _load_plan(plan_id, path)
    now = now or datetime.now(timezone.utc)
    if status != "pending":
        raise PlanError(f"This plan was already {status}; review again to act")
    if now > datetime.fromisoformat(plan.expires_at):
        raise PlanError("This review expired; review the selection again")
    if source.account != plan.account:
        raise PlanError("This plan belongs to a different account")
    if not digest or not secrets.compare_digest(str(digest), plan.digest):
        raise PlanError("The confirmation does not match the reviewed plan; review again")
    if _digest(plan.account, plan.action, plan.steps, plan.options, plan.dry_run) != plan.digest:
        raise PlanError("Stored plan was modified; review again")
    if not same_count(count, plan.count):
        raise PlanError("This confirmation does not match what was shown; review again")
    if not plan.runnable:
        raise PlanError("Nothing in this plan can run")
    missing = sorted({s.permission for s in plan.runnable if s.permission} - {""})
    if missing and not plan.dry_run:
        raise PlanError("Grant the missing permission first: " + "; ".join(missing))
    db = tracker.connect(path)
    try:
        # Atomic: a plan runs at most once, even if two confirms race.
        cur = db.execute("UPDATE plans SET status='executing' WHERE id=? AND status='pending'", (plan_id,))
        db.commit()
    finally:
        db.close()
    if cur.rowcount != 1:
        raise PlanError("This plan is already being executed")
    results = _execute(_Confirmed(plan, _Confirmed._key), source, path, http_post or oneclick.post, opener or webbrowser.open)
    db = tracker.connect(path)
    try:
        db.execute("UPDATE plans SET status='done' WHERE id=?", (plan_id,))
        db.commit()
    finally:
        db.close()
    return {"plan": plan.id, "action": plan.action, "dry_run": plan.dry_run, "results": results,
            "ok": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"])}


def _n(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def question(plan) -> str:
    """The plain yes/no question for step 2 (CLI): it names the action and
    the exact counts, like the web app's confirm button."""
    d = plan.to_dict() if hasattr(plan, "to_dict") else plan
    n, emails = d["runnable"], d.get("emails", 0)
    who = _n(n, "sender", "senders")
    dry = " (dry run: nothing is contacted or changed)" if d.get("dry_run") else ""
    q = {"unsubscribe": f"Unsubscribe from {who}? This cannot be undone from Clawback",
         "unsubscribe_trash": f"Unsubscribe from {who} and move their {_n(emails, 'email', 'emails')} to Trash? "
                              "The emails can be restored for 30 days; the unsubscribe cannot be undone",
         "trash": f"Move {_n(emails, 'email', 'emails')} from {who} to Trash? You can restore them for 30 days",
         "archive": f"Archive {_n(emails, 'email', 'emails')} from {who}?",
         "block": f"Filter future mail from {who} out of your inbox?",
         "report_spam": f"Report {_n(emails, 'email', 'emails')} from {who} as spam?",
         "escalate": f"Block and report {who} as spam?",
         "open_cancel": f"Open {_n(n, 'cancellation page', 'cancellation pages')}?",
         "send_request": f"Send {_n(n, 'privacy request', 'privacy requests')}?",
         "undo": f"Undo {_n(n, 'action', 'actions')}?"}.get(d["action"], f"Run {d['action']} for {n}?")
    return q + dry


def _privacy_request(rid: int, account: str, path=None) -> dict:
    db = tracker.connect(path)
    try:
        row = db.execute("SELECT * FROM privacy_requests WHERE id=? AND account=?", (rid, account)).fetchone()
    finally:
        db.close()
    if row is None:
        raise PlanError(f"Unknown request {rid}")
    return dict(row)


def _execute(token: _Confirmed, source, path, http_post, opener) -> list[dict]:
    if not isinstance(token, _Confirmed):
        raise PermissionError("Actions can only run through confirm()")
    plan = token.plan
    results = []
    for step in plan.runnable:
        try:
            res = _run_step(plan, step, source, path, http_post, opener)
        except (SourceError, OSError, ValueError) as exc:
            res = {"ok": False, "detail": str(exc)}
        except Exception as exc:  # a failure on one sender must not stop or hide the others
            res = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        res.setdefault("target", step.target)
        res.setdefault("label", step.label)
        res.setdefault("method", step.method)
        res["entry"] = tracker.audit(plan.account, plan.action, step.target, res["ok"], method=step.method,
                                     detail={"contact": step.contact, "url": step.url, "effects": step.effects,
                                             "detail": res.get("detail", ""), "open": res.get("open")},
                                     result=res.get("detail", ""), undo=res.get("undo"), plan_id=plan.id,
                                     dry_run=plan.dry_run, path=path)
        res.pop("undo", None)
        results.append(res)
    return results


def _ids(plan, step, path):
    return tracker.message_ids_for(plan.account, step.target, path=path)


def _run_step(plan: Plan, step: Step, source, path, http_post, opener) -> dict:
    if plan.dry_run:
        return {"ok": True, "detail": "dry run: nothing was contacted or changed", "dry_run": True}
    a = plan.action
    acct = plan.account
    if a == "unsubscribe_trash":
        ids = _ids(plan, step, path)
        if step.data.get("trash_only"):
            undo = source.trash(ids)
            tracker.mark_trashed(acct, step.target, len(ids), path=path)
            return {"ok": True, "detail": f"moved {len(ids)} email(s) to Trash (not unsubscribed: no safe way to)",
                    "undo": undo, "trashed": len(ids)}
        out = _unsubscribe(plan, step, source, path, http_post, opener)
        # The emails go to Trash even if the unsubscribe request failed: the
        # user asked for both, and the result says which part failed.
        undo = source.trash(ids)
        tracker.mark_trashed(acct, step.target, len(ids), path=path)
        out["undo"] = undo   # Undo restores the emails; the unsubscribe itself cannot be taken back
        out["detail"] = f"{out['detail']}; moved {len(ids)} email(s) to Trash"
        out["trashed"] = len(ids)
        return out
    if a == "unsubscribe":
        return _unsubscribe(plan, step, source, path, http_post, opener)
    return _run_mailbox_step(plan, step, source, path, opener)


def _unsubscribe(plan: Plan, step: Step, source, path, http_post, opener) -> dict:
    acct = plan.account
    out: dict = {}
    if step.method == "one-click":
        if getattr(source, "simulate_http", False):
            r = oneclick.PostResult(True, None, "demo: simulated, nothing was sent", step.url)
        else:
            r = http_post(step.url)
        out = {"ok": r.ok, "detail": f"one-click POST to {step.contact}: {r.detail}"}
    elif step.method == "mailto":
        to, subject, body = mailto_parts(step.url)
        source.send_email(to, subject, body)
        out = {"ok": True, "detail": f"unsubscribe email sent to {to}"}
    else:  # link / mailto-client: the user completes it
        out = {"ok": True, "detail": "opened for you to finish", "open": step.url, "needs_user": True}
        try:
            opener(step.url)
        except Exception:
            pass  # the UI/CLI also shows the link to open manually
    if out["ok"]:
        tracker.set_sender_status(acct, step.target, "unsubscribed", method=step.method, path=path)
        # An unsubscribe request cannot be taken back by Clawback: the sender
        # has it. No undo is recorded; the log says how to resubscribe.
        out["undo"] = None
        out["detail"] = (out.get("detail") or "done") + " (cannot be undone here; to resubscribe, use the sender's site)"
        extras = []
        if plan.options.get("also_archive") and "archive" in source.capabilities:
            extras.append(("archive", source.archive(_ids(plan, step, path))))
        if plan.options.get("also_trash") and "trash" in source.capabilities:
            ids = _ids(plan, step, path)
            extras.append(("trash", source.trash(ids)))
            tracker.mark_trashed(acct, step.target, len(ids), path=path)
        if plan.options.get("also_block") and "block" in source.capabilities:
            extras.append(("block", source.create_filter(step.target, "archive")))
        for kind, undo in extras:
            tracker.audit(acct, kind, step.target, True, method="with unsubscribe", undo=undo, plan_id=plan.id, path=path)
    return out


def _run_mailbox_step(plan: Plan, step: Step, source, path, opener) -> dict:
    a = plan.action
    acct = plan.account
    if a == "archive":
        ids = _ids(plan, step, path)
        return {"ok": True, "detail": f"archived {len(ids)} email(s)", "undo": source.archive(ids)}
    if a == "trash":
        ids = _ids(plan, step, path)
        undo = source.trash(ids)
        tracker.mark_trashed(acct, step.target, len(ids), path=path)
        return {"ok": True, "detail": f"moved {len(ids)} email(s) to Trash", "undo": undo, "trashed": len(ids)}
    if a == "block":
        undo = source.create_filter(step.target, plan.options.get("block_mode", "archive"))
        tracker.set_sender_status(acct, step.target, "blocked", method="filter", path=path)
        return {"ok": True, "detail": "filter created", "undo": undo}
    if a == "report_spam":
        ids = _ids(plan, step, path)
        return {"ok": True, "detail": f"reported {len(ids)} email(s) as spam", "undo": source.report_spam(ids)}
    if a == "escalate":
        undo_filter = source.create_filter(step.target, "trash")
        ids = _ids(plan, step, path)
        undo_spam = source.report_spam(ids)
        tracker.audit(acct, "report_spam", step.target, True, method="escalation", undo=undo_spam, plan_id=plan.id, path=path)
        tracker.set_sender_status(acct, step.target, "escalated", path=path)
        return {"ok": True, "detail": f"filter created and {len(ids)} email(s) reported as spam", "undo": undo_filter}
    if a == "open_cancel":
        try:
            opener(step.url)
        except Exception:
            pass
        return {"ok": True, "detail": "cancellation page opened; mark it cancelled once you have finished", "open": step.url, "needs_user": True}
    if a == "send_request":
        req = _privacy_request(int(step.target), acct, path)
        if step.method == "mailto-client":
            from urllib.parse import quote
            url = f"mailto:{req['to_address']}?subject={quote(req['subject'])}&body={quote(req['body'])}"
            try:
                opener(url)
            except Exception:
                pass
            return {"ok": True, "detail": "opened in your mail app; press Send there", "open": url, "needs_user": True}
        source.send_email(req["to_address"], req["subject"], req["body"])
        db = tracker.connect(path)
        try:
            db.execute("UPDATE privacy_requests SET status='sent', sent_at=? WHERE id=?", (tracker.now_iso(), req["id"]))
            db.commit()
        finally:
            db.close()
        return {"ok": True, "detail": f"request sent to {req['to_address']}"}
    if a == "undo":
        entry = tracker.audit_entry(int(step.target), path)
        undo = entry["undo"]
        op = undo.get("op")
        if op == "untrash":
            source.untrash(undo["ids"])
            tracker.mark_trashed(acct, entry["target"], 0, path=path)   # restored: no longer shown as deleted
        elif op == "unarchive":
            source.unarchive(undo["ids"])
        elif op == "unreport_spam":
            source.unreport_spam(undo["ids"])
        elif op == "delete_filter":
            source.delete_filter(undo["filter_id"])
            sender = tracker.get_sender(acct, entry["target"], path)
            if sender and sender["status"] in {"blocked", "escalated"}:
                tracker.set_sender_status(acct, entry["target"], "unsubscribed" if sender["unsubscribed_at"] and sender["unsub_method"] != "filter" else "active", path=path)
        elif op == "resubscribe_note":
            tracker.set_sender_status(acct, entry["target"], "active", path=path)
        else:
            raise ValueError(f"cannot undo {op!r}")
        tracker.mark_undone(entry["id"], path)
        return {"ok": True, "detail": f"undone: {op}"}
    raise ValueError(f"unknown action {a}")


# ------------------------------------------------------------------ all accounts
# A review in the "All accounts" view covers senders from several accounts.
# It is made of one ordinary plan per account (build_plan, unchanged), shown
# together with one confirm button. Confirming checks the combined count
# and digest, then confirms each part with confirm(), unchanged: same
# expiry, single use, account check and permission check.
GROUP_ACCOUNT = "__all__"
GROUP_ACTIONS = {"unsubscribe", "unsubscribe_trash", "archive", "trash", "block", "report_spam", "escalate"}


def _group_row(plan_id: str, path=None):
    db = tracker.connect(path)
    try:
        return db.execute("SELECT * FROM plans WHERE id=? AND account=?", (plan_id, GROUP_ACCOUNT)).fetchone()
    finally:
        db.close()


def is_group(plan_id: str, path=None) -> bool:
    return _group_row(plan_id, path) is not None


def _group_digest(parts: list[dict]) -> str:
    canon = json.dumps([[p["account"], p["plan_id"], p["digest"]] for p in parts], sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def build_group_plan(sources: dict, action: str, targets: list, options: dict | None = None, dry_run: bool = False,
                     path=None, now: datetime | None = None) -> dict:
    """Step 1 of 2 for the All accounts view. ``sources``: account -> live
    source for the accounts connected now; others act as OfflineSource."""
    from . import combined
    from .sources.base import OfflineSource
    if action not in GROUP_ACTIONS:
        raise PlanError("Choose one account (not All accounts) for this action")
    targets = list(dict.fromkeys(str(t) for t in targets if str(t).strip()))
    if not targets:
        raise PlanError("Select at least one item first")
    rows = {r["address"]: r for r in combined.senders(path=path)}
    cap = ACTIONS[action][1]
    per_account: dict[str, list[str]] = {}
    for t in targets:
        r = rows.get(t)
        if r is None:
            raise PlanError(f"Unknown sender {t!r} (scan first)")
        for a in combined.route(t, r["accounts"], sources, cap, path):
            per_account.setdefault(a, []).append(t)
    plans = [build_plan(sources.get(a) or OfflineSource(a), action, ts, options, dry_run, path, now)
             for a, ts in sorted(per_account.items())]
    parts = [{"account": p.account, "plan_id": p.id, "digest": p.digest, "count": p.count} for p in plans]
    runnable = sum(len(p.runnable) for p in plans)
    created = now or datetime.now(timezone.utc)
    steps = []
    for p in plans:
        for s in asdict(p)["steps"]:
            s["account"] = p.account
            steps.append(s)
    d = {"id": secrets.token_urlsafe(18), "account": GROUP_ACCOUNT, "action": action,
         "created_at": created.isoformat(timespec="seconds"), "expires_at": min(p.expires_at for p in plans),
         "count": runnable, "digest": _group_digest(parts), "steps": steps, "options": dict(options or {}),
         "dry_run": dry_run, "runnable": runnable, "group": parts,
         "emails": sum(st.emails_affected for p in plans for st in p.runnable),
         "permissions": sorted({f"{p.account}: {s.permission}" for p in plans for s in p.runnable if s.permission})}
    db = tracker.connect(path)
    try:
        db.execute("INSERT INTO plans(id, account, action, created_at, expires_at, digest, phrase, status, payload) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (d["id"], GROUP_ACCOUNT, action, d["created_at"], d["expires_at"], d["digest"], str(d["count"]),
                    "pending", json.dumps(d)))
        db.commit()
    finally:
        db.close()
    return d


def confirm_group(sources: dict, plan_id: str, count, digest: str, path=None, now: datetime | None = None,
                  http_post=None, opener=None) -> dict:
    """Step 2 of 2 for the All accounts view."""
    from .sources.base import OfflineSource
    row = _group_row(plan_id, path)
    if row is None:
        raise PlanError("Unknown plan: review the selection again")
    d = json.loads(row["payload"])
    now = now or datetime.now(timezone.utc)
    if row["status"] != "pending":
        raise PlanError(f"This plan was already {row['status']}; review again to act")
    if now > datetime.fromisoformat(row["expires_at"]):
        raise PlanError("This review expired; review the selection again")
    if not digest or not secrets.compare_digest(str(digest), row["digest"]) or _group_digest(d["group"]) != row["digest"]:
        raise PlanError("The confirmation does not match the reviewed plan; review again")
    if not same_count(count, d.get("count", -1)):
        raise PlanError("This confirmation does not match what was shown; review again")
    parts = []
    for g in d["group"]:
        plan, status = _load_plan(g["plan_id"], path)
        if status != "pending" or plan.digest != g["digest"] or plan.account != g["account"]:
            raise PlanError("Part of this review changed; review again")
        missing = sorted({s.permission for s in plan.runnable if s.permission} - {""})
        if missing and not plan.dry_run:
            raise PlanError(f"Grant the missing permission for {plan.account} first: " + "; ".join(missing))
        parts.append((plan, g))
    if not any(p.runnable for p, _ in parts):
        raise PlanError("Nothing in this plan can run")
    db = tracker.connect(path)
    try:
        cur = db.execute("UPDATE plans SET status='executing' WHERE id=? AND status='pending'", (plan_id,))
        db.commit()
    finally:
        db.close()
    if cur.rowcount != 1:
        raise PlanError("This plan is already being executed")
    results = []
    for plan, g in parts:
        if not plan.runnable:
            cancel_plan(plan.id, path)
            continue
        src = sources.get(plan.account) or OfflineSource(plan.account)
        res = confirm(src, plan.id, g["count"], g["digest"], path, now, http_post, opener)
        for r in res["results"]:
            r["account"] = plan.account
        results.extend(res["results"])
    db = tracker.connect(path)
    try:
        db.execute("UPDATE plans SET status='done' WHERE id=?", (plan_id,))
        db.commit()
    finally:
        db.close()
    return {"plan": plan_id, "action": d["action"], "dry_run": d["dry_run"], "results": results,
            "ok": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"])}
