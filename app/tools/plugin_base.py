"""Plugin base class: external tools subclass BaseTool and implement invoke(params).

Contract (identical constraints to built-in tools):
- Tools must be strictly read-only (observe/query); they never modify system state;
- ``name`` is unique inside the registry; ``risk_level`` is low/medium/high;
- ``params_schema`` is the JSON Schema of the parameters (used by describe(),
  the LLM prompt and documentation);
- ``invoke(params: dict)`` receives the parameter dict and returns a JSON-serializable
  result; raised exceptions mark the task as failed by the kernel.
  (Security note: the entry point takes a single dict instead of a variable keyword
  spread, which minimizes any injection surface.)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseTool(ABC):
    """Base class for read-only tool plugins."""

    #: Unique tool name (registry key), e.g. "log_tail"
    name: str = ""
    #: One-line description for the LLM / humans
    description: str = ""
    #: Risk level: low / medium / high (high is held for human approval)
    risk_level: str = "low"
    #: JSON Schema of the parameters (type: object)
    params_schema: Dict[str, Any] = {}

    @abstractmethod
    def invoke(self, params: dict) -> Any:
        """Tool entry point. Implemented by subclasses; ``params`` is a dict; read-only."""

    def help_line(self) -> str:
        """Single help line for describe() (argument summary from params_schema)."""
        props = (self.params_schema or {}).get("properties", {})
        args = ", ".join(
            "{}: {}".format(key, (spec or {}).get("type", "any"))
            for key, spec in props.items()
        )
        return "- {}({}) -> [{}] {}".format(self.name, args, self.risk_level, self.description)
