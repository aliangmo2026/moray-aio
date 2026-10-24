"""HTTP 接口：健康检查、只读日志查询、episode 生命周期与任务审批。

设计约束：所有接口都是只读分析入口；任何失败都不改写外部世界，
只返回对应的状态码与错误说明。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app import __version__
from app.config import settings
from app.deps import get_llm, get_loop, get_memory, get_tools
from app.kernel.state import Phase
from app.tools.log_reader import LogReadError, check_readable, recent_errors

router = APIRouter()


@router.get("/health")
def health():
    """健康检查：目标日志只读探测 + Ollama 可用性（探测失败不影响服务）。"""
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
    """最近 N 条 ERROR 日志（时间正序）。日志文件不存在返回 404。"""
    if limit is None:
        limit = settings.recent_error_limit
    try:
        # 白名单校验：log_path 必须落在只读根内，越界 403
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
    """异步启动一次自我迭代：立即返回 202 + episode_id，后台执行（轮询 GET /episodes/{id}）。"""
    episode_id = get_loop().start_async(trigger=trigger)
    return JSONResponse(
        status_code=202,
        content={"episode_id": episode_id, "phase": Phase.CREATED.value},
    )


@router.post("/episodes/sync")
def create_episode_sync(trigger: str = Query(default="recent_errors")):
    """同步启动（调试用）：阻塞直到本轮迭代到达终态/审批点，返回完整 episode。"""
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
    """批准一个待审批任务。非只读工具返回 403。"""
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
    """否决一个待审批任务。"""
    try:
        return get_loop().reject_task(episode_id, task_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/episodes/{episode_id}/tasks/approve-all")
def approve_all_tasks(episode_id: str):
    """批量批准：只批准 status=proposed 且 tool 在只读白名单内的任务。"""
    try:
        return get_loop().approve_all(episode_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/episodes/{episode_id}/tasks/reject-all")
def reject_all_tasks(episode_id: str):
    """批量拒绝：所有 status=proposed 的任务一律 rejected。"""
    try:
        return get_loop().reject_all(episode_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/memory/reflections")
def list_reflections(limit: int = Query(default=5, ge=1, le=50)):
    """最近的反思/教训记录（长效记忆，只读查询）。"""
    return get_memory().recent_reflections(limit=limit)


@router.post("/episodes/{episode_id}/resume")
def resume_episode(episode_id: str):
    """恢复一次迭代：无待审批任务时执行 approved 任务并继续循环。"""
    try:
        return get_loop().resume(episode_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
