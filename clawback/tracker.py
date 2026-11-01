"""Local SQLite store: subscriptions, senders, message metadata, keep-list,
action plans and the audit log. Nothing here ever leaves the computer.

Message BODIES are never stored, only sender/subject/date/read-state metadata
needed for counts, savings estimates and "did the unsubscribe work?" checks.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config
from .detect import base_domain

DB_PATH = None  # resolved lazily through config.db_path(); kept for v1 compatibility
# "loan": a loan EMI / repayment mandate. Shown separately ("Loans & EMIs,
# not cancellable here"), never counted in "you're paying" or in savings.
STATUSES = {"suspected", "confirmed", "dismissed", "cancelled", "loan"}
SENDER_STATUSES = {"active", "unsubscribed", "blocked", "kept", "escalated"}

_SUBS_SCHEMA = """CREATE TABLE subscriptions (
  id INTEGER PRIMARY KEY, account TEXT NOT NULL DEFAULT 'default', vendor TEXT NOT NULL, domain TEXT NOT NULL,
  identity_key TEXT NOT NULL DEFAULT 'default', amount REAL, currency TEXT NOT NULL DEFAULT '?',
  cycle TEXT NOT NULL DEFAULT 'unknown', monthly_cost REAL,
  status TEXT NOT NULL DEFAULT 'suspected' CHECK(status IN ('suspected','confirmed','dismissed','cancelled','loan')),
  trial_end TEXT, renewal_date TEXT, notes TEXT NOT NULL DEFAULT '', tax_note TEXT NOT NULL DEFAULT '',
  plan TEXT NOT NULL DEFAULT '', merchant TEXT NOT NULL DEFAULT '', region TEXT NOT NULL DEFAULT '',
  cycle_source TEXT NOT NULL DEFAULT 'email text', currency_certainty TEXT NOT NULL DEFAULT 'explicit',
  first_seen TEXT, last_seen TEXT, detected_renewal TEXT, detected_trial_end TEXT, hits INTEGER NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0, senders TEXT NOT NULL DEFAULT '[]', cancelled_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(account, domain, currency, identity_key))"""

_OTHER_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
  id INTEGER PRIMARY KEY, subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
  amount REAL NOT NULL, currency TEXT NOT NULL, observed_on TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual');
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY, subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
  subject TEXT NOT NULL, kind TEXT NOT NULL, confidence REAL NOT NULL, observed_at TEXT NOT NULL,
  UNIQUE(subscription_id, subject, kind));
CREATE TABLE IF NOT EXISTS senders (
  id INTEGER PRIMARY KEY, account TEXT NOT NULL DEFAULT 'default', address TEXT NOT NULL, domain TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'other',
  message_count INTEGER NOT NULL DEFAULT 0, unread_count INTEGER NOT NULL DEFAULT 0, read_known INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT, last_seen TEXT, unsubscribe TEXT NOT NULL DEFAULT '[]', one_click INTEGER NOT NULL DEFAULT 0,
  list_id TEXT NOT NULL DEFAULT '', spam_score INTEGER NOT NULL DEFAULT 0, flags TEXT NOT NULL DEFAULT '[]',
  subjects TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'active', unsubscribed_at TEXT,
  unsub_method TEXT, monthly_rate REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
  UNIQUE(account, address));
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, account TEXT NOT NULL DEFAULT 'default', source_id TEXT NOT NULL, sender TEXT NOT NULL,
  received TEXT, subject TEXT NOT NULL DEFAULT '', unread INTEGER, UNIQUE(account, source_id));
CREATE INDEX IF NOT EXISTS messages_sender ON messages(account, sender, received);
CREATE TABLE IF NOT EXISTS detections (
  account TEXT NOT NULL, source_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(account, source_id));
CREATE TABLE IF NOT EXISTS keep_list (
  id INTEGER PRIMARY KEY, pattern TEXT NOT NULL UNIQUE, note TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY, account TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  digest TEXT NOT NULL, phrase TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, account TEXT NOT NULL, plan_id TEXT, action TEXT NOT NULL,
  target TEXT NOT NULL, method TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}', ok INTEGER NOT NULL,
  result TEXT NOT NULL DEFAULT '', undo TEXT, undone_at TEXT, dry_run INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS privacy_requests (
  id INTEGER PRIMARY KEY, account TEXT NOT NULL, company TEXT NOT NULL, domain TEXT NOT NULL, law TEXT NOT NULL,
  to_address TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL, sent_at TEXT);
CREATE TABLE IF NOT EXISTS account_identity (
  account TEXT PRIMARY KEY, kind TEXT NOT NULL, address TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY, account TEXT NOT NULL, source TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  messages INTEGER NOT NULL DEFAULT 0, newest TEXT, error TEXT, oldest TEXT, since TEXT,
  partial INTEGER NOT NULL DEFAULT 0, capped INTEGER NOT NULL DEFAULT 0, total INTEGER);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(d) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat(timespec="seconds")
    return d.isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        try:
            d = datetime.combine(date.fromisoformat(value), datetime.min.time())
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# Database files whose schema this process has already created/migrated,
# keyed by (path, inode, creation time) so a replaced file is checked again.
# Running the schema script on every connect() cost ~14 ms per call on
# Windows (a commit each time), and the web app opens the database ~10 times
# per screen.
_READY: set[tuple] = set()
# Two threads (the web server and a scan) can open a new database at the same
# moment; setting up the tables is done by one of them at a time.
_SETUP_LOCK = threading.Lock()


def _file_key(p: Path):
    try:
        st = p.stat()
    except OSError:
        return None
    return (str(p.resolve()), st.st_ino, st.st_ctime_ns)


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    key = _file_key(p)
    db = sqlite3.connect(p, timeout=10, check_same_thread=False)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if key is not None and key in _READY:
        return db
    with _SETUP_LOCK:
        _setup(db, p)
    return db


def _setup(db: sqlite3.Connection, p: Path) -> None:
    if _file_key(p) in _READY:
        return
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscriptions'").fetchone()
    if not exists:
        # IF NOT EXISTS: another process (the CLI next to the web app) may have
        # created it between the check and here.
        db.execute(_SUBS_SCHEMA.replace("CREATE TABLE subscriptions", "CREATE TABLE IF NOT EXISTS subscriptions", 1))
    else:
        _migrate_subscriptions(db)
    # One transaction for the whole script: in autocommit mode every CREATE is
    # its own fsync (measured: a new database took ~250 ms, one commit ~30 ms).
    db.executescript("BEGIN;\n" + _OTHER_SCHEMA + "\nCOMMIT;")
    _add_missing_columns(db, "scans", _SCAN_COLUMNS)
    _add_missing_columns(db, "senders", _SENDER_COLUMNS)
    _add_missing_columns(db, "messages", {"cv": "INTEGER"})
    _add_missing_columns(db, "subscriptions", {"tax_note": "TEXT NOT NULL DEFAULT ''"})
    db.commit()
    key = _file_key(p)
    if key is not None:
        _READY.add(key)


# Columns added to `scans` after v2.0.0.dev0 shipped to testers; additive only.
_SCAN_COLUMNS = {"oldest": "TEXT", "since": "TEXT", "partial": "INTEGER NOT NULL DEFAULT 0",
                 "capped": "INTEGER NOT NULL DEFAULT 0", "total": "INTEGER", "heartbeat": "TEXT",
                 # what the user asked for: quick / full / since (a chosen date) / last (the newest last_n)
                 "mode": "TEXT", "last_n": "INTEGER"}
_SENDER_COLUMNS = {"notes": "TEXT NOT NULL DEFAULT '[]'", "signed": "TEXT NOT NULL DEFAULT '[]'",
                   "important": "TEXT NOT NULL DEFAULT '[]'",
                   # unsubscribe link found in an email body (no List-Unsubscribe
                   # header), and when a body was last read for it (NULL = never)
                   "body_unsub": "TEXT NOT NULL DEFAULT ''", "body_checked": "TEXT",
                   # emails moved to Trash by Clawback (shown as "37 deleted"; cleared by Undo)
                   "trashed_at": "TEXT", "trashed_count": "INTEGER NOT NULL DEFAULT 0"}


def _add_missing_columns(db: sqlite3.Connection, table: str, wanted: dict[str, str]) -> None:
    have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, decl in wanted.items():
        if name not in have:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_subscriptions(db: sqlite3.Connection) -> None:
    """Upgrade v1.x tables by rebuilding with foreign keys off, keeping IDs so
    evidence and price history stay attached."""
    columns = [row[1] for row in db.execute("PRAGMA table_info(subscriptions)")]
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='subscriptions'").fetchone()[0]
    if "account" in columns and "cancelled_at" in columns and "'loan'" in sql:
        return
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("BEGIN")
    # Build the new table under a temporary name, then swap. (Renaming the OLD
    # table would make SQLite rewrite the child tables' foreign keys to it.)
    db.execute(_SUBS_SCHEMA.replace("CREATE TABLE subscriptions", "CREATE TABLE subscriptions_new", 1))
    new_cols = {row[1] for row in db.execute("PRAGMA table_info(subscriptions_new)")}
    common = [c for c in columns if c in new_cols]
    cols = ",".join(common)
    db.execute(f"INSERT INTO subscriptions_new({cols}) SELECT {cols} FROM subscriptions")
    db.execute("DROP TABLE subscriptions")
    db.execute("ALTER TABLE subscriptions_new RENAME TO subscriptions")
    db.execute("UPDATE subscriptions SET cancelled_at=updated_at WHERE status='cancelled' AND cancelled_at IS NULL")
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")


# ------------------------------------------------------------------ subscriptions
def upsert_detection(sub, path: Path | None = None, account: str = "default") -> int:
    now = now_iso()
    db = connect(path)
    try:
        identity_key = getattr(sub, "identity_key", "default") or "default"
        row = db.execute("SELECT id FROM subscriptions WHERE account=? AND domain=? AND currency=? AND identity_key=?",
                         (account, sub.domain, sub.currency, identity_key)).fetchone()
        fields = dict(
            vendor=sub.vendor, amount=sub.amount, cycle=sub.cycle, monthly_cost=getattr(sub, "monthly_cost", None),
            plan=getattr(sub, "plan", "") or "", merchant=getattr(sub, "merchant", "") or "", region=getattr(sub, "region", "") or "",
            cycle_source=getattr(sub, "cycle_source", "email text"), currency_certainty=getattr(sub, "currency_certainty", "explicit"),
            first_seen=_iso(getattr(sub, "first_seen", None)), last_seen=_iso(getattr(sub, "last_seen", None)),
            detected_renewal=_iso(getattr(sub, "renewal_date", None)), detected_trial_end=_iso(getattr(sub, "trial_end", None)),
            hits=getattr(sub, "hits", 0), confidence=getattr(sub, "best_confidence", 0.0),
            senders=json.dumps(sorted(getattr(sub, "senders", set()) or [])),
            tax_note=getattr(sub, "tax_note", "") or "",
        )
        if row:
            sid = row["id"]
            sets = ", ".join(f"{k}=COALESCE(?,{k})" if k in {"amount", "monthly_cost", "first_seen", "detected_renewal", "detected_trial_end"} else f"{k}=?" for k in fields)
            db.execute(f"UPDATE subscriptions SET {sets}, updated_at=? WHERE id=?", (*fields.values(), now, sid))
        else:
            cols = ",".join(["account", "domain", "currency", "identity_key", "status", "created_at", "updated_at", *fields])
            marks = ",".join("?" * (7 + len(fields)))
            cur = db.execute(f"INSERT INTO subscriptions({cols}) VALUES({marks})",
                             (account, sub.domain, sub.currency, identity_key,
                              "loan" if "loan" in (sub.kinds or set()) else "suspected", now, now, *fields.values()))
            sid = cur.lastrowid
        for subject in (sub.evidence or [sub.last_subject]):
            if subject:
                db.execute("INSERT OR IGNORE INTO evidence(subscription_id,subject,kind,confidence,observed_at) VALUES(?,?,?,?,?)",
                           (sid, subject, ",".join(sorted(sub.kinds)), sub.best_confidence, now))
        prices = list(getattr(sub, "prices", []) or [])
        if not prices and sub.amount is not None:
            prices = [(None, sub.amount, sub.currency)]
        for when, amount, cur in sorted(prices, key=lambda p: _iso(p[0]) or ""):
            observed = (_iso(when) or date.today().isoformat())[:10]
            last = db.execute("SELECT amount, observed_on FROM price_history WHERE subscription_id=? ORDER BY observed_on DESC, id DESC LIMIT 1", (sid,)).fetchone()
            if last and observed < last["observed_on"]:
                continue  # older than what we already know; rescans must not reorder history
            if not last or float(last["amount"]) != float(amount):
                db.execute("INSERT INTO price_history(subscription_id,amount,currency,observed_on,source) VALUES(?,?,?,?,?)",
                           (sid, amount, cur, observed, "email scan"))
        db.commit()
        return int(sid)
    finally:
        db.close()


def retire_unsupported(account: str, keep: set, path: Path | None = None) -> int:
    """Remove this account's automatic rows (still "suspected", or a "loan"
    bucket row) that no stored detection supports any more, e.g. rows an
    older classifier created that the current one no longer finds. ``keep``
    holds the (domain, currency, identity_key) of every supported row. Rows
    the user touched (another status, a note, own dates, cancelled) are never
    removed. Their evidence and price history go with them (foreign keys)."""
    db = connect(path)
    try:
        rows = db.execute("SELECT id, domain, currency, identity_key FROM subscriptions WHERE account=? "
                          "AND status IN ('suspected','loan') AND notes='' AND trial_end IS NULL "
                          "AND renewal_date IS NULL AND cancelled_at IS NULL", (account,)).fetchall()
        gone = [r["id"] for r in rows if (r["domain"], r["currency"], r["identity_key"]) not in keep]
        db.executemany("DELETE FROM subscriptions WHERE id=?", [(i,) for i in gone])
        db.commit()
        return len(gone)
    finally:
        db.close()


def list_subscriptions(path: Path | None = None, account: str | None = None):
    db = connect(path)
    try:
        q = "SELECT * FROM subscriptions"
        args: tuple = ()
        if account:
            q += " WHERE account=?"; args = (account,)
        q += " ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'suspected' THEN 1 ELSE 2 END, vendor COLLATE NOCASE"
        return db.execute(q, args).fetchall()
    finally:
        db.close()


def get_subscription(identifier: int, path: Path | None = None):
    db = connect(path)
    try:
        return db.execute("SELECT * FROM subscriptions WHERE id=?", (identifier,)).fetchone()
    finally:
        db.close()


def set_status(identifier: int, status: str, path: Path | None = None, when: datetime | None = None) -> None:
    if status not in STATUSES:
        raise ValueError("Invalid status")
    db = connect(path)
    try:
        now = now_iso()
        cancelled_at = _iso(when) if when else now
        cur = db.execute(
            "UPDATE subscriptions SET status=?, updated_at=?, cancelled_at=CASE WHEN ?='cancelled' THEN COALESCE(cancelled_at, ?) ELSE NULL END WHERE id=?",
            (status, now, status, cancelled_at, identifier))
        if not cur.rowcount:
            raise ValueError(f"No subscription with ID {identifier}")
        db.commit()
    finally:
        db.close()


def update_dates(identifier: int, trial_end: str | None = None, renewal_date: str | None = None,
                 notes: str | None = None, path: Path | None = None) -> None:
    for value in (trial_end, renewal_date):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError as e:
                raise ValueError("Dates must use YYYY-MM-DD") from e
    fields, vals = [], []
    for key, val in [("trial_end", trial_end), ("renewal_date", renewal_date), ("notes", notes)]:
        if val is not None:
            fields.append(f"{key}=?"); vals.append(val)
    if not fields:
        raise ValueError("Provide at least one field to update")
    fields.append("updated_at=?"); vals.extend([now_iso(), identifier])
    db = connect(path)
    try:
        cur = db.execute(f"UPDATE subscriptions SET {', '.join(fields)} WHERE id=?", vals)
        if not cur.rowcount:
            raise ValueError(f"No subscription with ID {identifier}")
        db.commit()
    finally:
        db.close()


def reminders(days: int = 7, path: Path | None = None, today: date | None = None):
    """Trial ends and renewals in the next ``days`` days: manual dates first,
    dates detected in email text as a fallback (labelled in the UI)."""
    if days < 0:
        raise ValueError("days must be zero or greater")
    today = today or date.today()
    end = today + timedelta(days=days)
    out = []
    for r in list_subscriptions(path):
        if r["status"] not in {"suspected", "confirmed"}:
            continue
        for value in (r["trial_end"] or r["detected_trial_end"], r["renewal_date"] or r["detected_renewal"]):
            if value and today <= date.fromisoformat(value[:10]) <= end:
                out.append(r); break
    return out


def price_history(identifier: int, path: Path | None = None):
    db = connect(path)
    try:
        return db.execute("SELECT * FROM price_history WHERE subscription_id=? ORDER BY observed_on,id", (identifier,)).fetchall()
    finally:
        db.close()


def price_increases(path: Path | None = None) -> list[dict]:
    """Subscriptions whose latest recorded price is higher than the previous one.

    Two cases are kept apart, because a wrong "price went up" alarm is worse
    than none (seen on a real mailbox):
    * amounts that went DOWN at some point are several bills or plans mixed
      under one vendor (a phone bill and a recharge, usage receipts), so no
      alert at all;
    * the very first price followed by more than double is an introductory
      offer ending (kind "intro"), not a 400% rise (kind "rise").
    """
    out = []
    db = connect(path)
    try:
        for s in db.execute("SELECT * FROM subscriptions WHERE status IN ('suspected','confirmed')"):
            rows = db.execute("SELECT amount, currency, observed_on FROM price_history WHERE subscription_id=? ORDER BY observed_on, id", (s["id"],)).fetchall()
            cur = rows[-1]["currency"] if rows else None
            run = []                      # the latest run in one currency
            for r in reversed(rows):
                if r["currency"] != cur or r["amount"] is None:
                    break
                run.append(r["amount"])
            run.reverse()
            if len(run) < 2 or run[-1] <= run[-2]:
                continue
            if any(b < a for a, b in zip(run, run[1:])):
                continue                  # amounts vary: not one price going up
            old, new = run[-2], run[-1]
            intro = len(run) == len(rows) == 2 and old > 0 and new > 2 * old
            out.append({"id": s["id"], "vendor": s["vendor"], "currency": cur, "old": old, "new": new,
                        "since": rows[-1]["observed_on"], "kind": "intro" if intro else "rise",
                        "percent": round((new - old) / old * 100, 1) if old else None})
    finally:
        db.close()
    return out


# ------------------------------------------------------------------ senders/messages
def record_messages(account: str, rows: list[tuple], path: Path | None = None, cv: int | None = None,
                    hits: list | None = None) -> None:
    """rows: (source_id, sender, received_iso, subject, unread). ``cv`` is the
    classifier version that read them; ``hits`` the detections among them
    (kept so an incremental rescan can re-aggregate without re-reading)."""
    from .detect import hit_to_json
    db = connect(path)
    try:
        db.executemany("""INSERT INTO messages(account, source_id, sender, received, subject, unread, cv) VALUES(?,?,?,?,?,?,?)
                          ON CONFLICT(account, source_id) DO UPDATE SET unread=excluded.unread, cv=excluded.cv,
                          sender=excluded.sender, received=excluded.received, subject=excluded.subject""",
                       [(account, *r, cv) for r in rows])
        ids = [r[0] for r in rows]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            db.execute(f"DELETE FROM detections WHERE account=? AND source_id IN ({','.join('?' * len(part))})", (account, *part))
        db.executemany("INSERT OR REPLACE INTO detections(account, source_id, data) VALUES(?,?,?)",
                       [(account, h.source_id, hit_to_json(h)) for h in (hits or []) if h.source_id])
        db.commit()
    finally:
        db.close()


def known_message_ids(account: str, cv: int, path: Path | None = None) -> set[str]:
    """Messages an earlier scan already read with the current classifier."""
    db = connect(path)
    try:
        return {r[0] for r in db.execute("SELECT source_id FROM messages WHERE account=? AND cv=?", (account, cv))}
    finally:
        db.close()


def has_known_messages(account: str, cv: int, path: Path | None = None) -> bool:
    """True when an earlier scan already read some of this account's mail with
    the current classifier (so the next incremental scan only reads new mail)."""
    db = connect(path)
    try:
        return db.execute("SELECT 1 FROM messages WHERE account=? AND cv=? LIMIT 1", (account, cv)).fetchone() is not None
    finally:
        db.close()


def stored_hits(account: str, path: Path | None = None) -> list:
    from .detect import hit_from_json
    db = connect(path)
    try:
        return [hit_from_json(r[0]) for r in db.execute("SELECT data FROM detections WHERE account=?", (account,))]
    finally:
        db.close()


def mark_paid(account: str, addresses, path: Path | None = None) -> None:
    db = connect(path)
    try:
        db.executemany("UPDATE senders SET category='paid' WHERE account=? AND address=?",
                       [(account, a) for a in addresses])
        db.commit()
    finally:
        db.close()


def update_unread(account: str, states: dict[str, bool], path: Path | None = None) -> None:
    if not states:
        return
    db = connect(path)
    try:
        db.executemany("UPDATE messages SET unread=? WHERE account=? AND source_id=?",
                       [(int(v), account, k) for k, v in states.items()])
        db.commit()
    finally:
        db.close()


def upsert_senders(account: str, summaries, path: Path | None = None) -> None:
    """Merge a scan's sender summaries. Counts/dates are recomputed from the
    message table so repeated scans never double-count."""
    from .senders import monthly_rate
    db = connect(path)
    try:
        now = now_iso()
        for s in summaries:
            agg = db.execute("""SELECT COUNT(*) n, SUM(CASE WHEN unread=1 THEN 1 ELSE 0 END) u,
                                SUM(CASE WHEN unread IS NULL THEN 0 ELSE 1 END) k, MIN(received) f, MAX(received) l
                                FROM messages WHERE account=? AND sender=?""", (account, s.address)).fetchone()
            count = agg["n"] or s.message_count
            first, last = agg["f"] or _iso(s.first_seen), agg["l"] or _iso(s.last_seen)
            rate = monthly_rate(count, parse_dt(first), parse_dt(last))
            db.execute("""INSERT INTO senders(account,address,domain,display_name,category,message_count,unread_count,read_known,
                          first_seen,last_seen,unsubscribe,one_click,list_id,spam_score,flags,subjects,monthly_rate,updated_at,
                          notes,signed,important,body_unsub,body_checked)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(account,address) DO UPDATE SET domain=excluded.domain, display_name=excluded.display_name,
                          category=excluded.category, message_count=excluded.message_count, unread_count=excluded.unread_count,
                          read_known=excluded.read_known, first_seen=excluded.first_seen, last_seen=excluded.last_seen,
                          unsubscribe=CASE WHEN excluded.unsubscribe='[]' THEN senders.unsubscribe ELSE excluded.unsubscribe END,
                          one_click=excluded.one_click, list_id=excluded.list_id, spam_score=excluded.spam_score,
                          flags=excluded.flags, subjects=excluded.subjects, monthly_rate=CASE WHEN senders.status='active'
                          THEN excluded.monthly_rate ELSE senders.monthly_rate END, updated_at=excluded.updated_at,
                          notes=excluded.notes, signed=excluded.signed, important=excluded.important,
                          body_unsub=CASE WHEN excluded.body_unsub='' THEN senders.body_unsub ELSE excluded.body_unsub END,
                          body_checked=COALESCE(excluded.body_checked, senders.body_checked)""",
                       (account, s.address, s.domain, s.display_name, s.category, count, agg["u"] or 0, agg["k"] or 0,
                        first, last, json.dumps(s.list_unsubscribe), int(s.one_click), s.list_id, s.spam_score,
                        json.dumps(s.flags), json.dumps(s.subjects[:5]), rate, now, json.dumps(s.notes),
                        json.dumps(s.signed), json.dumps(getattr(s, "important", [])),
                        getattr(s, "body_unsub", "") or "", now if getattr(s, "body_checked", False) else None))
        db.commit()
    finally:
        db.close()


def subjects_by_sender(account: str, path: Path | None = None) -> dict[str, list[str]]:
    """Every stored subject of an account, grouped by sender (one query)."""
    db = connect(path)
    try:
        out: dict[str, list[str]] = {}
        for sender, subject in db.execute("SELECT sender, subject FROM messages WHERE account=?", (account,)):
            out.setdefault(sender, []).append(subject or "")
        return out
    finally:
        db.close()


def set_important(account: str, reasons_by_address: dict[str, list[str]], path: Path | None = None) -> None:
    """Re-file stored senders as "important" in one transaction (scan.recheck_stored)."""
    if not reasons_by_address:
        return
    db = connect(path)
    try:
        now = now_iso()
        db.executemany("UPDATE senders SET category='important', important=?, updated_at=? WHERE account=? AND address=?",
                       [(json.dumps(r[:6]), now, account, a) for a, r in reasons_by_address.items()])
        db.commit()
    finally:
        db.close()


def list_senders(account: str | None = None, category: str | None = None, path: Path | None = None) -> list[dict]:
    db = connect(path)
    try:
        q, args = "SELECT * FROM senders WHERE 1=1", []
        if account:
            q += " AND account=?"; args.append(account)
        if category:
            q += " AND category=?"; args.append(category)
        q += " ORDER BY message_count DESC, address"
        rows = [dict(r) for r in db.execute(q, args)]
    finally:
        db.close()
    keep = keep_patterns(path)
    for r in rows:
        r["unsubscribe"] = json.loads(r["unsubscribe"] or "[]")
        r["flags"] = json.loads(r["flags"] or "[]")
        r["subjects"] = json.loads(r["subjects"] or "[]")
        for col in ("notes", "signed", "important"):
            r[col] = json.loads(r.get(col) or "[]")
        # One company, many sending addresses (discover@/explore@ pinterest.com):
        # the UI groups rows by company and can act on all of them at once.
        r["company"] = base_domain(r["domain"])
        r["company_name"] = (r["company"].split(".")[0] or r["domain"]).replace("-", " ").title()
        if not r["unsubscribe"]:
            from .links import email_settings_for
            r["settings_guide"] = email_settings_for(r["domain"])
        r["body_link"] = _body_link(r)
        r["kept"] = is_kept(r["address"], keep)
    return rows


def _body_link(r: dict) -> dict | None:
    """The unsubscribe link found in the sender's email, with the same trust
    check as a List-Unsubscribe link; only offered when there is no header."""
    url = r.get("body_unsub") or ""
    if r["unsubscribe"] or not url:
        return None
    from .senders import check_link, host_of
    verdict = check_link(r["domain"], url, set(r.get("signed") or []))
    return {"url": url, "host": host_of(url), "level": verdict[0] if verdict else None,
            "text": verdict[1] if verdict else ""}


def set_body_link(account: str, address: str, url: str, path: Path | None = None) -> None:
    """Record that a body was read for this sender (on demand or by the
    after-scan top-up), and the unsubscribe link found in it, if any."""
    db = connect(path)
    try:
        db.execute("UPDATE senders SET body_checked=?, body_unsub=CASE WHEN ?='' THEN body_unsub ELSE ? END "
                   "WHERE account=? AND address=?", (now_iso(), url, url, account, address))
        db.commit()
    finally:
        db.close()


def latest_message_id(account: str, address: str, path: Path | None = None) -> str | None:
    db = connect(path)
    try:
        row = db.execute("SELECT source_id FROM messages WHERE account=? AND sender=? ORDER BY received DESC LIMIT 1",
                         (account, address)).fetchone()
        return row["source_id"] if row else None
    finally:
        db.close()


def body_link_candidates(account: str, limit: int = 300, path: Path | None = None) -> list[str]:
    """Newsletter senders still showing "How: None" whose email body was never
    read: the after-scan top-up looks in one recent email of each."""
    db = connect(path)
    try:
        return [r["address"] for r in db.execute(
            "SELECT address FROM senders WHERE account=? AND category='newsletter' AND status='active' "
            "AND unsubscribe='[]' AND body_unsub='' AND body_checked IS NULL "
            "ORDER BY message_count DESC, address LIMIT ?", (account, limit))]
    finally:
        db.close()


def get_sender(account: str, address: str, path: Path | None = None) -> dict | None:
    for r in list_senders(account, path=path):
        if r["address"] == address:
            return r
    return None


def set_sender_status(account: str, address: str, status: str, method: str | None = None,
                      when: datetime | None = None, path: Path | None = None) -> None:
    if status not in SENDER_STATUSES:
        raise ValueError("Invalid sender status")
    db = connect(path)
    try:
        stamp = _iso(when) if when else now_iso()
        if status == "unsubscribed":
            db.execute("UPDATE senders SET status=?, unsubscribed_at=?, unsub_method=?, updated_at=? WHERE account=? AND address=?",
                       (status, stamp, method, now_iso(), account, address))
        elif status == "active":
            db.execute("UPDATE senders SET status='active', unsubscribed_at=NULL, unsub_method=NULL, updated_at=? WHERE account=? AND address=?",
                       (now_iso(), account, address))
        else:
            db.execute("UPDATE senders SET status=?, unsubscribed_at=COALESCE(unsubscribed_at, ?), unsub_method=COALESCE(unsub_method, ?), updated_at=? WHERE account=? AND address=?",
                       (status, stamp, method, now_iso(), account, address))
        db.commit()
    finally:
        db.close()


def mark_trashed(account: str, address: str, count: int, path: Path | None = None) -> None:
    """Remember that Clawback moved ``count`` emails from this sender to Trash
    (0 = restored again). Only the marker: the mail itself is in the mailbox."""
    db = connect(path)
    try:
        db.execute("UPDATE senders SET trashed_at=?, trashed_count=?, updated_at=? WHERE account=? AND address=?",
                   (now_iso() if count else None, int(count), now_iso(), account, address))
        db.commit()
    finally:
        db.close()


def sender_messages(account: str, address: str, path: Path | None = None) -> list[dict]:
    db = connect(path)
    try:
        return [dict(r) for r in db.execute("SELECT source_id, received, subject, unread FROM messages WHERE account=? AND sender=? ORDER BY received",
                                            (account, address))]
    finally:
        db.close()


def message_ids_for(account: str, address: str, before: datetime | None = None, path: Path | None = None) -> list[str]:
    rows = sender_messages(account, address, path)
    return [r["source_id"] for r in rows if before is None or (r["received"] and parse_dt(r["received"]) <= before)]


def record_identity(account: str, kind: str, address: str | None, path: Path | None = None) -> None:
    """Which mailbox an account label is: the same Gmail connected through
    the API ("default") and IMAP ("me@gmail.com") is one mailbox."""
    db = connect(path)
    try:
        db.execute("INSERT INTO account_identity(account, kind, address, updated_at) VALUES(?,?,?,?) "
                   "ON CONFLICT(account) DO UPDATE SET kind=excluded.kind, "
                   "address=COALESCE(excluded.address, account_identity.address), updated_at=excluded.updated_at",
                   (account, kind, (address or "").strip().lower() or None, now_iso()))
        db.commit()
    finally:
        db.close()


def identities(path: Path | None = None) -> dict[str, dict]:
    db = connect(path)
    try:
        return {r["account"]: dict(r) for r in db.execute("SELECT * FROM account_identity")}
    finally:
        db.close()


def scanned_accounts(path: Path | None = None) -> list[str]:
    """Accounts with saved results (no placeholder when there are none)."""
    db = connect(path)
    try:
        return sorted({r[0] for r in db.execute("SELECT DISTINCT account FROM senders UNION "
                                                 "SELECT DISTINCT account FROM subscriptions")})
    finally:
        db.close()


def accounts(path: Path | None = None) -> list[str]:
    db = connect(path)
    try:
        names = {r[0] for r in db.execute("SELECT DISTINCT account FROM senders UNION SELECT DISTINCT account FROM subscriptions")}
    finally:
        db.close()
    return sorted(names) or ["default"]


# ------------------------------------------------------------------ scan records
# A scan row is written when the scan starts and completed when it ends, with a
# heartbeat while it runs. A row that never completed and whose heartbeat went
# quiet belongs to a process that was closed, killed or put to sleep: nothing
# inside it could report why, so the next run says so (CLI and web app).
ABANDONED_AFTER_SECONDS = 180
ABANDONED_TEXT = ("The scan stopped before it finished, without an error: Clawback was closed or stopped "
                  "(for example the terminal window was closed, the process was ended, or the computer went to sleep). "
                  "The emails read before it stopped are kept; scanning again continues from there.")


def begin_scan(account: str, kind: str, started: str, since, path: Path | None = None, mode: str | None = None,
               last_n: int | None = None) -> int:
    db = connect(path)
    try:
        cur = db.execute("INSERT INTO scans(account, source, started_at, since, heartbeat, mode, last_n) "
                         "VALUES(?,?,?,?,?,?,?)", (account, kind, started, _iso(since), started, mode, last_n))
        db.commit()
        return int(cur.lastrowid)
    finally:
        db.close()


def scan_heartbeat(scan_id: int, messages: int, path: Path | None = None) -> None:
    db = connect(path)
    try:
        db.execute("UPDATE scans SET heartbeat=?, messages=? WHERE id=? AND finished_at IS NULL",
                   (now_iso(), messages, scan_id))
        db.commit()
    finally:
        db.close()


def finish_scan(scan_id: int, path: Path | None = None, **fields) -> None:
    """Complete a scan row once (later calls change nothing)."""
    fields.setdefault("finished_at", now_iso())
    cols = ", ".join(f"{k}=?" for k in fields)
    db = connect(path)
    try:
        db.execute(f"UPDATE scans SET {cols} WHERE id=? AND finished_at IS NULL", (*fields.values(), scan_id))
        db.commit()
    finally:
        db.close()


def close_abandoned(path: Path | None = None, now: datetime | None = None) -> list[dict]:
    """Mark scans whose process went away without finishing; return them
    (account, started_at, messages) so the caller can tell the user."""
    now = now or datetime.now(timezone.utc)
    db = connect(path)
    try:
        rows = db.execute("SELECT id, account, source, started_at, heartbeat, messages FROM scans "
                          "WHERE finished_at IS NULL AND error IS NULL").fetchall()
        out = []
        for r in rows:
            beat = _parse_iso(r["heartbeat"] or r["started_at"])
            if beat is not None and (now - beat).total_seconds() < ABANDONED_AFTER_SECONDS:
                continue  # still alive (another window or the web app is scanning)
            db.execute("UPDATE scans SET finished_at=?, error=? WHERE id=?",
                       (r["heartbeat"] or r["started_at"], ABANDONED_TEXT, r["id"]))
            out.append({"account": r["account"], "source": r["source"], "started_at": r["started_at"],
                        "messages": r["messages"] or 0})
        db.commit()
        return out
    finally:
        db.close()


def last_failure(account: str, path: Path | None = None) -> dict | None:
    """The account's latest scan when it failed (an error, or stopped without
    one), so the app can say why; None when the latest scan worked."""
    db = connect(path)
    try:
        row = db.execute("SELECT started_at, finished_at, error FROM scans WHERE account=? AND finished_at IS NOT NULL "
                         "ORDER BY id DESC LIMIT 1", (account,)).fetchone()
    finally:
        db.close()
    if not row or not row["error"]:
        return None
    return {"started": row["started_at"], "finished": row["finished_at"], "error": row["error"]}


def _parse_iso(text):
    try:
        d = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def last_scan(account: str, path: Path | None = None) -> dict | None:
    """The account's most recent finished scan without an error, with the
    stored totals for that account (emails and senders kept so far), or None."""
    db = connect(path)
    try:
        row = db.execute("SELECT finished_at, since, mode, last_n, messages, partial, capped FROM scans "
                         "WHERE account=? AND error IS NULL AND finished_at IS NOT NULL "
                         "ORDER BY id DESC LIMIT 1", (account,)).fetchone()
        if not row:
            return None
        messages = db.execute("SELECT COUNT(*) FROM messages WHERE account=?", (account,)).fetchone()[0]
        senders = db.execute("SELECT COUNT(*) FROM senders WHERE account=?", (account,)).fetchone()[0]
    finally:
        db.close()
    # messages/senders: everything saved for the account; the rest describes
    # what that one scan covered, so the page can say it honestly
    return {"finished": row[0], "messages": messages, "senders": senders, "since": row["since"], "mode": row["mode"],
            "last_n": row["last_n"], "read": row["messages"], "partial": bool(row["partial"]), "capped": bool(row["capped"])}


def stored_count(account: str, since: datetime | None = None, path: Path | None = None) -> int:
    """Emails saved for the account (received on or after ``since``)."""
    db = connect(path)
    try:
        if since is None:
            return db.execute("SELECT COUNT(*) FROM messages WHERE account=?", (account,)).fetchone()[0]
        rows = db.execute("SELECT received FROM messages WHERE account=? AND received IS NOT NULL", (account,))
        return sum(1 for r in rows if (_parse_iso(r[0]) or since) >= since)
    finally:
        db.close()


def covered_since(account: str, path: Path | None = None):
    """How far back finished, complete scans of the account have read:
    ``"all"`` (all time), a datetime, or None (no complete date-window scan).
    A "newest N" scan covers a count, not a date, so it does not count."""
    db = connect(path)
    try:
        rows = db.execute("SELECT since FROM scans WHERE account=? AND error IS NULL AND finished_at IS NOT NULL "
                          "AND partial=0 AND capped=0 AND COALESCE(mode, '') != 'last'", (account,)).fetchall()
    finally:
        db.close()
    if not rows:
        return None
    if any(r[0] is None for r in rows):
        return "all"
    dates = [d for d in (_parse_iso(r[0]) for r in rows) if d is not None]
    return min(dates) if dates else None


def has_any_messages(account: str, path: Path | None = None) -> bool:
    """True when any scan, of any classifier version, saved this account's mail."""
    db = connect(path)
    try:
        return db.execute("SELECT 1 FROM messages WHERE account=? LIMIT 1", (account,)).fetchone() is not None
    finally:
        db.close()


