"""moray-aio 的 FastAPI 入口（含可选 API Key 鉴权中间件）。

鉴权策略：settings.api_key（MORAY_AIO_API_KEY，只从环境变量读取）非空时，
除 GET /health 外的所有请求必须携带匹配的 X-API-Key 头，否则 401；
为空时鉴权关闭（本地调试模式）。
"""

from __future__ import annotations

import hmac

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import Settings, settings

API_KEY_HEADER = "X-API-Key"


def _install_auth_middleware(application: FastAPI, api_key: str) -> None:
    @application.middleware("http")
    async def api_key_middleware(request: Request, call_next):
        if request.url.path != "/health":
            provided = request.headers.get(API_KEY_HEADER, "")
            if not hmac.compare_digest(
                provided.encode("utf-8"), api_key.encode("utf-8")
            ):
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)


def create_app(config: Settings = None) -> FastAPI:
    cfg = config or settings
    application = FastAPI(title=cfg.app_name, version=cfg.version)
    if cfg.api_key:
        _install_auth_middleware(application, cfg.api_key)
    application.include_router(router)
    return application


app = create_app()


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
