"""Structured logging setup: JSON for production (ELK/Loki friendly), colored text under DEBUG.

Usage: call ``setup_logging(settings.log_level, settings.log_format)`` once at startup
(FastAPI lifespan or CLI entry). Only the ``moray_aio`` logger tree is configured;
third-party loggers (uvicorn, httpx) keep their own settings.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Optional

_LOGGER_ROOT = "moray_aio"


class JsonFormatter(logging.Formatter):
    """One JSON object per line — ship it to ELK / Grafana Loki as-is."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class ColoredTextFormatter(logging.Formatter):
    """Human-readable colored text for local DEBUG sessions."""

    _COLORS = {
        "DEBUG": "\033[36m",  # cyan
        "INFO": "\033[32m",  # green
        "WARNING": "\033[33m",  # yellow
        "ERROR": "\033[31m",  # red
        "CRITICAL": "\033[35m",  # magenta
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self._COLORS.get(record.levelname, "")
        level = "{}{}{}".format(color, record.levelname, self._RESET)
        return "{} {} {}: {}".format(
            self.formatTime(record, "%H:%M:%S"), level, record.name, record.getMessage()
        )


def _enable_windows_ansi() -> None:
    """Best-effort VT enablement so ANSI colors render on legacy Windows consoles."""
    if os.name == "nt":
        try:
            os.system("")  # no-op that flips the console into VT processing mode
        except Exception:
            pass


def setup_logging(level: str = "INFO", fmt: str = "auto") -> None:
    """Configure the ``moray_aio`` logger tree. Idempotent (handlers are replaced)."""
    resolved_level = str(level or "INFO").upper()
    if resolved_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        resolved_level = "INFO"
    effective_fmt = str(fmt or "auto").lower()
    if effective_fmt not in ("json", "text", "auto"):
        effective_fmt = "auto"
    if effective_fmt == "auto":
        effective_fmt = "text" if resolved_level == "DEBUG" else "json"

    root = logging.getLogger(_LOGGER_ROOT)
    root.setLevel(resolved_level)
    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler: Optional[logging.Handler] = logging.StreamHandler(sys.stderr)
    if effective_fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        _enable_windows_ansi()
        handler.setFormatter(ColoredTextFormatter())
    root.addHandler(handler)
    root.propagate = False
