"""Quota pacing for the Gmail API.

Facts (developers.google.com/workspace/gmail/api/reference/quota, last
updated 2026-10-16): each user may spend 6,000 quota units per minute per
Cloud project; messages.list costs 5 units, messages.get 20 units, or 60 with
format=full/raw. Projects that used the API between November 2025 and April
2026 keep their earlier, higher quotas, so the limit is a setting
(``gmail_quota_per_minute``) with the documented value as the default.

What that means for speed: format=full costs 3x format=metadata, so reading
every message in full is capped at 100 messages a minute (3,000 messages =
30 minutes, which is what a real scan measured). Reading headers first and
bodies only for billing candidates raises the cap to about 300 a minute.
That cap is Google's, not ours: no client can read faster on such a project.

Pacing: a sliding 60-second window. A request waits until the units spent in
the last 60 seconds plus its own fit under the budget. The budget is 95% of
the quota: the 5% margin absorbs clock differences with Google's accounting
and a little use by other apps on the same project. (A token bucket was
measured first and rejected: its burst plus a full minute of refill spends
more than the quota in some 60-second windows, which caused 84 rate-limit
errors in a 3,000-message benchmark; the sliding window causes none.)

If Google still says "rate limit" (another app shares the quota, or the
project's quota is lower), the budget halves; each clean request then raises
it by 5% back towards the configured limit.
"""
from __future__ import annotations

import time
from collections import deque

DEFAULT_QUOTA_PER_MINUTE = 6000
UNITS = {"list": 5, "metadata": 20, "full": 60}
WINDOW = 60.0
SAFETY = 0.95
RECOVERY = 1.05
FLOOR_FRACTION = 1 / 16


class Pacer:
    def __init__(self, units_per_minute: float = DEFAULT_QUOTA_PER_MINUTE, clock=time.monotonic, sleep=time.sleep):
        self.limit = float(units_per_minute) * SAFETY       # units per WINDOW
        self.budget = self.limit
        self.clock, self.sleep = clock, sleep
        self.spent: deque[tuple[float, float]] = deque()
        self.waited = 0.0
        self.throttles = 0

    @property
    def rate(self) -> float:      # kept for callers/tests: current budget per minute
        return self.budget

    def _used(self, now: float) -> float:
        while self.spent and now - self.spent[0][0] >= WINDOW:
            self.spent.popleft()
        return sum(u for _, u in self.spent)

    def take(self, units: float) -> None:
        """Block until ``units`` fit in the sliding-window budget. A single
        request larger than the budget waits for an empty window."""
        units = float(units)
        while True:
            now = self.clock()
            used = self._used(now)
            if used + units <= self.budget or not self.spent:
                self.spent.append((now, units))
                return
            # wait until enough of the oldest spending leaves the window
            need, freed, wake = used + units - self.budget, 0.0, now
            for t, u in self.spent:
                freed += u
                wake = t + WINDOW
                if freed >= need:
                    break
            wait = max(0.01, wake - now)
            self.waited += wait
            self.sleep(wait)

    def seconds_for(self, units: float) -> float:
        """Seconds until ``units`` more units have all been taken, if every
        request is sent as soon as it fits (round trips ignored). Units free
        in the window go at once; after that the window refills only as old
        spending turns 60 s old, so the rest goes in whole-window steps
        (``budget`` per WINDOW) that start when the current entries expire,
        not as a smooth rate. (A smooth units/rate estimate was 12% early on
        a 1,000-message benchmark scan because the last step waits for a
        window boundary.)"""
        now = self.clock()
        used = self._used(now)
        rest = float(units) - max(0.0, self.budget - used)
        if rest <= 0:
            return 0.0
        cycles, rest = divmod(rest, self.budget)
        if rest == 0:           # ends exactly on a window step
            cycles, rest = cycles - 1, self.budget
        wake, freed = WINDOW, 0.0   # spending made from now on expires a WINDOW later
        for t, u in self.spent:
            freed += u
            if freed >= rest:
                wake = t + WINDOW - now
                break
        return cycles * WINDOW + max(0.0, wake)

    def settle(self) -> None:
        """The request paid for by the last take() has just finished. Google
        counts its units somewhere between sending and the answer, so they
        are re-dated to now: they then leave our window no earlier than they
        leave Google's. (Dated at take() time they left ~one round trip early;
        with requests larger than the 5% margin that let a burst through and
        caused 429s on the benchmark.)"""
        if self.spent:
            _, units = self.spent.pop()
            self.spent.append((self.clock(), units))

    def success(self) -> None:
        self.budget = min(self.limit, self.budget * RECOVERY)

    def throttled(self) -> None:
        self.throttles += 1
        self.budget = max(self.limit * FLOOR_FRACTION, self.budget / 2)
