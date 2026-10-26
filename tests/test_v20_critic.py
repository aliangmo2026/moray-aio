"""v2.0 Critic Agent dual-review tests: objection -> Reflexion round, trace
records of the internal debate, token attribution across the extra LLM calls,
fail-soft behavior (broken/unusable critic never blocks analysis), and the
OllamaClient critic prompt path."""

from __future__ import annotations

import dataclasses
import json as _jsonlib

from app.config import Settings
from app.kernel.loop import EvolutionLoop
from app.kernel.sqlite_repository import Memory
from app.kernel.state import Phase
from app.tools.registry import ToolRegistry

SAMPLE_LOG = "\n".join(
    [
        "2026-10-25 10:00:00 INFO service started",
        "2026-10-25 10:01:00 ERROR Connection refused to 127.0.0.1:6379",
    ]
)


class _CriticLLM:
    """Scripted Analyst+Critic with OllamaClient-style usage tracking
    (take_last_usage / stash_usage) so token attribution can be asserted."""

    def __init__(self, reviews=None, critic_raises=False):
        self.reviews = list(reviews or [])
        self.critic_raises = bool(critic_raises)
        self.analyze_calls = []
        self.critic_calls = 0
        self._usage = None

    def is_available(self):
        return True

    def _set_usage(self, tag):
        self._usage = {"prompt_tokens": 10, "completion_tokens": 5, "retries": 0, "call": tag}

    def take_last_usage(self):
        usage, self._usage = self._usage, None
        return usage

    def stash_usage(self, usage):
        self._usage = usage

    def analyze(
        self, errors, extra=None, lessons=None, failed=None, similar=None, critic_feedback=None
    ):
        self.analyze_calls.append({"critic_feedback": list(critic_feedback or [])})
        self._set_usage("analyze")
        round_no = len(self.analyze_calls)
        return {
            "summary": "round {}".format(round_no),
            "likely_root_causes": ["cause {}".format(round_no)],
            "information_gaps": [],
            "conclusion": "conclusion round {}".format(round_no),
        }

    def critic_review(self, errors, analysis):
        idx = self.critic_calls
        self.critic_calls += 1
        self._set_usage("critic")
        if self.critic_raises:
            raise RuntimeError("critic backend down")
        if idx < len(self.reviews):
            return self.reviews[idx]
        return {
            "verdict": "pass",
            "objections": [],
            "missing_evidence": [],
            "logic_issues": [],
            "confidence": 0.9,
        }


def _make_loop(tmp_path, llm, critic_enabled=True, max_reflexion=1, max_iterations=1):
    log = tmp_path / "moray.err.log"
    log.write_text(SAMPLE_LOG, encoding="utf-8")
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=max_iterations,
        auto_approve_readonly=True,
        critic_enabled=critic_enabled,
        critic_max_reflexion_rounds=max_reflexion,
    )
    memory = Memory(str(tmp_path / "memory.sqlite3"))
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    loop = EvolutionLoop(
        settings=settings, memory=memory, tools=tools, llm=llm, auto_approve_readonly=True
    )
    return loop, memory


def _revise(*objections):
    return {
        "verdict": "revise",
        "objections": list(objections),
        "missing_evidence": ["port 6379 reachability"],
        "logic_issues": ["correlation treated as causation"],
        "confidence": 0.3,
    }


# ---------- objection -> reflexion -> pass ----------


