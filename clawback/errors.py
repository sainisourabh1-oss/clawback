"""Error text for people: the plain reason and the fix first, the raw text a
server or library produced kept apart as "technical details".

Messages Clawback writes put the raw part after a marker (" Server said: "
for what a mail server answered, " Technical details: " for anything else),
so the CLI and the logs still print one full line while the web app shows
the reason up front and folds the rest. An exception nobody turned into a
sentence ("KeyError: 'token'") gets a generic plain reason, with the
original kept as the technical detail.
"""
from __future__ import annotations

import re

TECH = " Technical details: "
SERVER_SAID = " Server said: "

UNEXPECTED = ("Something went wrong that Clawback did not expect. Try again; if it happens again, "
              "the technical details below help when you report it.")

# "ValueError: ...", "imaplib.IMAP4.error: ...", "WSGITimeoutError: ..."
_RAW = re.compile(r"^[A-Za-z_][\w.]*(?:Error|error|Exception|Timeout\w*|Interrupt|Exit|Warning)\b:?\s?")
# "... (SomeError: text) ..." inside a sentence
_PAREN = re.compile(r"\s*\(([A-Za-z_][\w.]*(?:Error|error|Exception|Timeout\w*)\b:[^()]*)\)")


def explain(text: str | None) -> dict | None:
    """{"reason", "technical"} for an error string (technical may be None)."""
    if not text:
        return None
    text = str(text).strip()
    for marker in (SERVER_SAID, TECH):
        if marker in text:
            reason, _, raw = text.partition(marker)
            label = "Server said: " if marker == SERVER_SAID else ""
            return {"reason": reason.strip(), "technical": (label + raw.strip()) or None}
    if _RAW.match(text):
        return {"reason": UNEXPECTED, "technical": text}
    m = _PAREN.search(text)
    if m:
        reason = (text[:m.start()] + text[m.end():]).strip()
        return {"reason": reason, "technical": m.group(1).strip()}
    return {"reason": text, "technical": None}


def annotate(obj):
    """Add ``error_info`` next to every ``error`` string (and next to the
    ``detail`` of a failed action result) in a JSON-ready structure."""
    if isinstance(obj, dict):
        out = {k: annotate(v) for k, v in obj.items()}
        if isinstance(obj.get("error"), str) and obj["error"] and "error_info" not in obj:
            out["error_info"] = explain(obj["error"])
        if obj.get("ok") is False and isinstance(obj.get("detail"), str) and "detail_info" not in obj:
            out["detail_info"] = explain(obj["detail"])
        return out
    if isinstance(obj, list):
        return [annotate(v) for v in obj]
    return obj
