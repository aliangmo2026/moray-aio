"""moray-aio 的 FastAPI 入口。"""

from __future__ import annotations

from fastapi import FastAPI
import uvicorn

from app.api.routes import router
from app.config import settings


def create_app() -> FastAPI:
    application = FastAPI(title=settings.app_name, version=settings.version)
    application.include_router(router)
    return application


app = create_app()


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