def test_critic_objection_triggers_one_reflexion_round(tmp_path):
    llm = _CriticLLM(reviews=[_revise("结论没有引用 Connection refused 日志证据")])
    loop, _ = _make_loop(tmp_path, llm)
    ep = loop.start()

    assert ep.phase == Phase.CONCLUDED
    # analyst re-reasoned once with the objections in context
    assert len(llm.analyze_calls) == 2
    assert llm.analyze_calls[1]["critic_feedback"] == ["结论没有引用 Connection refused 日志证据"]
    # the revised conclusion won
    assert ep.analysis["conclusion"] == "conclusion round 2"
    assert ep.analysis["critic_review"]["verdict"] == "pass"

    # the internal debate is fully recorded in the trace
    critic_entries = [e for e in ep.trace if e["stage"] == "critic_review"]
    reflexion_entries = [e for e in ep.trace if e["stage"] == "reflexion"]
    assert [e["detail"]["verdict"] for e in critic_entries] == ["revise", "pass"]
    assert critic_entries[0]["detail"]["objections"] == ["结论没有引用 Connection refused 日志证据"]
    assert critic_entries[0]["detail"]["missing_evidence"] == ["port 6379 reachability"]
    assert len(reflexion_entries) == 1
    assert reflexion_entries[0]["detail"]["objections_fed"] == 1
    assert reflexion_entries[0]["detail"]["revised"] is True


def test_token_attribution_across_debate_calls(tmp_path):
    llm = _CriticLLM(reviews=[_revise("evidence gap")])
    loop, _ = _make_loop(tmp_path, llm)
    ep = loop.start()

    by_stage = {}
    for entry in ep.trace:
        by_stage.setdefault(entry["stage"], entry)
    # the analyze stage keeps the FIRST call's usage (restored via stash_usage)
    assert by_stage["analyze"]["llm"]["call"] == "analyze"
    # the extra debate calls are attributed to their own trace entries -> the
    # token budget breaker sees every token
    assert by_stage["critic_review"]["llm"]["call"] == "critic"
    assert by_stage["reflexion"]["llm"]["call"] == "analyze"
    critic_entries = [e for e in ep.trace if e["stage"] == "critic_review"]
    assert all(entry["llm"] is not None for entry in critic_entries)


def test_critic_pass_never_triggers_reflexion(tmp_path):
    llm = _CriticLLM()  # default: immediate pass
    loop, _ = _make_loop(tmp_path, llm)
    ep = loop.start()

    assert len(llm.analyze_calls) == 1
    assert not any(e["stage"] == "reflexion" for e in ep.trace)
    critic_entries = [e for e in ep.trace if e["stage"] == "critic_review"]
    assert len(critic_entries) == 1
    assert ep.analysis["conclusion"] == "conclusion round 1"
    assert ep.analysis["critic_review"]["verdict"] == "pass"


# ---------- budget & fail-soft ----------


def test_reflexion_budget_exhausted_keeps_last_revision(tmp_path):
    # the critic always objects; with max_reflexion=1 only one revision happens
    llm = _CriticLLM(reviews=[_revise("r0-a"), _revise("r0-b")])
    loop, _ = _make_loop(tmp_path, llm, max_reflexion=1)
    ep = loop.start()

    assert len(llm.analyze_calls) == 2  # initial + exactly one reflexion
    critic_entries = [e for e in ep.trace if e["stage"] == "critic_review"]
    assert [e["detail"]["round"] for e in critic_entries] == [0, 1]
    # final state honestly shows the unresolved disagreement
    assert ep.analysis["critic_review"]["verdict"] == "revise"
    assert ep.analysis["critic_review"]["objections"] == ["r0-b"]
    assert ep.phase == Phase.CONCLUDED


def test_critic_exception_never_blocks_analysis(tmp_path):
    llm = _CriticLLM(critic_raises=True)
    loop, _ = _make_loop(tmp_path, llm)
    ep = loop.start()

    assert ep.phase == Phase.CONCLUDED
    assert ep.analysis["conclusion"] == "conclusion round 1"
    assert "critic_review" not in ep.analysis  # no verdict to surface
    assert not any(e["stage"] == "reflexion" for e in ep.trace)


