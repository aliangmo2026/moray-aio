"""log_reader read-only behavior tests: tail windows, time order, truncated-line
dropping, error paths, and the tool whitelist."""

from __future__ import annotations

import pytest

from app.tools.log_reader import (
    LogReadError,
    check_readable,
    grep,
    recent_errors,
    tail,
)
from app.tools.registry import ToolRegistry


def _write(tmp_path, lines, name="moray.err.log"):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_recent_errors_returns_last_n_in_order(tmp_path):
    lines = ["2026-10-24 10:{:02d}:00 INFO ok {}".format(m, m) for m in range(20)]
    lines += ["2026-10-24 11:{:02d}:00 ERROR boom {}".format(m, m) for m in range(5)]
    path = _write(tmp_path, lines)

    hits = recent_errors(path, limit=3)

    assert len(hits) == 3
    assert [h.text for h in hits] == [
        "2026-10-24 11:02:00 ERROR boom 2",
        "2026-10-24 11:03:00 ERROR boom 3",
        "2026-10-24 11:04:00 ERROR boom 4",
    ]
    offsets = [h.offset for h in hits]
    assert offsets == sorted(offsets)
    assert all(0 <= o < path.stat().st_size for o in offsets)


def test_recent_errors_missing_file_raises(tmp_path):
    with pytest.raises(LogReadError):
        recent_errors(tmp_path / "nope.log", limit=5)


def test_recent_errors_directory_raises(tmp_path):
    with pytest.raises(LogReadError):
        recent_errors(tmp_path, limit=5)


def test_tail_with_keyword_keeps_order(tmp_path):
    lines = [
        "2026-10-24 10:00:00 INFO a",
        "2026-10-24 10:01:00 ERROR b",
        "2026-10-24 10:02:00 INFO c",
        "2026-10-24 10:03:00 ERROR d",
    ]
    path = _write(tmp_path, lines)
    assert tail(path, 10, keyword="ERROR") == [
        "2026-10-24 10:01:00 ERROR b",
        "2026-10-24 10:03:00 ERROR d",
    ]
    assert tail(path, 2) == [
        "2026-10-24 10:02:00 INFO c",
        "2026-10-24 10:03:00 ERROR d",
    ]


def test_grep_regex_time_ascending(tmp_path):
    lines = [
        "2026-10-24 10:00:00 ERROR Connection refused A",
        "2026-10-24 10:01:00 INFO fine",
        "2026-10-24 10:02:00 ERROR Connection refused B",
    ]
    path = _write(tmp_path, lines)
    hits = grep(path, r"Connection refused", limit=10)
    assert hits == [
        "2026-10-24 10:00:00 ERROR Connection refused A",
        "2026-10-24 10:02:00 ERROR Connection refused B",
    ]


def test_truncated_first_line_is_dropped(tmp_path):
    # a 100-byte giant line plus one normal ERROR; with a 50-byte window the window
    # starts mid-giant-line, so the first incomplete line must be dropped
    path = _write(tmp_path, ["A" * 100, "ERROR real"])
    hits = recent_errors(path, limit=10, max_scan_bytes=50)
    assert [h.text for h in hits] == ["ERROR real"]
    assert tail(path, 10, max_scan_bytes=50) == ["ERROR real"]
    # only the "real" line contains ERROR in the whole file
    assert len(recent_errors(path, limit=10)) == 1


def test_check_readable_reports_without_reading(tmp_path):
    path = _write(tmp_path, ["hello"])
    info = check_readable(path)
    assert info["exists"] is True
    assert info["readable"] is True
    assert info["size_bytes"] == path.stat().st_size

    missing = check_readable(tmp_path / "ghost.log")
    assert missing["exists"] is False
    assert missing["readable"] is False
    assert missing["size_bytes"] is None


def test_registry_whitelist_rejects_outside_paths(tmp_path):
    inside = _write(tmp_path, ["ERROR x"])
    registry = ToolRegistry([str(tmp_path)], max_scan_bytes=1024 * 1024)

    # inside the whitelist: works
    assert registry.call("log_tail", {"path": str(inside), "n": 10}) == ["ERROR x"]

    # outside the whitelist: PermissionError
    outside = str(tmp_path.parent / ("outside_" + inside.name))
    with pytest.raises(PermissionError):
        registry._check_path(outside)

    # directory traversal: resolves outside the root, also rejected
    with pytest.raises(PermissionError):
        registry._check_path(str(tmp_path / ".." / "escape.log"))

    # unknown tool: ValueError
    with pytest.raises(ValueError):
        registry.call("no_such_tool", {})
