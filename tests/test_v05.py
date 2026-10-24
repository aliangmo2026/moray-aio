"""v0.5.0 新能力测试：trace/metrics、插件动态加载、CLI、token 用量提取。"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.kernel.loop import EvolutionLoop
from app.kernel.memory import Memory
from app.tools.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parent.parent

ECHO_PLUGIN_SRC = """
from app.tools.plugin_base import BaseTool


class EchoTool(BaseTool):
    name = "dummy_echo"
    description = "echo plugin for tests"
    risk_level = "low"
    params_schema = {"type": "object", "properties": {"text": {"type": "string"}}}

    def invoke(self, params: dict):
        return {"echo": str(params.get("text", ""))[:64]}
"""


class _UsageLLM:
    """带 token 用量的假 LLM：analyze/plan 各消耗一组 token，之后不再提任务。"""

    def __init__(self):
        self._usage = None

    def is_available(self):
        return True

    def analyze(self, errors, extra=None, lessons=None, failed=None, similar=None):
        self._usage = {"prompt_tokens": 40, "completion_tokens": 12}
        return {"summary": "s", "likely_root_causes": [], "information_gaps": [], "conclusion": "c"}

    def plan_tasks(self, errors, log_path, tools, existing=None, lessons=None, failed=None):
        self._usage = {"prompt_tokens": 30, "completion_tokens": 8}
        return {"tasks": []}

    def take_last_usage(self):
        usage = self._usage
        self._usage = None
        return usage


# ---------- trace + /metrics ----------

def test_trace_and_metrics(tmp_path, monkeypatch):
    log = tmp_path / "moray.err.log"
    log.write_text("ERROR Connection refused to 127.0.0.1:6379\n", encoding="utf-8")
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=1,
        auto_approve_readonly=True,
    )
    memory = Memory(str(tmp_path / "memory.sqlite3"))
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    llm = _UsageLLM()
    loop = EvolutionLoop(
        settings=settings, memory=memory, tools=tools, llm=llm, auto_approve_readonly=True
    )

    ep = loop.start()

    # trace 完整覆盖核心阶段，且 token 归因正确
    stages = [e["stage"] for e in ep.trace]
    assert stages == ["observe", "analyze", "propose", "execute"]
    for entry in ep.trace:
        assert entry["duration_ms"] >= 0
        assert entry["timestamp"]
        assert entry["iteration"] == ep.iteration or entry["stage"] == "observe"
    by_stage = {e["stage"]: e for e in ep.trace}
    assert by_stage["analyze"]["llm"] == {"prompt_tokens": 40, "completion_tokens": 12}
    assert by_stage["propose"]["llm"] == {"prompt_tokens": 30, "completion_tokens": 8}
    assert by_stage["observe"]["llm"] is None
    assert by_stage["execute"]["llm"] is None

    # GET /episodes/{id} 携带 trace；GET /metrics 聚合正确
    from app.api import routes
    from app.main import app

    monkeypatch.setattr(routes, "get_memory", lambda: memory)
    monkeypatch.setattr(routes, "get_loop", lambda: loop)
    monkeypatch.setattr(routes, "get_llm", lambda: _UsageLLM())
    client = TestClient(app)

    ep_body = client.get("/episodes/{}".format(ep.id)).json()
    assert ep_body["trace"] and len(ep_body["trace"]) == 4

    body = client.get("/metrics").json()
    assert body["total_episodes"] == 1
    assert body["concluded"] == 1
    assert body["success_rate"] == 1.0
    assert body["total_prompt_tokens"] == 70
    assert body["total_completion_tokens"] == 20
    assert body["total_tokens"] == 90
    assert body["avg_episode_duration_ms"] >= 0
    assert body["total_tasks"] >= 1
    assert "done" in body["tasks_by_status"]


# ---------- 插件动态加载 ----------

def test_plugin_loading(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "echo_plugin.py").write_text(ECHO_PLUGIN_SRC, encoding="utf-8")

    registry = ToolRegistry([str(tmp_path)], 1024 * 1024, plugin_dirs=[str(plugin_dir)])
    assert "dummy_echo" in registry.names
    assert registry.plugin_errors == []
    assert registry.call("dummy_echo", {"text": "hello"}) == {"echo": "hello"}
    assert "dummy_echo" in registry.describe()
    # 内置工具不受影响（空日志返回空列表）
    (tmp_path / "x.log").write_text("", encoding="utf-8")
    assert registry.call("log_tail", {"path": str(tmp_path / "x.log"), "n": 1}) == []

    # 仓库自带示例插件（examples/）也能加载
    registry2 = ToolRegistry([str(tmp_path)], 1024 * 1024, plugin_dirs=[str(REPO_ROOT / "examples")])
    assert "dummy_echo" in registry2.names
    assert registry2.call("dummy_echo", {"text": "from example"}) == {"echo": "from example"}


def test_plugin_failure_isolated(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "broken.py").write_text("def broken(:\n", encoding="utf-8")  # 语法错误
    (plugin_dir / "not_a_tool.py").write_text("x = 1\n", encoding="utf-8")  # 无 BaseTool
    (plugin_dir / "echo_plugin.py").write_text(ECHO_PLUGIN_SRC, encoding="utf-8")

    registry = ToolRegistry([str(tmp_path)], 1024 * 1024, plugin_dirs=[str(plugin_dir)])
    # 坏插件只记录错误，不拖垮进程；好插件照常注册
    assert "dummy_echo" in registry.names
    assert len(registry.plugin_errors) == 2
    assert any("broken.py" in msg for msg in registry.plugin_errors)
    assert any("not_a_tool.py" in msg for msg in registry.plugin_errors)


# ---------- token 用量提取 ----------

def test_ollama_usage_extraction(monkeypatch):
    from app.llm import ollama_client as oc

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "response": '{"summary": "s"}',
                "prompt_eval_count": 12,
                "eval_count": 34,
            }

    client = oc.OllamaClient("http://127.0.0.1:11434", "m", 5, allowed_hosts=("127.0.0.1",))
    monkeypatch.setattr(oc.httpx, "post", lambda url, json=None, timeout=None: _FakeResp())

    result = client._generate("p", "s")
    assert result["summary"] == "s"
    assert client.take_last_usage() == {"prompt_tokens": 12, "completion_tokens": 34}
    assert client.take_last_usage() is None  # 取走即清空，避免跨阶段重复归因


# ---------- CLI ----------

def test_cli_run(tmp_path):
    log = tmp_path / "moray.err.log"
    log.write_text("ERROR Connection refused to 127.0.0.1:6379\n", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "MORAY_AIO_LOG_PATH": str(log),
            "MORAY_AIO_READONLY_ROOTS": str(tmp_path),
            "MORAY_AIO_MEMORY_DB_PATH": str(tmp_path / "mem.sqlite3"),
            "MORAY_AIO_AUTO_APPROVE_READONLY": "true",
            "MORAY_AIO_MAX_ITERATIONS": "1",
            "MORAY_AIO_OLLAMA_URL": "http://127.0.0.1:59999",  # 不可达 → 启发式，确定性
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
        }
    )
    proc = subprocess.run(
        [sys.executable, "cli.py", "run", "--trigger", "recent_errors", "--auto-approve"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "concluded" in proc.stdout
    assert "结论" in proc.stdout
    assert "trace" in proc.stdout
