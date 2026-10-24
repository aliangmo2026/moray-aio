"""内核主循环启发式路径测试：llm=None、auto_approve、审批暂停/恢复全链路。"""

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
        "2026-10-24 10:02:00 ERROR Connection refused to 127.0.0.1:6379",
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

    # 启发式规划器命中 Connection refused → 生成 log_grep 侦查任务
    assert any(t["tool"] == "log_grep" for t in ep.tasks)
    assert all(
        t["status"] in ("proposed", "approved", "done", "failed", "rejected", "empty")
        for t in ep.tasks
    )

    # 自动批准路径下收敛为 concluded，且至少有一条任务结果回填进 observations
    assert ep.phase == Phase.CONCLUDED
    assert any(o.get("kind") == "task_result" for o in ep.observations)

    # 结束时写入了 reflection
    assert len(memory.recent_reflections(limit=5)) >= 1


def test_loop_empty_log_still_concludes(tmp_path):
    loop, _memory = _make_loop(tmp_path, "2026-10-24 10:00:00 INFO all good\n")

    ep = loop.start()

    assert ep.phase == Phase.CONCLUDED
    assert ep.observations  # recent_errors 观察一定存在（哪怕结果为空）
    assert ep.analysis["summary"]


def test_loop_awaits_approval_then_resume_concludes(tmp_path):
    loop, _memory = _make_loop(tmp_path, SAMPLE_LOG, auto_approve=False, max_iterations=2)

    ep = loop.start()
    assert ep.phase == Phase.AWAITING_APPROVAL
    proposed = [t for t in ep.tasks if t["status"] == "proposed"]
    assert proposed, "auto_approve=False 时应产生待审批任务"

    # 未批准前 resume 应原样返回（还有 proposed）
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
    # 日志文件不存在：不能崩，episode 进入 failed 并记录 error
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
    # 持久化里能查到这条失败记录
    assert memory.load(ep.id).phase == Phase.FAILED


def test_approve_nonreadonly_tool_raises_permissionerror(tmp_path):
    loop, _memory = _make_loop(tmp_path, SAMPLE_LOG, auto_approve=False)
    ep = loop.start()
    # 手工构造一个非只读工具的待审批任务，验证审批闸门拒绝
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
    # log_path 指向白名单之外 → PermissionError → episode failed
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
    """捕获上下文的假 LLM：第一轮给出一个必然空结果的 pattern，之后不再提任务。"""

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
    # 第一个 grep 任务返回空列表 → 标记 empty("No matches found")；
    # 第二轮 analyze/plan 的 LLM 上下文必须携带该失败信息，供其避开无效 pattern
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

    # 第二轮 analyze 收到 failed/empty 反馈
    assert len(llm.analyze_calls) >= 2
    second_failed = llm.analyze_calls[1]["failed"]
    assert second_failed, "第二轮 analyze 的上下文必须包含 empty 任务"
    assert any(
        t["tool"] == "log_grep" and t["status"] == "empty" and t["error"] == "No matches found"
        for t in second_failed
    )

    # 第二轮 plan 收到同样的反馈（可据此避开 GHOST_PATTERN_XYZ）
    assert len(llm.plan_calls) >= 2
    plan_failed = llm.plan_calls[1]["failed"]
    assert any(t["tool"] == "log_grep" and t.get("error") == "No matches found" for t in plan_failed)
    # 反馈里必须能定位到无效 pattern 本身
    assert "GHOST_PATTERN_XYZ" in repr(plan_failed)
