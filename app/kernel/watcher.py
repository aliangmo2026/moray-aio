"""日志监听器：os.stat 轮询检测新增内容（跨平台，Windows 无 inotify）。

硬约束：
- 只读打开（'rb'），绝不写、截断、改名被监控日志；
- 循环内任何异常只记录日志并继续，绝不让线程崩溃；
- 文件被 logrotate 截断（变小）→ offset 重置 0；被删除重建（inode 变化）→ 重新探测。
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
    """轮询式日志尾随器：新内容按行切分，命中 error_pattern 的行回调出去。"""

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

    # ---------- 生命周期 ----------

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

    # ---------- 主循环 ----------

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

    # ---------- 读取（只读）----------

    def _read_new_lines(self) -> List[str]:
        """读取自上次 offset 以来的新增完整行；旋转/重建自动归零。"""
        try:
            st = os.stat(self.log_path)
        except OSError:
            self._sig = None
            return []  # 文件暂不存在：下一轮重新探测
        sig = (getattr(st, "st_ino", 0), getattr(st, "st_dev", 0))
        size = st.st_size
        if self._sig is not None and sig != self._sig:
            self._offset = 0  # 文件被删除重建
        if size < self._offset:
            self._offset = 0  # logrotate 截断
        self._sig = sig
        if size <= self._offset:
            return []
        with open(self.log_path, "rb") as fh:  # 只读二进制，绝不写
            fh.seek(self._offset)
            data = fh.read(size - self._offset)
        if not data:
            return []
        parts = data.split(b"\n")
        tail = parts.pop()  # 可能是不完整行：不消费
        self._offset += len(data) - len(tail)
        out = []
        for raw in parts:
            text = raw.decode("utf-8", errors="replace").rstrip("\r")
            if text:
                out.append(text)
        return out
