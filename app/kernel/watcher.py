"""Log watcher: os.stat polling for appended content (cross-platform; Windows has
no inotify and polling keeps behavior identical everywhere).

Hard rules:
- Opened read-only ('rb'); never writes, truncates or renames the watched log;
- Any exception inside the loop is logged and swallowed — the thread never dies;
- logrotate truncation (smaller file) resets the offset to 0; deleted-and-recreated
  files (inode change) are re-detected.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Callable, List, Optional, Tuple

from app.tools.log_reader import CONTINUATION_RE

logger = logging.getLogger("moray_aio.watcher")

DEFAULT_ERROR_PATTERN = r"\bERROR\b"
_MAX_RECORD_BYTES = 16384


class LogWatcher:
    """Polling log tailer: new lines are grouped into multi-line records (stack
    traces stay whole) and records matching error_pattern are pushed to the callback."""

    def __init__(
        self,
        log_path: str,
        on_lines: Callable[[List[str]], None],
        poll_interval: float = 2.0,
        error_pattern: str = DEFAULT_ERROR_PATTERN,
    ):
        self.log_path = str(log_path)
        self.on_lines = on_lines
        self.poll_interval = max(0.05, float(poll_interval))
        self._pattern = re.compile(error_pattern)
        self._offset = 0
        self._sig: Optional[Tuple[int, int]] = None
        self._carry: Optional[str] = None  # trailing record that may still grow
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="moray-log-watcher", daemon=True)
        self._thread.start()
        logger.info("log watcher started: %s (poll=%.2fs)", self.log_path, self.poll_interval)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        logger.info("log watcher stopped: %s", self.log_path)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------- main loop ----------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                lines = self._read_new_lines()
                matched = self._process_lines(lines)
                if matched:
                    self.on_lines(matched)
            except Exception:
                logger.exception("log watcher tick failed (ignored)")
            self._stop.wait(self.poll_interval)

    # ---------- record grouping ----------

    def _process_lines(self, new_lines: List[str]) -> List[str]:
        """Group raw lines into multi-line records and keep pattern matches.

        A trailing record whose last line looks like a continuation (a stack trace
        still being written) is carried into the next poll so frames arriving in
        the next chunk merge into it; a quiet poll flushes the carry.
        """
        if not new_lines:
            if self._carry is not None:
                record, self._carry = self._carry, None
                return [record] if self._pattern.search(record) else []
            return []

        buffer: List[str] = ([self._carry] if self._carry is not None else []) + new_lines
        self._carry = None
        records: List[str] = []
        current: Optional[str] = None
        for line in buffer:
            if (
                current is not None
                and CONTINUATION_RE.match(line)
                and len(current) + len(line) < _MAX_RECORD_BYTES
            ):
                current += "\n" + line
                continue
            if current is not None:
                records.append(current)
            current = line
        if current is not None:
            ended_on_continuation = bool(new_lines) and bool(CONTINUATION_RE.match(new_lines[-1]))
            if ended_on_continuation and self._pattern.search(current):
                self._carry = current  # the stack may continue in the next chunk
            else:
                records.append(current)
        return [r for r in records if self._pattern.search(r)]

    # ---------- reading (read-only) ----------

    def _read_new_lines(self) -> List[str]:
        """Read complete new lines since the last offset; rotation/reset auto-handles."""
        try:
            st = os.stat(self.log_path)
        except OSError:
            self._sig = None
            return []  # file temporarily missing: re-probe next tick
        sig = (getattr(st, "st_ino", 0), getattr(st, "st_dev", 0))
        size = st.st_size
        if self._sig is not None and sig != self._sig:
            self._offset = 0  # file was deleted and recreated
        if size < self._offset:
            self._offset = 0  # logrotate truncation
        self._sig = sig
        if size <= self._offset:
            return []
        with open(self.log_path, "rb") as fh:  # read-only binary, never write
            fh.seek(self._offset)
            data = fh.read(size - self._offset)
        if not data:
            return []
        parts = data.split(b"\n")
        tail = parts.pop()  # possibly an incomplete line: not consumed yet
        self._offset += len(data) - len(tail)
        out = []
        for raw in parts:
            text = raw.decode("utf-8", errors="replace").rstrip("\r")
            if text:
                out.append(text)
        return out
