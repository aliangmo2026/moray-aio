"""自我迭代主循环：Observe → Analyze → Propose → (审批闸门) → Execute。

硬约束：
- 只调用 ToolRegistry 内的只读工具；高危操作绝不执行，只能出现在结论文本里。
- 单任务失败只标记 failed 并记录 error，不阻塞其他任务。
- 有待审批（proposed）任务时停在 AWAITING_APPROVAL，等人批准后 resume。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.kernel import approval
from app.kernel.planner import propose_tasks
from app.kernel.state import Episode, Phase
from app.tools import log_reader


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvolutionLoop:
    """只读自我迭代内核。"""

    def __init__(self, settings, memory, tools, llm=None, auto_approve_readonly: bool = True):
        self.settings = settings
        self.memory = memory
        self.tools = tools
        self.llm = llm
        self.auto_approve_readonly = bool(auto_approve_readonly)
        self._episodes: Dict[str, Episode] = {}

    # ---------- 公开 API ----------

    def start(self, trigger: str = "recent_errors") -> Episode:
        ep = Episode.new(trigger=trigger)
        self._episodes[ep.id] = ep
        self.memory.save(ep)
        try:
            self._run(ep)
        except Exception as exc:  # 内核级兜底：异常转 FAILED，绝不向上炸
            self._fail(ep, exc)
        self.memory.save(ep)
        return ep

    def resume(self, episode_id: str) -> Episode:
        ep = self.require(episode_id)
        if any(t.get("status") == "proposed" for t in ep.tasks):
            return ep  # 还有任务等人工审批，不动
        if ep.phase in (Phase.CONCLUDED, Phase.FAILED, Phase.CREATED):
            return ep
        try:
            if ep.phase == Phase.OBSERVING:
                self._run(ep)
            else:
                if ep.phase == Phase.AWAITING_APPROVAL:
                    ep.transition(Phase.EXECUTING)
                if ep.phase == Phase.EXECUTING:
                    self._execute_approved(ep)
                self._iterate(ep)
        except Exception as exc:
            self._fail(ep, exc)
        self.memory.save(ep)
        return ep

    def approve_task(self, episode_id: str, task_id: str) -> Episode:
        ep = self.require(episode_id)
        task = self._find_task(ep, task_id)
        if task["tool"] not in approval.READONLY_TOOLS:
            raise PermissionError(
                "refusing to approve non-readonly tool {!r}".format(task["tool"])
            )
        if task.get("status") != "proposed":
            raise ValueError(
                "task {} is not awaiting approval (status={!r})".format(task_id, task.get("status"))
            )
        task["status"] = "approved"
        self.memory.save(ep)
        return ep

    def reject_task(self, episode_id: str, task_id: str) -> Episode:
        ep = self.require(episode_id)
        task = self._find_task(ep, task_id)
        if task.get("status") != "proposed":
            raise ValueError(
                "task {} is not awaiting approval (status={!r})".format(task_id, task.get("status"))
            )
        task["status"] = "rejected"
        self.memory.save(ep)
        return ep

    def approve_all(self, episode_id: str) -> Episode:
        """批量批准：仅 status=proposed 且 tool 在只读白名单内的任务会被批准。"""
        ep = self.require(episode_id)
        for task in ep.tasks:
            if task.get("status") == "proposed" and task.get("tool") in approval.READONLY_TOOLS:
                task["status"] = "approved"
        self.memory.save(ep)
        return ep

    def reject_all(self, episode_id: str) -> Episode:
        """批量拒绝：所有 status=proposed 的任务一律 rejected。"""
        ep = self.require(episode_id)
        for task in ep.tasks:
            if task.get("status") == "proposed":
                task["status"] = "rejected"
        self.memory.save(ep)
        return ep

    def list_recent(self, limit: int = 20) -> List[Episode]:
        return self.memory.list_recent(limit=limit)

    def require(self, episode_id: str) -> Episode:
        ep = self._episodes.get(episode_id)
        if ep is None:
            ep = self.memory.load(episode_id)
            if ep is None:
                raise KeyError("episode not found: {}".format(episode_id))
            self._episodes[ep.id] = ep
        return ep

    # ---------- 内部流程 ----------

    def _run(self, ep: Episode) -> None:
        ep.transition(Phase.OBSERVING)
        self._observe(ep)
        self.memory.save(ep)
        self._iterate(ep)

    def _iterate(self, ep: Episode) -> None:
        """Analyze → Propose → (停在审批 | Execute → 回到 Analyze)；
        最多 max_iterations 轮；结束写 reflection。"""
        while ep.iteration < self.settings.max_iterations:
            ep.iteration += 1
            if ep.phase != Phase.ANALYZING:
                ep.transition(Phase.ANALYZING)
            self._analyze(ep)

            ep.transition(Phase.PROPOSING)
            new_tasks = self._propose(ep)
            if any(t.get("status") == "proposed" for t in new_tasks):
                ep.transition(Phase.AWAITING_APPROVAL)
                self.memory.save(ep)
                return

            if not any(t.get("status") == "approved" for t in ep.tasks):
                break  # 无新任务且无 approved 可执行，本轮收敛

            ep.transition(Phase.EXECUTING)
            self._execute_approved(ep)

        self._conclude(ep)

    def _observe(self, ep: Episode) -> None:
        # 白名单校验：log_path 必须落在只读根内，越界抛 PermissionError
        checked = self.tools._check_path(self.settings.log_path)
        errors = log_reader.recent_errors(
            checked,
            limit=self.settings.recent_error_limit,
            max_scan_bytes=self.settings.max_scan_bytes,
        )
        ep.observations.append(
            {
                "iteration": ep.iteration,
                "kind": "recent_errors",
                "source": str(checked),
                "errors": [e.to_dict() for e in errors],
            }
        )

    def _analyze(self, ep: Episode) -> None:
        errors = self._last_errors(ep)
        extra = [o for o in ep.observations if o.get("kind") != "recent_errors"]
        lessons = [r["lesson"] for r in self.memory.recent_reflections(limit=5)]
        failed = self._failed_tasks(ep)

        analysis = None
        if self.llm is not None and self.llm.is_available():
            try:
                analysis = self.llm.analyze(
                    errors=errors, extra=extra, lessons=lessons, failed=failed
                )
            except Exception:
                analysis = None  # LLM 抖动 → 启发式兜底
        if not isinstance(analysis, dict) or analysis.get("parse_error"):
            analysis = self._heuristic_analysis(errors)
        ep.analysis = analysis
        self.memory.save(ep)

    def _propose(self, ep: Episode) -> List[dict]:
        errors = self._last_errors(ep)
        extra = [o for o in ep.observations if o.get("kind") != "recent_errors"]
        lessons = [r["lesson"] for r in self.memory.recent_reflections(limit=5)]
        failed = self._failed_tasks(ep)
        use_llm = self.llm is not None and self.llm.is_available()
        new_tasks = propose_tasks(
            errors=errors,
            existing_tasks=ep.tasks,
            log_path=self.settings.log_path,
            tools=self.tools,
            llm=self.llm if use_llm else None,
            auto_approve_readonly=self.auto_approve_readonly,
            failed_tasks=failed,
        )
        for task in new_tasks:
            if task.get("status") == "pending":
                task["status"] = "proposed"  # decide()=pending → 等人工审批
        ep.tasks.extend(new_tasks)
        self.memory.save(ep)
        return new_tasks

    def _execute_approved(self, ep: Episode) -> None:
        for task in ep.tasks:
            if task.get("status") != "approved":
                continue
            try:
                result = self.tools.call(task["tool"], task.get("params") or {})
                task["result"] = result
                if isinstance(result, list) and len(result) == 0:
                    # 空结果：不算执行错误，但对定位根因没有贡献；
                    # 标记 empty 并记录原因，反馈给下一轮规划避开该 pattern/参数。
                    task["status"] = "empty"
                    task["error"] = "No matches found"
                else:
                    task["status"] = "done"
            except Exception as exc:
                # 单任务失败只标 failed 记 error，不阻塞其他任务
                task["status"] = "failed"
                task["result"] = None
                task["error"] = "{}: {}".format(type(exc).__name__, exc)
            ep.observations.append(
                {
                    "iteration": ep.iteration,
                    "kind": "task_result",
                    "task_id": task["id"],
                    "tool": task["tool"],
                    "status": task["status"],
                    "result": task["result"],
                    "error": task.get("error"),
                }
            )
        self.memory.save(ep)

    def _conclude(self, ep: Episode) -> None:
        analysis = ep.analysis if isinstance(ep.analysis, dict) else {}
        ep.conclusion = analysis.get("conclusion") or (
            "本轮迭代结束（达到最大轮数或无新增侦查任务）。"
        )
        if ep.phase != Phase.CONCLUDED:
            ep.transition(Phase.CONCLUDED)
        self._reflect(ep)
        self.memory.save(ep)

    def _reflect(self, ep: Episode) -> None:
        done = sum(1 for t in ep.tasks if t.get("status") == "done")
        failed = sum(1 for t in ep.tasks if t.get("status") == "failed")
        lesson = (
            "trigger={} iterations={} tasks={}(done={},failed={}) conclusion={}".format(
                ep.trigger, ep.iteration, len(ep.tasks), done, failed, ep.conclusion
            )
        )
        self.memory.record_reflection(ep.id, lesson)

    def _fail(self, ep: Episode, exc: Exception) -> None:
        ep.error = "{}: {}".format(type(exc).__name__, exc)
        if ep.phase not in (Phase.CONCLUDED, Phase.FAILED):
            try:
                ep.transition(Phase.FAILED)
            except ValueError:
                pass
        self.memory.record_reflection(ep.id, "episode failed: {}".format(ep.error))
        self.memory.save(ep)

    # ---------- 辅助 ----------

    def _last_errors(self, ep: Episode) -> List[str]:
        for obs in reversed(ep.observations):
            if obs.get("kind") == "recent_errors":
                return [e.get("text", "") for e in obs.get("errors", [])]
        return []

    def _failed_tasks(self, ep: Episode) -> List[dict]:
        """上一轮 failed/empty 任务及其原因，作为下一轮 LLM 的反馈上下文。"""
        out: List[dict] = []
        for task in ep.tasks:
            if task.get("status") in ("failed", "empty"):
                out.append(
                    {
                        "tool": task.get("tool"),
                        "params": task.get("params"),
                        "status": task.get("status"),
                        "error": task.get("error"),
                        "rationale": task.get("rationale"),
                    }
                )
        return out

    @staticmethod
    def _find_task(ep: Episode, task_id: str) -> dict:
        for task in ep.tasks:
            if task.get("id") == task_id:
                return task
        raise KeyError("task not found: {}".format(task_id))

    @staticmethod
    def _heuristic_analysis(errors: List[str]) -> dict:
        """无 LLM 时的关键词归因，产出 summary/likely_root_causes/information_gaps/conclusion。"""
        if not errors:
            return {
                "summary": "扫描窗口内没有发现 ERROR 级别日志。",
                "likely_root_causes": [],
                "information_gaps": [
                    "等待下一次错误发生；也可调大 recent_error_limit 或 max_scan_bytes。"
                ],
                "conclusion": "当前无错误可分析（只读结论，仅供参考）。",
            }
        blob = "\n".join(errors).lower()
        causes: List[str] = []
        gaps: List[str] = []
        if "connection refused" in blob or "econnrefused" in blob:
            causes.append("疑似下游服务未监听或端口不通（出现 Connection refused/ECONNREFUSED）。")
            gaps.append("需要确认对应服务进程与端口状态；建议由人工检查（本引擎只读不执行）。")
        if "timeout" in blob or "timed out" in blob:
            causes.append("疑似外部依赖响应过慢或网络抖动（出现 timeout/timed out）。")
            gaps.append("需要确认超时是否集中在同一依赖；建议用 log_grep 回溯更多超时记录。")
        if "no such file" in blob or "filenotfounderror" in blob:
            causes.append("疑似配置或数据文件路径缺失（出现 No such file/FileNotFoundError）。")
            gaps.append("需要确认缺失文件应否存在；建议用 log_grep 回溯首次出现位置。")
        if not causes:
            causes.append("错误模式未命中已知启发式规则，需要人工查看原始日志。")
            gaps.append("建议用 log_tail/log_grep 获取更多上下文行。")
        summary = "启发式归因：在最近 {} 条 ERROR 中识别到 {} 类模式。".format(len(errors), len(causes))
        conclusion = (
            "基于只读日志分析的初步结论：" + "；".join(causes)
            + " 本引擎只读分析、不做任何变更；如需重启/清理等高危操作，请由人工评估后执行。"
        )
        return {
            "summary": summary,
            "likely_root_causes": causes,
            "information_gaps": gaps,
            "conclusion": conclusion,
        }
