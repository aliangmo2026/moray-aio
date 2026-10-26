"""MCP (Model Context Protocol) server tests: initialize handshake, tools/list
schema, tools/call dispatch (success + whitelist enforcement + error mapping),
JSON-RPC error codes, and notification handling."""

from __future__ import annotations

import json

import pytest

from app.tools.mcp_server import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    McpServer,
)
from app.tools.registry import ToolRegistry

SAMPLE_LOG = "\n".join(
    [
        "2026-10-25 10:00:00 INFO service started",
        "2026-10-25 10:01:00 ERROR Connection refused to 127.0.0.1:6379",
    ]
)


@pytest.fixture()
def mcp(tmp_path):
    log = tmp_path / "moray.err.log"
    log.write_text(SAMPLE_LOG, encoding="utf-8")
    registry = ToolRegistry([str(tmp_path)], max_scan_bytes=1024 * 1024)
    return McpServer(registry, version="test")


def _rpc(method, params=None, request_id=1):
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return json.dumps(payload)


def _call(mcp, method, params=None, request_id=1):
    response = json.loads(mcp.handle_message(_rpc(method, params, request_id)))
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == request_id
    return response


# ---------- initialize / handshake ----------


def test_initialize_returns_protocol_and_server_info(mcp):
    response = _call(mcp, "initialize", {"protocolVersion": "2025-03-26"})
    result = response["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "moray-aio"
    assert result["serverInfo"]["version"] == "test"
    assert "tools" in result["capabilities"]


def test_initialized_notification_gets_no_response(mcp):
    raw = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert mcp.handle_message(raw) is None


def test_ping_returns_empty_result(mcp):
    assert _call(mcp, "ping")["result"] == {}


# ---------- tools/list ----------


def test_tools_list_exposes_all_builtins_with_schemas(mcp):
    result = _call(mcp, "tools/list")["result"]
    tools = {t["name"]: t for t in result["tools"]}
    for name in (
        "log_tail",
        "log_grep",
        "list_files",
        "log_rolling_tail",
        "port_check",
        "process_list",
        "disk_usage",
        "windows_service_status",
    ):
        assert name in tools
        assert tools[name]["description"]
        assert tools[name]["inputSchema"]["type"] == "object"


def test_tools_list_param_schema_is_not_mutated(mcp):
    before = mcp._registry.get_tool("log_tail").params_schema
    snapshot = json.dumps(before, sort_keys=True)
    _call(mcp, "tools/list")
    assert json.dumps(before, sort_keys=True) == snapshot


# ---------- tools/call ----------


def test_tools_call_process_list_success(mcp):
    response = _call(mcp, "tools/call", {"name": "process_list", "arguments": {}})
    result = response["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert "processes" in payload and "count" in payload


def test_tools_call_log_tail_reads_whitelisted_file(mcp, tmp_path):
    response = _call(
        mcp,
        "tools/call",
        {"name": "log_tail", "arguments": {"path": str(tmp_path / "moray.err.log"), "n": 5}},
    )
    result = response["result"]
    assert result["isError"] is False
    lines = json.loads(result["content"][0]["text"])
    assert any("Connection refused" in line for line in lines)


def test_tools_call_whitelist_violation_is_tool_error(mcp):
    response = _call(
        mcp,
        "tools/call",
        {"name": "log_tail", "arguments": {"path": r"C:\Windows\System32\config", "n": 5}},
    )
    result = response["result"]
    assert result["isError"] is True
    assert "PermissionError" in result["content"][0]["text"]


def test_tools_call_unknown_tool_is_tool_error(mcp):
    response = _call(mcp, "tools/call", {"name": "process_kill", "arguments": {"pid": 1}})
    result = response["result"]
    assert result["isError"] is True
    assert "unknown tool" in result["content"][0]["text"]


# ---------- JSON-RPC error codes ----------


def test_parse_error_code(mcp):
    response = json.loads(mcp.handle_message("{not json"))
    assert response["error"]["code"] == PARSE_ERROR


def test_invalid_request_code(mcp):
    response = json.loads(mcp.handle_message(json.dumps({"id": 1, "method": "x"})))
    assert response["error"]["code"] == INVALID_REQUEST

    response = json.loads(mcp.handle_message(json.dumps(["not", "a", "request"])))
    assert response["error"]["code"] == INVALID_REQUEST


def test_method_not_found_code(mcp):
    response = _call(mcp, "resources/list")
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_invalid_params_code(mcp):
    response = _call(mcp, "tools/call", {"arguments": {}})
    assert response["error"]["code"] == INVALID_PARAMS

    response = _call(mcp, "tools/call", {"name": "log_tail", "arguments": ["not", "dict"]})
    assert response["error"]["code"] == INVALID_PARAMS


def test_internal_error_code(mcp):
    mcp._handlers["boom"] = lambda params: 1 / 0
    response = _call(mcp, "boom")
    assert response["error"]["code"] == INTERNAL_ERROR


def test_message_size_cap(mcp):
    raw = _rpc("ping") + " " * 1_100_000
    response = json.loads(mcp.handle_message(raw))
    assert response["error"]["code"] == INVALID_REQUEST
    assert "too large" in response["error"]["message"]
