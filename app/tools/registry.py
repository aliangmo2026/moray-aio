"""只读工具注册表：BaseTool 插件化注册 + 白名单校验 + 目录动态加载。

所有工具都是只读的：注册表只负责「按名分发 + 白名单收敛」，
任何工具的 path/dir/host 输入都必须先过校验，越界抛 PermissionError。
外部插件目录由 MORAY_AIO_PLUGIN_DIRS（分号分隔）指定，
每个 .py 文件在隔离命名空间中加载，坏插件只记录错误、不拖垮进程。
"""

from __future__ import annotations

import inspect
import runpy
from pathlib import Path
from typing import Any, Dict, List

from app.tools import log_reader, probes
from app.tools.plugin_base import BaseTool

_DEFAULT_PROBE_HOSTS = ("127.0.0.1", "localhost", "::1")


class ToolRegistry:
    """把「能读什么/能探什么」收敛到白名单之内。"""

    def __init__(
        self,
        readonly_roots: List[str],
        max_scan_bytes: int,
        probe_host_whitelist=None,
        probe_max_processes: int = 50,
        probe_output_max_bytes: int = 4096,
        plugin_dirs=None,
    ):
        self.readonly_roots = [Path(r) for r in (readonly_roots or [])]
        self.max_scan_bytes = int(max_scan_bytes)
        self.probe_host_whitelist = tuple(probe_host_whitelist or _DEFAULT_PROBE_HOSTS)
        self.probe_max_processes = int(probe_max_processes)
        self.probe_output_max_bytes = int(probe_output_max_bytes)
        self._tools: Dict[str, BaseTool] = {}
        self.plugin_errors: List[str] = []
        self._register_builtins()
        dirs = [str(d) for d in (plugin_dirs or []) if str(d).strip()]
        if dirs:
            self.load_plugins(dirs)

    # ---- 注册 ----

    def register_tool(self, tool: BaseTool) -> None:
        """动态注册一个工具实例；name 必须非空且唯一，risk_level 必须合法。"""
        if not isinstance(tool, BaseTool):
            raise TypeError("tool must be a BaseTool instance")
        name = (getattr(tool, "name", "") or "").strip()
        if not name:
            raise ValueError("tool.name must be a non-empty string")
        if getattr(tool, "risk_level", "") not in ("low", "medium", "high"):
            raise ValueError("tool.risk_level must be low/medium/high: {!r}".format(name))
        if name in self._tools:
            raise ValueError("tool already registered: {!r}".format(name))
        self._tools[name] = tool

    def _register_builtins(self) -> None:
        from app.tools.builtin.log_tools import (
            LogGrepTool,
            LogRollingTailTool,
            LogTailTool,
            ListFilesTool,
        )
        from app.tools.builtin.probe_tools import (
            DiskUsageTool,
            PortCheckTool,
            ProcessListTool,
            WindowsServiceStatusTool,
        )

        for tool in (
            LogTailTool(self),
            LogGrepTool(self),
            ListFilesTool(self),
            LogRollingTailTool(self),
            PortCheckTool(self),
            ProcessListTool(self),
            DiskUsageTool(self),
            WindowsServiceStatusTool(self),
        ):
            self.register_tool(tool)

    # ---- 目录动态加载（插件）----

    def load_plugins(self, plugin_dirs) -> None:
        """扫描目录下的 *.py（跳过 _ 开头），加载其中的 BaseTool 子类并注册。

        每个文件独立 try/except：坏插件只进 plugin_errors，绝不影响进程与其他工具。
        """
        for raw_dir in plugin_dirs:
            try:
                directory = Path(str(raw_dir)).resolve()
            except OSError:
                self.plugin_errors.append("cannot resolve plugin dir: {}".format(raw_dir))
                continue
            if not directory.is_dir():
                self.plugin_errors.append("plugin dir not found: {}".format(directory))
                continue
            for py_file in sorted(directory.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                self._load_plugin_file(py_file)

    def _load_plugin_file(self, py_file: Path) -> None:
        """在隔离命名空间中加载单个插件文件，注册其中定义的 BaseTool 子类。"""
        try:
            namespace = runpy.run_path(str(py_file))
        except Exception as exc:
            self.plugin_errors.append(
                "failed loading {}: {}: {}".format(py_file.name, type(exc).__name__, exc)
            )
            return
        loaded = 0
        for obj in namespace.values():
            if not (inspect.isclass(obj) and issubclass(obj, BaseTool) and obj is not BaseTool):
                continue
            try:
                self.register_tool(obj())
                loaded += 1
            except Exception as exc:
                self.plugin_errors.append(
                    "failed registering from {}: {}: {}".format(
                        py_file.name, type(exc).__name__, exc
                    )
                )
        if loaded == 0:
            self.plugin_errors.append("no BaseTool subclass found in {}".format(py_file.name))

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

    def check_probe_host(self, host: str) -> None:
        """探针主机白名单校验入口（供 planner 预检与工具实现共用）。"""
        probes.check_host_allowed(host, self.probe_host_whitelist)

    # ---- 分发 ----

    @property
    def names(self) -> List[str]:
        return sorted(self._tools)

    def call(self, name: str, params: dict = None) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError("unknown tool: {!r}; available: {}".format(name, self.names))
        return tool.invoke(dict(params or {}))

    def describe(self) -> str:
        """给 LLM 的工具清单（只含已注册工具；插件工具自动出现在末尾）。"""
        return "\n".join(self._tools[name].help_line() for name in self.names)
