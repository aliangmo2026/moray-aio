"""全局配置：frozen dataclass，全部字段可被环境变量覆盖。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value is not None and value != "" else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """只读配置。默认值面向 MoRay 主项目，可用 MORAY_AIO_* 环境变量覆盖。"""

    app_name: str = "moray-aio"
    version: str = "0.2.0"

    # HTTP 服务
    host: str = field(default_factory=lambda: _env_str("MORAY_AIO_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("MORAY_AIO_PORT", 8020))

    # 日志分析目标（只读；工具层会做白名单校验）
    log_path: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_LOG_PATH", r"D:\MoRay\logs\moray.err.log")
    )
    recent_error_limit: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_RECENT_ERROR_LIMIT", 10)
    )
    max_scan_bytes: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_MAX_SCAN_BYTES", 5 * 1024 * 1024)
    )
    # 只读白名单根目录，分号分隔
    readonly_roots_raw: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_READONLY_ROOTS", r"D:\MoRay\logs")
    )

    # Ollama 本地大模型
    ollama_url: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_OLLAMA_URL", "http://127.0.0.1:11434")
    )
    ollama_model: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_OLLAMA_MODEL", "qwen2.5:7b")
    )
    ollama_timeout: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_OLLAMA_TIMEOUT", 120)
    )
    # Ollama 主机白名单（SSRF 防护，替换语义）；默认仅允许环回地址
    ollama_allowed_hosts_raw: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_OLLAMA_ALLOWED_HOSTS", "127.0.0.1;localhost;::1")
    )

    # 自我迭代内核
    max_iterations: int = field(default_factory=lambda: _env_int("MORAY_AIO_MAX_ITERATIONS", 4))
    auto_approve_readonly: bool = field(
        default_factory=lambda: _env_bool("MORAY_AIO_AUTO_APPROVE_READONLY", True)
    )
    memory_db_path: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_MEMORY_DB_PATH", "data/memory.sqlite3")
    )

    @property
    def readonly_roots(self) -> list:
        """白名单根目录列表；空片段忽略。"""
        return [p.strip() for p in self.readonly_roots_raw.split(";") if p.strip()]

    @property
    def ollama_allowed_hosts(self) -> list:
        """Ollama 主机白名单列表；空片段忽略。"""
        return [h.strip() for h in self.ollama_allowed_hosts_raw.split(";") if h.strip()]


settings = Settings()
