"""moray-aio 一键演示脚本（仅标准库，无额外依赖）。

自动连接本机 moray-aio 服务（默认 http://127.0.0.1:8020），
以异步模式现场观看 Agent 自驱只读分析的全过程：

    POST /episodes                     -> 202 + episode_id（后台执行）
    轮询 GET /episodes/{id}（每 2 秒，最多 60 次）
    -> phase=awaiting_approval 时：
        POST /episodes/{id}/tasks/approve-all   （服务端只批只读白名单内的任务）
        POST /episodes/{id}/resume
    -> 直到 phase=concluded / failed
    -> 打印最终结论、探针结果（probe 模式）与长效记忆（反思记录）

用法：
    python scripts/demo.py [BASE_URL]
    set MORAY_AIO_DEMO_URL=http://127.0.0.1:8020
    set MORAY_AIO_DEMO_MODE=probe        # 探针演示：用模拟日志观察探针派发

probe 模式需要当前工作目录下存在演示日志 moray-demo-fixture.log（含
Connection refused 127.0.0.1:6379）。出于安全策略本脚本不写任何文件：
夹具缺失时会打印其内容与创建指引，创建后重试即可。
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8020"
# 演示脚本的连接目标白名单：只允许连本机 moray-aio 服务
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
POLL_INTERVAL_SECONDS = 2
MAX_POLLS = 60
PROBE_TOOLS = {"port_check", "process_list", "disk_usage", "windows_service_status"}

# probe 模式演示夹具：固定文件名字符串字面量（单层文件名，无目录分量、无 ..）
FIXTURE_LOG_NAME = "moray-demo-fixture.log"

# probe 模式：模拟一份「Connection refused 127.0.0.1:6379」日志，观察 Agent 是否派发探针
SIMULATED_LOG = "\n".join(
    [
        "2026-10-24 18:00:00 INFO moray backend started",
        "2026-10-24 18:01:12 ERROR Connection refused to cache 127.0.0.1:6379",
        "2026-10-24 18:02:40 ERROR Connection refused to cache 127.0.0.1:6379",
        "2026-10-24 18:04:01 INFO job tick",
    ]
) + "\n"


def ensure_simulated_log() -> str:
    """probe 模式夹具检查（只读）：存在返回文件名；缺失则打印内容与创建指引并返回空串。

    出于安全策略（脚本内不允许文件写入），本脚本不创建夹具；
    请按下述指引预先创建后再运行 probe 模式。
    """
    if os.path.isfile(FIXTURE_LOG_NAME):
        return FIXTURE_LOG_NAME
    print("[probe 模式] 未找到演示日志 {}（安全策略下本脚本不写文件）。".format(FIXTURE_LOG_NAME))
    print("请先在当前工作目录创建该文件，内容如下：")
    print("-" * 56)
    for line in SIMULATED_LOG.splitlines():
        print(line)
    print("-" * 56)
    print("创建示例（PowerShell，在脚本工作目录执行）：")
    print("  Set-Content -Path moray-demo-fixture.log -Encoding UTF8 -Value '2026-10-24 18:00:00 INFO moray backend started','2026-10-24 18:01:12 ERROR Connection refused to cache 127.0.0.1:6379','2026-10-24 18:02:40 ERROR Connection refused to cache 127.0.0.1:6379','2026-10-24 18:04:01 INFO job tick'")
    return ""


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
    mode = (os.environ.get("MORAY_AIO_DEMO_MODE") or "log").strip().lower()

    print("== moray-aio 一键演示（异步模式，mode={}）==".format(mode))
    print("目标服务: {}".format(origin))
    print("说明: 只调用只读分析接口；approve-all 只会批准服务端白名单内的只读任务。\n")

    if mode == "probe":
        log_path = ensure_simulated_log()
        if not log_path:
            raise SystemExit("probe 夹具缺失：请按上方指引创建 moray-demo-fixture.log 后重试。")
        print("[probe 模式] 使用模拟日志: {}\\{}（含 Connection refused 127.0.0.1:6379）".format(os.getcwd(), log_path))
        print("  （服务需以 MORAY_AIO_READONLY_ROOTS / MORAY_AIO_LOG_PATH 指向该目录启动）\n")

    created = api(origin, "POST", "/episodes")
    if created is None:
        raise SystemExit(1)
    episode_id = created["episode_id"]
    print("已创建 episode: {}（phase={}，后台执行中）".format(episode_id, created.get("phase")))

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
                print("  [分析] " + short(analysis.get("summary"), 96))
            last_signature = signature

        if phase == "awaiting_approval":
            print("待审批任务:")
            print_tasks(ep.get("tasks"), statuses={"proposed"})
            print(">> 批量批准只读任务 (approve-all) ...")
            ep = api(origin, "POST", "/episodes/{}/tasks/approve-all".format(episode_id)) or ep
            print(">> 恢复迭代 (resume) ...")
            ep = api(origin, "POST", "/episodes/{}/resume".format(episode_id)) or ep
            last_signature = None  # resume 后强制重印一次新状态
            continue
        if phase in ("concluded", "failed"):
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    print("\n==== 最终结果 ====")
    print_episode(ep, header="最终 episode:")
    print_tasks(ep.get("tasks"))
    print("\n结论: {}".format(ep.get("conclusion") or "(无)"))

    if mode == "probe":
        print("\n==== 探针结果（Agent 现在看得见系统状态了）====")
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
            print("  （本轮未派发探针任务）")

    print("\n==== 长效记忆（最近反思）====")
    reflections = api(origin, "GET", "/memory/reflections?limit=5") or []
    for item in reflections:
        print("  - {}".format(short(item.get("lesson"), 160)))
    if not reflections:
        print("  (暂无反思记录)")


if __name__ == "__main__":
    main()
