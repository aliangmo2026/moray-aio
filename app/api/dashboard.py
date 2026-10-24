"""可视化控制台：单页 HTML + 聚合数据接口（极客风、高信息密度、零构建依赖）。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.deps import get_loop, get_memory

dashboard_router = APIRouter()

_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"


@dashboard_router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    """单页控制台（Alpine.js + Tailwind CDN，暗色高密度）。"""
    return HTMLResponse(_TEMPLATE.read_text(encoding="utf-8"))


@dashboard_router.get("/api/dashboard/data")
def dashboard_data():
    """聚合数据：全局 metrics + 最近 20 个 episode（含 trace）+ watch 状态。"""
    memory = get_memory()
    loop = get_loop()
    episodes = []
    for ep in loop.list_recent(limit=20):
        data = ep.to_dict()
        episodes.append(
            {
                "id": data["id"],
                "trigger": data["trigger"],
                "phase": data["phase"],
                "iteration": data["iteration"],
                "conclusion": data.get("conclusion"),
                "error": data.get("error"),
                "created_at": data.get("created_at"),
                "tasks": [
                    {"tool": t.get("tool"), "status": t.get("status")}
                    for t in (data.get("tasks") or [])
                ],
                "trace": data.get("trace") or [],
            }
        )
    try:
        from app.kernel.watch_service import get_watch_manager

        watch = get_watch_manager().status()
    except Exception:
        watch = {"running": False, "rules": {}, "notifications_sent": 0, "pending_episodes": 0}
    return {
        "server_time": datetime.now(timezone.utc).isoformat(),
        "metrics": memory.metrics(),
        "episodes": episodes,
        "watch": watch,
    }
