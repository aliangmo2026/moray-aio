"""v0.3.0 新能力测试：异步 episode、滚动日志聚合、错误聚类、记忆线程安全。"""

from __future__ import annotations

import dataclasses
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.kernel.loop import EvolutionLoop
from app.kernel.memory import Memory
from app.kernel.state import Episode, Phase
from app.tools.clusterer import cluster, normalize
from app.tools.log_reader import LogReadError, read_rolling
from app.tools.registry import ToolRegistry


# ---------- 异步 episode ----------

class _OfflineLLM:
    def is_available(self):
        return False


def _make_loop(tmp_path, auto_approve=True):
    log = tmp_path / "moray.err.log"
    log.write_text(
        "2026-10-24 12:00:00 INFO boot\n"
        "2026-10-24 12:01:00 ERROR Connection refused to 127.0.0.1:6379\n",
        encoding="utf-8",
    )
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=2,
        auto_approve_readonly=auto_approve,
    )
    memory = Memory(str(tmp_path / "memory.sqlite3"))
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    return EvolutionLoop(
        settings=settings, memory=memory, tools=tools, llm=None,
        auto_approve_readonly=settings.auto_approve_readonly,
    )


def test_start_async_runs_in_background(tmp_path):
    loop = _make_loop(tmp_path)
    episode_id = loop.start_async()
    # 立即返回 id；随后能查到 episode 并最终收敛
    assert episode_id
    ep = loop.require(episode_id)
    assert ep.phase.value in ("created", "queued", "observing", "analyzing", "proposing", "executing")

    deadline = time.time() + 10
    while time.time() < deadline:
        ep = loop.require(episode_id)
        if ep.phase.value in ("concluded", "failed", "awaiting_approval"):
            break
        time.sleep(0.05)
    assert ep.phase.value in ("concluded", "awaiting_approval")
    assert any(o.get("kind") == "recent_errors" for o in ep.observations)


def test_async_episode_api_returns_202(tmp_path, monkeypatch):
    loop = _make_loop(tmp_path, auto_approve=True)

    from app.api import routes

    monkeypatch.setattr(routes, "get_loop", lambda: loop)
    monkeypatch.setattr(routes, "get_llm", lambda: _OfflineLLM())

    from app.main import app

    client = TestClient(app)

    resp = client.post("/episodes")
    assert resp.status_code == 202
    body = resp.json()
    assert body["phase"] == "created"
    episode_id = body["episode_id"]

    # 同步入口仍可用：返回完整 episode dict
    sync_ep = client.post("/episodes/sync").json()
    assert sync_ep["phase"] in ("concluded", "awaiting_approval", "failed")

    # 轮询异步 episode 到终态
    deadline = time.time() + 15
    final = None
    while time.time() < deadline:
        final = client.get("/episodes/{}".format(episode_id)).json()
        if final["phase"] in ("concluded", "failed", "awaiting_approval"):
            break
        time.sleep(0.1)
    assert final is not None and final["phase"] == "concluded"
    assert final["id"] == episode_id


# ---------- 滚动日志聚合 ----------

