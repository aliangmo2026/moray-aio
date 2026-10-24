"""moray-aio one-click demo script (standard library only, zero extra dependencies).

Connects to the local moray-aio service (default http://127.0.0.1:8020) and plays
the full event-driven analysis flow asynchronously:

    POST /episodes                     -> 202 + episode_id (background execution)
    Poll GET /episodes/{id} every 2 seconds (at most 60 times)
    -> when phase=awaiting_approval:
        POST /episodes/{id}/tasks/approve-all   (server only approves whitelisted
                                                 read-only tasks)
        POST /episodes/{id}/resume
    -> until phase=concluded / failed
    -> prints the final conclusion, probe results (probe mode) and long-term memory

Usage:
    python scripts/demo.py [BASE_URL]
    set MORAY_AIO_DEMO_URL=http://127.0.0.1:8020
    set MORAY_AIO_DEMO_MODE=probe        # probe demo: prepare the fixture log and
                                         # watch probe tasks being dispatched

Probe mode expects moray-demo-fixture.log in the current working directory
(containing "Connection refused 127.0.0.1:6379"). For security policy reasons this
script never writes files: when the fixture is missing it prints its content and
creation instructions.
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from moray_client import build_headers

DEFAULT_BASE_URL = "http://127.0.0.1:8020"
# Connection allowlist for the demo: only the local moray-aio service
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
POLL_INTERVAL_SECONDS = 2
MAX_POLLS = 60
PROBE_TOOLS = {"port_check", "process_list", "disk_usage", "windows_service_status"}

# Probe-mode fixture: a fixed single-segment filename literal (no directory
# component, no "..", no external input involved)
FIXTURE_LOG_NAME = "moray-demo-fixture.log"

# Simulated log: "Connection refused 127.0.0.1:6379" to watch probe dispatch
SIMULATED_LOG = "\n".join(
    [
        "2026-10-24 18:00:00 INFO moray backend started",
        "2026-10-24 18:01:12 ERROR Connection refused to cache 127.0.0.1:6379",
        "2026-10-24 18:02:40 ERROR Connection refused to cache 127.0.0.1:6379",
        "2026-10-24 18:04:01 INFO job tick",
    ]
) + "\n"


def ensure_simulated_log() -> str:
    """Probe-mode fixture check (read-only): returns the filename when present;
    otherwise prints the content plus creation instructions and returns "".

    For security policy reasons (no file writes inside scripts) this script does
    not create the fixture itself.
    """
    if os.path.isfile(FIXTURE_LOG_NAME):
        return FIXTURE_LOG_NAME
    print("[probe mode] demo log {} not found (this script never writes files).".format(FIXTURE_LOG_NAME))
    print("Create it in the current working directory with the following content:")
    print("-" * 56)
    for line in SIMULATED_LOG.splitlines():
        print(line)
    print("-" * 56)
    print("Creation example (PowerShell, from the script working directory):")
    print(
        "  Set-Content -Path moray-demo-fixture.log -Encoding UTF8 -Value "
        "'2026-10-24 18:00:00 INFO moray backend started',"
        "'2026-10-24 18:01:12 ERROR Connection refused to cache 127.0.0.1:6379',"
        "'2026-10-24 18:02:40 ERROR Connection refused to cache 127.0.0.1:6379',"
        "'2026-10-24 18:04:01 INFO job tick'"
    )
    return ""


def validate_base_url(base_url: str) -> str:
    """Accept http/https origin-only URLs whose host is in the local allowlist;
    returns the normalized origin."""
    parts = urlsplit(str(base_url).strip())
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https"):
        raise SystemExit("base_url must be http/https: {!r}".format(base_url))
    if not host or host not in ALLOWED_HOSTS:
        raise SystemExit(
            "the demo only connects to the local service {}; got {!r}".format(
                sorted(ALLOWED_HOSTS), base_url
            )
        )
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise SystemExit("base_url must be origin-only: {!r}".format(base_url))
    try:
        port = parts.port
    except ValueError:
        raise SystemExit("invalid base_url port: {!r}".format(base_url))
    host_repr = "[{}]".format(host) if ":" in host else host
    if port:
        return "{}://{}:{}".format(scheme, host_repr, port)
    return "{}://{}".format(scheme, host_repr)


def api(origin, method, path, payload=None):
    """One HTTP call (X-API-Key attached automatically); prints detail on HTTPError
    and returns None."""
    data = None
    headers = build_headers()
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(origin + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print("[HTTP {}] {} {}".format(exc.code, method, path))
        print("  " + detail[:500])
        return None
    except URLError as exc:
        print("[connection failed] {}{} -> {}".format(origin, path, exc.reason))
        print("Make sure the service is running: python -m uvicorn app.main:app --host 127.0.0.1 --port 8020")
        raise SystemExit(1)


def short(value, width=70):
    text = "" if value is None else str(value)
    return text if len(text) <= width else text[: width - 3] + "..."


def print_episode(ep, header=""):
    if header:
        print(header)
    print(
        "  phase={} iteration={} tasks={}".format(
            ep.get("phase"), ep.get("iteration"), len(ep.get("tasks") or [])
        )
    )
    analysis = ep.get("analysis") or {}
    if analysis.get("summary"):
        print("  [analysis] " + short(analysis.get("summary"), 96))
    for cause in (analysis.get("likely_root_causes") or [])[:3]:
        print("  [cause] " + short(cause, 96))
    if ep.get("error"):
        print("  [error] " + short(ep.get("error"), 96))


def print_tasks(tasks, statuses=None):
    for task in tasks or []:
        if statuses is not None and task.get("status") not in statuses:
            continue
        params = ",".join(
            "{}={}".format(k, short(v, 44)) for k, v in sorted((task.get("params") or {}).items())
        )
        print(
            "  - [{}] {} {} risk={} {}".format(
                task.get("status"),
                task.get("tool"),
                params,
                task.get("risk"),
                short(task.get("rationale"), 48),
            )
        )


def main():
    base = os.environ.get("MORAY_AIO_DEMO_URL") or DEFAULT_BASE_URL
    if len(sys.argv) > 1:
        base = sys.argv[1]
    origin = validate_base_url(base)
    mode = (os.environ.get("MORAY_AIO_DEMO_MODE") or "log").strip().lower()

    print("== moray-aio demo (async, mode={}) ==".format(mode))
    print("target service: {}".format(origin))
    print("note: read-only analysis endpoints only; approve-all releases whitelisted read-only tasks only.\n")

    if mode == "probe":
        log_path = ensure_simulated_log()
        if not log_path:
            raise SystemExit("probe fixture missing: create moray-demo-fixture.log (see above) and retry.")
        print("[probe mode] using simulated log: {}\\{} (contains Connection refused 127.0.0.1:6379)".format(os.getcwd(), log_path))
        print("  (start the service with MORAY_AIO_READONLY_ROOTS / MORAY_AIO_LOG_PATH pointing at this directory)\n")

    created = api(origin, "POST", "/episodes")
    if created is None:
        raise SystemExit(1)
    episode_id = created["episode_id"]
    print("episode created: {} (phase={}, running in background)".format(episode_id, created.get("phase")))

    ep = None
    last_signature = None
    for poll in range(1, MAX_POLLS + 1):
        ep = api(origin, "GET", "/episodes/{}".format(episode_id))
        if ep is None:
            raise SystemExit(1)
        phase = ep.get("phase")
        signature = (phase, ep.get("iteration"), len(ep.get("tasks") or []))
        if signature != last_signature:
            print("[poll {:02d}] phase={} iteration={} tasks={}".format(
                poll, phase, ep.get("iteration"), len(ep.get("tasks") or [])))
            analysis = ep.get("analysis") or {}
            if analysis.get("summary"):
                print("  [analysis] " + short(analysis.get("summary"), 96))
            last_signature = signature

        if phase == "awaiting_approval":
            print("tasks awaiting approval:")
            print_tasks(ep.get("tasks"), statuses={"proposed"})
            print(">> batch-approving read-only tasks (approve-all) ...")
            ep = api(origin, "POST", "/episodes/{}/tasks/approve-all".format(episode_id)) or ep
            print(">> resuming (resume) ...")
            ep = api(origin, "POST", "/episodes/{}/resume".format(episode_id)) or ep
            last_signature = None  # force a fresh status print after resume
            continue
        if phase in ("concluded", "failed"):
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    print("\n==== final result ====")
    print_episode(ep, header="final episode:")
    print_tasks(ep.get("tasks"))
    print("\nConclusion: {}".format(ep.get("conclusion") or "(none)"))

    if mode == "probe":
        print("\n==== probe results (the agent can see system state now) ====")
        found = False
        for task in ep.get("tasks") or []:
            if task.get("tool") in PROBE_TOOLS:
                found = True
                print(
                    "  - [{}] {} params={} result={}".format(
                        task.get("status"),
                        task.get("tool"),
                        json.dumps(task.get("params"), ensure_ascii=False),
                        json.dumps(task.get("result"), ensure_ascii=False)[:300],
                    )
                )
        if not found:
            print("  (no probe tasks dispatched this run)")

    print("\n==== long-term memory (recent reflections) ====")
    reflections = api(origin, "GET", "/memory/reflections?limit=5") or []
    for item in reflections:
        print("  - {}".format(short(item.get("lesson"), 160)))
    if not reflections:
        print("  (no reflections yet)")


if __name__ == "__main__":
    main()
