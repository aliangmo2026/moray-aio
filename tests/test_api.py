"""API 层测试：批量审批/拒绝接口、/health 的 ollama 字段、反思记忆查询。

不发起任何真实网络请求：通过 monkeypatch 把 routes.get_loop / get_llm
替换为基于 tmp_path 的真实 EvolutionLoop（llm=None）与离线 LLM 桩。
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.kernel.loop import EvolutionLoop
from app.kernel.memory import Memory
from app.tools.registry import ToolRegistry

RICH_LOG = "\n".join(
    [
        "2026-10-24 11:00:00 INFO boot",
        "2026-10-24 11:01:00 ERROR Connection refused to 127.0.0.1:6379 (ECONNREFUSED)",
        "2026-10-24 11:02:00 ERROR request timeout after 30s",
        "2026-10-24 11:03:00 ERROR FileNotFoundError: No such file: conf/app.yaml",
    ]
)


class _OfflineLLM:
    """永远不可用的 LLM 桩：/health 的 ollama.available 恒为 False。"""

    def is_available(self):
        return False


@pytest.fixture()
def env(tmp_path, monkeypatch):
    log = tmp_path / "moray.err.log"
    log.write_text(RICH_LOG + "\n", encoding="utf-8")
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=2,
        auto_approve_readonly=False,
    )
    memory = Memory(str(tmp_path / "memory.sqlite3"))
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    loop = EvolutionLoop(
        settings=settings,
        memory=memory,
        tools=tools,
        llm=None,
        auto_approve_readonly=False,
    )

    from app.api import routes

    monkeypatch.setattr(routes, "get_loop", lambda: loop)
    monkeypatch.setattr(routes, "get_llm", lambda: _OfflineLLM())

    from app.main import app

    return TestClient(app), loop


def test_health_includes_ollama_field(env):
    client, _loop = env
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["ollama"] == {"available": False, "model": Settings().ollama_model}
    # /health 依旧探测真实默认日志路径（tmp_path 不影响全局 settings）
    assert body["log"]["exists"] is False


def test_approve_all_then_resume_concludes(env):
    client, _loop = env
    ep = client.post("/episodes/sync").json()
    assert ep["phase"] == "awaiting_approval"
    proposed = [t for t in ep["tasks"] if t["status"] == "proposed"]
    # 连接被拒/超时/文件缺失 → 3 条 log_grep + port_check + process_list("redis")
    assert len(proposed) == 5
    assert {t["tool"] for t in proposed} == {"log_grep", "port_check", "process_list"}

    resp = client.post("/episodes/{}/tasks/approve-all".format(ep["id"]))
    assert resp.status_code == 200
    after = resp.json()
    assert all(t["status"] == "approved" for t in after["tasks"])

    resumed = client.post("/episodes/{}/resume".format(ep["id"])).json()
    assert resumed["phase"] == "concluded"
    assert all(t["status"] == "done" for t in resumed["tasks"])


def test_reject_all_leads_to_conclusion_without_done(env):
    client, _loop = env
    ep = client.post("/episodes/sync").json()
    resp = client.post("/episodes/{}/tasks/reject-all".format(ep["id"]))
    assert resp.status_code == 200
    after = resp.json()
    assert after["tasks"] and all(t["status"] == "rejected" for t in after["tasks"])

    resumed = client.post("/episodes/{}/resume".format(ep["id"])).json()
    assert resumed["phase"] == "concluded"
    assert all(t["status"] != "done" for t in resumed["tasks"])


def test_approve_all_skips_nonreadonly_proposed(env):
    client, loop = env
    ep = client.post("/episodes/sync").json()
    # 混入一个非只读工具的 proposed 任务，验证批量审批不会批准它
    loop.require(ep["id"]).tasks.append(
        {
            "id": "evil000001",
            "tool": "process_kill",
            "params": {"pid": 4242},
            "rationale": "restart the service",
            "risk": "high",
            "status": "proposed",
            "result": None,
            "created_at": ep["created_at"],
        }
    )

    after = client.post("/episodes/{}/tasks/approve-all".format(ep["id"])).json()
    by_id = {t["id"]: t for t in after["tasks"]}
    assert by_id["evil000001"]["status"] == "proposed"
    for task in after["tasks"]:
        if task["id"] != "evil000001":
            assert task["status"] == "approved"


def test_approve_all_and_reject_all_unknown_episode_404(env):
    client, _loop = env
    assert client.post("/episodes/no-such-id/tasks/approve-all").status_code == 404
    assert client.post("/episodes/no-such-id/tasks/reject-all").status_code == 404


def test_memory_reflections_endpoint(env):
    client, _loop = env
    ep = client.post("/episodes/sync").json()
    client.post("/episodes/{}/tasks/reject-all".format(ep["id"]))
    client.post("/episodes/{}/resume".format(ep["id"]))

    resp = client.get("/memory/reflections?limit=5")
    assert resp.status_code == 200
    lessons = resp.json()
    assert lessons and all({"id", "episode_id", "lesson", "created_at"} <= set(r) for r in lessons)
