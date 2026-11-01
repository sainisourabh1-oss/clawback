"""Offline source: Google Takeout export (.zip/.tgz or the .mbox inside it),
any .mbox file, or a folder of .eml files. No account, no OAuth, read-only.

The parser streams: it reads one line at a time, keeps at most
``MAX_MESSAGE_BYTES`` of each message (the rest is attachments), and never
loads the whole export into memory, so multi-gigabyte exports work.

Google Takeout adds an ``X-Gmail-Labels`` header (e.g. ``Inbox,Unread,Category
Promotions``), which gives read/unread state and the Gmail tab of each message.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import tarfile
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path

from ..extract import MAX_MESSAGE_BYTES, MessageInfo, from_bytes
from .base import ScanCancelled, Source, SourceError

MAX_LINE_BYTES = 1 << 20
_FROM_LINE = re.compile(rb"^From \S")
_ESCAPED_FROM = re.compile(rb"^>+From ")
_LABEL_MAP = {"inbox": "INBOX", "spam": "SPAM", "trash": "TRASH", "sent": "SENT", "unread": "UNREAD",
              "category promotions": "CATEGORY_PROMOTIONS", "category updates": "CATEGORY_UPDATES",
              "category social": "CATEGORY_SOCIAL", "category forums": "CATEGORY_FORUMS",
              "category personal": "CATEGORY_PERSONAL", "chat": "CHAT", "draft": "DRAFT", "drafts": "DRAFT"}
# Mail the user wrote or that the provider already binned is not "incoming
# mail to clean up", matching the Gmail source's default query.
SKIP_LABELS = {"SPAM", "TRASH", "SENT", "CHAT", "DRAFT"}


def split_mbox(stream: io.BufferedIOBase) -> Iterator[tuple[int, bytes]]:
    """Yield (offset, bytes) per message from an mbox byte stream.

    Handles mboxo/mboxrd quoting (">From " lines lose one ">"), CRLF exports
    and a missing trailing newline. Each message is capped at MAX_MESSAGE_BYTES.
    """
    buf: list[bytes] = []
    size = 0
    offset = start = 0
    prev_blank = True
    in_msg = False
    # readline with a limit: an export with no newline for gigabytes (a
    # damaged file or a zip bomb) is read in pieces, never as one line.
    for line in iter(lambda: stream.readline(MAX_LINE_BYTES), b""):
        is_sep = prev_blank and _FROM_LINE.match(line) is not None
        if is_sep:
            if in_msg:
                yield start, b"".join(buf)
            buf, size, in_msg, start = [], 0, True, offset
        elif in_msg:
            if _ESCAPED_FROM.match(line):
                line = line[1:]
            if size < MAX_MESSAGE_BYTES:
                buf.append(line)
                size += len(line)
        offset += len(line)
        prev_blank = line in (b"\n", b"\r\n")
    if in_msg and buf:
        yield start, b"".join(buf)


def _labels_of(raw: bytes) -> tuple[set[str], bool | None]:
    head = raw[:20000]
    m = re.search(rb"(?im)^X-Gmail-Labels:[ \t]*(.*(?:\r?\n[ \t].*)*)", head)
    if not m:
        return set(), None
    text = re.sub(rb"\r?\n[ \t]+", b" ", m.group(1)).decode("utf-8", errors="replace")
    labels = set()
    for item in text.split(","):
        item = item.strip().strip('"')
        labels.add(_LABEL_MAP.get(item.lower(), "USER:" + item if item else ""))
    labels.discard("")
    return labels, "UNREAD" in labels


_DATE_HDR = re.compile(rb"(?im)^Date:[ \t]*(.+(?:\r?\n[ \t].*)*)")
_RECEIVED_DATE = re.compile(rb"(?im)^Received:[^;]*(?:\r?\n[ \t][^;]*)*;[ \t]*(.+)")


def quick_date(raw: bytes):
    """Date of a raw message from its headers only (Date:, else the newest
    Received: timestamp), without parsing the whole message: lets a dated
    import skip older mail cheaply. None when neither can be read."""
    from email.utils import parsedate_to_datetime
    from datetime import timezone
    head = raw[:20000]
    end = re.search(rb"\r?\n\r?\n", head)
    head = head[:end.start()] if end else head
    for pat in (_DATE_HDR, _RECEIVED_DATE):
        m = pat.search(head)
        if not m:
            continue
        text = re.sub(rb"\r?\n[ \t]+", b" ", m.group(1)).decode("ascii", errors="replace").strip()
        try:
            d = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            continue
        if d is not None:
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _message_id(raw: bytes, fallback: str) -> str:
    m = re.search(rb"(?im)^Message-ID:[ \t]*(<[^>\r\n]+>)", raw[:20000])
    if m:
        return "mid:" + m.group(1).decode("ascii", errors="replace")
    return "sha1:" + hashlib.sha1(raw[:4096] + fallback.encode()).hexdigest()


class _CountingReader(io.RawIOBase):
    """Wrap a stream to report bytes consumed for progress."""

    def __init__(self, inner, counter: list[int]):
        self.inner, self.counter = inner, counter

    def readable(self):
        return True

    def readinto(self, b):
        data = self.inner.read(len(b))
        n = len(data)
        b[:n] = data
        self.counter[0] += n
        return n


class MboxSource(Source):
    kind = "mbox"

    def __init__(self, path: str | os.PathLike, account: str | None = None, include_all: bool = False):
        p = Path(path).expanduser()
        if not p.exists():
            raise SourceError(f"Not found: {p}")
        super().__init__(account or f"import:{p.name}")
        self.path = p
        self.include_all = include_all
        self.capabilities = {"one-click", "link", "mailto-client"}
        self.skipped = 0

    # -------------------------------------------------------------- inputs
    def _streams(self) -> Iterator[tuple[str, io.BufferedIOBase, int]]:
        p = self.path
        if p.is_dir():
            files = sorted(x for x in p.rglob("*") if x.suffix.lower() in {".eml", ".mbox"} and x.is_file())
            if not files:
                raise SourceError(f"No .mbox or .eml files in {p}")
            for f in files:
                with open(f, "rb") as fh:
                    yield str(f), fh, f.stat().st_size
            return
        name = p.name.lower()
        if name.endswith(".zip"):
            with zipfile.ZipFile(p) as z:
                members = [i for i in z.infolist() if i.filename.lower().endswith((".mbox", ".eml"))]
                if not members:
                    raise SourceError("This .zip has no .mbox files. In Google Takeout, select 'Mail' and export again.")
                for info in members:
                    with z.open(info) as fh:
                        yield info.filename, io.BufferedReader(fh), info.file_size
            return
        if name.endswith((".tgz", ".tar.gz", ".tar")):
            with tarfile.open(p, "r:*") as t:
                found = False
                for member in t:
                    if member.isfile() and member.name.lower().endswith((".mbox", ".eml")):
                        fh = t.extractfile(member)
                        if fh is not None:
                            found = True
                            yield member.name, io.BufferedReader(fh), member.size
                if not found:
                    raise SourceError("This archive has no .mbox files.")
            return
        with open(p, "rb") as fh:
            yield str(p), fh, p.stat().st_size

    # -------------------------------------------------------------- scan
    def _chunks(self, consumed):
        for name, fh, _size in self._streams():
            reader = io.BufferedReader(_CountingReader(fh, consumed), buffer_size=1 << 16)
            if name.lower().endswith(".eml"):
                yield name, 0, reader.read(MAX_MESSAGE_BYTES)
            else:
                for offset, raw in split_mbox(reader):
                    yield name, offset, raw

    def _wanted(self, raw: bytes, since) -> tuple[bool, object]:
        """(kept by the window and the label rules, its header date)."""
        d = quick_date(raw)
        if since is not None and d is not None and d < since:
            return False, d
        labels, _ = _labels_of(raw)
        return not (labels & SKIP_LABELS and not self.include_all), d

    def iter_messages(self, limit: int | None = None, progress=None, cancel=None, since=None,
                      skip_ids=None) -> Iterator[MessageInfo]:
        """Exports are not ordered by date, so ``since`` filters rather than
        stops: older messages are skipped from their headers alone.

        ``limit`` means the newest ``limit`` emails (by their Date header, as
        every other source reads newest first). An export is in no particular
        order, so that takes two passes over the file: the first reads only
        dates to find the newest ``limit``, the second reads those."""
        from ..progress import checkpoint
        self.capped = False
        self.older = 0
        streams = list(self._streams_meta())
        total_bytes = sum(size for _, size in streams) or None
        consumed = [0]
        keep = None
        passes = 1
        if limit is not None:
            passes = 2
            ranked = []
            for name, offset, raw in self._chunks(consumed):
                checkpoint(cancel)
                ok, d = self._wanted(raw, since)
                if ok:
                    ranked.append((d.timestamp() if d is not None else float("-inf"), len(ranked), name, offset))
                if progress and len(ranked) % 500 == 1:
                    progress(consumed[0], total_bytes and 2 * total_bytes)
            ranked.sort(key=lambda r: (-r[0], r[1]))
            self.capped = len(ranked) > limit
            keep = {(r[2], r[3]) for r in ranked[:limit]}
        count = 0
        for name, offset, raw in self._chunks(consumed):
            checkpoint(cancel)
            if keep is not None and (name, offset) not in keep:
                continue
            if since is not None:
                d = quick_date(raw)
                if d is not None and d < since:
                    self.older += 1
                    continue
            labels, unread = _labels_of(raw)
            if labels & SKIP_LABELS and not self.include_all:
                self.skipped += 1
                continue
            info = from_bytes(raw, _message_id(raw, f"{name}:{offset}"), unread=unread, labels=labels, folder=name)
            count += 1
            if progress and (count % 50 == 0 or count == 1):
                progress(consumed[0], total_bytes and passes * total_bytes)
            yield info
        if progress:
            progress(total_bytes * passes if total_bytes else consumed[0], total_bytes and passes * total_bytes)

    def _streams_meta(self):
        p = self.path
        if p.is_dir():
            return [(str(f), f.stat().st_size) for f in p.rglob("*") if f.suffix.lower() in {".eml", ".mbox"} and f.is_file()]
        if p.name.lower().endswith(".zip"):
            with zipfile.ZipFile(p) as z:
                return [(i.filename, i.file_size) for i in z.infolist() if i.filename.lower().endswith((".mbox", ".eml"))]
        if p.name.lower().endswith((".tgz", ".tar.gz", ".tar")):
            return [(str(p), 0)]   # uncompressed size unknown without a full pass: show a count only
        return [(str(p), p.stat().st_size)]

    def describe(self) -> dict:
        d = super().describe()
        d["path"] = str(self.path)
        return d
