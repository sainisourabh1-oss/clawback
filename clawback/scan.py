"""One scan: read messages from a source, detect paid subscriptions, summarise
senders, save metadata locally, and refresh unsubscribe-proof statuses.

Window: ``since`` (inclusive, a timezone-aware datetime) is "scan back to";
sources read newest first and stop there. ``limit`` is optional; when a limit
cuts the window short the result says so (``capped``).

Incremental: Gmail and IMAP skip messages an earlier scan already read with
the same CLASSIFIER_VERSION; their detections are stored, so the totals are
the same as a full rescan. Bump CLASSIFIER_VERSION whenever detection rules
change, so old messages are read again by the new rules.

Cancel keeps everything read so far and marks the scan partial.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from . import config, tracker
from .aggregate import aggregate
from .detect import classify_message
from .progress import Tracker
from .senders import SenderAccumulator
from .sources.base import ScanCancelled

FLUSH_EVERY = 500
# Progressive results: a partial view is published at most every 2 s (the web
# UI polls once a second, so results visibly grow without re-sorting the page
# more often than a person can follow). Previews only recompute senders that
# changed since the last one (SenderAccumulator.refresh). Measured on this
# machine with 5,000 senders: 4 ms when nothing changed, 133 ms when all 5,000
# changed. In 2 s the fast IMAP path reads ~770 emails, so a preview costs
# roughly 1-2% of scan time.
PREVIEW_SECONDS = 2.0
PREVIEW_SENDERS = 100
# 7 (2026-10-28): order, payment-failure, account-data, credit-report and other
# account notices are "important"; Gmail's Updates tab alone no longer makes a
# sender a newsletter (senders.list_signal).
CLASSIFIER_VERSION = 7
# While a scan runs its record gets a heartbeat at least this often (and on every
# flush), so a scan whose process vanished can be told apart from a running one
# (tracker.ABANDONED_AFTER_SECONDS, 6x this).
HEARTBEAT_SECONDS = 30
INCREMENTAL_KINDS = {"gmail", "imap", "outlook"}


@dataclass
class ScanResult:
    account: str
    messages: int = 0
    senders: int = 0
    subscriptions: list = field(default_factory=list)
    categories: dict = field(default_factory=dict)
    skipped: int = 0
    partial: bool = False
    capped: bool = False
    reused: int = 0          # already read by an earlier scan, not fetched again
    bodies: int | None = None  # messages whose full body was fetched (Gmail/IMAP)
    since: str | None = None
    progress: dict = field(default_factory=dict)
    mode: str | None = None    # quick / full / since / last (what was asked for)
    last: int | None = None    # mode "last": how many of the newest emails


def run_scan(source, limit: int | None = None, progress=None, cancel=None, region: str | None = None, path=None,
             since: datetime | None = None, incremental: bool = True, on_preview=None, mode: str | None = None,
             last: int | None = None) -> ScanResult:
    """``on_preview(dict)`` receives partial results while the scan runs
    (newest mail first, labelled with how far back it has read).

    The scan is recorded when it starts and completed when it ends; any
    error, raised anywhere in the scan, is stored with it so the app can
    show why it failed (tracker.last_failure)."""
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    tracker.close_abandoned(path)
    started = tracker.now_iso()
    scan_id = tracker.begin_scan(source.account, getattr(source, "kind", "unknown"), started, since, path,
                                 mode=mode, last_n=last)
    try:
        r = _run_scan(source, limit, progress, cancel, region, path, since, incremental, on_preview, started, scan_id)
        r.mode, r.last = mode, last
        return r
    except BaseException as exc:
        reason = "Stopped with Ctrl+C" if isinstance(exc, KeyboardInterrupt) else f"{type(exc).__name__}: {exc}"
        tracker.finish_scan(scan_id, path, error=reason)   # no-op when the scan already recorded its own error
        raise


def _run_scan(source, limit, progress, cancel, region, path, since, incremental, on_preview, started, scan_id):
    account = source.account
    region = region or config.load_settings().get("region")
    acc = SenderAccumulator()
    hits, batch_hits = [], []
    rows: list[tuple] = []
    count = 0
    span = _Span()
    prog = progress if isinstance(progress, Tracker) else Tracker(since=since)
    if getattr(source, "kind", "") == "mbox":
        prog.unit = "bytes"

    beat = [time.monotonic()]

    def report(done, total=None, current_date=None, **kw):
        prog(done, total, current_date, **kw)
        if time.monotonic() - beat[0] >= HEARTBEAT_SECONDS:
            beat[0] = time.monotonic()
            tracker.scan_heartbeat(scan_id, count, path)
        prog.senders = len(acc.by)
        if callable(progress) and progress is not prog:
            progress(prog.snapshot())

    skip = set()
    if incremental and getattr(source, "kind", "") in INCREMENTAL_KINDS:
        skip = tracker.known_message_ids(account, CLASSIFIER_VERSION, path)

    prior_hits = tracker.stored_hits(account, path) if (skip and on_preview) else []
    last_preview = [time.monotonic()]

    def preview(final=False):
        if not on_preview or (not final and time.monotonic() - last_preview[0] < PREVIEW_SECONDS):
            return
        last_preview[0] = time.monotonic()
        try:
            on_preview(_preview(acc, prior_hits + hits, prog, count))
        except Exception:  # noqa: BLE001 - a preview must never break the scan
            pass

    def flush():
        nonlocal rows, batch_hits
        if rows:
            tracker.record_messages(account, rows, path, cv=CLASSIFIER_VERSION, hits=batch_hits)
            beat[0] = time.monotonic()
            tracker.scan_heartbeat(scan_id, count, path)
        rows, batch_hits = [], []

    partial = False
    try:
        for info in source.iter_messages(limit, report, cancel, since=since, skip_ids=skip):
            if since is not None and info.date is not None and info.date < since:
                continue
            count += 1
            span.add(info.date)
            hit = classify_message(info, region)
            if hit:
                hits.append(hit)
                batch_hits.append(hit)
            info.body = ""  # bodies are never kept after classification
            acc.add(info)
            rows.append((info.source_id, info.from_addr or f"unknown@{info.domain}", tracker._iso(info.date),
                         (info.subject or "")[:200], None if info.unread is None else int(info.unread)))
            if len(rows) >= FLUSH_EVERY:
                flush()
            preview()
    except ScanCancelled:
        partial = True
    except BaseException as exc:
        flush()
        _log_scan(scan_id, count, error=f"{type(exc).__name__}: {exc}", span=span, path=path, total=prog.total)
        raise
    flush()
    tracker.update_unread(account, getattr(source, "unread_refresh", None) or {}, path)
    # Aggregate everything this account has: stored detections from earlier
    # scans plus this one (same answer as reading the whole window again).
    all_hits = tracker.stored_hits(account, path) if skip else hits
    subs = aggregate(all_hits)
    paid_senders = set().union(*(s.senders for s in subs)) if subs else set()
    prior = {r["address"]: r for r in tracker.list_senders(account, path=path)} if skip else {}
    for addr in acc.by:
        old = prior.get(addr)
        if old and old.get("important"):
            merged = acc.important.setdefault(addr, [])
            for reason in old["important"]:
                if reason not in merged and len(merged) < 6:
                    merged.append(reason)
    summaries = acc.finish(paid_senders)
    for sub in subs:
        tracker.upsert_detection(sub, path, account=account)
    if not partial:
        # Rows that no detection supports any more (an older classifier's
        # mistakes, kept from an earlier scan) must not linger as "suspected".
        supported = aggregate(tracker.stored_hits(account, path)) + subs
        tracker.retire_unsupported(account, {(s.domain, s.currency, getattr(s, "identity_key", "default") or "default")
                                             for s in supported}, path)
    tracker.upsert_senders(account, summaries, path)
    recheck_stored(account, set(acc.by), path)
    if paid_senders and skip:
        tracker.mark_paid(account, paid_senders, path)
    from . import proof
    proof.refresh(account, path=path)
    _record_identity(source, path)
    capped = bool(getattr(source, "capped", False))
    _log_scan(scan_id, count, span=span, path=path, partial=partial, capped=capped, total=prog.total)
    cats: dict[str, int] = {}
    for s in tracker.list_senders(account, path=path):
        cats[s["category"]] = cats.get(s["category"], 0) + 1
    return ScanResult(account, count, len(summaries), subs, cats, getattr(source, "skipped", 0), partial=partial,
                      capped=capped, reused=getattr(source, "reused", 0), bodies=getattr(source, "bodies", None),
                      since=since.date().isoformat() if since else None, progress=prog.snapshot())


def recheck_stored(account: str, seen: set[str], path=None) -> list[str]:
    """Stored "newsletter" senders this scan did not read again (all their
    mail is older than the scan window, or the scan was cancelled first) keep
    the category an older classifier gave them. Re-run the important rules
    over their stored subjects, so a security or payment-failure sender never
    stays under Newsletters after the rules change. Only moves senders to
    "important" (the safe direction); returns the addresses moved."""
    from .extract import MessageInfo
    from .important import is_important
    moved: dict[str, list[str]] = {}
    stale = [s for s in tracker.list_senders(account, "newsletter", path) if s["address"] not in seen]
    subjects = tracker.subjects_by_sender(account, path) if stale else {}
    for s in stale:
        reasons: list[str] = []
        for subject in dict.fromkeys(subjects.get(s["address"], [])):
            info = MessageInfo(source_id="", from_addr=s["address"], from_name=s["display_name"] or "",
                               domain=s["domain"], subject=subject, date=None, list_unsubscribe=list(s["unsubscribe"]))
            hit, why = is_important(info)
            if hit:
                reasons += [r for r in why if r not in reasons]
        if reasons:
            moved[s["address"]] = reasons
    tracker.set_important(account, moved, path)
    return list(moved)


def _preview(acc, hits, prog, count) -> dict:
    """Partial results from what has been read so far (nothing is saved)."""
    from .money import format_money
    subs = aggregate(hits)
    paid = set().union(*(s.senders for s in subs)) if subs else set()
    summaries = acc.refresh(paid)
    cats: dict[str, int] = {}
    for s in summaries:
        cats[s.category] = cats.get(s.category, 0) + 1
    newest = sorted(summaries, key=lambda s: s.last_seen.timestamp() if s.last_seen else 0, reverse=True)
    return {
        "partial": True,
        "messages": count,
        # The oldest date the source vouches for (everything newer is read);
        # None during Gmail's priority pass, which reads out of date order.
        "scanned_back_to": prog.current_date.date().isoformat() if prog.current_date else None,
        "note": prog.note,
        "categories": cats,
        "senders": [{"address": s.address, "name": s.display_name, "category": s.category, "count": s.message_count,
                     "last_seen": tracker._iso(s.last_seen)} for s in newest[:PREVIEW_SENDERS]],
        "subscriptions": [{"vendor": s.vendor, "amount": s.amount, "currency": s.currency, "cycle": s.cycle,
                           "loan": "loan" in s.kinds,
                           "display": format_money(s.amount, s.currency) if s.amount is not None else "?"}
                          for s in subs],
    }


# "Next scans take seconds": what the user is told about scan speed. Only
# Gmail (API) and IMAP scans are incremental (INCREMENTAL_KINDS); a Takeout /
# mbox import and the demo read their whole file every time, so they get no
# speed line. Measured with scripts/bench_scan.py (simulated, 200 ms RTT,
# 2026-10-25): API first scan of 3,000 messages 13.1 min; API rescan 1.4 s with
# nothing new and 2.5 s with a week of new mail (60 messages); IMAP rescan 0.9 s.
# The first-scan figure is a range ("10 to 15 minutes") because 3,000 messages
# of metadata alone are 60,000 quota units = 10 min at 6,000 units/min, plus
# 60 units per billing body read in full.
HINT_FIRST_API = "scan.hint.firstApi"
HINT_FIRST_IMAP = "scan.hint.firstImap"
HINT_RESCAN = "scan.hint.rescan"
HINT_NEXT = "scan.hint.next"
HINT_RESCAN_DONE = "scan.hint.rescanDone"
HINT_UPGRADE_API = "scan.hint.upgradeApi"
HINT_UPGRADE = "scan.hint.upgrade"
HINT_TEXT = {   # English for the CLI; the web app uses clawback/web/static/i18n/en.json
    HINT_FIRST_API: "Reading all your Gmail is the slow part: Google limits how much an app may read per minute, so about 3,000 emails "
                    "take 10 to 15 minutes. Next scans only read new mail and usually take seconds.",
    HINT_FIRST_IMAP: "Next scans only read new mail and usually take seconds.",
    HINT_RESCAN: "Only new mail is read; emails from earlier scans are skipped.",
    HINT_NEXT: "Next scans only read new mail, so they usually take seconds.",
    HINT_RESCAN_DONE: "Only new mail was read: {messages} new, {reused} from earlier scans skipped.",
    HINT_UPGRADE_API: "This version of Clawback sorts mail better, so this one scan reads all your mail in the window "
                      "again: Google limits how much an app may read per minute (about 3,000 emails take 10 to 15 "
                      "minutes). After this, quick updates only read new mail and take seconds.",
    HINT_UPGRADE: "This version of Clawback sorts mail better, so this one scan reads all your mail in the window "
                  "again. After this, quick updates only read new mail and take seconds.",
}


def upgrade_reread(kind: str, account: str, path=None) -> bool:
    """True when an incremental scan must still read the whole window
    because the saved mail was read by an older CLASSIFIER_VERSION.

    Why not re-sort the saved data instead (as recheck_stored does for the
    subject rules after every scan): a version change can depend on what is
    never saved - version 7 needs Gmail's category labels (the Updates tab
    rule) and the money rules read email bodies - so only reading the mail
    again gives the same answer as a first scan."""
    return (kind in INCREMENTAL_KINDS and tracker.has_any_messages(account, path)
            and not tracker.has_known_messages(account, CLASSIFIER_VERSION, path))


def reads_everything(kind: str, account: str, incremental: bool = True, path=None) -> bool:
    """True when this scan reads the whole window (first scan, ``--full`` or
    a classifier change), False when it only reads new mail."""
    if kind not in INCREMENTAL_KINDS:
        return True
    return not incremental or not tracker.has_known_messages(account, CLASSIFIER_VERSION, path)


def speed_hint(kind: str, status: str, full_read: bool, result: dict | None = None, upgrade: bool = False) -> str | None:
    """i18n key of the scan-speed line for this state, or None.

    running: Gmail API full read -> firstApi (the slow, quota-bound path);
    IMAP full read -> firstImap; incremental -> rescan. done: only when the
    scan finished the whole window (not cancelled, not capped by a limit):
    nothing reused -> next, otherwise rescanDone. Errors, mbox and demo: None."""
    if kind not in INCREMENTAL_KINDS:
        return None
    if status == "running":
        if not full_read:
            return HINT_RESCAN
        if upgrade:
            return HINT_UPGRADE_API if kind == "gmail" else HINT_UPGRADE
        return HINT_FIRST_API if kind == "gmail" else HINT_FIRST_IMAP
    if status == "done" and result and not result.get("partial") and not result.get("capped"):
        return HINT_RESCAN_DONE if result.get("reused") else HINT_NEXT
    return None


PRESETS = {"1m": 1, "3m": 3, "6m": 6, "1y": 12, "2y": 24}
EMAIL_EPOCH = date(1971, 1, 1)   # the first email was sent in late 1971; an earlier start date means "all"
DEFAULT_WINDOW = "1y"
MODES = ("quick", "full", "since", "last")
MAX_LAST = 1_000_000
# Safety cap of a quick / full / date scan from the web app (100,000 emails).
WEB_CAP = 100_000


def scan_request(mode: str | None, since=None, last=None, today=None) -> dict:
    """What a scan choice means: {mode, since, limit, incremental, last}.

    quick  only new mail since the last scan (in the last 12 months' window)
    full   every email of the last 12 months read again
    since  every email since a chosen date (emails already read are reused)
    last   the newest ``last`` emails, however old (emails already read are
           reused; every source reads newest first, see tests/test_scan_options.py)"""
    mode = mode or "quick"
    if mode not in MODES:
        raise ValueError("Scan type must be quick, full, since or last")
    if mode == "last":
        try:
            n = int(last)
        except (TypeError, ValueError):
            raise ValueError("Enter how many of your newest emails to scan") from None
        if not 1 <= n <= MAX_LAST:
            raise ValueError(f"The number of emails must be between 1 and {MAX_LAST:,}")
        return {"mode": mode, "since": None, "limit": n, "incremental": True, "last": n}
    if mode == "since":
        if not since:
            raise ValueError("Choose the date to scan back to")
        return {"mode": mode, "since": parse_since(str(since), today=today), "limit": WEB_CAP, "incremental": True,
                "last": None}
    return {"mode": mode, "since": parse_since(DEFAULT_WINDOW, today=today), "limit": WEB_CAP,
            "incremental": mode == "quick", "last": None}


# Time estimates, from what is known before reading anything: the emails
# saved by earlier scans (how many there are in the window) and each path's
# measured speed. Gmail API: Google's per-minute quota (Pacer.seconds_for),
# 20 units per email for headers plus 60 for each body read in full; the
# share of bodies is 0 to 1/6 (the "10 to 15 minutes for 3,000 emails" of the
# speed facts: 60,000 to 90,000 units), with at least 15 to 30 ms of round
# trips per email (bench: 60 new emails in 2.5 s). IMAP: the bench read 3,000
# emails in 8 simulated seconds; real servers vary, so 10 to 90 s per 3,000.
API_BODY_SHARE = (0.0, 1 / 6)
API_RTT = ((1.0, 0.015), (3.0, 0.03))      # (fixed s, s per email): low, high
IMAP_PER_3000 = (10.0, 90.0)
FEW_SECONDS = (1.0, 10.0)


def _seconds(kind: str, n: int, quota: float) -> list[float] | None:
    from .sources.pacing import UNITS, Pacer
    if kind == "gmail":
        out = []
        for share, (fixed, per) in zip(API_BODY_SHARE, API_RTT):
            units = n * (UNITS["metadata"] + share * UNITS["full"]) + (n / 500 + 1) * UNITS["list"]
            out.append(max(Pacer(quota).seconds_for(units), fixed + n * per))
        return out
    if kind == "imap":
        return [max(FEW_SECONDS[0], n / 3000 * IMAP_PER_3000[0]), max(FEW_SECONDS[1], n / 3000 * IMAP_PER_3000[1])]
    return None                     # Outlook (experimental): not measured


def estimate(kind: str, account: str, mode: str, since=None, last=None, path=None, quota: float | None = None,
             today=None) -> dict:
    """How long the chosen scan should take, and why. Keys: seconds ([low,
    high] or None when there is no basis), emails (how many will be read,
    None when unknown), at_least (mail older than any earlier scan read is
    not counted), first (nothing saved yet), reread (a new classifier
    version reads the window again), basis (i18n key of the explanation)."""
    from .sources.pacing import DEFAULT_QUOTA_PER_MINUTE
    quota = float(quota or config.load_settings().get("gmail_quota_per_minute") or DEFAULT_QUOTA_PER_MINUTE)
    req = scan_request(mode, since, last, today=today)
    first = not tracker.has_any_messages(account, path)
    reread = upgrade_reread(kind, account, path)
    out = {"kind": kind, "mode": req["mode"], "first": first, "reread": reread, "at_least": False, "emails": None,
           "seconds": None, "basis": None, "since": req["since"].date().isoformat() if req["since"] else None,
           "last": req["last"], "window": parse_since(DEFAULT_WINDOW, today=today).date().isoformat()}
    if kind == "demo":
        return dict(out, seconds=list(FEW_SECONDS), basis="estimate.demo")
    if kind == "mbox":
        return dict(out, basis="estimate.file")
    if kind not in INCREMENTAL_KINDS:
        return dict(out, basis="estimate.none")
    everything = first or reread or not req["incremental"]
    if req["mode"] == "last":
        n = req["last"]
        if not everything:
            # the newest N already saved are reused: only what is missing is read
            n = max(0, n - tracker.stored_count(account, None, path))
            if n == 0:
                return dict(out, seconds=list(FEW_SECONDS), emails=0, basis="estimate.onlyNew")
            return dict(out, emails=n, seconds=_seconds(kind, n, quota), basis="estimate.upToNew")
        return dict(out, emails=n, seconds=_seconds(kind, n, quota), basis="estimate.upTo")
    covered = tracker.covered_since(account, path)
    older = covered is None or (covered != "all" and (req["since"] is None or req["since"] < covered))
    if not everything and not older:
        return dict(out, seconds=list(FEW_SECONDS), basis="estimate.onlyNew")
    if first:
        return dict(out, basis="estimate.first")
    if not everything:
        # only mail older than what earlier scans read is new to Clawback
        return dict(out, at_least=True, basis="estimate.older" if req["since"] else "estimate.olderAll")
    n = tracker.stored_count(account, req["since"], path)
    return dict(out, emails=n, at_least=older, seconds=_seconds(kind, n, quota), basis="estimate.saved")


def _months_back(d, months: int):
    import calendar
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return d.replace(year=y, month=m + 1, day=min(d.day, calendar.monthrange(y, m + 1)[1]))


def parse_since(value, today=None, tz=None) -> datetime | None:
    """"Scan back to" from a preset (1m, 3m, 6m, 1y, 2y, all) or a date
    (YYYY-MM-DD). Months are calendar months (24 Oct -> 24 Jul for 3m) and the
    day starts at local midnight, so "back to 12 Mar" includes all of 12 Mar
    in the user's own time zone. ``None`` means all time."""
    from datetime import date as _date
    tz = tz or datetime.now().astimezone().tzinfo
    if value in (None, "", "all"):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    today = today or datetime.now(tz).date()
    text = str(value).strip().lower()
    if text in PRESETS:
        day = _months_back(today, PRESETS[text])
    else:
        try:
            day = _date.fromisoformat(text)
        except ValueError:
            raise ValueError("Scan window must be 1m, 3m, 6m, 1y, 2y, all, or a date like 2026-03-12") from None
        if day > today:
            raise ValueError("The scan start date is in the future")
        if day < EMAIL_EPOCH:
            return None     # no email is older: the same as "all" (and Gmail's after: takes no negative time)
    return datetime(day.year, day.month, day.day, tzinfo=tz)


class _Span:
    """Newest and oldest message date seen in a scan (messages without a
    parseable date are ignored, they cannot widen the window)."""

    def __init__(self):
        self.newest = self.oldest = None

    def add(self, d):
        if d is None:
            return
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if self.newest is None or d > self.newest:
            self.newest = d
        if self.oldest is None or d < self.oldest:
            self.oldest = d


def _record_identity(source, path=None) -> None:
    """Remember which mailbox this account is (for the All accounts view).
    Gmail API: the profile address (read-only call); IMAP and Outlook: the
    address signed in with. Never fails a scan."""
    kind = getattr(source, "kind", "")
    address = None
    try:
        fn = getattr(source, "mailbox_address", None)
        address = fn() if callable(fn) else None
    except Exception:  # noqa: BLE001 - identity is a convenience, not part of the scan
        address = None
    if not address and kind in {"imap", "outlook"}:
        address = getattr(source, "user", None) or source.account
    if kind in {"gmail", "imap", "outlook", "demo"}:
        tracker.record_identity(source.account, kind, address if address and "@" in address else None, path)


def _log_scan(scan_id, count, error=None, span=None, path=None, partial=False, capped=False, total=None):
    span = span or _Span()
    tracker.finish_scan(scan_id, path, messages=count, error=error, newest=tracker._iso(span.newest),
                        oldest=tracker._iso(span.oldest), partial=int(partial), capped=int(capped), total=total)
