"""v0.4.0 新能力测试：只读系统探针、planner 智能派发、历史相似 episode 检索。"""

from __future__ import annotations

import dataclasses
import os
import socket

import pytest

from app.config import Settings
from app.kernel.loop import EvolutionLoop
from app.kernel.memory import Memory
from app.kernel.planner import propose_tasks
from app.kernel.state import Episode
from app.llm.ollama_client import OllamaClient
from app.tools import probes
from app.tools.registry import ToolRegistry


def _registry(tmp_path):
    return ToolRegistry([str(tmp_path)], 1024 * 1024)


def _seed(memory, text, conclusion):
    """种一个带错误文本与结论的 episode，供相似检索测试使用。"""
    ep = Episode.new(trigger="seed")
    ep.transition("queued")
    ep.transition("observing")
    ep.observations.append(
        {"iteration": 0, "kind": "recent_errors", "errors": [{"text": text}]}
    )
    ep.conclusion = conclusion
    memory.save(ep)
    return ep


# ---------- 探针：port_check ----------

def test_port_check_local_listener_open_then_closed():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    try:
        result = probes.port_check("127.0.0.1", port, timeout=2)
        assert result["open"] is True
        assert result["host"] == "127.0.0.1" and result["port"] == port
        assert isinstance(result["latency_ms"], float)
    finally:
        listener.close()
    closed = probes.port_check("127.0.0.1", port, timeout=1)
    assert closed["open"] is False
    assert closed["latency_ms"] is None


def test_port_check_host_whitelist():
    with pytest.raises(PermissionError):
        probes.port_check("8.8.8.8", 53, timeout=1)
    with pytest.raises(PermissionError):
        probes.port_check("10.9.9.9", 6379, timeout=1)
    with pytest.raises(ValueError):
        probes.port_check("127.0.0.1", 99999, timeout=1)


# ---------- 探针：process_list ----------

def test_process_list_returns_self():
    result = probes.process_list(name_pattern="python")
    assert result["count"] >= 1
    assert os.getpid() in [p["pid"] for p in result["processes"]]
    sample = result["processes"][0]
    assert {"pid", "name", "username", "cpu_percent", "memory_mb", "create_time"} <= set(sample)
    capped = probes.process_list(max_processes=5)
    assert capped["count"] <= 5


# ---------- 探针：windows_service_status ----------

def test_windows_service_injection_blocked():
    for bad in ("foo; rm -rf /", "abc&&def", "x" * 65, "", "name|pipe"):
        with pytest.raises(PermissionError):
            probes.windows_service_status(bad)
    # 合法名字不抛：Windows 返回 state；非 Windows 返回 not_windows（优雅降级）
    ok = probes.windows_service_status("Spooler")
    assert ok["name"] == "Spooler"
    assert ok["state"] in ("RUNNING", "STOPPED", "UNKNOWN") or ok.get("error") == "not_windows"


# ---------- 探针：disk_usage 白名单 ----------

def test_disk_usage_whitelist(tmp_path):
    registry = _registry(tmp_path)
    ok = registry.call("disk_usage", {"path": str(tmp_path)})
    assert ok["total_gb"] > 0 and ok["used_percent"] is not None
    with pytest.raises(PermissionError):
        registry.call("disk_usage", {"path": "C:\\Windows"})


# ---------- planner 智能派发 ----------

def test_heuristic_redis_probe_dispatch(tmp_path):
    registry = _registry(tmp_path)
    log = tmp_path / "moray.err.log"
    log.write_text("x", encoding="utf-8")
    tasks = propose_tasks(
        errors=["2026-10-24 ERROR Connection refused to 127.0.0.1:6379"],
        existing_tasks=[],
        log_path=str(log),
        tools=registry,
        llm=None,
        auto_approve_readonly=True,
    )
    by_tool = {}
    for t in tasks:
        by_tool.setdefault(t["tool"], t)
    assert "port_check" in by_tool
    pc = by_tool["port_check"]
    assert pc["params"]["host"] == "127.0.0.1" and pc["params"]["port"] == 6379
    assert pc["status"] == "approved"
    assert "探针只能观察" in pc["rationale"]
    assert "process_list" in by_tool
    assert by_tool["process_list"]["params"]["name_pattern"] == "redis"


