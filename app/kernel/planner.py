"""Self-driven task generation: LLM planning first, heuristic fallback when the
LLM is unavailable or produces nothing.

Safety constraints (fail-closed):
- Only whitelisted tools are accepted (log_tail/log_grep/list_files/log_rolling_tail
  plus the four read-only probes);
- Params are filtered by a per-tool key whitelist (hallucinated extra keys from the
  LLM are dropped);
- path/dir must pass ToolRegistry._check_path; probe hosts must hit the host
  whitelist; service_name must pass the safety regex; name_pattern must be a valid
  regex — otherwise the whole candidate is dropped;
- Dedup against existing_tasks by (tool, params).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.kernel import approval
from app.tools.probes import SERVICE_NAME_RE

ALLOWED_TOOLS = (
    "log_tail",
    "log_grep",
    "list_files",
    "log_rolling_tail",
    "port_check",
    "process_list",
    "disk_usage",
    "windows_service_status",
    "execute_safe_python",
)

# Allowed parameter keys per tool; anything else (possibly dangerous instructions)
# is dropped outright
_TOOL_PARAM_KEYS: Dict[str, tuple] = {
    "log_tail": ("path", "n", "keyword"),
    "log_grep": ("path", "pattern", "limit"),
    "list_files": ("dir",),
    "log_rolling_tail": ("dir", "pattern", "n", "max_files"),
    "port_check": ("host", "port", "timeout"),
    "process_list": ("name_pattern",),
    "disk_usage": ("path",),
    "windows_service_status": ("service_name",),
    "execute_safe_python": ("code",),
}

# Common service ports -> guessed process name (used as process_list name_pattern)
SERVICE_PORT_HINTS: Dict[str, str] = {
    "6379": "redis",
    "5432": "postgres",
    "3306": "mysql",
    "27017": "mongodb",
    "11211": "memcached",
    "8000": "python|java|node",
    "8080": "python|java|node",
    "9200": "elasticsearch",
    "5672": "rabbitmq",
}

# Known Windows service names: windows_service_status is only proposed when one of
# these words actually appears in the error text (never invented)
KNOWN_SERVICE_TOKENS = (
    "redis", "mysql", "postgres", "postgresql", "mongodb", "memcached",
    "rabbitmq", "elasticsearch", "spooler", "dnscache", "wuauserv",
)

# Only 127.0.0.1:PORT / localhost:PORT extracted from the error text are probed —
# nothing is ever fabricated (hosts are inside the probe whitelist by construction)
_HOST_PORT_RE = re.compile(r"\b((?:127\.0\.0\.1|localhost)):(\d{1,5})", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canon(params) -> str:
    try:
        return json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(params)


def propose_tasks(
    *,
    errors: List[str],
    existing_tasks: List[dict],
    log_path: str,
    tools,
    llm=None,
    auto_approve_readonly: bool = True,
    failed_tasks: Optional[List[dict]] = None,
) -> List[dict]:
    """Generate the next batch of read-only investigation tasks (pure function).

    ``failed_tasks``: failed/empty tasks from previous rounds with their reasons —
    passed to the LLM so the next round avoids disproven patterns/parameters.
    """
    candidates: List[dict] = []
    if llm is not None:
        try:
            raw = llm.plan_tasks(
                errors=errors,
                log_path=log_path,
                tools=tools.describe(),
                existing=existing_tasks,
                failed=failed_tasks,
            )
            candidates = _coerce_llm_tasks(raw)
        except Exception:
            candidates = []  # any LLM hiccup falls back to heuristics
    if not candidates:
        candidates = _heuristic_candidates(errors, log_path)

    accepted: List[dict] = []
    seen = {(t.get("tool"), _canon(t.get("params"))) for t in (existing_tasks or [])}
    for cand in candidates:
        task = _validate_candidate(cand, tools, log_path, auto_approve_readonly)
        if task is None:
            continue
        key = (task["tool"], _canon(task["params"]))
        if key in seen:
            continue
        seen.add(key)
        accepted.append(task)
    return accepted


# ---- validation ----

def _validate_candidate(
    cand: dict, tools, log_path: str, auto_approve_readonly: bool
) -> Optional[dict]:
    """Validate one candidate; returns a task dict or None (fail-closed)."""
    tool = cand.get("tool")
    if tool not in _TOOL_PARAM_KEYS:
        return None
    # The tool must actually be registered (plugins add tools; operators can
    # disable the AST sandbox) — otherwise the task could never execute.
    if tools.get_tool(tool) is None:
        return None
    raw_params = cand.get("params")
    if not isinstance(raw_params, dict):
        return None
    params = {k: v for k, v in raw_params.items() if k in _TOOL_PARAM_KEYS[tool]}
    rationale = str(cand.get("rationale", ""))[:500]

    # path/dir whitelist: any failure drops the whole candidate
    for key in ("path", "dir"):
        if key in params:
            try:
                tools._check_path(str(params[key]))
            except Exception:
                return None

    # tail/grep default to the current log when path is missing (also whitelist-checked)
    if tool in ("log_tail", "log_grep") and "path" not in params:
        try:
            tools._check_path(str(log_path))
        except Exception:
            return None
        params["path"] = log_path

    if tool == "log_grep" and not str(params.get("pattern", "")).strip():
        return None

    # ---- probe pre-validation (fail-closed: drop tasks rather than allow risky params) ----
    if tool == "port_check":
        try:
            tools.check_probe_host(str(params.get("host", "")))
            port = int(params.get("port"))
        except Exception:
            return None
        if not (1 <= port <= 65535):
            return None
        params["port"] = port

    if tool == "process_list" and "name_pattern" in params:
        try:
            re.compile(str(params["name_pattern"]))
        except re.error:
            return None

    if tool == "windows_service_status":
        name = str(params.get("service_name", "")).strip()
        if not SERVICE_NAME_RE.match(name):
            return None
        params["service_name"] = name

    for key in ("n", "limit", "max_files", "port"):
        if key in params:
            try:
                params[key] = int(params[key])
            except (TypeError, ValueError):
                return None

    return _make_task(tool, params, rationale, auto_approve_readonly)


def _make_task(tool: str, params: dict, rationale: str, auto_approve_readonly: bool) -> dict:
    risk = approval.classify(tool, rationale)
    return {
        "id": uuid.uuid4().hex[:12],
        "tool": tool,
        "params": params,
        "rationale": rationale,
        "risk": risk,
        "status": approval.decide(tool, risk, auto_approve_readonly),
        "result": None,
        "created_at": _now(),
    }


# ---- heuristic fallback ----

def _heuristic_candidates(errors: List[str], log_path: str) -> List[dict]:
    blob = "\n".join(str(e) for e in (errors or []))
    lower = blob.lower()
    candidates: List[dict] = []

    if "connection refused" in lower or "econnrefused" in lower:
        candidates.append(
            {
                "tool": "log_grep",
                "params": {
                    "path": log_path,
                    "pattern": "Connection refused|ECONNREFUSED",
                    "limit": 50,
                },
                "rationale": "Connection refused errors found; trace frequency and targets.",
            }
        )
        # Only probe host:port pairs actually present in the error text — never invented
        seen_ports = set()
        for match in _HOST_PORT_RE.finditer(blob):
            host = match.group(1).lower()
            port = match.group(2)
            if port in seen_ports:
                continue
            seen_ports.add(port)
            candidates.append(
                {
                    "tool": "port_check",
                    "params": {"host": host, "port": int(port)},
                    "rationale": "probes are read-only: check whether {}:{} is reachable now.".format(host, port),
                }
            )
            hint = SERVICE_PORT_HINTS.get(port)
            if hint:
                candidates.append(
                    {
                        "tool": "process_list",
                        "params": {"name_pattern": hint},
                        "rationale": "probes are read-only: port {} usually means {}; check the process.".format(port, hint),
                    }
                )
            if len(seen_ports) >= 3:
                break  # probe at most 3 ports to avoid explosion

    if "timeout" in lower or "timed out" in lower:
        candidates.append(
            {
                "tool": "log_grep",
                "params": {"path": log_path, "pattern": "(?i)timeout|timed out", "limit": 50},
                "rationale": "Timeout errors found; trace all records to spot one bad dependency.",
            }
        )
    if "no such file" in lower or "filenotfounderror" in lower:
        candidates.append(
            {
                "tool": "log_grep",
                "params": {
                    "path": log_path,
                    "pattern": "No such file|FileNotFoundError",
                    "limit": 50,
                },
                "rationale": "Missing-file errors found; trace the exact paths involved.",
            }
        )
    if any(token in lower for token in ("disk", "no space", "quota")):
        candidates.append(
            {
                "tool": "disk_usage",
                "params": {"path": os.path.dirname(str(log_path)) or str(log_path)},
                "rationale": "probes are read-only: check disk usage of the log directory.",
            }
        )
    if "service" in lower or "daemon" in lower:
        for token in KNOWN_SERVICE_TOKENS:
            if re.search(r"\b{}\b".format(re.escape(token)), lower):
                candidates.append(
                    {
                        "tool": "windows_service_status",
                        "params": {"service_name": token},
                        "rationale": "probes are read-only: query the Windows service state (never start/stop).",
                    }
                )
                break
    return candidates


# ---- LLM output coercion ----

def _coerce_llm_tasks(raw) -> List[dict]:
    """Fold the LLM JSON output into candidate tasks; anything malformed -> []."""
    if not isinstance(raw, dict) or raw.get("parse_error"):
        return []
    items = raw.get("tasks")
    if not isinstance(items, list):
        return []
    out: List[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        tool = item.get("tool")
        params = item.get("params")
        if not isinstance(tool, str) or not isinstance(params, dict):
            continue
        out.append(
            {
                "tool": tool,
                "params": params,
                "rationale": str(item.get("rationale", "")),
            }
        )
    return out
