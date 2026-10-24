"""只读工具注册表：路径白名单校验 + 按名分发。

任何工具调用涉及的 path/dir 参数都必须落在 readonly_roots 内，
用 Path.resolve() + relative_to 校验，越界直接抛 PermissionError。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List

from app.tools import log_reader, probes

_DEFAULT_PROBE_HOSTS = ("127.0.0.1", "localhost", "::1")


class ToolRegistry:
    """把「能读什么/能探什么」收敛到白名单之内。所有工具都是只读的。"""

    def __init__(
        self,
        readonly_roots: List[str],
        max_scan_bytes: int,
        probe_host_whitelist=None,
        probe_max_processes: int = 50,
        probe_output_max_bytes: int = 4096,
    ):
        self.readonly_roots = [Path(r) for r in (readonly_roots or [])]
        self.max_scan_bytes = int(max_scan_bytes)
        self.probe_host_whitelist = tuple(probe_host_whitelist or _DEFAULT_PROBE_HOSTS)
        self.probe_max_processes = int(probe_max_processes)
        self.probe_output_max_bytes = int(probe_output_max_bytes)
        self._tools: Dict[str, Callable[..., Any]] = {}
        self._register_defaults()

    # ---- 注册 ----

    def _register_defaults(self) -> None:
        self.register("log_tail", self._tool_log_tail)
        self.register("log_grep", self._tool_log_grep)
        self.register("list_files", self._tool_list_files)
        self.register("log_rolling_tail", self._tool_log_rolling_tail)
        self.register("port_check", self._tool_port_check)
        self.register("process_list", self._tool_process_list)
        self.register("disk_usage", self._tool_disk_usage)
        self.register("windows_service_status", self._tool_windows_service_status)

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

    # ---- 只读系统探针（观察，不修改）----

    def check_probe_host(self, host: str) -> None:
        """探针主机白名单校验入口（供 planner 预检与工具实现共用）。"""
        probes.check_host_allowed(host, self.probe_host_whitelist)

    def _tool_port_check(self, host: str, port: int, timeout: float = 2) -> dict:
        return probes.port_check(
            str(host), port, timeout=timeout, host_whitelist=self.probe_host_whitelist
        )

    def _tool_process_list(self, name_pattern: str = None) -> dict:
        return probes.process_list(
            name_pattern=name_pattern, max_processes=self.probe_max_processes
        )

    def _tool_disk_usage(self, path: str) -> dict:
        checked = self._check_path(str(path))
        return probes.disk_usage(str(checked))

    def _tool_windows_service_status(self, service_name: str) -> dict:
        return probes.windows_service_status(
            service_name, output_max_bytes=self.probe_output_max_bytes
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
                '- log_rolling_tail(dir: str, pattern: str = "moray.err.log*", n: int = 50, max_files: int = 5) -> 跨滚动日志文件聚合的尾部行（时间正序）',
                "- port_check(host: str, port: int, timeout: float = 2) -> [只读探针] 探测端口可连性，返回 open/latency_ms",
                "- process_list(name_pattern: str = null) -> [只读探针] 列出进程（pid/name/内存等，最多 50 条）",
                "- disk_usage(path: str) -> [只读探针] 查询磁盘占用（path 需在白名单内）",
                "- windows_service_status(service_name: str) -> [只读探针] 查询 Windows 服务状态（只读查询，绝不启停）",
            ]
        )
