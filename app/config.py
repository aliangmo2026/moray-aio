"""Global configuration: frozen dataclass, every field overridable via environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict


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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Read-only settings. Defaults target the MoRay main project; override via MORAY_AIO_* env vars."""

    app_name: str = "moray-aio"
    version: str = "1.0.0"

    # HTTP service
    host: str = field(default_factory=lambda: _env_str("MORAY_AIO_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("MORAY_AIO_PORT", 8020))

    # Log analysis target (read-only; the tool layer enforces the whitelist)
    log_path: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_LOG_PATH", r"D:\MoRay\logs\moray.err.log")
    )
    recent_error_limit: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_RECENT_ERROR_LIMIT", 10)
    )
    max_scan_bytes: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_MAX_SCAN_BYTES", 5 * 1024 * 1024)
    )
    # Read-only whitelist roots, semicolon separated
    readonly_roots_raw: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_READONLY_ROOTS", r"D:\MoRay\logs")
    )

    # Ollama local LLM
    ollama_url: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_OLLAMA_URL", "http://127.0.0.1:11434")
    )
    ollama_model: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_OLLAMA_MODEL", "qwen2.5:7b")
    )
    ollama_timeout: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_OLLAMA_TIMEOUT", 120)
    )
    # Ollama host whitelist (SSRF guard, replace semantics); loopback only by default
    ollama_allowed_hosts_raw: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_OLLAMA_ALLOWED_HOSTS", "127.0.0.1;localhost;::1")
    )

    # Self-evolution kernel
    max_iterations: int = field(default_factory=lambda: _env_int("MORAY_AIO_MAX_ITERATIONS", 4))
    auto_approve_readonly: bool = field(
        default_factory=lambda: _env_bool("MORAY_AIO_AUTO_APPROVE_READONLY", True)
    )
    memory_db_path: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_MEMORY_DB_PATH", "data/memory.sqlite3")
    )
    # Async execution: background thread pool size (POST /episodes returns 202 immediately)
    async_workers: int = field(default_factory=lambda: _env_int("MORAY_AIO_ASYNC_WORKERS", 2))

    # Rolling log aggregation: when log_dir is set, read via glob across rotated files
    log_dir: str = field(default_factory=lambda: _env_str("MORAY_AIO_LOG_DIR", ""))
    log_glob_pattern: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_LOG_GLOB_PATTERN", "moray.err.log*")
    )

    # Error clustering: max number of classes fed to the LLM
    cluster_top_n: int = field(default_factory=lambda: _env_int("MORAY_AIO_CLUSTER_TOP_N", 10))

    # Read-only system probes
    # Host whitelist probes may connect to (semicolon separated); loopback only by default
    probe_host_whitelist_raw: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_PROBE_HOST_WHITELIST", "127.0.0.1;localhost;::1")
    )
    probe_max_processes: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_PROBE_MAX_PROCESSES", 50)
    )
    probe_output_max_bytes: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_PROBE_OUTPUT_MAX_BYTES", 4096)
    )

    # Plugin directories (semicolon separated): scanned at startup for BaseTool subclasses
    plugin_dirs_raw: str = field(default_factory=lambda: _env_str("MORAY_AIO_PLUGIN_DIRS", ""))

    # ---- Production infrastructure (v0.6.0) ----
    # Persistence: non-empty DSN -> Postgres; empty -> default SQLite (memory_db_path)
    db_dsn: str = field(default_factory=lambda: _env_str("MORAY_AIO_DB_DSN", ""))
    # Token budget circuit breaker: per-episode LLM usage cap, exceeded -> FAILED
    max_tokens_per_episode: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_MAX_TOKENS_PER_EPISODE", 50000)
    )
    # API auth: when non-empty, every request (except GET /health) must carry X-API-Key
    api_key: str = field(default_factory=lambda: _env_str("MORAY_AIO_API_KEY", ""))

    # ---- Event-driven watch mode (v0.7.0) ----
    # Off by default (safety first); when on, the lifespan starts the listener thread
    watch_enabled: bool = field(default_factory=lambda: _env_bool("MORAY_AIO_WATCH_ENABLED", False))
    watch_poll_interval_seconds: float = field(
        default_factory=lambda: float(_env_int("MORAY_AIO_WATCH_POLL_INTERVAL_SECONDS", 2))
    )
    # Optional JSON array overriding the default trigger rules
    watch_rules_config: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_WATCH_RULES_CONFIG", "")
    )
    # Alert push: console;webhook;file (semicolon separated)
    notifier_types_raw: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_NOTIFIER_TYPES", "console")
    )
    webhook_url: str = field(default_factory=lambda: _env_str("MORAY_AIO_WEBHOOK_URL", ""))
    # Webhook domain allowlist (semicolon separated); empty -> webhook refuses to work (fail-closed)
    webhook_allowed_hosts_raw: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_WEBHOOK_ALLOWED_HOSTS", "")
    )
    alerts_log_path: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_ALERTS_LOG_PATH", "data/alerts.log")
    )

    # ---- Observability & protection (v1.1.0) ----
    # Log level (DEBUG/INFO/...); DEBUG switches the console to readable colored text
    log_level: str = field(default_factory=lambda: _env_str("MORAY_AIO_LOG_LEVEL", "INFO"))
    # Log format: "auto" -> JSON in production, colored text under DEBUG; or force "json"/"text"
    log_format: str = field(default_factory=lambda: _env_str("MORAY_AIO_LOG_FORMAT", "auto"))
    # Rate limiting: analysis-triggering endpoints, per client IP per minute; 0 disables
    rate_limit_per_minute: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_RATE_LIMIT_PER_MINUTE", 5)
    )

    # ---- Semantic long-term memory (v2.0.0) ----
    # Master switch; when off (or the embedding model is missing) the kernel
    # silently uses keyword Jaccard retrieval instead
    vector_memory_enabled: bool = field(
        default_factory=lambda: _env_bool("MORAY_AIO_VECTOR_MEMORY_ENABLED", True)
    )
    vector_db_path: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_VECTOR_DB_PATH", "data/vector_memory.sqlite3")
    )
    # Local Ollama embedding model (pull: ollama pull nomic-embed-text)
    embedding_model: str = field(
        default_factory=lambda: _env_str("MORAY_AIO_EMBEDDING_MODEL", "nomic-embed-text")
    )
    vector_top_k: int = field(default_factory=lambda: _env_int("MORAY_AIO_VECTOR_TOP_K", 3))
    # Minimum cosine similarity for a stored case to count as "similar"
    vector_similarity_threshold: float = field(
        default_factory=lambda: _env_float("MORAY_AIO_VECTOR_SIMILARITY_THRESHOLD", 0.35)
    )
    # Upper bound on stored episodes (oldest evicted first)
    vector_max_items: int = field(
        default_factory=lambda: _env_int("MORAY_AIO_VECTOR_MAX_ITEMS", 500)
    )

    @property
    def readonly_roots(self) -> list:
        """Whitelist roots; empty segments ignored."""
        return [p.strip() for p in self.readonly_roots_raw.split(";") if p.strip()]

    @property
    def ollama_allowed_hosts(self) -> list:
        """Ollama host whitelist; empty segments ignored."""
        return [h.strip() for h in self.ollama_allowed_hosts_raw.split(";") if h.strip()]

    @property
    def probe_host_whitelist(self) -> list:
        """Probe host whitelist; empty segments ignored."""
        return [h.strip() for h in self.probe_host_whitelist_raw.split(";") if h.strip()]

    @property
    def plugin_dirs(self) -> list:
        """Plugin directories; empty segments ignored."""
        return [p.strip() for p in self.plugin_dirs_raw.split(";") if p.strip()]

    def as_dict(self) -> Dict[str, Any]:
        """Non-secret view of the settings (never includes api_key/db_dsn secrets)."""
        return {
            k: v
            for k, v in self.__dict__.items()
            if k not in ("api_key", "db_dsn", "webhook_url")
        }


settings = Settings()
