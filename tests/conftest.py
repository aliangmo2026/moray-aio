"""Shared pytest configuration.

The module-level ``app.main.app`` instance is shared across many test modules;
its rate limiter would otherwise bleed quota between tests (they all present as
the same client IP). Tests never exercise the limiter through the shared app —
tests/test_v11.py builds a dedicated app with an explicit limit instead.
"""

from __future__ import annotations

import os

# Applied before any test module imports app.main (conftest loads first).
os.environ.setdefault("MORAY_AIO_RATE_LIMIT_PER_MINUTE", "0")
os.environ.setdefault("MORAY_AIO_WATCH_ENABLED", "false")
