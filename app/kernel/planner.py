"""自驱任务生成：优先 LLM 规划，LLM 不可用或产出为空时启发式兜底。

安全约束（fail-closed）：
- 只接受白名单工具（log_tail/log_grep/list_files）；
- 参数先按工具白名单过滤（LLM 幻觉出的多余键直接丢弃）；
- path/dir 必须通过 ToolRegistry._check_path，否则整条任务丢弃；
- 与 existing_tasks 按 (tool, params) 去重。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.kernel import approval

ALLOWED_TOOLS = ("log_tail", "log_grep", "list_files")

# 每个工具允许的参数键；其余键（可能包含危险指令）直接丢弃
_TOOL_PARAM_KEYS: Dict[str, tuple] = {
    "log_tail": ("path", "n", "keyword"),
    "log_grep": ("path", "pattern", "limit"),
    "list_files": ("dir",),
}


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
) -> List[dict]:
    """生成下一批只读侦查任务（时间无关，纯函数式入口）。"""
    candidates: List[dict] = []
    if llm is not None:
        try:
            raw = llm.plan_tasks(
                errors=errors, log_path=log_path, tools=tools.describe(), existing=existing_tasks
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

    for key in ("n", "limit"):
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