# ------------------------------------------------------------------ keep-list
def keep_patterns(path: Path | None = None) -> list[str]:
    db = connect(path)
    try:
        return [r[0] for r in db.execute("SELECT pattern FROM keep_list ORDER BY pattern")]
    finally:
        db.close()


def _normalise_pattern(pattern: str) -> str:
    p = pattern.strip().lower()
    if not p or " " in p or (p.count("@") > 1):
        raise ValueError("Use an address (news@example.com) or a domain (@example.com or example.com)")
    if "@" not in p:
        p = "@" + p
    return p


def keep_add(pattern: str, note: str = "", path: Path | None = None) -> str:
    p = _normalise_pattern(pattern)
    db = connect(path)
    try:
        db.execute("INSERT OR IGNORE INTO keep_list(pattern, note, added_at) VALUES(?,?,?)", (p, note, now_iso()))
        db.commit()
    finally:
        db.close()
    return p


def keep_remove(pattern: str, path: Path | None = None) -> None:
    p = _normalise_pattern(pattern)
    db = connect(path)
    try:
        db.execute("DELETE FROM keep_list WHERE pattern=?", (p,))
        db.commit()
    finally:
        db.close()


def is_kept(address: str, patterns: list[str] | None = None) -> bool:
    address = (address or "").lower()
    for p in patterns if patterns is not None else keep_patterns():
        if p.startswith("@"):
            d = address.rsplit("@", 1)[-1]
            if d == p[1:] or d.endswith("." + p[1:]):
                return True
        elif address == p:
            return True
    return False


