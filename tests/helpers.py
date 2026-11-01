"""Test helpers: build synthetic mbox files from raw messages."""
from __future__ import annotations

import re
from pathlib import Path


def write_mbox(path: Path, raws: list[bytes], crlf: bool = False) -> Path:
    with open(path, "wb") as f:
        for i, raw in enumerate(raws):
            f.write(b"From 1700000000%04d@xxx Mon Jan 01 00:00:00 +0000 2024\n" % i)
            body = re.sub(rb"(?m)^(>*From )", rb">\1", raw.replace(b"\r\n", b"\n"))
            if crlf:
                body = body.replace(b"\n", b"\r\n")
            f.write(body)
            if not body.endswith(b"\n"):
                f.write(b"\n")
            f.write(b"\r\n" if crlf else b"\n")
    return path
