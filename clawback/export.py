"""Export subscriptions to CSV."""
from __future__ import annotations

import csv
from pathlib import Path

from .aggregate import Subscription

HEADER = ["vendor", "domain", "amount", "currency", "cycle", "monthly_cost", "hits", "confidence"]


def _spreadsheet_safe(value):
    """Neutralize spreadsheet formula prefixes in untrusted text fields."""
    if not isinstance(value, str):
        return value
    probe = value.lstrip(" \t\r\n")
    if probe.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def to_csv(subs: list[Subscription], path: Path) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for s in subs:
            w.writerow([
                _spreadsheet_safe(s.vendor), _spreadsheet_safe(s.domain), s.amount, s.currency, s.cycle,
                s.monthly_cost, s.hits, f"{s.best_confidence:.2f}",
            ])
    return path
