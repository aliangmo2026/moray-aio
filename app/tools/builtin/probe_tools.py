"""内置系统探针工具（BaseTool 实现）：只读观察，绝不修改系统状态。"""

from __future__ import annotations

from app.tools import probes
from app.tools.plugin_base import BaseTool


class PortCheckTool(BaseTool):
    name = "port_check"
    description = "[只读探针] 探测端口可连性，返回 open/latency_ms"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "主机（须在探针白名单内）"},
            "port": {"type": "integer", "description": "端口 1-65535"},
            "timeout": {"type": "number", "description": "超时秒数，默认 2"},
        },
        "required": ["host", "port"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: dict):
        host = str(params.get("host", ""))
        port = int(params.get("port"))
        timeout = float(params.get("timeout", 2))
        return probes.port_check(
            host, port, timeout=timeout, host_whitelist=self._registry.probe_host_whitelist
        )


class ProcessListTool(BaseTool):
    name = "process_list"
    description = "[只读探针] 列出进程（pid/name/内存等，有条数上限）"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "name_pattern": {"type": "string", "description": "进程名正则（可选）"},
        },
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: dict):
        name_pattern = params.get("name_pattern")
        return probes.process_list(
            name_pattern=name_pattern, max_processes=self._registry.probe_max_processes
        )


class DiskUsageTool(BaseTool):
    name = "disk_usage"
    description = "[只读探针] 查询磁盘占用（path 须在白名单内）"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "路径（须在白名单内）"}},
        "required": ["path"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: dict):
        checked = self._registry._check_path(str(params.get("path", "")))
        return probes.disk_usage(str(checked))


class WindowsServiceStatusTool(BaseTool):
    name = "windows_service_status"
    description = "[只读探针] 查询 Windows 服务状态（只读查询，绝不启停）"
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "service_name": {"type": "string", "description": "服务名（安全正则校验）"},
        },
        "required": ["service_name"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: dict):
        service_name = str(params.get("service_name", ""))
        return probes.windows_service_status(
            service_name, output_max_bytes=self._registry.probe_output_max_bytes
        )
