"""Dependency wiring: process-wide singletons via lru_cache, consumed lazily by
FastAPI routes and the CLI."""

from __future__ import annotations

from functools import lru_cache
from typing import List

from app.config import settings
from app.kernel.loop import EvolutionLoop
from app.kernel.repository import EpisodeRepository
from app.kernel.sqlite_repository import SqliteEpisodeRepository
from app.kernel.vector_memory import VectorMemory
from app.llm.ollama_client import OllamaClient
from app.tools.registry import ToolRegistry


@lru_cache(maxsize=1)
def get_repository() -> EpisodeRepository:
    """Pick the repository by config: MORAY_AIO_DB_DSN -> Postgres; else SQLite."""
    if settings.db_dsn:
        from app.kernel.postgres_repository import PostgresEpisodeRepository

        return PostgresEpisodeRepository(settings.db_dsn)
    return SqliteEpisodeRepository(settings.memory_db_path)


@lru_cache(maxsize=1)
def get_memory() -> EpisodeRepository:
    """Backward-compatible alias (since v0.6.0 persistence goes through the
    EpisodeRepository interface)."""
    return get_repository()


@lru_cache(maxsize=1)
def get_tools() -> ToolRegistry:
    """Read-only tool registry (built-in tools + MORAY_AIO_PLUGIN_DIRS plugins)."""
    return ToolRegistry(
        settings.readonly_roots,
        settings.max_scan_bytes,
        probe_host_whitelist=settings.probe_host_whitelist,
        probe_max_processes=settings.probe_max_processes,
        probe_output_max_bytes=settings.probe_output_max_bytes,
        plugin_dirs=settings.plugin_dirs,
        sandbox_enabled=settings.sandbox_enabled,
        sandbox_timeout_seconds=settings.sandbox_timeout_seconds,
    )


@lru_cache(maxsize=1)
def get_llm() -> OllamaClient:
    """Ollama client. When unavailable, the kernel falls back to heuristics at the
    call site (see loop/planner)."""
    return OllamaClient(
        settings.ollama_url,
        settings.ollama_model,
        settings.ollama_timeout,
        allowed_hosts=settings.ollama_allowed_hosts,
    )


@lru_cache(maxsize=1)
def get_vector_memory() -> VectorMemory:
    """Local semantic long-term memory (pure-Python vector store + Ollama
    embeddings). Fail-soft by design: if the embedding model is missing or Ollama
    is down, remember()/search() degrade and the kernel falls back to Jaccard."""
    if not settings.vector_memory_enabled:
        return VectorMemory(settings.vector_db_path, embed_fn=None, enabled=False)
    client = get_llm()

    def _embed(text: str) -> List[float]:
        return client.embed(text, model=settings.embedding_model)

    return VectorMemory(
        settings.vector_db_path,
        embed_fn=_embed,
        enabled=True,
        top_k=settings.vector_top_k,
        threshold=settings.vector_similarity_threshold,
        max_items=settings.vector_max_items,
    )


@lru_cache(maxsize=1)
def get_loop() -> EvolutionLoop:
    """Self-evolution main loop (assembles memory/tools/llm/vector memory)."""
    return EvolutionLoop(
        settings=settings,
        memory=get_memory(),
        tools=get_tools(),
        llm=get_llm(),
        auto_approve_readonly=settings.auto_approve_readonly,
        vector_memory=get_vector_memory(),
    )
