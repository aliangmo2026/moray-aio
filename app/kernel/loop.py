"""Self-evolution main loop: Observe → Analyze → Propose → (approval gate) → Execute.

Hard rules:
- Only calls read-only tools from the ToolRegistry; dangerous operations are never
  executed and may only appear as suggestion text inside conclusions;
- A single failing task is marked failed with its error and never blocks the others;
- When proposed tasks exist the loop halts at AWAITING_APPROVAL until a human
  approves and resumes;
- A per-episode LLM token budget circuit breaker guards against runaway loops
  (effective in both sync and async modes).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.kernel import approval
from app.kernel.planner import propose_tasks
from app.kernel.repository import extract_keywords
from app.kernel.state import Episode, Phase
from app.kernel.vector_memory import VectorMemory
from app.kernel.events import publish as publish_event
from app.tools import clusterer, log_reader


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvolutionLoop:
    """Read-only self-evolution kernel."""

    def __init__(
        self,
        settings,
        memory,
        tools,
        llm=None,
        auto_approve_readonly: bool = True,
        vector_memory: Optional[VectorMemory] = None,
    ):
        self.settings = settings
        self.memory = memory
        self.tools = tools
        self.llm = llm
        self.auto_approve_readonly = bool(auto_approve_readonly)
        # Semantic long-term memory (v2.0). Optional and fail-soft: when absent
        # or unavailable, _retrieve_similar falls back to keyword Jaccard.
        self.vector_memory = vector_memory
        self._episodes: Dict[str, Episode] = {}
        # Async execution: the loop is fully synchronous code, so a thread pool is
        # simpler and more direct than FastAPI BackgroundTasks
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(getattr(settings, "async_workers", 2))),
            thread_name_prefix="moray-loop",
        )

    # ---------- public API ----------

    def start(self, trigger: str = "recent_errors") -> Episode:
        ep = Episode.new(trigger=trigger)
        self._episodes[ep.id] = ep
        self.memory.save(ep)
        try:
            self._run(ep)
        except Exception as exc:  # kernel-level guard: exceptions become FAILED, never raised
            self._fail(ep, exc)
        self.memory.save(ep)
        return ep

    def start_async(self, trigger: str = "recent_errors") -> str:
        """Async entry: creates the Episode (phase=created) and runs it in the
        background, returning the episode_id immediately.

        Transition chain: created -> queued (enqueued, not started) -> observing -> ...
        Used by POST /episodes so slow LLM calls never block HTTP.
        """
        ep = Episode.new(trigger=trigger)
        self._episodes[ep.id] = ep
        self.memory.save(ep)
        self._executor.submit(self._run_background, ep)
        return ep.id

    def _run_background(self, ep: Episode) -> None:
        try:
            if ep.phase == Phase.CREATED:
                ep.transition(Phase.QUEUED)
                self.memory.save(ep)
            self._run(ep)
        except Exception as exc:
            self._fail(ep, exc)
        finally:
            self.memory.save(ep)

    def resume(self, episode_id: str) -> Episode:
        """Resume a run: if proposed tasks remain, return as-is; otherwise execute
        approved tasks and continue the analyze loop."""
        ep = self.require(episode_id)
        if any(t.get("status") == "proposed" for t in ep.tasks):
            return ep  # still waiting for human approval
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
        """Approve one proposed task; non-read-only tools raise PermissionError."""
        ep = self.require(episode_id)
        task = self._find_task(ep, task_id)
        if task["tool"] not in approval.SAFE_TOOLS:
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
        """Reject one proposed task."""
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
        """Batch approve: only status=proposed tasks whose tool is in the read-only
        whitelist (log tools + probes) get approved."""
        ep = self.require(episode_id)
        for task in ep.tasks:
            if task.get("status") == "proposed" and task.get("tool") in approval.SAFE_TOOLS:
                task["status"] = "approved"
        self.memory.save(ep)
        return ep

    def reject_all(self, episode_id: str) -> Episode:
        """Batch reject: every status=proposed task becomes rejected."""
        ep = self.require(episode_id)
        for task in ep.tasks:
            if task.get("status") == "proposed":
                task["status"] = "rejected"
        self.memory.save(ep)
        return ep

    def list_recent(self, limit: int = 20) -> List[Episode]:
        return self.memory.list_recent(limit=limit)

    def require(self, episode_id: str) -> Episode:
        """Load an episode (memory cache first, repository second); KeyError if missing."""
        ep = self._episodes.get(episode_id)
        if ep is None:
            ep = self.memory.load(episode_id)
            if ep is None:
                raise KeyError("episode not found: {}".format(episode_id))
            self._episodes[ep.id] = ep
        return ep

    # ---------- internal flow ----------

    def _run(self, ep: Episode) -> None:
        ep.transition(Phase.OBSERVING)
        self._timed(ep, "observe", self._observe, ep)
        self.memory.save(ep)
        self._iterate(ep)

    def _timed(self, ep: Episode, stage: str, fn: Callable[..., Any], *args: Any) -> Any:
        """Trace one core stage: stage/iteration/phase/duration/timestamp/LLM tokens."""
        started = time.perf_counter()
        result = fn(*args)
        ep.trace.append(
            {
                "stage": stage,
                "iteration": ep.iteration,
                "phase": ep.phase.value,
                "timestamp": _now(),
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "llm": self._take_usage(),
            }
        )
        return result

    def _take_usage(self) -> Optional[dict]:
        if self.llm is None:
            return None
        take = getattr(self.llm, "take_last_usage", None)
        return take() if callable(take) else None

    def _iterate(self, ep: Episode) -> None:
        """Analyze → Propose → (halt at approval | Execute → back to Analyze);
        at most max_iterations rounds; writes a reflection when finished."""
        while ep.iteration < self.settings.max_iterations:
            ep.iteration += 1
            if ep.phase != Phase.ANALYZING:
                ep.transition(Phase.ANALYZING)
            self._timed(ep, "analyze", self._analyze, ep)
            if self._break_on_token_budget(ep):
                return

            ep.transition(Phase.PROPOSING)
            new_tasks = self._timed(ep, "propose", self._propose, ep)
            if self._break_on_token_budget(ep):
                return
            if any(t.get("status") == "proposed" for t in new_tasks):
                ep.transition(Phase.AWAITING_APPROVAL)
                self.memory.save(ep)
                return

            if not any(t.get("status") == "approved" for t in ep.tasks):
                break  # no new tasks and nothing approved left: converged

            ep.transition(Phase.EXECUTING)
            self._timed(ep, "execute", self._execute_approved, ep)

        self._conclude(ep)

    def _break_on_token_budget(self, ep: Episode) -> bool:
        """Token budget circuit breaker: once accumulated LLM usage exceeds the cap,
        immediately FAIL the episode and record a circuit_break trace event.
        Sync and async modes share _iterate, so both are protected."""
        used = 0
        for entry in ep.trace:
            usage = entry.get("llm") or {}
            used += int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        limit = int(getattr(self.settings, "max_tokens_per_episode", 50000))
        if used <= limit:
            return False
        ep.error = "Token budget exceeded: {} / {}".format(used, limit)
        ep.trace.append(
            {
                "stage": "circuit_break",
                "iteration": ep.iteration,
                "phase": ep.phase.value,
                "timestamp": _now(),
                "duration_ms": 0.0,
                "llm": None,
                "detail": {"used_tokens": used, "max_tokens": limit},
            }
        )
        if ep.phase not in (Phase.CONCLUDED, Phase.FAILED):
            try:
                ep.transition(Phase.FAILED)
            except ValueError:
                pass
        self.memory.save(ep)
        return True

    def _observe(self, ep: Episode) -> None:
        log_dir = str(getattr(self.settings, "log_dir", "") or "").strip()
        if log_dir:
            self._observe_rolling(ep, log_dir)
        else:
            self._observe_single(ep)

    def _observe_single(self, ep: Episode) -> None:
        # Whitelist check: log_path must live inside the read-only roots,
        # otherwise PermissionError is raised
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
                "mode": "single",
                "source": str(checked),
                "errors": [e.to_dict() for e in errors],
            }
        )

    def _observe_rolling(self, ep: Episode, log_dir: str) -> None:
        """Rolling observation: take the latest N ERROR lines across rotated files
        (still constrained by the read-only whitelist)."""
        checked = self.tools._check_path(str(log_dir))
        pattern = str(getattr(self.settings, "log_glob_pattern", "moray.err.log*"))
        lines = log_reader.read_rolling(
            checked,
            pattern,
            n=self.settings.recent_error_limit,
            max_files=5,
            max_scan_bytes=self.settings.max_scan_bytes,
            keyword="ERROR",
        )
        ep.observations.append(
            {
                "iteration": ep.iteration,
                "kind": "recent_errors",
                "mode": "rolling",
                "source": str(checked),
                "glob": pattern,
                "errors": [{"text": text} for text in lines],
            }
        )

    def _analyze(self, ep: Episode) -> None:
        raw_errors = self._last_errors(ep)
        # Cluster before feeding the LLM: N identical errors carry no extra signal
        clusters = clusterer.cluster(
            raw_errors, top_n=int(getattr(self.settings, "cluster_top_n", 10))
        )
        clustered_errors = [
            "[x{}] {} | 样本: {}".format(c["count"], c["template"], c["sample_line"])
            for c in clusters
        ]
        extra = self._summarize_extra(ep)
        lessons = [r["lesson"] for r in self.memory.recent_reflections(limit=5)]
        failed = self._failed_tasks(ep)
        # Retrieve similar history: semantic vector memory first, keyword Jaccard fallback
        similar, retrieval_mode = self._retrieve_similar(ep, raw_errors, clustered_errors)
        ep.trace.append(
            {
                "stage": "memory_retrieval",
                "iteration": ep.iteration,
                "phase": ep.phase.value,
                "timestamp": _now(),
                "duration_ms": 0.0,
                "llm": None,
                "detail": {"mode": retrieval_mode, "hits": len(similar)},
            }
        )

        analysis: Optional[dict] = None
        if self.llm is not None and self.llm.is_available():
            try:
                analysis = self.llm.analyze(
                    errors=clustered_errors,
                    extra=extra,
                    lessons=lessons,
                    failed=failed,
                    similar=similar,
                )
            except Exception:
                analysis = None  # LLM hiccup -> heuristic fallback
            llm_ok = isinstance(analysis, dict) and not analysis.get("parse_error")
            if (
                llm_ok
                and bool(getattr(self.settings, "critic_enabled", True))
                and callable(getattr(self.llm, "critic_review", None))
                and callable(getattr(self.llm, "stash_usage", None))
            ):
                # v2.0 dual-review: park the first call's token usage aside (the
                # critic/reflexion calls would overwrite the thread-local slot),
                # let the Critic Agent audit, then restore it for _timed()'s
                # analyze-stage attribution — every extra call gets its own
                # trace entry, so the token budget breaker counts them all.
                first_usage = self._take_usage()
                analysis = self._critique_and_revise(
                    ep,
                    analysis,
                    context={
                        "errors": clustered_errors,
                        "extra": extra,
                        "lessons": lessons,
                        "failed": failed,
                        "similar": similar,
                    },
                    first_usage=first_usage,
                )
        if not isinstance(analysis, dict) or analysis.get("parse_error"):
            analysis = self._heuristic_analysis(clustered_errors)
        ep.analysis = analysis
        self.memory.save(ep)

    def _critique_and_revise(
        self, ep: Episode, analysis: dict, context: dict, first_usage: Optional[dict]
    ) -> dict:
        """Dual-review (v2.0): the Critic Agent audits the Analyst conclusion
        ("evidence-backed? omissions? logic jumps?"); substantive objections
        (verdict=revise) trigger a Reflexion round where the Analyst re-reasons
        with the criticism injected into the prompt.

        Every round lands in ep.trace (stage critic_review / reflexion) and on
        the SSE event bus, so the dashboard shows the internal debate. Fail-soft:
        a broken/unusable critic never blocks, replaces, or delays the analysis.
        """
        critic_fn = getattr(self.llm, "critic_review", None)
        if not callable(critic_fn):
            self._stash_usage(first_usage)
            return analysis
        max_rounds = max(0, int(getattr(self.settings, "critic_max_reflexion_rounds", 1)))
        review: Optional[dict] = None
        try:
            for round_idx in range(max_rounds + 1):
                started = time.perf_counter()
                try:
                    review = critic_fn(errors=context["errors"], analysis=analysis)
                except Exception:
                    review = None
                usage = self._take_usage()
                duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
                if not isinstance(review, dict) or review.get("parse_error"):
                    if review is not None:
                        # unusable critic output: record it, keep the analysis
                        self._trace_critic(
                            ep, round_idx, {"verdict": "unusable", "objections": []}, usage, duration_ms
                        )
                    break
                verdict = str(review.get("verdict", "")).strip().lower()
                objections = [str(o) for o in (review.get("objections") or []) if str(o).strip()]
                self._trace_critic(ep, round_idx, review, usage, duration_ms)
                if verdict != "revise" or not objections:
                    break
                if round_idx >= max_rounds:
                    break  # reflexion budget exhausted: keep the current analysis
                revised = self._reflexion(ep, round_idx, context, objections)
                if revised is None:
                    break  # re-analysis failed/parsed badly: keep the previous one
                analysis = revised
        finally:
            self._stash_usage(first_usage)
        # surface the final verdict in the analysis payload (API/dashboard visibility)
        if isinstance(review, dict) and not review.get("parse_error"):
            analysis = dict(analysis)
            analysis["critic_review"] = {
                "verdict": str(review.get("verdict", "")),
                "objections": [str(o) for o in (review.get("objections") or [])],
            }
        return analysis

    def _reflexion(
        self, ep: Episode, round_idx: int, context: dict, objections: List[str]
    ) -> Optional[dict]:
        """One Reflexion round: re-run the analysis with the critic's objections
        injected; returns the revised dict or None (previous analysis is kept)."""
        started = time.perf_counter()
        try:
            revised = self.llm.analyze(
                errors=context["errors"],
                extra=context["extra"],
                lessons=context["lessons"],
                failed=context["failed"],
                similar=context["similar"],
                critic_feedback=objections,
            )
        except Exception:
            revised = None
        usage = self._take_usage()
        valid = isinstance(revised, dict) and not revised.get("parse_error")
        ep.trace.append(
            {
                "stage": "reflexion",
                "iteration": ep.iteration,
                "phase": ep.phase.value,
                "timestamp": _now(),
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "llm": usage,
                "detail": {
                    "round": round_idx + 1,
                    "objections_fed": len(objections),
                    "revised": valid,
                },
            }
        )
        publish_event(
            "critic_review",
            {
                "episode_id": ep.id,
                "kind": "reflexion",
                "round": round_idx + 1,
                "objections": objections[:5],
                "revised": valid,
            },
        )
        return revised if valid else None

    def _trace_critic(
        self, ep: Episode, round_idx: int, review: dict, usage: Optional[dict], duration_ms: float
    ) -> None:
        """Record one Critic verdict into the episode trace + SSE bus."""
        objections = [str(o) for o in (review.get("objections") or [])]
        ep.trace.append(
            {
                "stage": "critic_review",
                "iteration": ep.iteration,
                "phase": ep.phase.value,
                "timestamp": _now(),
                "duration_ms": duration_ms,
                "llm": usage,
                "detail": {
                    "round": round_idx,
                    "verdict": str(review.get("verdict", "")),
                    "objections": objections,
                    "missing_evidence": [str(m) for m in (review.get("missing_evidence") or [])],
                    "logic_issues": [str(m) for m in (review.get("logic_issues") or [])],
                    "confidence": review.get("confidence"),
                },
            }
        )
        publish_event(
            "critic_review",
            {
                "episode_id": ep.id,
                "kind": "verdict",
                "round": round_idx,
                "verdict": str(review.get("verdict", "")),
                "objections": objections[:5],
            },
        )

    def _stash_usage(self, usage: Optional[dict]) -> None:
        """Re-park a taken usage record so _timed()'s stage attribution still
        sees the first LLM call's tokens (best-effort; fakes may not support it)."""
        if usage is None:
            return
        stash = getattr(self.llm, "stash_usage", None)
        if callable(stash):
            stash(usage)

    def _retrieve_similar(
        self, ep: Episode, raw_errors: List[str], clustered_errors: List[str]
    ) -> Tuple[List[dict], str]:
        """Retrieve similar historical conclusions for the LLM prompt.

        v2.0 order: semantic vector memory (Ollama embeddings + local vector
        store) first; keyword Jaccard as the guaranteed fallback. Returns
        (cases, mode) where mode is "vector" / "jaccard" / "none" (traceable).
        Any vector-memory failure degrades to Jaccard, never raises.
        """
        vm = self.vector_memory
        if vm is not None:
            query = "\n".join(list(clustered_errors)[:10])
            try:
                hits = vm.search(query, top_k=3, exclude_episode_id=ep.id) if query else []
            except Exception:  # noqa: BLE001 — memory must never break analysis
                hits = []
            if hits:
                return hits, "vector"
        keywords = extract_keywords(raw_errors, top_n=10)
        similar = (
            self.memory.find_similar(keywords, top_k=3, exclude_episode_id=ep.id)
            if keywords
            else []
        )
        return similar, ("jaccard" if similar else "none")

    @staticmethod
    def _summarize_extra(ep: Episode) -> List[dict]:
        """Compress probe results before injecting them into the LLM
        (process_list 50 rows -> count + first 3 names)."""
        out: List[dict] = []
        for obs in ep.observations:
            if obs.get("kind") == "recent_errors":
                continue
            if obs.get("kind") in ("task_result", "probe") and obs.get("tool") == "process_list":
                result = obs.get("result") or {}
                processes = result.get("processes") or []
                out.append(
                    {
                        "kind": obs.get("kind"),
                        "tool": "process_list",
                        "task_id": obs.get("task_id"),
                        "status": obs.get("status"),
                        "summary": {
                            "count": result.get("count"),
                            "sample_names": [p.get("name") for p in processes[:3]],
                        },
                        "error": obs.get("error"),
                    }
                )
                continue
            if (
                obs.get("kind") in ("task_result", "probe")
                and obs.get("tool") == "windows_service_status"
            ):
                result = obs.get("result") or {}
                out.append(
                    {
                        "kind": obs.get("kind"),
                        "tool": "windows_service_status",
                        "task_id": obs.get("task_id"),
                        "status": obs.get("status"),
                        "summary": {"name": result.get("name"), "state": result.get("state")},
                        "error": obs.get("error"),
                    }
                )
                continue
            out.append(obs)
        return out

    def _propose(self, ep: Episode) -> List[dict]:
        errors = self._last_errors(ep)
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
                task["status"] = "proposed"  # decide()=pending -> awaiting human approval
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
                    # Empty result: not an execution error, but it contributes nothing
                    # to root-cause analysis; mark empty and feed the reason back so
                    # the next planning round avoids this pattern/parameters.
                    task["status"] = "empty"
                    task["error"] = "No matches found"
                else:
                    task["status"] = "done"
            except Exception as exc:
                # one failing task never blocks the others
                task["status"] = "failed"
                task["result"] = None
                task["error"] = "{}: {}".format(type(exc).__name__, exc)
            obs_kind = "probe" if task["tool"] in approval.PROBE_TOOLS else "task_result"
            ep.observations.append(
                {
                    "iteration": ep.iteration,
                    "kind": obs_kind,
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
            "This run ended (max rounds reached or no new investigation tasks)."
        )
        self._remember_conclusion(ep)
        if ep.phase != Phase.CONCLUDED:
            ep.transition(Phase.CONCLUDED)
        self._reflect(ep)
        self.memory.save(ep)

    def _remember_conclusion(self, ep: Episode) -> None:
        """Write (error features + conclusion) into semantic long-term memory so
        future episodes can retrieve it by meaning. Fail-soft: a memory failure
        never blocks concluding, and it is visible in the trace."""
        vm = self.vector_memory
        if vm is None or not ep.conclusion:
            return
        query = self._memory_query_text(ep)
        if not query:
            return
        started = time.perf_counter()
        try:
            stored = vm.remember(ep.id, query, str(ep.conclusion))
        except Exception:  # noqa: BLE001 — fail-soft by contract
            stored = False
        ep.trace.append(
            {
                "stage": "memory_write",
                "iteration": ep.iteration,
                "phase": ep.phase.value,
                "timestamp": _now(),
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "llm": None,
                "detail": {"stored": stored},
            }
        )

    @staticmethod
    def _memory_query_text(ep: Episode) -> str:
        """The error-feature text used as the memory key (latest error block)."""
        for obs in reversed(ep.observations):
            if obs.get("kind") == "recent_errors":
                lines = [str(e.get("text", "")) for e in obs.get("errors", []) if e.get("text")]
                return "\n".join(lines)[:4000]
        return ""

    def _reflect(self, ep: Episode) -> None:
        done = sum(1 for t in ep.tasks if t.get("status") == "done")
        empty = sum(1 for t in ep.tasks if t.get("status") == "empty")
        failed = sum(1 for t in ep.tasks if t.get("status") == "failed")
        lesson = (
            "trigger={} iterations={} tasks={}(done={},empty={},failed={}) conclusion={}".format(
                ep.trigger, ep.iteration, len(ep.tasks), done, empty, failed, ep.conclusion
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

    # ---------- helpers ----------

    def _last_errors(self, ep: Episode) -> List[str]:
        for obs in reversed(ep.observations):
            if obs.get("kind") == "recent_errors":
                return [e.get("text", "") for e in obs.get("errors", [])]
        return []

    def _failed_tasks(self, ep: Episode) -> List[dict]:
        """Failed/empty tasks from previous rounds with their reasons — fed back to
        the LLM as context so the next round avoids disproven patterns."""
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
        """Keyword attribution without an LLM; produces
        summary/likely_root_causes/information_gaps/conclusion."""
        if not errors:
            return {
                "summary": "No ERROR-level lines found in the scan window.",
                "likely_root_causes": [],
                "information_gaps": [
                    "Wait for the next error; or raise recent_error_limit / max_scan_bytes."
                ],
                "conclusion": "No errors to analyze right now (read-only conclusion, for reference only).",
            }
        blob = "\n".join(errors).lower()
        causes: List[str] = []
        gaps: List[str] = []
        if "connection refused" in blob or "econnrefused" in blob:
            causes.append(
                "Suspected downstream service not listening or port blocked "
                "(Connection refused/ECONNREFUSED found)."
            )
            gaps.append("Confirm the service process and port manually; this engine is read-only.")
        if "timeout" in blob or "timed out" in blob:
            causes.append("Suspected slow external dependency or network jitter (timeout/timed out found).")
            gaps.append("Check whether timeouts concentrate on one dependency; grep more records.")
        if "no such file" in blob or "filenotfounderror" in blob:
            causes.append("Suspected missing config/data file (No such file/FileNotFoundError found).")
            gaps.append("Verify whether the missing file should exist; grep its first occurrence.")
        if not causes:
            causes.append("Error pattern matched no known heuristic; manual inspection required.")
            gaps.append("Fetch more context lines with log_tail/log_grep.")
        summary = "Heuristic attribution: {} error pattern(s) found in the latest {} ERROR lines.".format(
            len(causes), len(errors)
        )
        conclusion = (
            "Preliminary read-only log analysis: "
            + "; ".join(causes)
            + " This engine only analyzes and never changes anything; dangerous operations such as"
            " restart/cleanup must be evaluated and executed by a human."
        )
        return {
            "summary": summary,
            "likely_root_causes": causes,
            "information_gaps": gaps,
            "conclusion": conclusion,
        }
