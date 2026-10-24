"""moray-aio 的 FastAPI 入口（含可选 API Key 鉴权中间件 + Watch 模式生命周期）。

鉴权策略：settings.api_key（MORAY_AIO_API_KEY，只从环境变量读取）非空时，
除 GET /health 外的所有请求必须携带匹配的 X-API-Key 头，否则 401；
为空时鉴权关闭（本地调试模式）。

Watch 策略：settings.watch_enabled 为 True 时，lifespan 启动日志监听线程，
关闭时优雅停止（默认关闭，安全优先）。
"""

from __future__ import annotations

import logging
import hmac
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.api.dashboard import dashboard_router
from app.config import Settings, settings

logger = logging.getLogger("moray_aio.main")

API_KEY_HEADER = "X-API-Key"


def _install_auth_middleware(application: FastAPI, api_key: str) -> None:
    @application.middleware("http")
    async def api_key_middleware(request: Request, call_next):
        if request.url.path != "/health":
            # 支持 Header 或 URL 传参（?api_key=...，浏览器加载 /dashboard 用）
            provided = request.headers.get(API_KEY_HEADER, "") or request.query_params.get(
                "api_key", ""
            )
            if not hmac.compare_digest(
                provided.encode("utf-8"), api_key.encode("utf-8")
            ):
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
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


def create_app(config: Settings = None) -> FastAPI:
    cfg = config or settings
    application = FastAPI(
        title=cfg.app_name, version=cfg.version, lifespan=_make_lifespan(cfg)
    )
    if cfg.api_key:
        _install_auth_middleware(application, cfg.api_key)
    application.include_router(router)
    application.include_router(dashboard_router)
    return application


app = create_app()


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
