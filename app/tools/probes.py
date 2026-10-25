"""Read-only system probes: observe system state, never modify anything.

Hard rules:
- No state-changing operations whatsoever (no kill, no service start/stop, no file
  writes, no privilege escalation);
- port_check hosts must hit the whitelist; windows_service_status service_name must
  pass the safety regex ^[A-Za-z0-9_\\-\\s]{1,64}$ (anti command-injection) —
  violations raise PermissionError (fail-closed);
- psutil read-only APIs are preferred (zero subprocess); only when psutil is
  unavailable do we fall back to list-form, shell=False read-only commands
  (tasklist / sc query);
- Every probe failure degrades gracefully to {"error": ...} or open=false — a probe
  can never crash an episode;
- Output is length-capped (probe_output_max_bytes) to protect the episode payload.
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


def check_host_allowed(host: str, whitelist) -> None:
    """Host whitelist validation; raises PermissionError (fail-closed)."""
    if not host or str(host).strip().lower() not in _normalize_whitelist(whitelist):
        raise PermissionError(
            "probe host {!r} is not in the host whitelist {}".format(
                host, sorted(_normalize_whitelist(whitelist))
            )
        )


def _run_readonly_command(cmd: List[str]) -> Optional[str]:
    """List-form, shell=False read-only command; any failure returns None, never raises."""
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

def port_check(host: str, port: int, timeout: float = 2, host_whitelist=None) -> dict:
    """Probe TCP port reachability. Failures/timeouts return open=false, never raise."""
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

def process_list(name_pattern: Optional[str] = None, max_processes: int = 50) -> dict:
    """Read-only process listing (pid/name/username/cpu/memory/create_time), capped
    at max_processes entries.

    Never terminates/signals anything. Falls back to tasklist (list form,
    shell=False) when psutil is unavailable.
    """
    limit = max(1, int(max_processes))
    regex = None
    if name_pattern:
        regex = re.compile(str(name_pattern))  # invalid regex -> re.error, handled by caller

    try:
        import psutil  # local import: probes do not hard-require psutil
    except ImportError:
        psutil = None

    if psutil is not None:
        processes: List[dict] = []
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

    # Fallback: tasklist (Windows only, list form, read-only query)
    if os.name != "nt":
        return {
            "error": "probe_unavailable",
            "detail": "psutil unavailable; tasklist is windows-only",
        }
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
            {
                "pid": pid,
                "name": name,
                "username": parts[2] if len(parts) > 2 else None,
                "cpu_percent": None,
                "memory_mb": memory_mb,
                "create_time": None,
            }
        )
        if len(processes) >= limit:
            break
    return {"count": len(processes), "processes": processes, "source": "tasklist"}


# ---------------------------------------------------------------- disk_usage

def disk_usage(path: str) -> dict:
    """Read-only disk usage via shutil.disk_usage (writes nothing).
    Path whitelist is enforced by ToolRegistry."""
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

def windows_service_status(service_name: str, output_max_bytes: int = 4096) -> dict:
    """Read-only Windows service state query (never start/stop).

    service_name must pass the safety regex or PermissionError is raised.
    psutil.win_service_iter (pure read-only enumeration, zero subprocess) is tried
    first; `sc query <name>` (list form + shell=False) is the fallback.
    Non-Windows returns {"error": "not_windows"}.
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
            return {
                "name": name, "state": "UNKNOWN", "raw_output": "",
                "detail": "service not found",
            }
        except Exception as exc:
            # psutil query failed -> fall through to sc query
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
