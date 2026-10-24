"""依赖装配：进程级单例（lru_cache），FastAPI 路由按需取用。"""

from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.kernel.loop import EvolutionLoop
from app.kernel.repository import EpisodeRepository
from app.kernel.sqlite_repository import SqliteEpisodeRepository
from app.llm.ollama_client import OllamaClient
from app.tools.registry import ToolRegistry


@lru_cache(maxsize=1)
def get_repository() -> EpisodeRepository:
    """按配置选择仓储：MORAY_AIO_DB_DSN 存在 → Postgres；否则默认 SQLite。"""
    if settings.db_dsn:
        from app.kernel.postgres_repository import PostgresEpisodeRepository

        return PostgresEpisodeRepository(settings.db_dsn)
    return SqliteEpisodeRepository(settings.memory_db_path)


@lru_cache(maxsize=1)
def get_memory() -> EpisodeRepository:
    """向后兼容别名（v0.6.0 起持久化统一走 EpisodeRepository 接口）。"""
    return get_repository()


@lru_cache(maxsize=1)
def get_tools() -> ToolRegistry:
    """只读工具注册表（内置工具 + MORAY_AIO_PLUGIN_DIRS 目录插件）。"""
    return ToolRegistry(
        settings.readonly_roots,
        settings.max_scan_bytes,
        probe_host_whitelist=settings.probe_host_whitelist,
        probe_max_processes=settings.probe_max_processes,
        probe_output_max_bytes=settings.probe_output_max_bytes,
        plugin_dirs=settings.plugin_dirs,
    )


@lru_cache(maxsize=1)
def get_llm() -> OllamaClient:
    """Ollama 客户端。不可用时内核会在调用点回退启发式（见 loop/planner）。"""
    return OllamaClient(
        settings.ollama_url,
        settings.ollama_model,
        settings.ollama_timeout,
        allowed_hosts=settings.ollama_allowed_hosts,
    )


@lru_cache(maxsize=1)
def get_loop() -> EvolutionLoop:
    """自我迭代主循环（组装 memory/tools/llm）。"""
    return EvolutionLoop(
        settings=settings,
        memory=get_memory(),
        tools=get_tools(),
        llm=get_llm(),
        auto_approve_readonly=settings.auto_approve_readonly,
    )
