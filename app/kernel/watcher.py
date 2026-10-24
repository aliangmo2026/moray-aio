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

logger = logging.getLogger("moray_aio.watcher")

DEFAULT_ERROR_PATTERN = r"\bERROR\b"


class LogWatcher:
    """Polling log tailer: new lines are split and lines matching error_pattern
    are pushed to the callback."""

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
                if lines:
                    matched = [ln for ln in lines if self._pattern.search(ln)]
                    if matched:
                        self.on_lines(matched)
            except Exception:
                logger.exception("log watcher tick failed (ignored)")
            self._stop.wait(self.poll_interval)

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
