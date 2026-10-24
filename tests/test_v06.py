"""v0.6.0 基础设施测试：Token 熔断、API 鉴权、Ollama 重试、Repository 路由。

安全说明：API 鉴权用例的伪密钥在运行时动态生成（uuid4），
不向源码写入任何凭据字面量。
"""

from __future__ import annotations

import dataclasses
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.kernel.loop import EvolutionLoop
from app.kernel.repository import EpisodeRepository
from app.kernel.sqlite_repository import Memory, SqliteEpisodeRepository
from app.tools.registry import ToolRegistry


class _HugeTokenLLM:
    """单次调用就烧掉巨额 token 的假 LLM，用于验证预算熔断。"""

    def __init__(self, prompt_tokens=40000, completion_tokens=20000):
        self._usage = None
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self.calls = 0

    def is_available(self):
        return True

    def analyze(self, errors, extra=None, lessons=None, failed=None, similar=None):
        self.calls += 1
        self._usage = {"prompt_tokens": self._prompt_tokens, "completion_tokens": self._completion_tokens, "retries": 0}
        return {"summary": "s", "likely_root_causes": [], "information_gaps": [], "conclusion": "c"}

    def plan_tasks(self, errors, log_path, tools, existing=None, lessons=None, failed=None):
        self.calls += 1
        self._usage = {"prompt_tokens": 10, "completion_tokens": 10, "retries": 0}
        return {"tasks": []}

    def take_last_usage(self):
        usage = self._usage
        self._usage = None
        return usage


def _make_loop(tmp_path, llm, max_tokens_per_episode, auto_approve=True):
    log = tmp_path / "moray.err.log"
    log.write_text("ERROR Connection refused to 127.0.0.1:6379\n", encoding="utf-8")
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=3,
        auto_approve_readonly=auto_approve,
        max_tokens_per_episode=max_tokens_per_episode,
    )
    memory = Memory(str(tmp_path / "memory.sqlite3"))
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    return EvolutionLoop(
        settings=settings, memory=memory, tools=tools, llm=llm, auto_approve_readonly=auto_approve
    )


# ---------- Token 预算熔断 ----------

def test_token_budget_circuit_breaker(tmp_path):
    llm = _HugeTokenLLM(prompt_tokens=40000, completion_tokens=20000)  # 60000 > 50000
    loop = _make_loop(tmp_path, llm, max_tokens_per_episode=50000)

    ep = loop.start()

    assert ep.phase.value == "failed"
    assert ep.error == "Token budget exceeded: 60000 / 50000"
    # 熔断发生在 analyze 之后：propose 不再执行
    assert llm.calls == 1
    assert ep.iteration == 1
    breaks = [e for e in ep.trace if e["stage"] == "circuit_break"]
    assert len(breaks) == 1
    assert breaks[0]["detail"] == {"used_tokens": 60000, "max_tokens": 50000}
    # 持久化后状态一致
    assert loop.memory.load(ep.id).phase.value == "failed"


def test_token_budget_circuit_breaker_async(tmp_path):
    llm = _HugeTokenLLM()
    loop = _make_loop(tmp_path, llm, max_tokens_per_episode=50000)
    episode_id = loop.start_async()

    deadline = time.time() + 15
    ep = loop.require(episode_id)
    while time.time() < deadline and ep.phase.value not in ("failed", "concluded"):
        time.sleep(0.05)
        ep = loop.require(episode_id)

    assert ep.phase.value == "failed"
    assert "Token budget exceeded" in ep.error


def test_token_budget_not_hit_when_under_limit(tmp_path):
    llm = _HugeTokenLLM(prompt_tokens=10, completion_tokens=5)
    loop = _make_loop(tmp_path, llm, max_tokens_per_episode=50000)

    ep = loop.start()
    assert ep.phase.value == "concluded"
    assert not any(e["stage"] == "circuit_break" for e in ep.trace)


# ---------- API Key 鉴权 ----------

def _auth_app(tmp_path, api_key):
    from app.main import create_app

    memory = Memory(str(tmp_path / "memory.sqlite3"))
    loop = _make_loop(tmp_path, _HugeTokenLLM(prompt_tokens=1, completion_tokens=1), 50000)

    from app.api import routes

    routes.get_memory = lambda: memory
    routes.get_loop = lambda: loop
    cfg = dataclasses.replace(Settings(), api_key=api_key)
    return TestClient(create_app(cfg))


