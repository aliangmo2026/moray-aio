"""Read-only tool registry: BaseTool plugin registration + whitelist validation +
dynamic plugin directory loading.

All tools are read-only: the registry only does "dispatch by name + whitelist
confinement". Every path/dir/host input goes through validation first — anything
outside the whitelist raises PermissionError.
External plugin directories come from MORAY_AIO_PLUGIN_DIRS (semicolon separated);
each .py file is loaded in an isolated namespace, and a broken plugin only records
an error instead of taking the process down.
"""

from __future__ import annotations

import inspect
import runpy
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.tools import probes
from app.tools.plugin_base import BaseTool

_DEFAULT_PROBE_HOSTS = ("127.0.0.1", "localhost", "::1")


class ToolRegistry:
    """Confines "what can be read / probed" to the whitelists."""

    def __init__(
        self,
        readonly_roots: List[str],
        max_scan_bytes: int,
        probe_host_whitelist: Optional[List[str]] = None,
        probe_max_processes: int = 50,
        probe_output_max_bytes: int = 4096,
        plugin_dirs: Optional[List[str]] = None,
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

    # ---- registration ----

    def register_tool(self, tool: BaseTool) -> None:
        """Register one tool instance; name must be non-empty and unique,
        risk_level must be low/medium/high."""
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

    # ---- dynamic plugin loading ----

    def load_plugins(self, plugin_dirs: List[str]) -> None:
        """Scan directories for *.py (skipping _-prefixed files) and register every
        BaseTool subclass found there.

        Each file is wrapped in its own try/except: a broken plugin only appends to
        plugin_errors and never takes the process or other tools down.
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
        """Load one plugin file in an isolated namespace and register its BaseTool
        subclasses."""
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

    # ---- whitelist ----

    def _check_path(self, path_str: str) -> Path:
        """Whitelist validation: resolve + relative_to; raises PermissionError
        (fail-closed) for anything outside the roots (including .. traversal)."""
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
        """Probe host whitelist entry point (shared by planner pre-check and tools)."""
        probes.check_host_allowed(host, self.probe_host_whitelist)

    # ---- dispatch ----

    @property
    def names(self) -> List[str]:
        return sorted(self._tools)

    def call(self, name: str, params: Optional[dict] = None) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError("unknown tool: {!r}; available: {}".format(name, self.names))
        return tool.invoke(dict(params or {}))

    def describe(self) -> str:
        """Tool list for the LLM (registered tools only; plugins appear automatically)."""
        return "\n".join(self._tools[name].help_line() for name in self.names)
