"""v2 planner probe-fallback tests (2026-10-26 chaos-drill finding): the
read-only port_check/process_list set derived from host:port pairs in the
error text is force-merged into the candidate list even when the LLM planner
returned tasks — a small local LLM tends to propose only log tools, skipping
the probes that actually verify the root cause."""

from __future__ import annotations

from app.kernel.planner import propose_tasks
from app.tools.registry import ToolRegistry

ERROR_LINE = "2026-10-26 10:00:00 ERROR Connection refused to redis 127.0.0.1:6379"


class _FakeLLM:
    """LLM stub returning fixed task proposals (keyword-only, like the real client)."""

    def __init__(self, tasks):
        self._tasks = tasks
        self.calls = 0

    def plan_tasks(self, *, errors, log_path, tools, existing, failed):
        self.calls += 1
        return {"tasks": list(self._tasks)}

    def is_available(self):
        return True


def _registry(tmp_path):
    return ToolRegistry([str(tmp_path)], 1024 * 1024)


def _log(tmp_path):
    log = tmp_path / "moray.err.log"
    log.write_text("x", encoding="utf-8")
    return log


def test_probe_fallback_merged_under_llm_proposals(tmp_path):
    """LLM returned a log_grep task -> the final list still contains
    port_check (open-state of the real host:port) and process_list (redis)."""
    registry = _registry(tmp_path)
    log = _log(tmp_path)
    llm = _FakeLLM(
        [
            {
                "tool": "log_grep",
                "params": {"path": str(log), "pattern": "Connection refused", "limit": 50},
                "rationale": "LLM: trace connection refused records",
            }
        ]
    )
    tasks = propose_tasks(
        errors=[ERROR_LINE],
        existing_tasks=[],
        log_path=str(log),
        tools=registry,
        llm=llm,
        auto_approve_readonly=True,
    )
    by_tool = {}
    for t in tasks:
        by_tool.setdefault(t["tool"], t)
    # the LLM proposal survives
    assert "log_grep" in by_tool
    # chaos-drill fix: probe fallback is force-merged despite the LLM output
    assert "port_check" in by_tool
    assert by_tool["port_check"]["params"] == {"host": "127.0.0.1", "port": 6379}
    assert by_tool["port_check"]["status"] == "approved"
    assert "process_list" in by_tool
    assert by_tool["process_list"]["params"]["name_pattern"] == "redis"
    assert by_tool["process_list"]["status"] == "approved"


def test_probe_fallback_dedup_against_llm_and_existing(tmp_path):
    """When the LLM itself proposes the same probes (or they already exist in
    earlier rounds), the merged fallback is deduped, never duplicated."""
    registry = _registry(tmp_path)
    log = _log(tmp_path)
    errors = [ERROR_LINE]
    llm = _FakeLLM(
        [
            {"tool": "port_check", "params": {"host": "127.0.0.1", "port": 6379}, "rationale": "llm probe"},
            {"tool": "process_list", "params": {"name_pattern": "redis"}, "rationale": "llm probe"},
        ]
    )
    tasks = propose_tasks(
        errors=errors, existing_tasks=[], log_path=str(log),
        tools=registry, llm=llm, auto_approve_readonly=True,
    )
    assert sum(1 for t in tasks if t["tool"] == "port_check") == 1
    assert sum(1 for t in tasks if t["tool"] == "process_list") == 1
    # next planning round: everything already exists -> no probe duplicates
    round2 = propose_tasks(
        errors=errors, existing_tasks=tasks, log_path=str(log),
        tools=registry, llm=llm, auto_approve_readonly=True,
    )
    assert not any(t["tool"] in ("port_check", "process_list") for t in round2)


def test_probe_fallback_requires_connection_refused(tmp_path):
    """The fallback only arms for connection-refused evidence: a timeout line
    carrying a host:port never gets a probed port invented."""
    registry = _registry(tmp_path)
    log = _log(tmp_path)
    llm = _FakeLLM(
        [{"tool": "log_tail", "params": {"path": str(log), "n": 20}, "rationale": "llm tail"}]
    )
    tasks = propose_tasks(
        errors=["2026-10-26 ERROR Timeout contacting 127.0.0.1:6379"],
        existing_tasks=[], log_path=str(log),
        tools=registry, llm=llm, auto_approve_readonly=True,
    )
    assert not any(t["tool"] == "port_check" for t in tasks)
    assert not any(t["tool"] == "process_list" for t in tasks)
    assert any(t["tool"] == "log_tail" for t in tasks)


def test_heuristic_offline_path_still_includes_probes(tmp_path):
    """LLM offline (None): the full heuristic set is unchanged — log_grep plus
    the probe pair still arrive together."""
    registry = _registry(tmp_path)
    log = _log(tmp_path)
    tasks = propose_tasks(
        errors=[ERROR_LINE],
        existing_tasks=[],
        log_path=str(log),
        tools=registry,
        llm=None,
        auto_approve_readonly=True,
    )
    tools_used = {t["tool"] for t in tasks}
    assert {"log_grep", "port_check", "process_list"} <= tools_used
