"""Built-in log tools (BaseTool implementations): strictly read-only, paths
validated against the registry whitelist."""

from __future__ import annotations

from typing import Any, Dict, List

from app.tools import log_reader
from app.tools.plugin_base import BaseTool


class LogTailTool(BaseTool):
    name = "log_tail"
    description = "Last n lines of a log file (time ascending, optional keyword filter)"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Log file path (must be whitelisted)"},
            "n": {"type": "integer", "description": "Number of lines, default 50"},
            "keyword": {"type": "string", "description": "Optional keyword filter"},
        },
        "required": ["path"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: Dict[str, Any]) -> List[str]:
        checked = self._registry._check_path(str(params.get("path", "")))
        n = int(params.get("n", 50))
        keyword = params.get("keyword")
        return log_reader.tail(
            checked, n, keyword=keyword, max_scan_bytes=self._registry.max_scan_bytes
        )


class LogGrepTool(BaseTool):
    name = "log_grep"
    description = "Regex-matched log lines from the tail window (time ascending)"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Log file path (must be whitelisted)"},
            "pattern": {"type": "string", "description": "Valid regular expression"},
            "limit": {"type": "integer", "description": "Max lines returned, default 100"},
        },
        "required": ["path", "pattern"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: Dict[str, Any]) -> List[str]:
        checked = self._registry._check_path(str(params.get("path", "")))
        pattern = str(params.get("pattern", ""))
        limit = int(params.get("limit", 100))
        return log_reader.grep(
            checked, pattern, limit=limit, max_scan_bytes=self._registry.max_scan_bytes
        )


class ListFilesTool(BaseTool):
    name = "list_files"
    description = "List a whitelisted directory (names/metadata only, no file contents)"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {"dir": {"type": "string", "description": "Directory (must be whitelisted)"}},
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: Dict[str, Any]) -> List[str]:
        roots = self._registry.readonly_roots
        raw_dir = params.get("dir")
        target = str(raw_dir) if raw_dir else (str(roots[0]) if roots else "")
        checked = self._registry._check_path(target)
        if not checked.is_dir():
            raise NotADirectoryError("not a directory: {}".format(checked))
        entries: List[str] = []
        for child in sorted(checked.iterdir(), key=lambda p: p.name):
            if child.is_dir():
                entries.append("dir\t{}".format(child.name))
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = -1
                entries.append("file\t{}\t{}".format(child.name, size))
        return entries


class LogRollingTailTool(BaseTool):
    name = "log_rolling_tail"
    description = "Aggregated tail lines across rotated log files (time ascending)"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "dir": {"type": "string", "description": "Log directory (must be whitelisted)"},
            "pattern": {"type": "string", "description": "Glob pattern, default moray.err.log*"},
            "n": {"type": "integer", "description": "Number of lines, default 50"},
            "max_files": {"type": "integer", "description": "Max files aggregated, default 5"},
        },
        "required": ["dir"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: Dict[str, Any]) -> List[str]:
        checked = self._registry._check_path(str(params.get("dir", "")))
        pattern = str(params.get("pattern") or "moray.err.log*")
        n = int(params.get("n", 50))
        max_files = int(params.get("max_files", 5))
        return log_reader.read_rolling(
            checked, pattern, n, max_files=max_files, max_scan_bytes=self._registry.max_scan_bytes
        )
