#!/usr/bin/env python
"""moray-aio 命令行入口（仅标准库，无需启动 HTTP 即可使用）。

用法：
    python cli.py serve [--host H] [--port P]     启动 FastAPI 服务
    python cli.py run --trigger recent_errors --auto-approve
                                                  直接跑一次完整分析（不经 HTTP），终端彩色输出
    python cli.py health [--url URL]              检查服务状态

run 的配置全部来自 MORAY_AIO_* 环境变量（同服务）；输出使用纯 ANSI 转义码，
设置 NO_COLOR=1 可关闭颜色。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:8020"
# health 命令的连接目标白名单：只允许连本机 moray-aio 服务
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

_ANSI = {"bold": "1", "red": "31", "green": "32", "yellow": "33", "cyan": "36", "dim": "2"}


def paint(text: str, style: str) -> str:
    if os.environ.get("NO_COLOR", "") or not sys.stdout.isatty():
        return str(text)
    return "\033[{}m{}\033[0m".format(_ANSI.get(style, "0"), text)


def validate_base_url(base_url: str) -> str:
    """仅接受 origin-only 的 http/https 且主机在本机白名单内；返回规范化 origin。"""
    parts = urlsplit(str(base_url).strip())
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https"):
        raise SystemExit("url 只允许 http/https：{!r}".format(base_url))
    if not host or host not in ALLOWED_HOSTS:
        raise SystemExit(
            "health 只允许连接本机服务 {}，收到：{!r}".format(sorted(ALLOWED_HOSTS), base_url)
        )
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise SystemExit("url 必须是 origin-only：{!r}".format(base_url))
    try:
        port = parts.port
    except ValueError:
        raise SystemExit("url 端口非法：{!r}".format(base_url))
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
    print(paint("moray-aio 服务启动: http://{}:{}".format(host, port), "cyan"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
    return 0


# ---------------------------------------------------------------- run

def cmd_run(args) -> int:
    from app.config import Settings
    from app.kernel.loop import EvolutionLoop
    from app.kernel.memory import Memory
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

    print(paint("== moray-aio run（trigger={}）==".format(args.trigger), "bold"))
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
    print(paint("phase: {}   iteration: {}   tasks: {}".format(phase, ep.iteration, len(ep.tasks)), style))

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
        print(paint("-- 分析 --", "bold"))
        summary = (ep.analysis or {}).get("summary")
        if summary:
            print("  " + str(summary))
        for cause in ((ep.analysis or {}).get("likely_root_causes") or [])[:3]:
            print("  - " + str(cause))

    print()
    print(paint("结论: {}".format(ep.conclusion or "(无)"), "cyan"))
    if ep.error:
        print(paint("错误: {}".format(ep.error), "red"))


# ---------------------------------------------------------------- health

def cmd_health(args) -> int:
    origin = validate_base_url(args.url or DEFAULT_URL)
    req = Request(origin + "/health", method="GET")
    try:
        with urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        print(paint("health 检查失败: HTTP {}".format(exc.code), "red"))
        return 1
    except (URLError, OSError) as exc:
        print(paint("无法连接 {}: {}".format(origin, exc), "red"))
        print("请确认服务已启动：python cli.py serve")
        return 1

    print(paint("== moray-aio health ==", "bold"))
    print("  status:  " + paint(str(body.get("status")), "green" if body.get("status") == "ok" else "red"))
    print("  version: {}".format(body.get("version")))
    log = body.get("log") or {}
    log_style = "green" if log.get("readable") else "yellow"
    print("  log:     " + paint(
        "{} exists={} readable={} size={}".format(
            log.get("path"), log.get("exists"), log.get("readable"), log.get("size_bytes")
        ),
        log_style,
    ))
    ollama = body.get("ollama") or {}
    ollama_style = "green" if ollama.get("available") else "yellow"
    print("  ollama:  " + paint(
        "available={} model={}".format(ollama.get("available"), ollama.get("model")), ollama_style
    ))
    return 0


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moray-aio", description="只读 AIOps 分析引擎 CLI")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="启动 FastAPI 服务")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=cmd_serve)

    p_run = sub.add_parser("run", help="直接跑一次完整分析（不经 HTTP）")
    p_run.add_argument("--trigger", default="recent_errors")
    p_run.add_argument("--auto-approve", action="store_true", help="自动批准只读任务")
    p_run.add_argument("--max-iterations", type=int, default=None)
    p_run.set_defaults(func=cmd_run)

    p_health = sub.add_parser("health", help="检查服务状态")
    p_health.add_argument("--url", default=None, help="服务地址（默认 {}）".format(DEFAULT_URL))
    p_health.set_defaults(func=cmd_health)

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
