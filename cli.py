#!/usr/bin/env python
"""moray-aio command line entry point (standard library only).

Usage:
    python cli.py serve [--host H] [--port P]     Start the FastAPI service
    python cli.py run --trigger recent_errors --auto-approve
                                                  Run one full analysis without HTTP,
                                                  printing a colored terminal report
    python cli.py health [--url URL]              Check the service status
    python cli.py watch [--log-path PATH]         Event-driven watch mode (Ctrl+C to stop)

``run`` reads the MORAY_AIO_* environment variables (same as the service).
Output uses plain ANSI escapes; set NO_COLOR=1 to disable colors.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:8020"
# Connection allowlist for the health command: only the local moray-aio service
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

_ANSI = {"bold": "1", "red": "31", "green": "32", "yellow": "33", "cyan": "36", "dim": "2"}


def paint(text: str, style: str) -> str:
    if os.environ.get("NO_COLOR", "") or not sys.stdout.isatty():
        return str(text)
    return "\033[{}m{}\033[0m".format(_ANSI.get(style, "0"), text)


def validate_base_url(base_url: str) -> str:
    """Accept http/https origin-only URLs whose host is in the local allowlist;
    returns the normalized origin."""
    parts = urlsplit(str(base_url).strip())
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https"):
        raise SystemExit("url must be http/https: {!r}".format(base_url))
    if not host or host not in ALLOWED_HOSTS:
        raise SystemExit(
            "health only connects to the local service {}; got {!r}".format(
                sorted(ALLOWED_HOSTS), base_url
            )
        )
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise SystemExit("url must be origin-only: {!r}".format(base_url))
    try:
        port = parts.port
    except ValueError:
        raise SystemExit("invalid url port: {!r}".format(base_url))
    host_repr = "[{}]".format(host) if ":" in host else host
    if port:
        return "{}://{}:{}".format(scheme, host_repr, port)
    return "{}://{}".format(scheme, host_repr)


# ---------------------------------------------------------------- serve

def cmd_serve(args) -> int:
    import uvicorn

    from app.config import settings

    host = args.host or settings.host
    port = args.port or settings.port
    print(paint("moray-aio service starting: http://{}:{}".format(host, port), "cyan"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
    return 0


# ---------------------------------------------------------------- run

def cmd_run(args) -> int:
    from app.config import Settings
    from app.kernel.loop import EvolutionLoop
    from app.kernel.sqlite_repository import Memory
    from app.kernel.state import Phase
    from app.llm.ollama_client import OllamaClient
    from app.tools.registry import ToolRegistry

    s = Settings()
    if args.auto_approve:
        s = dataclasses.replace(s, auto_approve_readonly=True)
    if args.max_iterations:
        s = dataclasses.replace(s, max_iterations=int(args.max_iterations))

    memory = Memory(s.memory_db_path)
    tools = ToolRegistry(
        s.readonly_roots,
        s.max_scan_bytes,
        probe_host_whitelist=s.probe_host_whitelist,
        probe_max_processes=s.probe_max_processes,
        probe_output_max_bytes=s.probe_output_max_bytes,
        plugin_dirs=s.plugin_dirs,
    )
    llm = OllamaClient(
        s.ollama_url, s.ollama_model, s.ollama_timeout, allowed_hosts=s.ollama_allowed_hosts
    )
    loop = EvolutionLoop(
        settings=s,
        memory=memory,
        tools=tools,
        llm=llm,
        auto_approve_readonly=s.auto_approve_readonly,
    )

    print(paint("== moray-aio run (trigger={}) ==".format(args.trigger), "bold"))
    ep = loop.start(trigger=args.trigger)
    _print_report(ep)

    if ep.phase == Phase.FAILED:
        return 1
    return 0


def _print_report(ep) -> None:
    phase = ep.phase.value
    style = "green" if phase == "concluded" else ("red" if phase == "failed" else "yellow")
    print()
    print(paint("episode: {}".format(ep.id), "dim"))
    print(
        paint(
            "phase: {}   iteration: {}   tasks: {}".format(phase, ep.iteration, len(ep.tasks)),
            style,
        )
    )

    if ep.trace:
        print()
        print(paint("-- trace --", "bold"))
        total_prompt = 0
        total_completion = 0
        for entry in ep.trace:
            usage = entry.get("llm") or {}
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            total_prompt += prompt
            total_completion += completion
            token_note = ""
            if usage:
                token_note = "  tokens: prompt={} completion={}".format(prompt, completion)
            print(
                "  [{stage}] iteration={it} {ms} ms{token}".format(
                    stage=entry.get("stage"),
                    it=entry.get("iteration"),
                    ms=entry.get("duration_ms"),
                    token=token_note,
                )
            )
        print(
            paint(
                "  total tokens: prompt={} completion={}".format(total_prompt, total_completion),
                "dim",
            )
        )

    if ep.tasks:
        print()
        print(paint("-- tasks --", "bold"))
        for task in ep.tasks:
            status = task.get("status")
            style = {"done": "green", "failed": "red", "empty": "yellow"}.get(status, "cyan")
            print(
                "  {} {} {}".format(
                    paint("[{}]".format(status), style),
                    task.get("tool"),
                    json.dumps(task.get("params"), ensure_ascii=False)[:120],
                )
            )

    if ep.analysis:
        print()
        print(paint("-- analysis --", "bold"))
        summary = (ep.analysis or {}).get("summary")
        if summary:
            print("  " + str(summary))
        for cause in ((ep.analysis or {}).get("likely_root_causes") or [])[:3]:
            print("  - " + str(cause))

    print()
    print(paint("Conclusion: {}".format(ep.conclusion or "(none)"), "cyan"))
    if ep.error:
        print(paint("Error: {}".format(ep.error), "red"))


# ---------------------------------------------------------------- health

def cmd_health(args) -> int:
    origin = validate_base_url(args.url or DEFAULT_URL)
    headers = {}
    api_key = os.environ.get("MORAY_AIO_API_KEY", "").strip()
    if api_key:
        headers["X-API-Key"] = api_key
    req = Request(origin + "/health", method="GET", headers=headers)
    try:
        with urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        print(paint("health check failed: HTTP {}".format(exc.code), "red"))
        return 1
    except (URLError, OSError) as exc:
        print(paint("cannot reach {}: {}".format(origin, exc), "red"))
        print("Make sure the service is running: python cli.py serve")
        return 1

    print(paint("== moray-aio health ==", "bold"))
    print(
        "  status:  "
        + paint(str(body.get("status")), "green" if body.get("status") == "ok" else "red")
    )
    print("  version: {}".format(body.get("version")))
    log = body.get("log") or {}
    log_style = "green" if log.get("readable") else "yellow"
    print(
        "  log:     "
        + paint(
            "{} exists={} readable={} size={}".format(
                log.get("path"), log.get("exists"), log.get("readable"), log.get("size_bytes")
            ),
            log_style,
        )
    )
    ollama = body.get("ollama") or {}
    ollama_style = "green" if ollama.get("available") else "yellow"
    print(
        "  ollama:  "
        + paint(
            "available={} model={}".format(ollama.get("available"), ollama.get("model")),
            ollama_style,
        )
    )
    return 0


# ---------------------------------------------------------------- watch

def cmd_watch(args) -> int:
    """Watch mode without FastAPI: log changes -> rules -> colored terminal alerts."""
    from app.config import Settings
    from app.kernel.loop import EvolutionLoop
    from app.kernel.sqlite_repository import Memory
    from app.kernel.trigger_rules import parse_rules_config
    from app.kernel.watch_service import WatchService
    from app.kernel.watcher import LogWatcher
    from app.llm.ollama_client import OllamaClient
    from app.notifier import build_notifiers
    from app.tools.registry import ToolRegistry

    s = Settings()
    tools = ToolRegistry(
        s.readonly_roots,
        s.max_scan_bytes,
        probe_host_whitelist=s.probe_host_whitelist,
        probe_max_processes=s.probe_max_processes,
        probe_output_max_bytes=s.probe_output_max_bytes,
        plugin_dirs=s.plugin_dirs,
    )
    log_path = args.log_path or s.log_path
    checked = tools._check_path(str(log_path))  # whitelist, fail-closed
    memory = Memory(s.memory_db_path)
    llm = OllamaClient(
        s.ollama_url, s.ollama_model, s.ollama_timeout, allowed_hosts=s.ollama_allowed_hosts
    )
    loop = EvolutionLoop(
        settings=s, memory=memory, tools=tools, llm=llm, auto_approve_readonly=True
    )
    watcher = LogWatcher(
        str(checked),
        on_lines=lambda lines: None,
        poll_interval=s.watch_poll_interval_seconds,
    )
    service = WatchService(
        log_path=str(checked),
        rules=parse_rules_config(s.watch_rules_config),
        watcher=watcher,
        loop=loop,
        notifier=build_notifiers(s),
        check_interval=s.watch_poll_interval_seconds,
    )

    print(paint("== moray-aio watch ==", "bold"))
    print("  watching: {}".format(checked))
    print(
        "  poll interval: {}s   rules: {}".format(
            s.watch_poll_interval_seconds, ", ".join(service.engine.fire_count)
        )
    )
    print(paint("  press Ctrl+C to stop", "dim"))
    service.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(paint("\nCtrl+C received, stopping...", "yellow"))
    finally:
        service.stop()
        print(paint("stopped. notifications_sent={}".format(service.notifications_sent), "dim"))
    return 0


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moray-aio", description="Read-only AIOps agent CLI")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Start the FastAPI service")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=cmd_serve)

    p_run = sub.add_parser("run", help="Run one full analysis without HTTP")
    p_run.add_argument("--trigger", default="recent_errors")
    p_run.add_argument("--auto-approve", action="store_true", help="Auto-approve read-only tasks")
    p_run.add_argument("--max-iterations", type=int, default=None)
    p_run.set_defaults(func=cmd_run)

    p_health = sub.add_parser("health", help="Check the service status")
    p_health.add_argument("--url", default=None, help="Service URL (default {})".format(DEFAULT_URL))
    p_health.set_defaults(func=cmd_health)

    p_watch = sub.add_parser("watch", help="Event-driven watch mode (auto analysis + alerts)")
    p_watch.add_argument("--log-path", default=None, help="Log to watch (default settings.log_path)")
    p_watch.set_defaults(func=cmd_watch)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