# ------------------------------------------------------------------ audit log
def audit(account: str, action: str, target: str, ok: bool, method: str = "", detail: dict | None = None,
          result: str = "", undo: dict | None = None, plan_id: str | None = None, dry_run: bool = False,
          path: Path | None = None) -> int:
    db = connect(path)
    try:
        cur = db.execute("""INSERT INTO audit_log(ts,account,plan_id,action,target,method,detail,ok,result,undo,dry_run)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                         (now_iso(), account, plan_id, action, target, method, json.dumps(detail or {}), int(ok), result,
                          json.dumps(undo) if undo else None, int(dry_run)))
        db.commit()
        return int(cur.lastrowid)
    finally:
        db.close()


def audit_entries(account: str | None = None, path: Path | None = None, limit: int = 500) -> list[dict]:
    db = connect(path)
    try:
        q, args = "SELECT * FROM audit_log", []
        if account:
            q += " WHERE account=?"; args.append(account)
        q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        rows = [dict(r) for r in db.execute(q, args)]
    finally:
        db.close()
    for r in rows:
        r["detail"] = json.loads(r["detail"] or "{}")
        r["undo"] = json.loads(r["undo"]) if r["undo"] else None
    return rows


def audit_entry(entry_id: int, path: Path | None = None) -> dict | None:
    for r in audit_entries(path=path, limit=100000):
        if r["id"] == entry_id:
            return r
    return None


def mark_undone(entry_id: int, path: Path | None = None) -> None:
    db = connect(path)
    try:
        db.execute("UPDATE audit_log SET undone_at=? WHERE id=?", (now_iso(), entry_id))
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------ export / import
EXPORT_VERSION = 2


def export_json(path: Path | None = None) -> dict:
    db = connect(path)
    try:
        data = {"format": "unsubscribe-tracker", "version": EXPORT_VERSION, "exported_at": now_iso()}
        for table in ("subscriptions", "price_history", "senders", "keep_list", "audit_log", "privacy_requests"):
            data[table] = [dict(r) for r in db.execute(f"SELECT * FROM {table}")]
        return data
    finally:
        db.close()


def import_json(data: dict, path: Path | None = None) -> dict:
    """Merge an export into this database. Existing rows win on conflict, so an
    import can never overwrite decisions made on this machine."""
    if not isinstance(data, dict) or data.get("format") != "unsubscribe-tracker":
        raise ValueError("Not an unsubscribe tracker export")
    db = connect(path)
    counts = {}
    try:
        id_map = {}
        for r in data.get("subscriptions", []):
            exists = db.execute("SELECT id FROM subscriptions WHERE account=? AND domain=? AND currency=? AND identity_key=?",
                                (r.get("account", "default"), r["domain"], r["currency"], r["identity_key"])).fetchone()
            if exists:
                id_map[r["id"]] = exists["id"]; continue
            cols = [c for c in r if c != "id" and c in {row[1] for row in db.execute("PRAGMA table_info(subscriptions)")}]
            cur = db.execute(f"INSERT INTO subscriptions({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [r[c] for c in cols])
            id_map[r["id"]] = cur.lastrowid
            counts["subscriptions"] = counts.get("subscriptions", 0) + 1
        for r in data.get("price_history", []):
            sid = id_map.get(r["subscription_id"])
            if sid and not db.execute("SELECT 1 FROM price_history WHERE subscription_id=? AND observed_on=? AND amount=?", (sid, r["observed_on"], r["amount"])).fetchone():
                db.execute("INSERT INTO price_history(subscription_id,amount,currency,observed_on,source) VALUES(?,?,?,?,?)",
                           (sid, r["amount"], r["currency"], r["observed_on"], r.get("source", "import")))
        for table, key in (("senders", ("account", "address")), ("keep_list", ("pattern",))):
            cols_ok = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            for r in data.get(table, []):
                cols = [c for c in r if c != "id" and c in cols_ok]
                cur = db.execute(f"INSERT OR IGNORE INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [r[c] for c in cols])
                counts[table] = counts.get(table, 0) + cur.rowcount
        db.commit()
    finally:
        db.close()
    return counts
