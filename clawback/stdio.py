"""Make terminal output encoding-safe.

Real-world failure this guards against: on Windows, when stdout is a legacy
code page (cp1252) or is redirected to a file/pipe, printing "₹" or an emoji
from a subject line raised UnicodeEncodeError *after* the scan had finished,
so the user saw a traceback instead of their results.

Rule: output must never crash because of a character.
- Redirected output (file/pipe) with no explicit PYTHONIOENCODING becomes
  UTF-8, the format every modern tool reads.
- A terminal, or an encoding the user chose explicitly, keeps its encoding,
  but characters it cannot show are replaced ("?") instead of raising.
"""
from __future__ import annotations

import os
import sys

PROBE = "\u20b9\u2757"  # ₹ and ❗, both seen in real subject lines


def _fix(stream):
    if stream is None or not hasattr(stream, "reconfigure"):
        return
    enc = getattr(stream, "encoding", None) or "ascii"
    try:
        PROBE.encode(enc)
        return  # already able to print everything we print
    except (UnicodeEncodeError, LookupError):
        pass
    try:
        isatty = stream.isatty()
    except (OSError, ValueError):
        isatty = False
    try:
        if not isatty and not os.environ.get("PYTHONIOENCODING"):
            stream.reconfigure(encoding="utf-8", errors="replace")
        else:
            stream.reconfigure(errors="replace")
    except (OSError, ValueError):  # detached or closed stream: nothing to do
        pass


def make_safe() -> None:
    _fix(sys.stdout)
    _fix(sys.stderr)
