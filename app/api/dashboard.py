"""Visualization console: single-page HTML, aggregated data endpoint and the SSE
live stream (geek-styled, information-dense, zero build-step dependencies)."""

from __future__ import annotations

import asyncio
import json
import queue
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from app.deps import get_loop, get_memory
from app.kernel.events import EVENT_BUS

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


# ---------------------------------------------------------------- SSE live stream

async def _sse_generator(subscriber: "queue.Queue", is_disconnected) -> AsyncIterator[str]:
    """SSE frame generator: hello, then bus events; `: heartbeat` when idle.

    Kept endpoint-agnostic (``is_disconnected`` is injectable) so tests can drive
    it without an HTTP stack; production passes ``request.is_disconnected``.
    """
    try:
        yield "event: hello\ndata: {}\n\n".format(json.dumps({"ok": True}))
        while True:
            if await is_disconnected():
                break
            delivered = 0
            while True:
                try:
                    event_type, payload = subscriber.get_nowait()
                except queue.Empty:
                    break
                delivered += 1
                yield "event: {}\ndata: {}\n\n".format(
                    event_type, json.dumps(payload, ensure_ascii=False)
                )
            if delivered == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1)
    finally:
        EVENT_BUS.unsubscribe(subscriber)


@dashboard_router.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    """Server-Sent Events live stream: log captures, rule fires, phase transitions.

    Event types: hello (on connect), log_captured, rule_fired, episode_phase, alert.
    Idle cycles emit `: heartbeat` comment lines so proxies keep the connection open.
    """
    subscriber = EVENT_BUS.subscribe()
    return StreamingResponse(
        _sse_generator(subscriber, request.is_disconnected),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
