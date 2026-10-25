"""HTTP API: health check, read-only log queries, episode lifecycle, task approval,
watch-mode control and the dashboard data endpoint.

Design rule: every endpoint is a read/analysis entry point — failures never mutate
the outside world, they only produce the matching status code and message.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from app import __version__
from app.config import settings
from app.deps import get_llm, get_loop, get_memory, get_tools
from app.kernel.state import Phase
from app.tools.log_reader import LogReadError, check_readable, recent_errors

router = APIRouter()


@router.get("/health")
def health():
    """Health check: read-only probe of the target log + Ollama availability."""
    llm = get_llm()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": __version__,
        "time": datetime.now(timezone.utc).isoformat(),
        "log": check_readable(settings.log_path),
        "ollama": {"available": llm.is_available(), "model": settings.ollama_model},
    }


@router.get("/logs/recent-errors")
def logs_recent_errors(limit: int = Query(default=None, ge=1, le=200)):
    """Latest N ERROR lines (time ascending); 404 when the file is missing,
    403 when the path is outside the whitelist."""
    if limit is None:
        limit = settings.recent_error_limit
    try:
        path = get_tools()._check_path(settings.log_path)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    try:
        errors = recent_errors(path, limit=limit, max_scan_bytes=settings.max_scan_bytes)
    except LogReadError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "path": str(path),
        "limit": limit,
        "count": len(errors),
        "errors": [e.to_dict() for e in errors],
    }


@router.post("/episodes", status_code=202)
def create_episode(trigger: str = Query(default="recent_errors")):
    """Async start: returns 202 + episode_id immediately; the kernel runs in the
    background (poll GET /episodes/{id})."""
    episode_id = get_loop().start_async(trigger=trigger)
    return {
        "episode_id": episode_id,
        "phase": Phase.CREATED.value,
    }


@router.post("/episodes/sync")
def create_episode_sync(trigger: str = Query(default="recent_errors")):
    """Synchronous start (debugging): blocks until the run reaches a terminal or
    approval state, returning the full episode."""
    episode = get_loop().start(trigger=trigger)
    return episode.to_dict()


@router.get("/episodes")
def list_episodes(limit: int = Query(default=20, ge=1, le=100)):
    return [ep.to_dict() for ep in get_loop().list_recent(limit=limit)]


@router.get("/episodes/{episode_id}")
def get_episode(episode_id: str):
    try:
        return get_loop().require(episode_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/episodes/{episode_id}/tasks/{task_id}/approve")
def approve_task(episode_id: str, task_id: str):
    """Approve one proposed task; non-read-only tools return 403."""
    try:
        return get_loop().approve_task(episode_id, task_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/episodes/{episode_id}/tasks/{task_id}/reject")
def reject_task(episode_id: str, task_id: str):
    """Reject one proposed task."""
    try:
        return get_loop().reject_task(episode_id, task_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/episodes/{episode_id}/tasks/approve-all")
def approve_all_tasks(episode_id: str):
    """Batch approve: only proposed tasks whose tool is in the read-only whitelist."""
    try:
        return get_loop().approve_all(episode_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/episodes/{episode_id}/tasks/reject-all")
def reject_all_tasks(episode_id: str):
    """Batch reject: every proposed task becomes rejected."""
    try:
        return get_loop().reject_all(episode_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/episodes/{episode_id}/resume")
def resume_episode(episode_id: str):
    """Resume a run: when nothing is awaiting approval, execute approved tasks and
    continue the loop."""
    try:
        return get_loop().resume(episode_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/memory/reflections")
def list_reflections(limit: int = Query(default=5, ge=1, le=50)):
    """Recent reflections / lessons (long-term memory, read-only query)."""
    return get_memory().recent_reflections(limit=limit)


@router.get("/memory/similar")
def find_similar_episodes(
    keywords: str = Query(...),
    top_k: int = Query(default=3, ge=1, le=10),
    threshold: float = Query(default=0.3, ge=0.0, le=1.0),
):
    """Retrieve history episodes similar to the keywords (Jaccard, read-only)."""
    words = [w.strip() for w in keywords.split(",") if w.strip()]
    return get_memory().find_similar(words, top_k=top_k, threshold=threshold)


@router.get("/metrics")
def metrics():
    """Global metrics (read-only aggregation): episodes/success rate/tokens/
    average duration/task mix."""
    return get_memory().metrics()


# ---------------------------------------------------------------- watch mode

@router.post("/watch/start")
def watch_start():
    """Manually start the log listener (idempotent; the log path must be whitelisted)."""
    from app.kernel.watch_service import get_watch_manager

    try:
        return get_watch_manager().start()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/watch/stop")
def watch_stop():
    """Stop the log listener (idempotent)."""
    from app.kernel.watch_service import get_watch_manager

    return get_watch_manager().stop()


@router.get("/watch/status")
def watch_status():
    """Listener status: running, rule hit stats, last fire time, pending episodes."""
    from app.kernel.watch_service import get_watch_manager

    return get_watch_manager().status()
