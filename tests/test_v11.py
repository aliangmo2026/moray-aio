"""v1.1.0 polish tests: analysis rate limiting and structured logging."""

from __future__ import annotations

import dataclasses
import json
import logging
import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.kernel.loop import EvolutionLoop
from app.kernel.sqlite_repository import Memory
from app.logging_setup import ColoredTextFormatter, JsonFormatter, setup_logging
from app.tools.registry import ToolRegistry


class _OfflineLLM:
    def is_available(self):
        return False


def _build_app(tmp_path, monkeypatch, **overrides):
    log = tmp_path / "moray.err.log"
    log.write_text("ERROR Connection refused to 127.0.0.1:6379\n", encoding="utf-8")
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp_path),
        max_iterations=1,
        memory_db_path=str(tmp_path / "mem.sqlite3"),
        **overrides,
    )
    memory = Memory(settings.memory_db_path)
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    loop = EvolutionLoop(
        settings=settings, memory=memory, tools=tools, llm=_OfflineLLM(), auto_approve_readonly=True
    )

    from app.api import routes
    from app.main import create_app

    monkeypatch.setattr(routes, "get_loop", lambda: loop)
    monkeypatch.setattr(routes, "get_memory", lambda: memory)
    monkeypatch.setattr(routes, "get_llm", lambda: _OfflineLLM())
    return TestClient(create_app(settings))


# ---------- rate limiting ----------

def test_rate_limit_blocks_sixth_analysis_request(tmp_path, monkeypatch):
    client = _build_app(tmp_path, monkeypatch, rate_limit_per_minute=2)

    assert client.post("/episodes").status_code == 202
    assert client.post("/episodes").status_code == 202
    limited = client.post("/episodes")
    assert limited.status_code == 429
    assert "Rate limit exceeded" in limited.json()["detail"]
    # /health is never limited
    assert client.get("/health").status_code == 200


def test_rate_limit_disabled_when_zero(tmp_path, monkeypatch):
    client = _build_app(tmp_path, monkeypatch, rate_limit_per_minute=0)
    for _ in range(8):
        assert client.post("/episodes").status_code == 202


def test_rate_limit_is_per_ip_and_windowed(tmp_path, monkeypatch):
    from app.main import _RateLimiter

    limiter = _RateLimiter(2)
    assert limiter.allow("ip-a")
    assert limiter.allow("ip-a")
    assert not limiter.allow("ip-a")
    assert limiter.allow("ip-b")  # independent per client ip


def test_rate_limit_window_expires(tmp_path, monkeypatch):
    from app.main import _RateLimiter

    limiter = _RateLimiter(1)
    assert limiter.allow("ip-a")
    assert not limiter.allow("ip-a")
    # simulate one minute passing
    original = time.monotonic
    monkeypatch.setattr(
        "app.main.time.monotonic", lambda: original() + 61.0
    )
    assert limiter.allow("ip-a")


# ---------- structured logging ----------

def test_json_formatter_emits_parseable_records():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        "moray_aio.test", logging.INFO, "path.py", 1, "hello %s", ("world",), None
    )
    payload = json.loads(formatter.format(record))
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "moray_aio.test"
    assert {"timestamp", "module", "func", "line"} <= set(payload)


def test_text_formatter_is_colored():
    formatter = ColoredTextFormatter()
    record = logging.LogRecord(
        "moray_aio.test", logging.INFO, "path.py", 1, "colored", (), None
    )
    out = formatter.format(record)
    assert "\033[32m" in out  # green INFO
    assert "colored" in out


def test_setup_logging_auto_selects_format():
    setup_logging("INFO", "auto")
    root = logging.getLogger("moray_aio")
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.level == logging.INFO

    setup_logging("DEBUG", "auto")
    assert isinstance(root.handlers[0].formatter, ColoredTextFormatter)
    assert root.level == logging.DEBUG

    setup_logging("INFO", "json")  # restore the production default
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
