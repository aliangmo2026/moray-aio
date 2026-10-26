"""Approval gate: risk classification for candidate tasks and auto/manual decisions.

Hard rules: dangerous operations (kill/reboot/restart/shutdown/rm/del/format) are NEVER
executed by this engine — they may only appear as suggestion text in conclusions; any task
whose rationale hits a high-risk token is held as pending for a human.
"""

from __future__ import annotations

from typing import Set

READONLY_TOOLS: Set[str] = {"log_tail", "log_grep", "list_files", "log_rolling_tail"}

# Read-only system probes: same low risk as log tools, but semantically different —
# probes only observe system state, they never read log files
PROBE_TOOLS: Set[str] = {"port_check", "process_list", "disk_usage", "windows_service_status"}

# AST-sandboxed snippet execution (v2.0): read-only by construction — snippets are
# statically vetted (allowlisted imports, no dunder access, no file/process verbs),
# run with empty __builtins__ under a hard timeout; operators can disable the tool
# entirely via MORAY_AIO_SANDBOX_ENABLED=false
SANDBOX_TOOLS: Set[str] = {"execute_safe_python"}

# Everything auto/batch approval may release (log tools + probes + AST sandbox)
SAFE_TOOLS: Set[str] = READONLY_TOOLS | PROBE_TOOLS | SANDBOX_TOOLS

# Any token hit (case-insensitive substring) classifies the task as high risk
HIGH_RISK_TOKENS: Set[str] = {"kill", "reboot", "restart", "shutdown", "rm ", "del ", "format"}


def classify(tool: str, rationale: str = "") -> str:
    """Risk level: high-risk token -> high; read-only tool/probe -> low; otherwise medium."""
    text = "{} {}".format(tool or "", rationale or "").lower()
    if any(token in text for token in HIGH_RISK_TOKENS):
        return "high"
    if tool in SAFE_TOOLS:
        return "low"
    return "medium"


def decide(tool: str, risk: str, auto_approve_readonly: bool = True) -> str:
    """Decision: non-read-only tool -> rejected; high -> always pending;
    low + auto approve -> approved; everything else -> pending."""
    if tool not in SAFE_TOOLS:
        return "rejected"
    if risk == "high":
        return "pending"
    if risk == "low" and auto_approve_readonly:
        return "approved"
    return "pending"
