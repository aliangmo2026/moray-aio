"""内置日志类工具（BaseTool 实现）：全部只读，路径经注册表白名单校验。"""

from __future__ import annotations

from app.tools import log_reader
from app.tools.plugin_base import BaseTool


class LogTailTool(BaseTool):
    name = "log_tail"
    description = "日志尾部 n 行（时间正序，可按关键词过滤）"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "日志文件路径（须在白名单内）"},
            "n": {"type": "integer", "description": "返回行数，默认 50"},
            "keyword": {"type": "string", "description": "可选关键词过滤"},
        },
        "required": ["path"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: dict):
        checked = self._registry._check_path(str(params.get("path", "")))
        n = int(params.get("n", 50))
        keyword = params.get("keyword")
        return log_reader.tail(
            checked, n, keyword=keyword, max_scan_bytes=self._registry.max_scan_bytes
        )


class LogGrepTool(BaseTool):
    name = "log_grep"
    description = "尾部正则匹配的日志行（时间正序）"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "日志文件路径（须在白名单内）"},
            "pattern": {"type": "string", "description": "合法正则表达式"},
            "limit": {"type": "integer", "description": "最多返回行数，默认 100"},
        },
        "required": ["path", "pattern"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: dict):
        checked = self._registry._check_path(str(params.get("path", "")))
        pattern = str(params.get("pattern", ""))
        limit = int(params.get("limit", 100))
        return log_reader.grep(
            checked, pattern, limit=limit, max_scan_bytes=self._registry.max_scan_bytes
        )


class ListFilesTool(BaseTool):
    name = "list_files"
    description = "列出只读白名单目录内容（不读文件内容）"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {"dir": {"type": "string", "description": "目录（须在白名单内）"}},
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: dict):
        roots = self._registry.readonly_roots
        raw_dir = params.get("dir")
        target = str(raw_dir) if raw_dir else (str(roots[0]) if roots else "")
        checked = self._registry._check_path(target)
        if not checked.is_dir():
            raise NotADirectoryError("not a directory: {}".format(checked))
        entries = []
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
    description = "跨滚动日志文件聚合的尾部行（时间正序）"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "dir": {"type": "string", "description": "日志目录（须在白名单内）"},
            "pattern": {"type": "string", "description": "glob 模式，默认 moray.err.log*"},
            "n": {"type": "integer", "description": "返回行数，默认 50"},
            "max_files": {"type": "integer", "description": "最多聚合文件数，默认 5"},
        },
        "required": ["dir"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: dict):
        checked = self._registry._check_path(str(params.get("dir", "")))
        pattern = str(params.get("pattern") or "moray.err.log*")
        n = int(params.get("n", 50))
        max_files = int(params.get("max_files", 5))
        return log_reader.read_rolling(
            checked, pattern, n, max_files=max_files, max_scan_bytes=self._registry.max_scan_bytes
        )
