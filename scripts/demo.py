"""moray-aio 一键演示脚本（仅标准库，无额外依赖）。

自动连接本机 moray-aio 服务（默认 http://127.0.0.1:8020），
现场观看 Agent 自驱只读分析的全过程：

    POST /episodes
    → 若停在 awaiting_approval：
        POST /episodes/{id}/tasks/approve-all   （服务端只批只读白名单内的任务）
        POST /episodes/{id}/resume
    → 循环，直到 phase=concluded / failed（或达到轮数上限）
    → 打印最终结论与长效记忆（反思记录）

用法：
    python scripts/demo.py [BASE_URL]
    # 或 set MORAY_AIO_DEMO_URL=http://127.0.0.1:8020
"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8020"
# 演示脚本的连接目标白名单：只允许连本机 moray-aio 服务
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_ROUNDS = 12


def validate_base_url(base_url: str) -> str:
    """仅接受 origin-only 的 http/https 且主机在本机白名单内；返回规范化 origin。"""
    parts = urlsplit(str(base_url).strip())
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https"):
        raise SystemExit("base_url 只允许 http/https：{!r}".format(base_url))
    if not host or host not in ALLOWED_HOSTS:
        raise SystemExit(
            "演示脚本只允许连接本机服务 {}，收到：{!r}".format(sorted(ALLOWED_HOSTS), base_url)
        )
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise SystemExit("base_url 必须是 origin-only（无路径/凭据/query/fragment）：{!r}".format(base_url))
    try:
        port = parts.port
    except ValueError:
        raise SystemExit("base_url 端口非法：{!r}".format(base_url))
    host_repr = "[{}]".format(host) if ":" in host else host
    if port:
        return "{}://{}:{}".format(scheme, host_repr, port)
    return "{}://{}".format(scheme, host_repr)


def api(origin, method, path, payload=None):
    """调用一次 HTTP 接口；HTTPError 时打印 detail 并返回 None。"""
    data = None
    headers = {}
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
        print("[连接失败] {}{} -> {}".format(origin, path, exc.reason))
        print("请确认服务已启动：python -m uvicorn app.main:app --host 127.0.0.1 --port 8020")
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
        print("  [分析] " + short(analysis.get("summary"), 96))
    for cause in (analysis.get("likely_root_causes") or [])[:3]:
        print("  [根因] " + short(cause, 96))
    if ep.get("error"):
        print("  [错误] " + short(ep.get("error"), 96))


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

    print("== moray-aio 一键演示 ==")
    print("目标服务: {}".format(origin))
    print("说明: 本脚本只调用只读分析接口；approve-all 只会批准服务端白名单内的只读任务。\n")

    ep = api(origin, "POST", "/episodes")
    if ep is None:
        raise SystemExit(1)

    for round_no in range(1, MAX_ROUNDS + 1):
        print("---- 第 {} 轮 ----".format(round_no))
        print_episode(ep)
        phase = ep.get("phase")
        if phase == "awaiting_approval":
            print("待审批任务:")
            print_tasks(ep.get("tasks"), statuses={"proposed"})
            print(">> 批量批准只读任务 (approve-all) ...")
            ep = api(origin, "POST", "/episodes/{}/tasks/approve-all".format(ep["id"]))
            if ep is None:
                raise SystemExit(1)
            print(">> 恢复迭代 (resume) ...")
            ep = api(origin, "POST", "/episodes/{}/resume".format(ep["id"]))
            if ep is None:
                raise SystemExit(1)
            continue
        if phase in ("concluded", "failed"):
            break
        print(">> 状态为 {}，无需审批，结束轮询".format(phase))
        break

    print("\n==== 最终结果 ====")
    print_episode(ep, header="最终 episode:")
    print_tasks(ep.get("tasks"))
    print("\n结论: {}".format(ep.get("conclusion") or "(无)"))

    print("\n==== 长效记忆（最近反思）====")
    reflections = api(origin, "GET", "/memory/reflections?limit=5") or []
    for item in reflections:
        print("  - {}".format(short(item.get("lesson"), 160)))
    if not reflections:
        print("  (暂无反思记录)")


if __name__ == "__main__":
    main()
