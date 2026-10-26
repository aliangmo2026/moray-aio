"""v2.0 semantic vector memory tests: pure-Python vector store, Ollama embed
client path, fail-soft degradation (Jaccard fallback, never crash), and kernel
integration (memory_retrieval / memory_write trace entries)."""

from __future__ import annotations

import dataclasses

import pytest

from app.config import Settings
from app.kernel.loop import EvolutionLoop
from app.kernel.sqlite_repository import Memory
from app.kernel.state import Episode, Phase
from app.kernel.vector_memory import VectorMemory, VectorStore, cosine_similarity
from app.tools.registry import ToolRegistry

SAMPLE_LOG = "\n".join(
    [
        "2026-10-25 10:00:00 INFO service started",
        "2026-10-25 10:01:00 ERROR Connection refused to 127.0.0.1:6379",
        "2026-10-25 10:01:01 ERROR Connection refused to 127.0.0.1:6379",
    ]
)


def _fake_embed(text):
    """Deterministic 2-D embedder: 'connection refused' texts share one axis."""
    lower = str(text).lower()
    if "connection" in lower and "refused" in lower:
        return [1.0, 0.0]
    if "timeout" in lower:
        return [0.0, 1.0]
    return [0.7, 0.7]


def _broken_embed(text):
    raise RuntimeError("embedding model not available (simulated)")


# ---------- cosine + store ----------


def test_cosine_similarity_math():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_vector_store_search_orders_by_similarity(tmp_path):
    store = VectorStore(tmp_path / "v.sqlite3")
    store.upsert("a", [1.0, 0.0], "conclusion A")
    store.upsert("b", [0.9, 0.1], "conclusion B")
    store.upsert("c", [0.0, 1.0], "conclusion C")

    hits = store.search([1.0, 0.0], top_k=2, threshold=0.3)
    assert [h["episode_id"] for h in hits] == ["a", "b"]  # exact match ranks first
    assert hits[0]["similarity"] > hits[1]["similarity"] >= 0.3
    assert store.count() == 3
    store.close()


def test_vector_store_exclude_and_threshold(tmp_path):
    store = VectorStore(tmp_path / "v.sqlite3")
    store.upsert("self", [1.0, 0.0], "me")
    store.upsert("other", [0.0, 1.0], "unrelated")

    assert store.search([1.0, 0.0], top_k=3, exclude_episode_id="self") == []
    hits = store.search([1.0, 0.0], top_k=3, threshold=0.9)
    assert [h["episode_id"] for h in hits] == ["self"]
    store.close()


def test_vector_store_evicts_beyond_max_items(tmp_path):
    store = VectorStore(tmp_path / "v.sqlite3", max_items=2)
    store.upsert("e1", [1.0, 0.0], "one")
    store.upsert("e2", [1.0, 0.0], "two")
    store.upsert("e3", [1.0, 0.0], "three")
    assert store.count() == 2
    ids = {row[0] for row in store.rows()}
    assert "e1" not in ids  # oldest evicted
    store.close()


# ---------- VectorMemory fail-soft contract ----------


def test_vector_memory_remember_and_search_roundtrip(tmp_path):
    vm = VectorMemory(tmp_path / "v.sqlite3", embed_fn=_fake_embed, threshold=0.5)

    assert vm.remember("hist-1", "ERROR Connection refused to 127.0.0.1:6379", "redis was down") is True
    hits = vm.search("ERROR Connection refused again", top_k=3)
    assert hits and hits[0]["episode_id"] == "hist-1"
    assert hits[0]["conclusion"] == "redis was down"
    assert hits[0]["similarity"] >= 0.5
    assert vm.count() == 1
    vm.close()


def test_vector_memory_embedding_failure_fails_soft_then_backs_off_then_heals(tmp_path):
    calls = {"n": 0}

    def flaky_embed(text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model missing")
        return _fake_embed(text)

    # a real backoff window so the "down" state is observable
    vm = VectorMemory(
        tmp_path / "v.sqlite3", embed_fn=flaky_embed, retry_after_seconds=300.0
    )
    # first failure: no exception, nothing stored, embedder marked down
    assert vm.remember("h", "Connection refused", "c") is False
    assert vm.is_available() is False
    assert vm.search("Connection refused") == []  # short-circuits while down
    assert calls["n"] == 1  # backoff: the short-circuit did not re-embed
    vm.close()


def test_vector_memory_self_heals_after_retry_window(tmp_path):
    calls = {"n": 0}

    def flaky_embed(text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model missing")
        return _fake_embed(text)

    # retry_after=0 -> the backoff window has always already elapsed, so the very
    # next call retries; a success re-arms availability (self-healing)
    vm = VectorMemory(tmp_path / "v.sqlite3", embed_fn=flaky_embed, retry_after_seconds=0.0)
    assert vm.remember("h", "Connection refused", "c") is False
    assert vm.remember("h", "Connection refused", "c") is True
    assert vm.is_available() is True
    assert vm.search("Connection refused")[0]["episode_id"] == "h"
    vm.close()


def test_vector_memory_disabled_is_noop(tmp_path):
    vm = VectorMemory(tmp_path / "v.sqlite3", embed_fn=None, enabled=False)
    assert vm.is_available() is False
    assert vm.remember("h", "text", "c") is False
    assert vm.search("text") == []
    assert vm.count() == 0
    vm.close()


def test_vector_memory_broken_embed_fn_never_raises(tmp_path):
    vm = VectorMemory(tmp_path / "v.sqlite3", embed_fn=_broken_embed, retry_after_seconds=0.0)
    assert vm.remember("h", "Connection refused", "c") is False
    assert vm.search("Connection refused") == []
    vm.close()


# ---------- OllamaClient.embed ----------


class _FakeResp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "err", request=None, response=None
            )

    def json(self):
        return self._payload


def test_ollama_embed_success_and_fixed_path(monkeypatch):
    from app.llm import ollama_client as oc

    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        seen["payload"] = json
        return _FakeResp({"embedding": [0.1, 0.2, 0.3]})

    client = oc.OllamaClient("http://127.0.0.1:11434", "chat-model", 5)
    monkeypatch.setattr(oc.httpx, "post", fake_post)

    vector = client.embed("some text", model="nomic-embed-text")
    assert vector == [0.1, 0.2, 0.3]
    assert seen["url"] == "http://127.0.0.1:11434/api/embeddings"
    assert seen["payload"] == {"model": "nomic-embed-text", "prompt": "some text"}


def test_ollama_embed_empty_vector_raises(monkeypatch):
    from app.llm import ollama_client as oc

    client = oc.OllamaClient("http://127.0.0.1:11434", "m", 5)
    monkeypatch.setattr(oc.httpx, "post", lambda url, json=None, timeout=None: _FakeResp({}))
    with pytest.raises(ValueError):
        client.embed("x")


# ---------- kernel integration ----------


def _make_loop(tmp_path, llm=None, vector_memory=None, max_iterations=1):
    log = tmp_path / "moray.err.log"
    log.write_text(SAMPLE_LOG, encoding="utf-8")
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=max_iterations,
        auto_approve_readonly=True,
    )
    memory = Memory(str(tmp_path / "memory.sqlite3"))
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    loop = EvolutionLoop(
        settings=settings,
        memory=memory,
        tools=tools,
        llm=llm,
        auto_approve_readonly=True,
        vector_memory=vector_memory,
    )
    return loop, memory


