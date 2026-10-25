"""Read-only log reader.

Hard rules:
- Log files are only opened with ``open(path, "rb")`` — never written, truncated,
  renamed or deleted;
- Scans a byte window from the tail (5MB by default) instead of loading whole files;
- When the window starts mid-line, the first incomplete line is dropped;
- Decoding uses errors="replace" so bad bytes never crash the process.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Union

PathLike = Union[str, "os.PathLike"]

DEFAULT_MAX_SCAN_BYTES = 5 * 1024 * 1024


class LogReadError(Exception):
    """Raised when the log file does not exist or is not a regular file."""


@dataclass(frozen=True)
class LogLine:
    """One log line: text plus the byte offset of the line start inside the file."""

    text: str
    offset: int

    def to_dict(self) -> dict:
        return {"text": self.text, "offset": self.offset}


def _require_readable_file(log_path: PathLike) -> Tuple[Path, int]:
    path = Path(log_path)
    if not path.exists():
        raise LogReadError("log file not found: {}".format(path))
    if not path.is_file():
        raise LogReadError("not a regular file: {}".format(path))
    return path, path.stat().st_size


def _scan_window(fh, max_scan_bytes: int) -> List[Tuple[int, str]]:
    """Read the tail byte window and decode it line by line (time ascending).

    When the window starts mid-line the first incomplete line is dropped; when the
    window contains no newline at all (a single line bigger than the window) the
    scan is abandoned — conclusions are never drawn from truncated fragments.
    """
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    scan_start = max(0, size - max(1, int(max_scan_bytes)))
    fh.seek(scan_start)
    data = fh.read(size - scan_start)
    if not data:
        return []

    first_offset = scan_start
    if scan_start > 0:
        newline_at = data.find(b"\n")
        if newline_at < 0:
            return []
        first_offset = scan_start + newline_at + 1
        data = data[newline_at + 1:]

    out: List[Tuple[int, str]] = []
    pos = first_offset
    for raw in data.split(b"\n"):
        line_start = pos
        pos += len(raw) + 1
        # a trailing split artifact (file ends with \n) starts exactly at EOF: drop
        if raw == b"" and line_start == size:
            continue
        # strip the UTF-8 BOM sometimes found in Windows-produced files
        text = raw.decode("utf-8", errors="replace").rstrip("\r").lstrip("\ufeff")
        out.append((line_start, text))
    return out


def _tail_pairs(
    log_path: PathLike, n: int, keyword: Optional[str], max_scan_bytes: int
) -> List[Tuple[int, str]]:
    pairs: List[Tuple[int, str]] = []
    n = max(0, int(n))
    if n == 0:
        return pairs
    path, _size = _require_readable_file(log_path)
    with open(path, "rb") as fh:  # read-only binary mode, never write
        for offset, text in _scan_window(fh, max_scan_bytes):
            if keyword is None or keyword in text:
                pairs.append((offset, text))
    return pairs[-n:]


def tail(
    log_path: PathLike,
    n: int,
    keyword: Optional[str] = None,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
) -> List[str]:
    """Last n lines of the file (time ascending); keyword filters when given."""
    return [text for _offset, text in _tail_pairs(log_path, n, keyword, max_scan_bytes)]


def recent_errors(
    log_path: PathLike,
    limit: int = 10,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
    keyword: str = "ERROR",
) -> List[LogLine]:
    """Latest ``limit`` lines containing ``keyword`` (default "ERROR"), time ascending."""
    return [
        LogLine(text=text, offset=offset)
        for offset, text in _tail_pairs(log_path, limit, keyword, max_scan_bytes)
    ]


def grep(
    log_path: PathLike,
    pattern: str,
    limit: int = 100,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
) -> List[str]:
    """Regex matches inside the tail window (time ascending, last ``limit`` kept)."""
    limit = max(0, int(limit))
    if limit == 0:
        return []
    regex = re.compile(pattern)
    path, _size = _require_readable_file(log_path)
    hits: List[str] = []
    with open(path, "rb") as fh:
        for _offset, text in _scan_window(fh, max_scan_bytes):
            if regex.search(text):
                hits.append(text)
    return hits[-limit:]


def read_rolling(
    log_dir: PathLike,
    pattern: str,
    n: int,
    max_files: int = 5,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
    keyword: Optional[str] = None,
) -> List[str]:
    """Aggregated read across rotated logs: glob-matched files ordered by mtime
    (newest first), tails merged into one time-ascending list.

    - ``pattern`` like "moray.err.log*" matches regular files only;
    - at most ``max_files`` files are read; stops after ``n`` lines;
    - optional ``keyword`` filter (e.g. "ERROR"); empty result when nothing matches;
    - missing/non-directory paths raise LogReadError.
    """
    n = max(0, int(n))
    if n == 0:
        return []
    directory = Path(log_dir)
    if not directory.exists():
        raise LogReadError("log dir not found: {}".format(directory))
    if not directory.is_dir():
        raise LogReadError("not a directory: {}".format(directory))

    files = [p for p in sorted(directory.glob(str(pattern))) if p.is_file()]
    # mtime descending: newest file first
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    files = files[: max(1, int(max_files))]

    batches: List[List[str]] = []  # per-file tail lines (ascending), newest file first
    total = 0
    for path in files:
        remaining = n - total
        if remaining <= 0:
            break
        pairs = _tail_pairs(path, remaining, keyword, max_scan_bytes)
        if not pairs:
            continue
        batches.append([text for _offset, text in pairs])
        total += len(batches[-1])

    out: List[str] = []
    for batch in reversed(batches):  # oldest file first -> time ascending
        out.extend(batch)
    return out


def check_readable(log_path: PathLike) -> dict:
    """Metadata-only probe (exists/readable/size_bytes); reads nothing, never raises."""
    path = Path(log_path)
    exists = path.exists()
    readable = False
    size_bytes: Optional[int] = None
    if exists:
        try:
            size_bytes = path.stat().st_size
            readable = path.is_file() and os.access(path, os.R_OK)
        except OSError:
            readable = False
    return {
        "path": str(path),
        "exists": exists,
        "readable": readable,
        "size_bytes": size_bytes,
    }


# ---------------------------------------------------------------- multiline records

# A continuation line starts with whitespace, or is a stack frame like "at com.x.y(...)".
# Real ERROR entries (Java/Python stack traces) span multiple lines; these get folded
# back into the previous record instead of being shred into pieces.
CONTINUATION_RE = re.compile(r"^(?:[ \t]|\s*at\s)")

_MAX_RECORD_BYTES = 16384


def group_multiline(lines: List[str], max_record_bytes: int = _MAX_RECORD_BYTES) -> List[str]:
    """Merge continuation lines (leading whitespace / "at ..." frames) into the
    previous record, keeping multi-line stack traces whole.

    Records are joined with newlines; a record that would exceed
    ``max_record_bytes`` starts a new one instead (memory safety).
    """
    records: List[str] = []
    for line in lines:
        if (
            records
            and CONTINUATION_RE.match(line)
            and len(records[-1]) + len(line) < max_record_bytes
        ):
            records[-1] += "\n" + line
        else:
            records.append(line)
    return records