def test_unusable_critic_output_recorded_and_skipped(tmp_path):
    llm = _CriticLLM(reviews=[{"raw": "I refuse to output JSON", "parse_error": True}])
    loop, _ = _make_loop(tmp_path, llm)
    ep = loop.start()

    assert len(llm.analyze_calls) == 1  # no reflexion on unusable review
    critic_entries = [e for e in ep.trace if e["stage"] == "critic_review"]
    assert critic_entries[0]["detail"]["verdict"] == "unusable"
    assert ep.analysis["conclusion"] == "conclusion round 1"


def test_broken_reflexion_keeps_previous_conclusion(tmp_path):
    # critic objects, but the re-analysis itself fails to parse -> keep round 1
    llm = _CriticLLM(reviews=[_revise("evidence gap")])
    original_analyze = llm.analyze

    def broken_second_analyze(*args, **kwargs):
        if len(llm.analyze_calls) >= 1 and kwargs.get("critic_feedback"):
            llm.analyze_calls.append({"critic_feedback": list(kwargs["critic_feedback"])})
            return {"raw": "garbage", "parse_error": True}
        return original_analyze(*args, **kwargs)

    llm.analyze = broken_second_analyze
    loop, _ = _make_loop(tmp_path, llm)
    ep = loop.start()

    assert ep.phase == Phase.CONCLUDED
    assert ep.analysis["conclusion"] == "conclusion round 1"  # previous kept
    reflexion_entries = [e for e in ep.trace if e["stage"] == "reflexion"]
    assert reflexion_entries and reflexion_entries[0]["detail"]["revised"] is False


def test_critic_disabled_skips_dual_review_entirely(tmp_path):
    llm = _CriticLLM(reviews=[_revise("should never be seen")])
    loop, _ = _make_loop(tmp_path, llm, critic_enabled=False)
    ep = loop.start()

    assert llm.critic_calls == 0
    assert len(llm.analyze_calls) == 1
    assert not any(e["stage"] in ("critic_review", "reflexion") for e in ep.trace)
    assert "critic_review" not in ep.analysis


def test_heuristic_path_has_no_critic(tmp_path):
    loop, _ = _make_loop(tmp_path, llm=None)  # no LLM -> heuristic analysis
    ep = loop.start()
    assert ep.phase == Phase.CONCLUDED
    assert not any(e["stage"] in ("critic_review", "reflexion") for e in ep.trace)


# ---------- OllamaClient critic path ----------


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_ollama_client_critic_review_parses_verdict(monkeypatch):
    from app.llm import ollama_client as oc

    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["payload"] = json
        body = {
            "verdict": "revise",
            "objections": ["conclusion lacks log evidence for the root cause"],
            "missing_evidence": ["port 6379 reachability"],
            "logic_issues": [],
            "confidence": 0.35,
        }
        return _FakeResp({"response": _jsonlib.dumps(body)})

    client = oc.OllamaClient("http://127.0.0.1:11434", "m", 5)
    monkeypatch.setattr(oc.httpx, "post", fake_post)

    review = client.critic_review(
        errors=["[x2] Connection refused to 127.0.0.1:6379"],
        analysis={"conclusion": "redis is down"},
    )
    assert review["verdict"] == "revise"
    assert review["objections"] == ["conclusion lacks log evidence for the root cause"]
    # the critic system prompt is the audit-focused one
    assert "批判者" in captured["payload"]["system"]
    assert "redis is down" in captured["payload"]["prompt"]


def test_ollama_client_analyze_embeds_critic_feedback(monkeypatch):
    from app.llm import ollama_client as oc

    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["prompt"] = json["prompt"]
        body = {
            "summary": "s",
            "likely_root_causes": [],
            "information_gaps": [],
            "conclusion": "c",
        }
        return _FakeResp({"response": _jsonlib.dumps(body)})

    client = oc.OllamaClient("http://127.0.0.1:11434", "m", 5)
    monkeypatch.setattr(oc.httpx, "post", fake_post)

    client.analyze(errors=["e1"], critic_feedback=["证据不足：未引用端口探针结果"])
    assert "证据不足：未引用端口探针结果" in captured["prompt"]
    assert "批判者" in captured["prompt"]
