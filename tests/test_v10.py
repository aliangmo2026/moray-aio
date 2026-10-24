"""v1.0.0 控制台测试：/api/dashboard/data 结构、/dashboard 渲染、URL 传参鉴权。"""

from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from app.config import Settings
from app.kernel.loop import EvolutionLoop
from app.kernel.sqlite_repository import Memory
from app.tools.registry import ToolRegistry


class _QuickLLM:
    """立即返回的假 LLM：让 episode 快速收敛（含 token 供 trace 展示）。"""

    def is_available(self):
        return True

    def analyze(self, errors, extra=None, lessons=None, failed=None, similar=None):
        self._u = {"prompt_tokens": 1115, "completion_tokens": 393, "retries": 0}
        return {"summary": "s", "likely_root_causes": [], "information_gaps": [], "conclusion": "c"}

    def plan_tasks(self, errors, log_path, tools, existing=None, lessons=None, failed=None):
        self._u = {"prompt_tokens": 885, "completion_tokens": 94, "retries": 0}
        return {"tasks": []}

    def take_last_usage(self):
        u = getattr(self, "_u", None)
        self._u = None
        return u


def _build(tmp_path, monkeypatch):
    log = tmp_path / "moray.err.log"
    log.write_text("ERROR Connection refused to 127.0.0.1:6379\n", encoding="utf-8")
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=1,
        auto_approve_readonly=True,
        memory_db_path=str(tmp_path / "mem.sqlite3"),
    )
    memory = Memory(settings.memory_db_path)
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    loop = EvolutionLoop(
        settings=settings, memory=memory, tools=tools, llm=_QuickLLM(), auto_approve_readonly=True
    )
    ep = loop.start()

    from app.api import dashboard as dash
    from app.api import routes
    from app.kernel import watch_service as ws

    monkeypatch.setattr(dash, "get_memory", lambda: memory)
    monkeypatch.setattr(dash, "get_loop", lambda: loop)
    monkeypatch.setattr(routes, "get_memory", lambda: memory)
    monkeypatch.setattr(routes, "get_loop", lambda: loop)
    monkeypatch.setattr(
        ws, "get_watch_manager",
        lambda: type("M", (), {
            "status": staticmethod(lambda: {"running": True, "rules": {}, "notifications_sent": 2, "pending_episodes": 0}),
        })(),
    )
    return ep


def test_dashboard_api(tmp_path, monkeypatch):
    ep = _build(tmp_path, monkeypatch)
    from app.main import create_app

    client = TestClient(create_app(Settings()))
    body = client.get("/api/dashboard/data").json()

    # 顶层结构
    assert set(body) >= {"server_time", "metrics", "episodes", "watch"}
    # metrics 完整
    assert set(body["metrics"]) >= {
        "total_episodes", "success_rate", "total_prompt_tokens",
        "total_completion_tokens", "total_tokens", "tasks_by_status",
    }
    # episodes 带 trace 与任务摘要
    assert len(body["episodes"]) == 1
    first = body["episodes"][0]
    assert first["id"] == ep.id
    assert set(first) >= {"id", "trigger", "phase", "trace", "tasks", "conclusion"}
    assert first["trace"] and {"stage", "duration_ms", "llm"} <= set(first["trace"][0])
    # watch 状态透传
    assert body["watch"]["running"] is True
    assert body["watch"]["notifications_sent"] == 2


def test_dashboard_html_render(tmp_path, monkeypatch):
    _build(tmp_path, monkeypatch)
    from app.main import create_app

    client = TestClient(create_app(Settings()))
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    text = resp.text
    assert "alpine" in text.lower()          # Alpine.js
    assert "tailwind" in text.lower()        # Tailwind CDN
    assert "bg-gray-900" in text             # 暗色主题
    assert "trace waterfall" in text         # 瀑布图区域
    assert "/api/dashboard/data" in text     # 数据接口绑定


def test_dashboard_auth_query_param(tmp_path, monkeypatch):
    _build(tmp_path, monkeypatch)
    from app.main import create_app

    key = "test-" + __import__("uuid").uuid4().hex  # 运行时生成，零凭据字面量
    client = TestClient(create_app(dataclasses.replace(Settings(), api_key=key)))

    assert client.get("/dashboard").status_code == 401                # 无凭据
    assert client.get("/dashboard?api_key=wrong").status_code == 401  # 错误凭据
    ok = client.get("/dashboard?api_key=" + key)                      # URL 传参（浏览器场景）
    assert ok.status_code == 200
    data = client.get("/api/dashboard/data", headers={"X-API-Key": key})
    assert data.status_code == 200