def test_rolling_reader_merges_files_in_order(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    f2 = log_dir / "moray.err.log.2"  # 最旧
    f1 = log_dir / "moray.err.log.1"
    f0 = log_dir / "moray.err.log"    # 最新
    f2.write_text("2026-10-24 10:00:00 ERROR old-1\n2026-10-24 10:01:00 ERROR old-2\n", encoding="utf-8")
    f1.write_text("2026-10-24 11:00:00 ERROR mid-1\n2026-10-24 11:01:00 INFO mid-noise\n2026-10-24 11:02:00 ERROR mid-2\n", encoding="utf-8")
    f0.write_text("2026-10-24 12:00:00 ERROR new-1\n2026-10-24 12:01:00 ERROR new-2\n2026-10-24 12:02:00 ERROR new-3\n2026-10-24 12:03:00 ERROR new-4\n", encoding="utf-8")
    base = 1_700_000_000
    os.utime(f2, (base, base))
    os.utime(f1, (base + 100, base + 100))
    os.utime(f0, (base + 200, base + 200))

    lines = read_rolling(log_dir, "moray.err.log*", n=6)
    # 时间正序跨文件：旧文件尾部在前，新文件在后
    assert lines == [
        "2026-10-24 11:01:00 INFO mid-noise",
        "2026-10-24 11:02:00 ERROR mid-2",
        "2026-10-24 12:00:00 ERROR new-1",
        "2026-10-24 12:01:00 ERROR new-2",
        "2026-10-24 12:02:00 ERROR new-3",
        "2026-10-24 12:03:00 ERROR new-4",
    ]

    # keyword 过滤（f0 有 4 条、f1 有 2 条、f2 有 2 条 ERROR，共 8 条，时间正序）
    errors = read_rolling(log_dir, "moray.err.log*", n=10, keyword="ERROR")
    assert errors == [
        "2026-10-24 10:00:00 ERROR old-1",
        "2026-10-24 10:01:00 ERROR old-2",
        "2026-10-24 11:00:00 ERROR mid-1",
        "2026-10-24 11:02:00 ERROR mid-2",
        "2026-10-24 12:00:00 ERROR new-1",
        "2026-10-24 12:01:00 ERROR new-2",
        "2026-10-24 12:02:00 ERROR new-3",
        "2026-10-24 12:03:00 ERROR new-4",
    ]

    # 目录不存在
    with pytest.raises(LogReadError):
        read_rolling(tmp_path / "ghost-dir", "moray.err.log*", n=5)


def test_rolling_tail_tool_whitelisted(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "moray.err.log").write_text("2026-10-24 12:00:00 ERROR a\n", encoding="utf-8")
    registry = ToolRegistry([str(tmp_path)], max_scan_bytes=1024 * 1024)

    lines = registry.call(
        "log_rolling_tail", {"dir": str(log_dir), "pattern": "moray.err.log*", "n": 10}
    )
    assert lines == ["2026-10-24 12:00:00 ERROR a"]

    with pytest.raises(PermissionError):
        registry.call("log_rolling_tail", {"dir": str(tmp_path.parent), "n": 10})


def test_observe_uses_rolling_when_log_dir_set(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "moray.err.log").write_text(
        "2026-10-24 12:00:00 INFO boot\n2026-10-24 12:01:00 ERROR Connection refused\n",
        encoding="utf-8",
    )
    settings = dataclasses.replace(
        Settings(),
        log_path=str(tmp_path / "unused.log"),
        readonly_roots_raw=str(tmp_path),
        log_dir=str(log_dir),
        log_glob_pattern="moray.err.log*",
        max_iterations=1,
    )
    memory = Memory(str(tmp_path / "memory.sqlite3"))
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    loop = EvolutionLoop(
        settings=settings, memory=memory, tools=tools, llm=None, auto_approve_readonly=True
    )

    ep = loop.start()
    assert ep.phase == Phase.CONCLUDED
    obs = [o for o in ep.observations if o.get("kind") == "recent_errors"]
    assert obs and obs[0]["mode"] == "rolling"
    assert any("Connection refused" in e.get("text", "") for e in obs[0]["errors"])


# ---------- 错误聚类 ----------

def test_normalize_replaces_volatile_fields():
    line = "2026-10-24 14:20:01 ERROR dial tcp 127.0.0.1:6379 refused id=a1b2c3"
    assert normalize(line) == "<TS> ERROR dial tcp <IP>:<PORT> refused id=<HEX>"


def test_clusterer_folds_1000_lines_into_few_classes():
    lines = []
    for i in range(333):
        # id 用 >=6 位的十六进制串，保证被归一化为 <HEX>
        lines.append(
            "2026-10-24 14:{:02d}:{:02d} ERROR dial tcp 10.0.0.{}:{} refused id=beef{:06x}".format(
                i % 60, i % 60, i % 250, 6000 + i % 300, i
            )
        )
    for i in range(333):
        lines.append(
            "2026-10-24 15:{:02d}:{:02d} ERROR request timeout after {}ms: http://127.0.0.1:{}/api/tasks".format(
                i % 60, i % 60, 100 + i % 500, 8000 + i % 100
            )
        )
    for i in range(334):
        lines.append(
            "2026-10-24 16:{:02d}:{:02d} ERROR FileNotFoundError: No such file: data/cache_{}.bin".format(
                i % 60, i % 60, 100000 + i
            )
        )
    assert len(lines) == 1000

    result = cluster(lines, top_n=10)
    assert 3 <= len(result) <= 5
    assert result[0]["count"] >= 334
    assert "<IP>" in result[0]["template"] or "<NUM>" in result[0]["template"]
    # 样本保留原始行，LLM 仍能看到真实日志样例
    assert result[0]["sample_line"].startswith("2026-")
    # top_n 截断生效
    assert len(cluster(lines, top_n=2)) == 2


# ---------- 记忆线程安全 ----------

def test_memory_threadsafe_concurrent_saves(tmp_path):
    memory = Memory(str(tmp_path / "memory.sqlite3"))
    errors = []

    def worker(worker_id):
        try:
            for i in range(25):
                ep = Episode.new(trigger="t{}-{}".format(worker_id, i))
                ep.transition("queued")
                ep.transition("observing")
                memory.save(ep)
        except Exception as exc:  # 任何异常都算失败
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert not errors
    all_eps = memory.list_recent(limit=1000)
    assert len(all_eps) == 200  # 8 线程 × 25 条，一条不丢
    assert all(ep.phase.value == "observing" for ep in all_eps)
