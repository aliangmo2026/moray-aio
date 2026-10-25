"""Self-evolution kernel tests: heuristic loop convergence, empty log, approval
pause/resume, graceful failures, whitelist violations, and LLM feedback loops."""

from __future__ import annotations

import dataclasses

import pytest

from app.config import Settings
from app.kernel.loop import EvolutionLoop
from app.kernel.sqlite_repository import Memory
from app.kernel.state import Phase
from app.tools.registry import ToolRegistry

SAMPLE_LOG = "\n".join(
    [
        "2026-10-24 10:00:00 INFO service started",
        "2026-10-24 10:01:00 ERROR Connection refused to 127.0.0.1:6379",
        "2026-10-24 10:01:00 ERROR Connection refused to 127.0.0.1:6379",
        "2026-10-24 10:03:00 INFO retry scheduled",
    ]
)


def _make_loop(tmp_path, log_text, auto_approve=True, max_iterations=2):
    log = tmp_path / "moray.err.log"
    log.write_text(log_text, encoding="utf-8")
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=max_iterations,
        auto_approve_readonly=auto_approve,
    )
    memory = Memory(str(tmp_path / "memory.sqlite3"))
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    loop = EvolutionLoop(
        settings=settings,
        memory=memory,
        tools=tools,
        llm=None,
        auto_approve_readonly=settings.auto_approve_readonly,
    )
    return loop, memory


def test_loop_heuristic_concludes_with_auto_approve(tmp_path):
    loop, memory = _make_loop(tmp_path, SAMPLE_LOG, auto_approve=True, max_iterations=2)

    ep = loop.start()

    assert ep.phase in (Phase.AWAITING_APPROVAL, Phase.CONCLUDED)
    assert len([o for o in ep.observations if o.get("kind") == "recent_errors"]) >= 1
    assert ep.analysis is not None
    assert "summary" in ep.analysis and "conclusion" in ep.analysis

    # the heuristic planner hits "Connection refused" -> a log_grep task is proposed
    assert any(t["tool"] == "log_grep" for t in ep.tasks)
    assert all(
        t["status"] in ("proposed", "approved", "done", "failed", "rejected", "empty")
        for t in ep.tasks
    )

    # with auto-approval the run converges to concluded and at least one task result
    # lands in observations
    assert ep.phase == Phase.CONCLUDED
    assert any(o.get("kind") == "task_result" for o in ep.observations)

    # a reflection was written on completion
    assert len(memory.recent_reflections(limit=5)) >= 1


def test_loop_empty_log_still_concludes(tmp_path):
    loop, _memory = _make_loop(tmp_path, "2026-10-24 10:00:00 INFO all good\n")

    ep = loop.start()

    assert ep.phase == Phase.CONCLUDED
    assert ep.observations  # the recent_errors observation always exists (even when empty)
    assert ep.analysis["summary"]


def test_loop_awaits_approval_then_resume_concludes(tmp_path):
    loop, _memory = _make_loop(tmp_path, SAMPLE_LOG, auto_approve=False, max_iterations=2)

    ep = loop.start()
    assert ep.phase == Phase.AWAITING_APPROVAL
    proposed = [t for t in ep.tasks if t["status"] == "proposed"]
    assert proposed, "auto_approve=False must produce tasks awaiting approval"

    # resume before approval returns the episode untouched (proposed tasks remain)
    assert loop.resume(ep.id).phase == Phase.AWAITING_APPROVAL

    for task in proposed:
        loop.approve_task(ep.id, task["id"])
    ep2 = loop.resume(ep.id)
    assert ep2.phase == Phase.CONCLUDED
    assert any(t["status"] == "done" for t in ep2.tasks)


def test_loop_reject_task_leads_to_conclusion(tmp_path):
    loop, _memory = _make_loop(tmp_path, SAMPLE_LOG, auto_approve=False, max_iterations=1)

    ep = loop.start()
    proposed = [t for t in ep.tasks if t["status"] == "proposed"]
    assert proposed
    for task in proposed:
        loop.reject_task(ep.id, task["id"])

    ep2 = loop.resume(ep.id)
    assert ep2.phase == Phase.CONCLUDED
    assert all(t["status"] != "done" for t in ep2.tasks)


