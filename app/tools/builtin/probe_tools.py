"""Built-in system probe tools (BaseTool implementations): read-only observation,
never modifying system state."""

from __future__ import annotations

from typing import Any, Dict

from app.tools import probes
from app.tools.plugin_base import BaseTool


class PortCheckTool(BaseTool):
    name = "port_check"
    description = "[read-only probe] TCP port reachability; returns open/latency_ms"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "Host (must be whitelisted)"},
            "port": {"type": "integer", "description": "Port 1-65535"},
            "timeout": {"type": "number", "description": "Timeout seconds, default 2"},
        },
        "required": ["host", "port"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: Dict[str, Any]) -> Dict[str, Any]:
        host = str(params.get("host", ""))
        port = int(params.get("port"))
        timeout = float(params.get("timeout", 2))
        return probes.port_check(
            host, port, timeout=timeout, host_whitelist=self._registry.probe_host_whitelist
        )


class ProcessListTool(BaseTool):
    name = "process_list"
    description = "[read-only probe] Process listing (pid/name/memory..., capped)"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "name_pattern": {"type": "string", "description": "Process-name regex (optional)"},
        },
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name_pattern = params.get("name_pattern")
        return probes.process_list(
            name_pattern=name_pattern, max_processes=self._registry.probe_max_processes
        )


class DiskUsageTool(BaseTool):
    name = "disk_usage"
    description = "[read-only probe] Disk usage for a path (must be whitelisted)"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path (must be whitelisted)"}},
        "required": ["path"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: Dict[str, Any]) -> Dict[str, Any]:
        checked = self._registry._check_path(str(params.get("path", "")))
        return probes.disk_usage(str(checked))


class WindowsServiceStatusTool(BaseTool):
    name = "windows_service_status"
    description = "[read-only probe] Windows service state (query only, never start/stop)"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string", "description": "Service name (safety-regex checked)"},
        },
        "required": ["service_name"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: Dict[str, Any]) -> Dict[str, Any]:
        service_name = str(params.get("service_name", ""))
        return probes.windows_service_status(
            service_name, output_max_bytes=self._registry.probe_output_max_bytes
        )
