"""Built-in tool wrapping the dynamic AST sandbox (BaseTool implementation).

The LLM may propose an ``execute_safe_python`` task with a short read-only
snippet; the registry dispatches it through the same whitelist-confined call
path as every other tool. The snippet itself is gated by the AST static check +
empty-builtins namespace + timeout circuit breaker in
``app.tools.dynamic_sandbox`` — this wrapper only adapts it to the BaseTool
contract and maps the structured status onto a normal tool result.
"""

from __future__ import annotations

from typing import Any, Dict

from app.tools.dynamic_sandbox import execute_safe_python
from app.tools.plugin_base import BaseTool


class ExecuteSafePythonTool(BaseTool):
    name = "execute_safe_python"
    description = (
        "[read-only AST sandbox] Run a short read-only Python snippet to gather "
        "system facts; dangerous syntax is statically rejected before execution"
    )
    risk_level = "low"
    params_schema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "Python snippet; only math/json/psutil/socket read-only imports "
                    "allowed, 3s timeout, empty builtins"
                ),
            },
        },
        "required": ["code"],
    }

    def __init__(self, registry):
        self._registry = registry

    def invoke(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return execute_safe_python(
            str(params.get("code", "")),
            timeout=float(getattr(self._registry, "sandbox_timeout_seconds", 3.0)),
        )
