"""Scan progress: counts, the date reached, and an ETA that does not jump.

ETA method, and why:
* Rate = messages read during the last RATE_WINDOW seconds / that time.
  30 seconds spans at least three Gmail batches at the default pacing (one
  batch of 50 every ~10 s under a 6,000 units/minute quota), so a single
  slow or fast batch cannot swing it, yet it follows a real slow-down
  (throttling, a congested network) within half a minute.
* No ETA ("estimating…") until MIN_SECONDS of reading and MIN_DONE
  messages: the first seconds are dominated by connection set-up.
* The shown ETA is smoothed (exponential, ALPHA = 0.3 per update) and may
  never rise by more than MAX_RISE of its previous value per update, so the
  number moves steadily instead of jumping; it is never negative.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime

RATE_WINDOW = 30.0
MIN_SECONDS = 5.0
MIN_DONE = 20
ALPHA = 0.3
MAX_RISE = 0.25


class ScanControl:
    """Cancel + pause for a running scan. ``is_set()`` means "cancel"
    (so it can be passed wherever a cancel ``threading.Event`` is expected)."""

    def __init__(self):
        self._cancel = threading.Event()
        self._running = threading.Event()
        self._running.set()

    def cancel(self):
        self._cancel.set()
        self._running.set()   # wake a paused scan so it can stop

    def set(self):  # threading.Event compatibility
        self.cancel()

    def is_set(self) -> bool:
        return self._cancel.is_set()

    def pause(self):
        self._running.clear()

    def resume(self):
        self._running.set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def wait_if_paused(self, timeout: float | None = None) -> None:
        self._running.wait(timeout)


def checkpoint(control) -> None:
    """Called by sources between units of work: honour pause, then cancel."""
    from .sources.base import ScanCancelled
    if control is None:
        return
    waiter = getattr(control, "wait_if_paused", None)
    if waiter is not None:
        waiter()
    if control.is_set():
        raise ScanCancelled("Scan cancelled")


class Tracker:
    """Collects progress reports from a source and computes rate/ETA."""

    def __init__(self, clock=time.monotonic, since: datetime | None = None):
        self.clock = clock
        self.since = since
        self.phase = "counting"
        self.done = 0
        self.total: int | None = None
        self.current_date: datetime | None = None
        self.senders = 0
        self.started = clock()
        self._read_started: float | None = None
        self._samples: deque[tuple[float, int]] = deque()
        self._eta: float | None = None
        self.capped = False
        self.note = ""
        self.unit = "messages"

    # sources call this
    def __call__(self, done: int, total: int | None = None, current_date: datetime | None = None,
                 phase: str | None = None, eta_hint: float | None = None, note: str | None = None, **_):
        """``eta_hint``: a source that knows its own bound (the Gmail API is
        quota-bound) passes seconds left directly instead of the rate guess."""
        now = self.clock()
        if note is not None:
            self.note = note
        if phase:
            self.phase = phase
        elif done:
            self.phase = "reading"
        if total is not None:
            self.total = total
        if current_date is not None and (self.current_date is None or current_date < self.current_date):
            self.current_date = current_date   # newest-first: the oldest date reached so far
        if done and self._read_started is None:
            self._read_started = now
        self.done = done
        self._samples.append((now, done))
        while self._samples and now - self._samples[0][0] > RATE_WINDOW:
            self._samples.popleft()
        if eta_hint is not None and self.total is not None:
            self._eta = max(0.0, float(eta_hint))
        else:
            self._update_eta(now)

    def rate(self) -> float | None:
        """Messages per second over the rolling window."""
        if len(self._samples) < 2:
            return None
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        if t1 - t0 <= 0:
            return None
        return max(0.0, (d1 - d0) / (t1 - t0))

    def _update_eta(self, now: float):
        if self.total is None or self._read_started is None:
            self._eta = None
            return
        remaining = max(0, self.total - self.done)
        if remaining == 0:
            self._eta = 0.0
            return
        if now - self._read_started < MIN_SECONDS or self.done < MIN_DONE:
            return
        r = self.rate()
        if not r:
            return
        raw = remaining / r
        if self._eta is None:
            self._eta = raw
        else:
            smoothed = self._eta + ALPHA * (raw - self._eta)
            self._eta = min(smoothed, self._eta * (1 + MAX_RISE) + 1.0)
        self._eta = max(0.0, self._eta)

    @property
    def eta_seconds(self) -> float | None:
        return None if self._eta is None else round(self._eta, 1)

    def percent(self) -> int | None:
        if not self.total:
            return None
        return min(100, int(self.done * 100 / self.total))

    def snapshot(self) -> dict:
        return {
            "phase": self.phase, "done": self.done, "total": self.total, "percent": self.percent(),
            "eta_seconds": self.eta_seconds, "eta_text": eta_text(self.eta_seconds, self.phase),
            "rate": None if self.rate() is None else round(self.rate(), 2),
            "current_date": self.current_date.date().isoformat() if self.current_date else None,
            "since": self.since.date().isoformat() if self.since else None,
            "senders": self.senders, "elapsed": round(self.clock() - self.started, 1),
            "capped": self.capped, "note": self.note, "unit": self.unit,
        }


def eta_text(seconds: float | None, phase: str = "reading") -> str:
    if phase == "counting":
        return "counting emails…"
    if seconds is None:
        return "estimating…"
    if seconds < 45:
        return "less than a minute left"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} min left"
    h, m = divmod(minutes, 60)
    return f"about {h} h {m} min left"


def describe(snap: dict) -> str:
    """One line for the CLI and logs, e.g.
    '1,240 of 3,180 emails · 39% · about 4 min left · back to 12 Mar 2026'."""
    parts = []
    if snap.get("unit") == "bytes":
        mb = lambda n: f"{n / 1e6:,.0f} MB"
        parts.append(f"{mb(snap['done'])} of {mb(snap['total'])}" if snap["total"] else f"{mb(snap['done'])} read")
        if snap["percent"] is not None:
            parts.append(f"{snap['percent']}%")
    elif snap["total"] is not None:
        parts.append(f"{snap['done']:,} of {snap['total']:,} emails")
        if snap["percent"] is not None:
            parts.append(f"{snap['percent']}%")
    else:
        parts.append(f"{snap['done']:,} emails")
    if snap.get("phase") == "paused":
        parts.append("paused")
        return " · ".join(parts)
    parts.append(snap["eta_text"])
    if snap["current_date"]:
        d = datetime.fromisoformat(snap["current_date"])
        parts.append(f"back to {d.day} {d.strftime('%b %Y')}")
    return " · ".join(parts)
