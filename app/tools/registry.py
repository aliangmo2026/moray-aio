"""只读工具注册表：路径白名单校验 + 按名分发。

任何工具调用涉及的 path/dir 参数都必须落在 readonly_roots 内，
用 Path.resolve() + relative_to 校验，越界直接抛 PermissionError。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List

from app.tools import log_reader


class ToolRegistry:
    """把「能读什么」收敛到白名单之内。所有工具都是只读的。"""

    def __init__(self, readonly_roots: List[str], max_scan_bytes: int):
        self.readonly_roots = [Path(r) for r in (readonly_roots or [])]
        self.max_scan_bytes = int(max_scan_bytes)
        self._tools: Dict[str, Callable[..., Any]] = {}
        self._register_defaults()

    # ---- 注册 ----

    def _register_defaults(self) -> None:
        self.register("log_tail", self._tool_log_tail)
        self.register("log_grep", self._tool_log_grep)
        self.register("list_files", self._tool_list_files)
        self.register("log_rolling_tail", self._tool_log_rolling_tail)

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        self._tools[name] = fn

    @property
    def names(self) -> List[str]:
        return sorted(self._tools)

    # ---- 白名单 ----

    def _check_path(self, path_str: str) -> Path:
        """白名单校验：resolve + relative_to；越界抛 PermissionError（fail-closed）。"""
        if not isinstance(path_str, str) or not path_str.strip():
            raise PermissionError("path must be a non-empty string inside readonly roots")
        try:
            resolved = Path(path_str).resolve()
        except OSError as exc:
            raise PermissionError("cannot resolve path: {!r}".format(path_str)) from exc
        for root in self.readonly_roots:
            try:
                root_resolved = root.resolve()
            except OSError:
                root_resolved = root
            try:
                resolved.relative_to(root_resolved)
            except ValueError:
                continue
            return resolved
        raise PermissionError(
            "path outside readonly roots: {!r}; allowed roots: {}".format(
                path_str, [str(r) for r in self.readonly_roots]
            )
        )

    # ---- 工具实现 ----

    def _tool_log_tail(self, path: str, n: int = 50, keyword: str = None) -> list:
        checked = self._check_path(str(path))
        return log_reader.tail(
            checked, int(n), keyword=keyword, max_scan_bytes=self.max_scan_bytes
        )

    def _tool_log_grep(self, path: str, pattern: str, limit: int = 100) -> list:
        checked = self._check_path(str(path))
        return log_reader.grep(
            checked, str(pattern), limit=int(limit), max_scan_bytes=self.max_scan_bytes
        )

    def _tool_list_files(self, dir: str = None) -> list:
        target = str(dir) if dir else (str(self.readonly_roots[0]) if self.readonly_roots else "")
        checked = self._check_path(target)
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

    def _tool_log_rolling_tail(
        self, dir: str, pattern: str = "moray.err.log*", n: int = 50, max_files: int = 5
    ) -> list:
        """滚动日志聚合尾部：dir 必须在白名单内，pattern 为 glob（如 moray.err.log*）。"""
        checked = self._check_path(str(dir))
        return log_reader.read_rolling(
            checked,
            str(pattern or "moray.err.log*"),
            int(n),
            max_files=int(max_files),
            max_scan_bytes=self.max_scan_bytes,
        )

    # ---- 分发 ----

    def call(self, name: str, params: dict = None) -> Any:
        fn = self._tools.get(name)
        if fn is None:
            raise ValueError(
                "unknown tool: {!r}; available: {}".format(name, self.names)
            )
        return fn(**dict(params or {}))

    def describe(self) -> str:
        """给 LLM 的工具清单（只含白名单内工具与参数说明）。"""
        default_dir = str(self.readonly_roots[0]) if self.readonly_roots else "(未配置)"
        return "\n".join(
            [
                "- log_tail(path: str, n: int = 50, keyword: str = null) -> 日志尾部 n 行（时间正序，可按关键词过滤）",
                "- log_grep(path: str, pattern: str, limit: int = 100) -> 尾部正则匹配的日志行（时间正序）",
                "- list_files(dir: str) -> 列出目录内容（默认: {}）".format(default_dir),
                "- log_rolling_tail(dir: str, pattern: str = \"moray.err.log*\", n: int = 50, max_files: int = 5) -> 跨滚动日志文件聚合的尾部行（时间正序）",
            ]
        )