class _ScriptedLLM:
    """Minimal LLM: analyze returns a fixed conclusion; plan proposes nothing."""

    def __init__(self):
        self.analyze_calls = []

    def is_available(self):
        return True

    def analyze(self, errors, extra=None, lessons=None, failed=None, similar=None):
        self.analyze_calls.append({"similar": list(similar or [])})
        return {
            "summary": "scripted",
            "likely_root_causes": [],
            "information_gaps": [],
            "conclusion": "scripted conclusion: redis port 6379 refused connections",
        }

    def plan_tasks(self, errors, log_path, tools, existing=None, lessons=None, failed=None):
        return {"tasks": []}


def test_loop_stores_conclusion_and_retrieves_by_vector_next_time(tmp_path):
    # episode 1: conclusion is vectorized into memory at conclude time
    vm = VectorMemory(tmp_path / "v.sqlite3", embed_fn=_fake_embed, threshold=0.5)
    llm1 = _ScriptedLLM()
    loop1, _ = _make_loop(tmp_path, llm=llm1, vector_memory=vm)
    ep1 = loop1.start()
    assert ep1.phase == Phase.CONCLUDED

    write_entries = [e for e in ep1.trace if e["stage"] == "memory_write"]
    assert write_entries and write_entries[-1]["detail"]["stored"] is True
    assert vm.count() == 1

    # episode 2 (same log): retrieval must come from the vector store and be
    # injected into the LLM prompt as the similar case
    llm2 = _ScriptedLLM()
    loop2, _ = _make_loop(tmp_path, llm=llm2, vector_memory=vm)
    ep2 = loop2.start()
    retrieval = [e for e in ep2.trace if e["stage"] == "memory_retrieval"]
    assert retrieval and retrieval[-1]["detail"]["mode"] == "vector"
    assert retrieval[-1]["detail"]["hits"] >= 1
    similar = llm2.analyze_calls[0]["similar"]
    assert similar and similar[0]["episode_id"] == ep1.id
    assert "redis" in similar[0]["conclusion"]
    vm.close()


def test_loop_falls_back_to_jaccard_when_embedding_down(tmp_path):
    # embedding model "missing": remember/search fail soft, kernel still works
    vm = VectorMemory(tmp_path / "v.sqlite3", embed_fn=_broken_embed, retry_after_seconds=0.0)
    llm = _ScriptedLLM()
    loop, memory = _make_loop(tmp_path, llm=llm, vector_memory=vm)

    # seed keyword history so the Jaccard fallback has something to find
    history = Episode.new(trigger="recent_errors")
    history.observations.append(
        {
            "kind": "recent_errors",
            "errors": [{"text": "ERROR Connection refused to 127.0.0.1:6379"}],
        }
    )
    history.conclusion = "historic keyword conclusion"
    memory.save(history)

    ep = loop.start()
    assert ep.phase == Phase.CONCLUDED
    retrieval = [e for e in ep.trace if e["stage"] == "memory_retrieval"]
    assert retrieval and retrieval[-1]["detail"]["mode"] == "jaccard"
    similar = llm.analyze_calls[0]["similar"]
    assert any(s["episode_id"] == history.id for s in similar)
    # memory_write recorded the failure without breaking the run
    write_entries = [e for e in ep.trace if e["stage"] == "memory_write"]
    assert write_entries and write_entries[-1]["detail"]["stored"] is False
    vm.close()


def test_loop_without_vector_memory_still_works(tmp_path):
    llm = _ScriptedLLM()
    loop, _ = _make_loop(tmp_path, llm=llm, vector_memory=None)
    ep = loop.start()
    assert ep.phase == Phase.CONCLUDED
    retrieval = [e for e in ep.trace if e["stage"] == "memory_retrieval"]
    assert retrieval and retrieval[-1]["detail"]["mode"] in ("jaccard", "none")
    assert not any(e["stage"] == "memory_write" for e in ep.trace)
