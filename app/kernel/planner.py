"""自驱任务生成：优先 LLM 规划，LLM 不可用或产出为空时启发式兜底。

安全约束（fail-closed）：
- 只接受白名单工具（log_tail/log_grep/list_files/log_rolling_tail + 4 个只读探针）；
- 参数先按工具白名单过滤（LLM 幻觉出的多余键直接丢弃）；
- path/dir 必须通过 ToolRegistry._check_path；探针 host 必须命中主机白名单；
  service_name 必须通过安全正则；name_pattern 必须是合法正则——否则整条任务丢弃；
- 与 existing_tasks 按 (tool, params) 去重。
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
)

# 每个工具允许的参数键；其余键（可能包含危险指令）直接丢弃
_TOOL_PARAM_KEYS: Dict[str, tuple] = {
    "log_tail": ("path", "n", "keyword"),
    "log_grep": ("path", "pattern", "limit"),
    "list_files": ("dir",),
    "log_rolling_tail": ("dir", "pattern", "n", "max_files"),
    "port_check": ("host", "port", "timeout"),
    "process_list": ("name_pattern",),
    "disk_usage": ("path",),
    "windows_service_status": ("service_name",),
}

# 常见服务端口 -> 推测的进程名（process_list 的 name_pattern 用）
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

# 常见 Windows 服务名提示：错误文本中出现这些词才敢建议 windows_service_status
KNOWN_SERVICE_TOKENS = (
    "redis", "mysql", "postgres", "postgresql", "mongodb", "memcached",
    "rabbitmq", "elasticsearch", "spooler", "dnscache", "wuauserv",
)

# 只从错误文本中提取 127.0.0.1:PORT / localhost:PORT（host 天然在探针白名单内）
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
    """生成下一批只读侦查任务（时间无关，纯函数式入口）。

    failed_tasks：上一轮 failed/empty 任务及其原因，作为 LLM 的反馈上下文，
    让下一轮规划避开已被证伪的 pattern/参数。
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
            candidates = []  # LLM 任何抖动都回落启发式
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


# ---- 校验与落库 ----

def _validate_candidate(cand: dict, tools, log_path: str, auto_approve_readonly: bool):
    tool = cand.get("tool")
    if tool not in _TOOL_PARAM_KEYS:
        return None
    raw_params = cand.get("params")
    if not isinstance(raw_params, dict):
        return None
    params = {k: v for k, v in raw_params.items() if k in _TOOL_PARAM_KEYS[tool]}
    rationale = str(cand.get("rationale", ""))[:500]

    # path/dir 白名单校验：任何一条不过就丢弃整条任务
    for key in ("path", "dir"):
        if key in params:
            try:
                tools._check_path(str(params[key]))
            except Exception:
                return None

    # tail/grep 缺省 path 时默认分析当前日志（同样要过白名单）
    if tool in ("log_tail", "log_grep") and "path" not in params:
        try:
            tools._check_path(str(log_path))
        except Exception:
            return None
        params["path"] = log_path

    if tool == "log_grep" and not str(params.get("pattern", "")).strip():
        return None

    # ---- 探针任务的预校验（fail-closed，宁丢任务不放危险参数）----
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


# ---- 启发式兜底 ----

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
                "rationale": "发现连接被拒错误，回溯同类错误的出现频率与目标地址。",
            }
        )
        # 只针对错误文本里实际出现的 127.0.0.1:PORT / localhost:PORT 派探针，绝不编造
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
                    "rationale": "探针只能观察，不能修改：确认 {}:{} 当前是否可连。".format(host, port),
                }
            )
            hint = SERVICE_PORT_HINTS.get(port)
            if hint:
                candidates.append(
                    {
                        "tool": "process_list",
                        "params": {"name_pattern": hint},
                        "rationale": "探针只能观察，不能修改：端口 {} 常见于 {}，观察对应进程是否存在。".format(port, hint),
                    }
                )
            if len(seen_ports) >= 3:
                break  # 最多探 3 个端口，防爆炸

    if "timeout" in lower or "timed out" in lower:
        candidates.append(
            {
                "tool": "log_grep",
                "params": {"path": log_path, "pattern": "(?i)timeout|timed out", "limit": 50},
                "rationale": "发现超时错误，回溯全部超时记录判断是否集中在同一依赖。",
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
                "rationale": "发现文件缺失错误，回溯相关行确认缺失的具体路径。",
            }
        )
    if any(token in lower for token in ("disk", "no space", "quota")):
        candidates.append(
            {
                "tool": "disk_usage",
                "params": {"path": os.path.dirname(str(log_path)) or str(log_path)},
                "rationale": "探针只能观察，不能修改：检查日志所在磁盘的占用情况。",
            }
        )
    if "service" in lower or "daemon" in lower:
        for token in KNOWN_SERVICE_TOKENS:
            if re.search(r"\b{}\b".format(re.escape(token)), lower):
                candidates.append(
                    {
                        "tool": "windows_service_status",
                        "params": {"service_name": token},
                        "rationale": "探针只能观察，不能修改：查询 Windows 服务状态（只读查询，绝不启停）。",
                    }
                )
                break
    return candidates


# ---- LLM 输出收敛 ----

def _coerce_llm_tasks(raw) -> List[dict]:
    """把 LLM 的 JSON 输出收敛成候选任务列表；任何不合形都返回空列表。"""
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