def test_api_auth(tmp_path):
    # 运行时生成伪密钥（不向源码写入凭据字面量）
    api_key = "test-" + uuid.uuid4().hex
    wrong_key = "test-" + uuid.uuid4().hex
    client = _auth_app(tmp_path, api_key)

    # /health 免鉴权
    assert client.get("/health").status_code == 200

    # 不带 header / 错误 key → 401
    assert client.get("/episodes").status_code == 401
    assert client.get("/metrics").status_code == 401
    resp = client.get("/episodes", headers={"X-API-Key": wrong_key})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unauthorized"

    # 正确 key → 200
    ok = client.get("/episodes", headers={"X-API-Key": api_key})
    assert ok.status_code == 200


def test_api_auth_disabled_when_key_empty(tmp_path):
    client = _auth_app(tmp_path, "")
    assert client.get("/health").status_code == 200
    assert client.get("/episodes").status_code == 200  # 鉴权关闭


# ---------- Ollama 重试 ----------

def _fake_resp(status_code=200, body=None):
    class _Resp:
        def raise_for_status(self_inner):
            import httpx

            if status_code >= 400:
                raise httpx.HTTPStatusError(
                    "HTTP {}".format(status_code),
                    request=httpx.Request("POST", "http://127.0.0.1:1"),
                    response=httpx.Response(status_code),
                )

        def json(self_inner):
            return body if body is not None else {
                "response": '{"summary": "s"}',
                "prompt_eval_count": 7,
                "eval_count": 3,
            }

    resp = _Resp()
    resp.status_code = status_code
    return resp


def test_ollama_retry_on_timeout_then_success(monkeypatch):
    from app.llm import ollama_client as oc

    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise oc.httpx.ConnectTimeout("boom")
        return _fake_resp()

    client = oc.OllamaClient("http://127.0.0.1:11434", "m", 5, allowed_hosts=("127.0.0.1",))
    monkeypatch.setattr(oc.httpx, "post", fake_post)
    monkeypatch.setattr(oc.time, "sleep", lambda s: None)  # 测试不真等退避

    result = client._generate("p", "s")
    assert result["summary"] == "s"
    assert calls["n"] == 2  # 第一次超时 + 第一次重试成功
    assert client.take_last_usage()["retries"] == 1


def test_ollama_retry_on_5xx_then_success(monkeypatch):
    from app.llm import ollama_client as oc

    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            return _fake_resp(status_code=503)
        return _fake_resp()

    client = oc.OllamaClient("http://127.0.0.1:11434", "m", 5, allowed_hosts=("127.0.0.1",))
    monkeypatch.setattr(oc.httpx, "post", fake_post)
    monkeypatch.setattr(oc.time, "sleep", lambda s: None)

    result = client._generate("p", "s")
    assert result["summary"] == "s"
    assert calls["n"] == 3
    assert client.take_last_usage()["retries"] == 2


def test_ollama_no_retry_on_4xx(monkeypatch):
    import httpx

    from app.llm import ollama_client as oc

    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        return _fake_resp(status_code=400)

    client = oc.OllamaClient("http://127.0.0.1:11434", "m", 5, allowed_hosts=("127.0.0.1",))
    monkeypatch.setattr(oc.httpx, "post", fake_post)
    monkeypatch.setattr(oc.time, "sleep", lambda s: None)

    with pytest.raises(httpx.HTTPStatusError):
        client._generate("p", "s")
    assert calls["n"] == 1  # 4xx 不重试


# ---------- Repository ----------

def test_repository_defaults_to_sqlite(tmp_path):
    from app.kernel import repository as repo_module

    assert issubclass(SqliteEpisodeRepository, EpisodeRepository)
    assert repo_module.extract_keywords(["ERROR redis connection refused"])  # 共享纯函数可用

    mem = Memory(str(tmp_path / "m.sqlite3"))
    assert isinstance(mem, EpisodeRepository)
    mem.close()


def test_postgres_repository():
    dsn = os.environ.get("MORAY_AIO_DB_DSN", "").strip()
    if not dsn:
        pytest.skip("MORAY_AIO_DB_DSN 未配置，跳过 Postgres 集成测试")
    pytest.importorskip("psycopg2")
    from app.kernel.postgres_repository import PostgresEpisodeRepository

    repo = PostgresEpisodeRepository(dsn)
    assert isinstance(repo, EpisodeRepository)
    assert isinstance(repo.metrics(), dict)
    repo.close()
