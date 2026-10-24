"""审批闸门：给候选任务定风险级，并决定自动批准 / 等待人工 / 拒绝。

硬约束：高危操作（kill/reboot/restart/shutdown/rm/del/format）在本引擎中
绝不执行，只能作为「建议文本」出现在分析结论里；命中高危 token 的任务
一律 pending，等待人工处置。
"""

from __future__ import annotations

from typing import Set

READONLY_TOOLS: Set[str] = {"log_tail", "log_grep", "list_files", "log_rolling_tail"}

# 只读系统探针：与 READONLY_TOOLS 同级低风险，但语义不同——探针只能观察系统状态，不能读日志文件
PROBE_TOOLS: Set[str] = {"port_check", "process_list", "disk_usage", "windows_service_status"}

# 自动审批/批量审批可放行的全部只读工具（日志类 + 探针类）
SAFE_TOOLS: Set[str] = READONLY_TOOLS | PROBE_TOOLS

# 命中任意 token（子串匹配，忽略大小写）即判为 high
HIGH_RISK_TOKENS: Set[str] = {"kill", "reboot", "restart", "shutdown", "rm ", "del ", "format"}


def classify(tool: str, rationale: str = "") -> str:
    """风险分级：命中高危 token -> high；只读工具/只读探针 -> low；其余 -> medium。"""
    text = "{} {}".format(tool or "", rationale or "").lower()
    if any(token in text for token in HIGH_RISK_TOKENS):
        return "high"
    if tool in SAFE_TOOLS:
        return "low"
    return "medium"


def decide(tool: str, risk: str, auto_approve_readonly: bool = True) -> str:
    """裁决：非只读工具直接 rejected；high 永远 pending；
    low 且允许自动批准时 approved；其余 pending。"""
    if tool not in SAFE_TOOLS:
        return "rejected"
    if risk == "high":
        return "pending"
    if risk == "low" and auto_approve_readonly:
        return "approved"
    return "pending"