def test_heuristic_disk_and_service_dispatch(tmp_path):
    registry = _registry(tmp_path)
    log = tmp_path / "moray.err.log"
    log.write_text("x", encoding="utf-8")
    tasks = propose_tasks(
        errors=[
            "ERROR No space left on device: disk quota exceeded",
            "ERROR service daemon failure: redis unavailable",
        ],
        existing_tasks=[],
        log_path=str(log),
        tools=registry,
        llm=None,
        auto_approve_readonly=True,
    )
    tools_used = {t["tool"] for t in tasks}
    assert {"disk_usage", "windows_service_status"} <= tools_used


def test_heuristic_skips_non_local_host_probe(tmp_path):
    registry = _registry(tmp_path)
    log = tmp_path / "moray.err.log"
    log.write_text("x", encoding="utf-8")
    tasks = propose_tasks(
        errors=["ERROR Connection refused to 10.9.9.9:6379"],
        existing_tasks=[],
        log_path=str(log),
        tools=registry,
        llm=None,
        auto_approve_readonly=True,
    )
    # 非本机地址绝不派探针（host 白名单 fail-closed）
    assert not any(t["tool"] == "port_check" for t in tasks)


# ---------- 历史相似 episode 检索 ----------

def test_memory_similar_episode(tmp_path):
    memory = Memory(str(tmp_path / "m.sqlite3"))
    e1 = _seed(memory, "ERROR Connection refused to redis server", "建议检查 redis 服务")
    e2 = _seed(memory, "ERROR redis connection refused again", "建议确认 redis 端口")
    _seed(memory, "ERROR disk quota exceeded for volume", "建议清理磁盘")

    hits = memory.find_similar(["redis", "connection", "refused"], top_k=2)
    assert {h["episode_id"] for h in hits} == {e1.id, e2.id}
    assert all(h["similarity"] >= 0.3 for h in hits)
    assert all(h["conclusion"] for h in hits)
    # 阈值过滤：完全不相似的 disk 案例不出现
    assert _seed(memory, "zzz", "x").id not in {h["episode_id"] for h in hits}


def test_analyze_injects_similar_cases(tmp_path, monkeypatch):
    memory = Memory(str(tmp_path / "m.sqlite3"))
    _seed(memory, "ERROR Connection refused to redis server", "历史结论：建议检查 redis 配置")
    log = tmp_path / "moray.err.log"
    log.write_text("ERROR Connection refused to redis server\n", encoding="utf-8")
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=1,
        auto_approve_readonly=True,
    )
    registry = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    llm = OllamaClient("http://127.0.0.1:11434", "m", 5, allowed_hosts=("127.0.0.1",))
    llm._available = True  # 跳过 httpx 探测
    prompts = []

    def fake_generate(prompt, system):
        prompts.append(prompt)
        return {"summary": "s", "likely_root_causes": [], "information_gaps": [], "conclusion": "c"}

    monkeypatch.setattr(llm, "_generate", fake_generate)
    loop = EvolutionLoop(
        settings=settings, memory=memory, tools=registry, llm=llm, auto_approve_readonly=True
    )

    ep = loop.start()
    assert ep.analysis["summary"] == "s"
    # ANALYZE prompt 里必须带历史类似案例的 conclusion
    assert any("历史结论：建议检查 redis 配置" in p for p in prompts)


def test_probe_tasks_record_kind_probe(tmp_path):
    log = tmp_path / "moray.err.log"
    log.write_text("ERROR Connection refused to 127.0.0.1:65000\n", encoding="utf-8")
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=1,
        auto_approve_readonly=True,
    )
    memory = Memory(str(tmp_path / "m.sqlite3"))
    registry = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    loop = EvolutionLoop(
        settings=settings, memory=memory, tools=registry, llm=None, auto_approve_readonly=True
    )

    ep = loop.start()
    probe_obs = [o for o in ep.observations if o.get("kind") == "probe"]
    assert probe_obs, "探针任务的执行结果应记录为 kind=probe"
    assert any(o["tool"] == "port_check" for o in probe_obs)
