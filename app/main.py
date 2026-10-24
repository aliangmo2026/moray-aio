"""FastAPI entry point for moray-aio.

Includes:
- optional API-Key auth middleware (X-API-Key header or ?api_key= query param,
  constant-time comparison; GET /health is always exempt);
- optional per-IP rate limiting on analysis-triggering endpoints (protects the
  GPU token budget);
- optional Watch-mode lifecycle (starts/stops the log listener with the app);
- structured logging setup (JSON in production, colored text under DEBUG).
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Deque, Dict, Optional, Set

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.dashboard import dashboard_router
from app.api.routes import router
from app.config import Settings, settings
from app.logging_setup import setup_logging

logger = logging.getLogger("moray_aio.main")

API_KEY_HEADER = "X-API-Key"
RATE_LIMITED_PATHS: Set[str] = {"/episodes", "/episodes/sync"}


def _install_auth_middleware(application: FastAPI, api_key: str) -> None:
    @application.middleware("http")
    async def api_key_middleware(request: Request, call_next):
        if request.url.path != "/health":
            # header or URL parameter (?api_key=... for the browser dashboard)
            provided = request.headers.get(API_KEY_HEADER, "") or request.query_params.get(
                "api_key", ""
            )
            if not hmac.compare_digest(
                provided.encode("utf-8"), api_key.encode("utf-8")
            ):
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)


class _RateLimiter:
    """Tiny per-key sliding-window limiter (thread-safe; no external dependency)."""

    def __init__(self, limit_per_minute: int):
        self.limit = max(0, int(limit_per_minute))
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            window = self._hits[key]
            while window and now - window[0] > 60.0:
                window.popleft()
            if len(window) >= self.limit:
                return False
            window.append(now)
            return True


def _install_rate_limit_middleware(application: FastAPI, limit_per_minute: int) -> None:
    limiter = _RateLimiter(limit_per_minute)

    @application.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        # Only analysis-triggering endpoints are limited (they can burn GPU tokens)
        if request.method == "POST" and request.url.path in RATE_LIMITED_PATHS:
            client_ip = request.client.host if request.client else "unknown"
            if not limiter.allow(client_ip):
                logger.warning("rate limit exceeded for %s on %s", client_ip, request.url.path)
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "Rate limit exceeded: max {} analysis requests per minute per IP".format(
                            limiter.limit
                        )
                    },
                )
        return await call_next(request)


def _make_lifespan(cfg: Settings):
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        started = False
        if cfg.watch_enabled:
            try:
                from app.kernel.watch_service import get_watch_manager

                get_watch_manager().start()
                started = True
                logger.info("watch service auto-started (watch_enabled=true)")
            except Exception:
                logger.exception("watch service failed to auto-start")
        yield
        if started:
            try:
                from app.kernel.watch_service import get_watch_manager

                get_watch_manager().stop()
            except Exception:
                logger.exception("watch service stop failed")

    return lifespan


def create_app(config: Optional[Settings] = None) -> FastAPI:
    cfg = config or settings
    setup_logging(cfg.log_level, cfg.log_format)
    application = FastAPI(
        title=cfg.app_name, version=cfg.version, lifespan=_make_lifespan(cfg)
    )
    if cfg.api_key:
        _install_auth_middleware(application, cfg.api_key)
    if cfg.rate_limit_per_minute > 0:
        _install_rate_limit_middleware(application, cfg.rate_limit_per_minute)
    application.include_router(router)
    application.include_router(dashboard_router)
    return application


app = create_app()


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
