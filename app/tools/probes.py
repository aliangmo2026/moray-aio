"""只读系统探针：观察系统状态，绝不修改。

硬约束：
- 绝不执行任何修改系统状态的操作（不 kill、不启停服务、不写文件、不提权）；
- port_check 的 host 必须命中白名单，windows_service_status 的 service_name 必须通过
  安全正则 ^[A-Za-z0-9_\\-\\s]{1,64}$（防命令注入）——违规直接抛 PermissionError（fail-closed）；
- 优先使用 psutil 纯只读 API（零 subprocess）；仅在 psutil 不可用时才退到
  list 形式 + shell=False 的只读命令（tasklist / sc query）；
- 所有探测失败都优雅降级为 {"error": ...} 或 open=false，绝不让 episode 崩溃；
- 输出有长度上限（probe_output_max_bytes），防止撑爆 episode 载荷。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from typing import List, Optional

DEFAULT_HOST_WHITELIST = ("127.0.0.1", "localhost", "::1")
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\s]{1,64}$")
_PORT_MIN, _PORT_MAX = 1, 65535
_SUBPROC_TIMEOUT = 10


def _normalize_whitelist(whitelist) -> set:
    return {str(h).strip().lower() for h in (whitelist or DEFAULT_HOST_WHITELIST) if h}


def check_host_allowed(host, whitelist) -> None:
    """host 白名单校验；越界抛 PermissionError（fail-closed）。"""
    if not host or str(host).strip().lower() not in _normalize_whitelist(whitelist):
        raise PermissionError(
            "probe host {!r} is not in the host whitelist {}".format(host, sorted(_normalize_whitelist(whitelist)))
        )


def _run_readonly_command(cmd: List[str]) -> Optional[str]:
    """list 形式 + shell=False 的只读命令；任何失败返回 None，绝不抛出。"""
    try:
        completed = subprocess.run(
            cmd, shell=False, capture_output=True, timeout=_SUBPROC_TIMEOUT,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- port_check

def port_check(host, port, timeout=2, host_whitelist=None) -> dict:
    """探测 TCP 端口是否可连。失败/超时返回 open=false，不抛异常。"""
    check_host_allowed(host, host_whitelist)
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("port must be an integer: {!r}".format(port))
    if not (_PORT_MIN <= port <= _PORT_MAX):
        raise ValueError("port out of range {}-{}: {}".format(_PORT_MIN, _PORT_MAX, port))

    started = time.perf_counter()
    try:
        sock = socket.create_connection((str(host), port), timeout=max(0.1, float(timeout)))
        sock.close()
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return {"host": str(host), "port": port, "open": True, "latency_ms": latency_ms}
    except OSError:
        return {"host": str(host), "port": port, "open": False, "latency_ms": None}


# ---------------------------------------------------------------- process_list

def process_list(name_pattern=None, max_processes=50) -> dict:
    """只读列出进程（pid/name/username/cpu/memory/create_time），最多 max_processes 条。

    绝不 terminate/发送信号。psutil 不可用时退到 tasklist（list 形式，shell=False）。
    """
    limit = max(1, int(max_processes))
    regex = None
    if name_pattern:
        regex = re.compile(str(name_pattern))  # 非法正则抛 re.error，由调用方按失败任务处理

    try:
        import psutil  # 局部导入：探针层不强依赖
    except ImportError:
        psutil = None

    if psutil is not None:
        processes = []
        fields = ["pid", "name", "username", "cpu_percent", "memory_info", "create_time"]
        for proc in psutil.process_iter(attrs=fields, ad_value=None):
            info = proc.info
            name = info.get("name") or ""
            if regex is not None and not regex.search(name):
                continue
            mem = info.get("memory_info")
            processes.append(
                {
                    "pid": info.get("pid"),
                    "name": name,
                    "username": info.get("username"),
                    "cpu_percent": info.get("cpu_percent"),
                    "memory_mb": round(mem.rss / 1048576.0, 1) if mem else None,
                    "create_time": info.get("create_time"),
                }
            )
            if len(processes) >= limit:
                break
        return {"count": len(processes), "processes": processes, "source": "psutil"}

    # 兜底：tasklist（Windows only，list 形式只读查询）
    if os.name != "nt":
        return {"error": "probe_unavailable", "detail": "psutil unavailable; tasklist is windows-only"}
    raw = _run_readonly_command(["tasklist", "/fo", "csv", "/nh"])
    if raw is None:
        return {"error": "probe_unavailable", "detail": "psutil unavailable and tasklist failed"}
    processes = []
    for line in raw.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 5:
            continue
        name = parts[0]
        if regex is not None and not regex.search(name):
            continue
        try:
            pid = int(parts[1])
        except (ValueError, IndexError):
            continue
        mem_kb = parts[-1].replace(",", "").replace(" K", "").replace("K", "")
        try:
            memory_mb = round(float(mem_kb) / 1024.0, 1)
        except ValueError:
            memory_mb = None
        processes.append(
            {"pid": pid, "name": name, "username": parts[2] if len(parts) > 2 else None,
             "cpu_percent": None, "memory_mb": memory_mb, "create_time": None}
        )
        if len(processes) >= limit:
            break
    return {"count": len(processes), "processes": processes, "source": "tasklist"}


# ---------------------------------------------------------------- disk_usage

def disk_usage(path) -> dict:
    """只读查询磁盘占用（shutil.disk_usage，不写任何数据）。白名单由 ToolRegistry 校验。"""
    total, used, free = shutil.disk_usage(str(path))
    total_gb = round(total / 1073741824.0, 2)
    used_gb = round(used / 1073741824.0, 2)
    free_gb = round(free / 1073741824.0, 2)
    used_percent = round(used * 100.0 / total, 1) if total > 0 else None
    return {
        "path": str(path),
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "used_percent": used_percent,
    }


# ---------------------------------------------------------------- windows_service_status

def windows_service_status(service_name, output_max_bytes=4096) -> dict:
    """只读查询 Windows 服务状态（绝不 start/stop）。

    service_name 必须通过安全正则，否则抛 PermissionError。
    首选 psutil.win_service_iter（纯只读枚举，零 subprocess）；不可用时退到
    `sc query <name>`（list 形式 + shell=False）。非 Windows 返回 {"error": "not_windows"}。
    """
    name = str(service_name or "").strip()
    if not name or not SERVICE_NAME_RE.match(name):
        raise PermissionError(
            "service_name failed safety regex ^[A-Za-z0-9_\\-\\s]{{1,64}}$: {!r}".format(service_name)
        )
    cap = max(64, int(output_max_bytes))

    if os.name != "nt":
        return {"name": name, "state": "UNKNOWN", "error": "not_windows", "raw_output": ""}

    try:
        import psutil
    except ImportError:
        psutil = None

    if psutil is not None:
        try:
            for svc in psutil.win_service_iter():
                info = svc.as_dict()
                if (info.get("name") or "").lower() == name.lower() or (
                    info.get("display_name") or ""
                ).lower() == name.lower():
                    status = str(info.get("status") or "").upper()
                    state = "RUNNING" if status == "RUNNING" else (
                        "STOPPED" if status == "STOPPED" else "UNKNOWN"
                    )
                    return {
                        "name": name,
                        "state": state,
                        "raw_output": json.dumps(info, ensure_ascii=False)[:cap],
                    }
            return {"name": name, "state": "UNKNOWN", "raw_output": "", "detail": "service not found"}
        except Exception as exc:
            # psutil 查询失败 → 落到 sc query 兜底
            fallback_reason = "{}: {}".format(type(exc).__name__, exc)
    else:
        fallback_reason = "psutil unavailable"

    raw = _run_readonly_command(["sc.exe", "query", name])
    if raw is None:
        return {
            "name": name, "state": "UNKNOWN", "error": "probe_failed",
            "detail": fallback_reason, "raw_output": "",
        }
    state = "UNKNOWN"
    if "RUNNING" in raw:
        state = "RUNNING"
    elif "STOPPED" in raw:
        state = "STOPPED"
    return {"name": name, "state": state, "raw_output": raw[:cap]}
