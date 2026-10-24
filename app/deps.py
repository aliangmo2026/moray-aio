"""依赖装配：进程级单例（lru_cache），FastAPI 路由按需取用。"""

from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.kernel.loop import EvolutionLoop
from app.kernel.memory import Memory
from app.llm.ollama_client import OllamaClient
from app.tools.registry import ToolRegistry


@lru_cache(maxsize=1)
def get_memory() -> Memory:
    """SQLite 长效记忆（episodes + reflections）。"""
    return Memory(settings.memory_db_path)


@lru_cache(maxsize=1)
def get_tools() -> ToolRegistry:
    """只读工具注册表（白名单来自 settings.readonly_roots）。"""
    return ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)


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
