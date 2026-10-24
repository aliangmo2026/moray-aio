"""只读日志读取工具。

硬约束：
- 只以 ``open(path, "rb")`` 打开日志文件，绝不写入/截断/重命名/删除。
- 从文件尾部按字节窗口扫描（默认最多 5MB），不把整份大日志读进内存。
- 窗口起点若落在行中间，丢弃首个不完整行。
- 解码使用 errors="replace"，坏字节不会让程序崩溃。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union

PathLike = Union[str, "os.PathLike"]

DEFAULT_MAX_SCAN_BYTES = 5 * 1024 * 1024


class LogReadError(Exception):
    """日志文件不存在或不是普通文件时抛出。"""


@dataclass(frozen=True)
class LogLine:
    """一行日志：文本 + 该行起始字节在文件中的偏移。"""

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


def _scan_window(fh, max_scan_bytes: int):
    """读取尾部字节窗口，返回 [(行起始偏移, 解码文本), ...]（时间正序）。

    窗口起点落在行中间时丢弃首个不完整行；整个窗口内找不到换行时
    （单行超过扫描窗口）放弃本次扫描，绝不基于截断片段下结论。
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

    out = []
    pos = first_offset
    for raw in data.split(b"\n"):
        line_start = pos
        pos += len(raw) + 1
        # 文件以换行结尾时 split 会产生一个空尾巴，其起点恰为文件末尾，丢弃
        if raw == b"" and line_start == size:
            continue
        # 剥掉 Windows 常见的 UTF-8 BOM（只会出现在文件首个字节）
        text = raw.decode("utf-8", errors="replace").rstrip("\r").lstrip("\ufeff")
        out.append((line_start, text))
    return out


def _tail_pairs(log_path: PathLike, n: int, keyword: Optional[str], max_scan_bytes: int):
    pairs = []
    n = max(0, int(n))
    if n == 0:
        return pairs
    path, _size = _require_readable_file(log_path)
    with open(path, "rb") as fh:  # 只读二进制模式，绝不写入
        for offset, text in _scan_window(fh, max_scan_bytes):
            if keyword is None or keyword in text:
                pairs.append((offset, text))
    return pairs[-n:]


def tail(
    log_path: PathLike,
    n: int,
    keyword: Optional[str] = None,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
) -> list:
    """文件尾部 n 行（时间正序）；keyword 给定时只保留包含该关键词的行。"""
    return [text for _offset, text in _tail_pairs(log_path, n, keyword, max_scan_bytes)]


def recent_errors(
    log_path: PathLike,
    limit: int = 10,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
    keyword: str = "ERROR",
) -> list:
    """尾部扫描窗口内最近 limit 条含 keyword 的日志行（默认 "ERROR"，时间正序）。"""
    return [
        LogLine(text=text, offset=offset)
        for offset, text in _tail_pairs(log_path, limit, keyword, max_scan_bytes)
    ]


def grep(
    log_path: PathLike,
    pattern: str,
    limit: int = 100,
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
) -> list:
    """尾部窗口内的正则匹配行（时间正序，取最后 limit 条）。"""
    limit = max(0, int(limit))
    if limit == 0:
        return []
    regex = re.compile(pattern)
    path, _size = _require_readable_file(log_path)
    hits = []
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
) -> list:
    """滚动日志聚合读取：目录下匹配 glob 的文件按 mtime 新→旧逐个取尾部，合并为时间正序。

    - pattern 形如 "moray.err.log*"，只匹配普通文件；
    - 最多读 max_files 个文件，读满 n 条即停；
    - keyword 可选过滤（如 "ERROR"）；任何文件都不存在匹配时返回空列表；
    - 目录不存在/不是目录抛 LogReadError。
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
    # mtime 降序：最新的文件先读
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    files = files[: max(1, int(max_files))]

    batches = []  # 每个文件的尾部行（文件内时间正序），最新文件在前
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

    out: list = []
    for batch in reversed(batches):  # 旧文件在前 → 时间正序合并
        out.extend(batch)
    return out


def check_readable(log_path: PathLike) -> dict:
    """只探测元数据（exists/readable/size_bytes），不读任何内容，绝不抛异常。"""
    path = Path(log_path)
    exists = path.exists()
    readable = False
    size_bytes = None
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
