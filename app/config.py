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
    version: str = "0.7.0"

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
    # 异步执行：后台线程池大小（POST /episodes 立即返回 202，后台跑内核）
    async_workers: int = field(default_factory=lambda: _env_int("MORAY_AIO_ASYNC_WORKERS", 2))

    # 日志滚动聚合：配置了 log_dir 就优先按 glob 聚合读取（如 moray.err.log / .1 / 带日期后缀）
    log_dir: str = field(default_factory=lambda: _env_str("MORAY_AIO_LOG_DIR", ""))
    log_glob_pattern: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_LOG_GLOB_PATTERN", "moray.err.log*")
    )

    # 错误聚类：喂给 LLM 的最大类数
    cluster_top_n: int = field(default_factory=lambda: _env_int("MORAY_AIO_CLUSTER_TOP_N", 10))

    # 只读系统探针
    # 探针可连的主机白名单（分号分隔）；默认与 Ollama 白名单同值（仅本机）
    probe_host_whitelist_raw: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_PROBE_HOST_WHITELIST", "127.0.0.1;localhost;::1")
    )
    probe_max_processes: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_PROBE_MAX_PROCESSES", 50)
    )
    probe_output_max_bytes: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_PROBE_OUTPUT_MAX_BYTES", 4096)
    )

    # 插件目录（分号分隔）：启动时扫描其中的 *.py，加载 BaseTool 子类为外部工具
    plugin_dirs_raw: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_PLUGIN_DIRS", "")
    )

    # ---- 生产级基础设施（v0.6.0）----
    # 持久化：DSN 非空 → Postgres；为空 → 默认 SQLite（memory_db_path）
    db_dsn: str = field(default_factory=lambda: _env_str("MORAY_AIO_DB_DSN", ""))
    # Token 预算熔断：单个 episode 的 LLM 用量上限，超限立即 FAILED
    max_tokens_per_episode: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_MAX_TOKENS_PER_EPISODE", 50000)
    )
    # API 鉴权：非空时所有请求（除 GET /health）必须携带 X-API-Key（只从环境变量读取）
    api_key: str = field(default_factory=lambda: _env_str("MORAY_AIO_API_KEY", ""))

    # ---- 事件驱动 Watch 模式（v0.7.0）----
    # 默认关闭（安全优先），需手动开启；开启后 lifespan 自动启动监听线程
    watch_enabled: bool = field(default_factory=lambda: _env_bool("MORAY_AIO_WATCH_ENABLED", False))
    watch_poll_interval_seconds: float = field(
        default_factory=lambda: float(_env_int("MORAY_AIO_WATCH_POLL_INTERVAL_SECONDS", 2))
    )
    # 可选 JSON 数组覆盖默认触发规则
    watch_rules_config: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_WATCH_RULES_CONFIG", "")
    )
    # 告警推送：console;webhook;file（分号分隔）
    notifier_types_raw: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_NOTIFIER_TYPES", "console")
    )
    webhook_url: str = field(default_factory=lambda: _env_str("MORAY_AIO_WEBHOOK_URL", ""))
    # Webhook 域名白名单（分号分隔）：为空时 webhook 推送拒绝工作（fail-closed）
    webhook_allowed_hosts_raw: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_WEBHOOK_ALLOWED_HOSTS", "")
    )
    alerts_log_path: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_ALERTS_LOG_PATH", "data/alerts.log")
    )

    @property
    def readonly_roots(self) -> list:
        """白名单根目录列表；空片段忽略。"""
        return [p.strip() for p in self.readonly_roots_raw.split(";") if p.strip()]

    @property
    def ollama_allowed_hosts(self) -> list:
        """Ollama 主机白名单列表；空片段忽略。"""
        return [h.strip() for h in self.ollama_allowed_hosts_raw.split(";") if h.strip()]

    @property
    def probe_host_whitelist(self) -> list:
        """探针可连主机白名单列表；空片段忽略。"""
        return [h.strip() for h in self.probe_host_whitelist_raw.split(";") if h.strip()]

    @property
    def plugin_dirs(self) -> list:
        """插件目录列表；空片段忽略。"""
        return [p.strip() for p in self.plugin_dirs_raw.split(";") if p.strip()]


settings = Settings()