def test_loop_missing_log_fails_gracefully(tmp_path):
    # missing log file: must not crash; the episode enters failed with an error
    settings_log = tmp_path / "ghost.log"
    settings = dataclasses.replace(
        Settings(),
        log_path=str(settings_log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=2,
        auto_approve_readonly=True,
    )
    memory = Memory(str(tmp_path / "memory.sqlite3"))
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    loop = EvolutionLoop(
        settings=settings, memory=memory, tools=tools, llm=None, auto_approve_readonly=True
    )

    ep = loop.start()
    assert ep.phase == Phase.FAILED
    assert ep.error
    # the failure is present in the persisted state too
    assert memory.load(ep.id).phase == Phase.FAILED


def test_approve_nonreadonly_tool_raises_permissionerror(tmp_path):
    loop, _memory = _make_loop(tmp_path, SAMPLE_LOG, auto_approve=False)
    ep = loop.start()
    # hand-craft a proposed task with a non-read-only tool; the gate must refuse
    ep.tasks.append(
        {
            "id": "evil01",
            "tool": "process_kill",
            "params": {"pid": 1},
            "rationale": "restart the service",
            "risk": "high",
            "status": "proposed",
            "result": None,
            "created_at": ep.created_at,
        }
    )
    with pytest.raises(PermissionError):
        loop.approve_task(ep.id, "evil01")


def test_log_path_outside_whitelist_fails(tmp_path):
    # log_path outside the whitelist -> PermissionError -> episode failed
    outside_root = tmp_path / "elsewhere"
    outside_root.mkdir()
    log = outside_root / "x.log"
    log.write_text("ERROR x\n", encoding="utf-8")
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path / "inside"),
        max_iterations=2,
    )
    (tmp_path / "inside").mkdir()
    memory = Memory(str(tmp_path / "memory.sqlite3"))
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    loop = EvolutionLoop(
        settings=settings, memory=memory, tools=tools, llm=None, auto_approve_readonly=True
    )

    ep = loop.start()
    assert ep.phase == Phase.FAILED
    assert "PermissionError" in ep.error


class _ScriptedLLM:
    """Fake LLM capturing context: round 1 proposes a pattern guaranteed to match
    nothing, afterwards it proposes nothing."""

    def __init__(self, pattern):
        self.pattern = pattern
        self.analyze_calls = []
        self.plan_calls = []

    def is_available(self):
        return True

    def analyze(self, errors, extra=None, lessons=None, failed=None, similar=None):
        self.analyze_calls.append({"errors": list(errors), "failed": failed, "similar": similar})
        return {
            "summary": "scripted",
            "likely_root_causes": [],
            "information_gaps": [],
            "conclusion": "scripted conclusion",
        }

    def plan_tasks(self, errors, log_path, tools, existing=None, lessons=None, failed=None):
        self.plan_calls.append({"failed": failed, "existing": existing})
        if len(self.plan_calls) == 1:
            return {
                "tasks": [
                    {
                        "tool": "log_grep",
                        "params": {"path": log_path, "pattern": self.pattern, "limit": 50},
                        "rationale": "check ghost pattern",
                    }
                ]
            }
        return {"tasks": []}


def test_loop_feeds_empty_task_back_to_llm(tmp_path):
    # the first grep task returns an empty list -> marked empty ("No matches found");
    # round 2 must carry that failure into analyze/plan context so the LLM avoids
    # the disproven pattern
    log = tmp_path / "moray.err.log"
    log.write_text("2026-10-24 10:00:00 ERROR real error here\n", encoding="utf-8")
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=3,
        auto_approve_readonly=True,
    )
    memory = Memory(str(tmp_path / "memory.sqlite3"))
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    llm = _ScriptedLLM(pattern="GHOST_PATTERN_XYZ")
    loop = EvolutionLoop(
        settings=settings, memory=memory, tools=tools, llm=llm, auto_approve_readonly=True
    )

    ep = loop.start()

    assert ep.phase == Phase.CONCLUDED
    assert ep.tasks[0]["status"] == "empty"
    assert ep.tasks[0]["error"] == "No matches found"

    # round 2 analyze receives the failed/empty feedback
    assert len(llm.analyze_calls) >= 2
    second_failed = llm.analyze_calls[1]["failed"]
    assert second_failed, "round 2 analyze context must include the empty task"
    assert any(
        t["tool"] == "log_grep" and t["status"] == "empty" and t["error"] == "No matches found"
        for t in second_failed
    )

    # round 2 plan receives the same feedback (can avoid GHOST_PATTERN_XYZ)
    assert len(llm.plan_calls) >= 2
    plan_failed = llm.plan_calls[1]["failed"]
    assert any(t["tool"] == "log_grep" and t.get("error") == "No matches found" for t in plan_failed)
    assert "GHOST_PATTERN_XYZ" in repr(plan_failed)
