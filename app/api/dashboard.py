"""Visualization console: single-page HTML + aggregated data endpoint
(geek-styled, information-dense, zero build-step dependencies)."""

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
    """Single-page console (Alpine.js + Tailwind via CDN, dark, high density)."""
    return HTMLResponse(_TEMPLATE.read_text(encoding="utf-8"))


@dashboard_router.get("/api/dashboard/data")
def dashboard_data():
    """Aggregated data: global metrics + the 20 most recent episodes (with trace)
    + watch status."""
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
