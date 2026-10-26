"""Model Context Protocol (MCP) server: expose the read-only ToolRegistry to any
MCP-capable client (Claude Desktop / Cursor / the MoRay main project / future
agents) over the standard JSON-RPC 2.0 protocol.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout — the canonical MCP
stdio transport. Protocol frames are the ONLY thing ever written to stdout;
all logging goes to stderr, otherwise a single stray print would corrupt the
stream and desynchronize the client.

Security posture (inherited, never weakened):
- Only tools registered in the ToolRegistry are advertised; every tools/call
  goes through the exact same whitelist validation as the HTTP API (path
  resolve + relative_to, probe host whitelist, ...);
- Malformed input never raises out of the server: JSON-RPC error codes
  (-32700 parse / -32600 invalid request / -32601 unknown method /
  -32602 invalid params / -32603 internal) are returned instead;
- Tool execution failures are reported as MCP tool results with isError=true
  (per spec, a failing tool is a result, not a protocol error);
- Request size is capped to bound memory use of the line reader.

Run: ``python -m app.tools.mcp_server`` (reads MORAY_AIO_* env like the API).
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Callable, Dict, Optional

from app.tools.registry import ToolRegistry

logger = logging.getLogger("moray_aio.mcp")

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "moray-aio"
SERVER_INSTRUCTIONS = (
    "moray-aio read-only ops engine. Tools only observe (logs/probes/sandboxed "
    "system queries); they can never modify system state. Dangerous actions may "
    "only be suggested as text, never executed."
)

# JSON-RPC / MCP error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Upper bound for one incoming line (chars) — a defense against memory abuse
MAX_MESSAGE_CHARS = 1_000_000


class McpInvalidParams(Exception):
    """Raised by handlers when the params object is structurally invalid."""


def _error_response(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class McpServer:
    """Stateless JSON-RPC method dispatcher over a ToolRegistry instance."""

    def __init__(self, registry: ToolRegistry, version: str = "0.0.0"):
        self._registry = registry
        self._version = str(version)
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "initialize": self._initialize,
            "ping": self._ping,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
        }

    # ---- protocol plumbing ----

    def handle_message(self, raw: str) -> Optional[str]:
        """Parse one JSON-RPC line and return the response line (None for
        notifications / nothing to answer). Never raises."""
        if len(raw) > MAX_MESSAGE_CHARS:
            return json.dumps(
                _error_response(None, INVALID_REQUEST, "message too large"), ensure_ascii=False
            )
        try:
            request = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return json.dumps(
                _error_response(None, PARSE_ERROR, "Parse error"), ensure_ascii=False
            )
        response = self.dispatch(request)
        if response is None:
            return None
        return json.dumps(response, ensure_ascii=False)

    def dispatch(self, request: Any) -> Optional[Dict[str, Any]]:
        """Route one parsed JSON-RPC request; None means "no response"."""
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return _error_response(
                request.get("id") if isinstance(request, dict) else None,
                INVALID_REQUEST,
                "Invalid Request: jsonrpc field must be '2.0'",
            )
        method = request.get("method")
        if not isinstance(method, str) or not method:
            return _error_response(
                request.get("id"), INVALID_REQUEST, "Invalid Request: method missing"
            )
        request_id = request.get("id")
        is_notification = "id" not in request
        params = request.get("params") if request.get("params") is not None else {}
        if not isinstance(params, dict):
            return _error_response(request_id, INVALID_PARAMS, "params must be an object")

        if method.startswith("notifications/"):
            return None  # accepted silently per spec (initialized / cancelled / ...)

        handler = self._handlers.get(method)
        if handler is None:
            return _error_response(request_id, METHOD_NOT_FOUND, "method not found: {}".format(method))
        try:
            result = handler(params)
        except McpInvalidParams as exc:
            return _error_response(request_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 — protocol must never leak a traceback
            logger.exception("mcp method %s failed", method)
            return _error_response(
                request_id, INTERNAL_ERROR, "internal error: {}: {}".format(type(exc).__name__, exc)
            )
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # ---- method handlers ----

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": self._version},
            "instructions": SERVER_INSTRUCTIONS,
        }

    def _ping(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    def _tools_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"tools": [self._descriptor(name) for name in self._registry.names]}

    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise McpInvalidParams("tools/call requires a non-empty string 'name'")
        arguments = params.get("arguments") if params.get("arguments") is not None else {}
        if not isinstance(arguments, dict):
            raise McpInvalidParams("tools/call 'arguments' must be an object")
        if self._registry.get_tool(name) is None:
            # Unknown tool: per spec this is a tool-level error result, not a
            # protocol error (the client sees it in-band with isError=true)
            return _tool_error("unknown tool {!r}; available: {}".format(name, self._registry.names))
        try:
            result = self._registry.call(name, arguments)
        except Exception as exc:  # noqa: BLE001 — whitelist/param failures land here
            return _tool_error("{}: {}".format(type(exc).__name__, exc))
        return {
            "content": [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}
            ],
            "isError": False,
        }

    def _descriptor(self, name: str) -> Dict[str, Any]:
        tool = self._registry.get_tool(name)
        if tool is None:  # names come from the registry itself; defensive only
            raise McpInvalidParams("unknown tool: {!r}".format(name))
        schema = tool.params_schema if isinstance(tool.params_schema, dict) else {}
        if schema.get("type") != "object":
            schema = {"type": "object", **schema}
        return {"name": tool.name, "description": tool.description, "inputSchema": schema}


def _tool_error(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def serve_stdio(registry: ToolRegistry, version: str = "0.0.0") -> None:
    """Blocking stdio loop: one JSON-RPC request per line, response per line."""
    server = McpServer(registry, version=version)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response = server.handle_message(line)
        if response is not None:
            sys.stdout.write(response + "\n")
            sys.stdout.flush()


def main() -> None:
    """CLI entry: build the registry from MORAY_AIO_* settings and serve stdio."""
    from app import __version__
    from app.deps import get_tools

    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    serve_stdio(get_tools(), version=__version__)


if __name__ == "__main__":
    main()
