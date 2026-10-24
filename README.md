# moray-aio

基于本地大模型（Ollama）的 **AIOps 智能运维 Agent**，定位是「**只读分析、辅助决策**」。
本仓库是独立引擎（MVP，v0.2.0），未来通过 HTTP 接入主项目 MoRay；本项目**不引用、不修改** MoRay 的任何代码。

## 核心能力（本阶段 MVP）

- FastAPI 服务骨架（默认 `127.0.0.1:8020`），`/health`、只读日志查询、episode 生命周期接口
- 只读日志分析：按字节窗口从尾部扫描大日志，提取最近 N 条 ERROR，不整读文件
- 自我迭代内核（预留）：状态机（Phase）、SQLite 长效记忆（episodes + reflections）、
  自驱任务生成（LLM 优先 / 启发式兜底）、审批闸门（proposed → approve/reject → resume）
- Ollama 接入只留接口：本地模型不可用时全程启发式兜底，服务不依赖 Ollama 存活

## 硬约束（代码层强制，不是口头约定）

1. **只读日志**：所有日志文件只以 `open(path, "rb")` 打开；绝不写、截断、重命名、删除日志。
2. **路径白名单**：所有工具读取的路径必须落在只读白名单（默认 `D:\MoRay\logs`）内，
   用 `Path.resolve() + relative_to` 校验，越界直接抛 `PermissionError`（含 `..` 目录穿越）。
3. **高危操作绝不执行**：kill/reboot/restart/shutdown/rm/del/format 只能作为
   「建议文本」出现在分析结论里；命中高危 token 的任务一律 `pending` 等人工处置。
4. **不碰 D:\MoRay 源码**：只允许只读访问 `D:\MoRay\logs` 下的日志文件。
5. **SSRF 防护**：Ollama URL 仅接受 origin-only 的 http/https，且主机必须命中显式白名单
   （默认仅环回地址，可用 `MORAY_AIO_OLLAMA_ALLOWED_HOSTS` 替换）。
6. **SQL 全参数绑定**：SQLite 访问一律 `?` 占位，无字符串拼接。

## 目录结构

```
E:\moray-aio\
├── .gitignore / .env.example / README.md
├── requirements.txt        # fastapi, uvicorn[standard], httpx
├── requirements-dev.txt    # -r requirements.txt, pytest
├── app\
│   ├── config.py           # Settings(frozen dataclass)，MORAY_AIO_* 环境变量全覆盖
│   ├── deps.py             # lru_cache 装配 memory/tools/llm/loop
│   ├── main.py             # FastAPI 入口
│   ├── api\routes.py       # HTTP 接口
│   ├── kernel\             # 自我迭代内核
│   │   ├── state.py        # Phase 枚举 + Episode（含 trace）+ 合法转移表
│   │   ├── repository.py   # EpisodeRepository 抽象接口 + 共享纯函数
│   │   ├── sqlite_repository.py  # SQLite 实现（默认单机模式，参数绑定+锁）
│   │   ├── postgres_repository.py # Postgres 实现（MORAY_AIO_DB_DSN 启用）
│   │   ├── approval.py     # 审批闸门（classify/decide）
│   │   ├── planner.py      # 自驱任务生成（LLM 优先 / 启发式兜底）
│   │   └── loop.py         # 主循环 Observe→Analyze→Propose→(审批)→Execute
│   ├── tools\              # 只读工具集（全部插件化）
│   │   ├── plugin_base.py  # BaseTool 插件基类（name/description/risk_level/params_schema/invoke）
│   │   ├── builtin\        # 内置工具：log_tools.py（4 个日志工具）+ probe_tools.py（4 个探针）
│   │   ├── log_reader.py   # tail/recent_errors/grep/read_rolling/check_readable（"rb" 只读）
│   │   ├── clusterer.py    # 错误聚类：normalize + cluster（模板折叠省 token）
│   │   ├── probes.py       # 只读系统探针实现（port/process/disk/service）
│   │   └── registry.py     # ToolRegistry：白名单校验 + 插件注册/目录动态加载
│   └── llm\
│       ├── prompts.py      # ANALYZE_*/PLAN_*（只读、白名单、严格 JSON）
│       └── ollama_client.py# /api/generate，token 用量统计，不可用自动兜底
├── examples\
│   └── dummy_echo_plugin.py  # 示例插件（MORAY_AIO_PLUGIN_DIRS=examples 即可加载）
├── cli.py                  # 命令行入口：serve / run / health（纯 ANSI）
├── Dockerfile              # python:3.10-slim，非 root，最小镜像
├── docker-compose.yml      # 一键启动（日志只读挂载）
├── scripts\backup.ps1      # 把目标文件复制到 .backup\<时间戳>-<文件名>，不改原文件
└── tests\                  # pytest
```

## 快速开始

```powershell
cd E:\moray-aio
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

# 跑测试
pytest -q

# 命令行直接体验（无需起服务）
python cli.py run --trigger recent_errors --auto-approve
python cli.py health

# 拉取本地模型（Ollama 不可用时服务自动走启发式兜底，不阻塞启动）
ollama pull qwen2.5:7b

# 起服务
python -m uvicorn app.main:app --host 127.0.0.1 --port 8020
# 或 python cli.py serve
```

配置全部可用环境变量覆盖（见 `.env.example`）。默认目标日志是
`D:\MoRay\logs\moray.err.log`；如果该文件不存在，服务不会崩：
`/health` 如实返回 `readable=false`，`/logs/recent-errors` 返回 404，
`POST /episodes` 返回 `phase=failed` 并记录 error。
本地开发想用临时日志时，覆盖两个变量即可：

```powershell
$env:MORAY_AIO_READONLY_ROOTS = "C:\temp\demo-logs"
$env:MORAY_AIO_LOG_PATH       = "C:\temp\demo-logs\moray.err.log"
```

## 接口列表

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查；`log` 字段为目标日志的只读探测（exists/readable/size_bytes），`ollama` 为模型可用性 |
| GET | `/metrics` | 全局统计：episode 数/成功率/Token 消耗/平均耗时/任务分布 |
| GET | `/logs/recent-errors?limit=10` | 最近 N 条 ERROR（时间正序）；文件不存在 404，白名单外 403 |
| POST | `/episodes?trigger=recent_errors` | **异步**启动迭代：202 + `{episode_id, phase:"created"}`，后台执行，轮询下一条接口 |
| POST | `/episodes/sync` | 同步启动（调试用）：阻塞到终态/审批点，返回完整 episode |
| GET | `/episodes?limit=20` | 最近的 episode 列表 |
| GET | `/episodes/{id}` | 单个 episode 详情（异步轮询用） |
| POST | `/episodes/{id}/tasks/{task_id}/approve` | 批准待审批任务；非只读工具 403 |
| POST | `/episodes/{id}/tasks/{task_id}/reject` | 否决待审批任务 |
| POST | `/episodes/{id}/tasks/approve-all` | 批量批准：仅 proposed 且只读白名单内的任务 |
| POST | `/episodes/{id}/tasks/reject-all` | 批量拒绝：所有 proposed 任务 |
| POST | `/episodes/{id}/resume` | 恢复迭代：无待审批任务时执行 approved 任务并继续循环 |
| GET | `/memory/reflections?limit=5` | 最近反思/教训记录（长效记忆，只读） |
| GET | `/memory/similar?keywords=a,b,c` | 按关键词检索历史相似 episode（Jaccard，只读调试） |

交互文档：服务启动后访问 `/docs`（Swagger UI）。

**API 鉴权**：设置 `MORAY_AIO_API_KEY` 后，除 `GET /health` 外所有请求必须携带
`X-API-Key` 头（401 否则）；留空则鉴权关闭（本地调试）。客户端侧
`scripts/moray_client.py` / `scripts/demo.py` / `cli.py` 会自动从环境变量带上该头。

## 一键演示

服务运行期间，另开一个终端：

```powershell
python scripts/demo.py
# 或指定服务地址：python scripts/demo.py http://127.0.0.1:8020
# 或 set MORAY_AIO_DEMO_URL=http://127.0.0.1:8020
```

脚本（仅标准库 urllib）走**异步模式**：POST /episodes 拿到 202 + episode_id →
每 2 秒轮询 GET /episodes/{id}（最多 60 次）→ phase 变成 awaiting_approval 时
自动 approve-all（服务端只批只读白名单内的任务）+ resume → 直到 concluded/failed，
最后打印结论与长效记忆（反思记录），一键观看 Agent 自驱分析全过程。

**探针演示**（`set MORAY_AIO_DEMO_MODE=probe`）：观察 Agent 是否对
「Connection refused 127.0.0.1:6379」自动派发 port_check / process_list 探针并打印结果。
probe 模式需要当前工作目录存在 `moray-demo-fixture.log`（含模拟错误日志）；
出于安全策略脚本不写文件，缺失时会打印内容与创建指引。

想在本地临时日志上体验（不动 D:\MoRay）：

```powershell
$env:MORAY_AIO_READONLY_ROOTS        = "C:\temp\demo-logs"
$env:MORAY_AIO_LOG_DIR               = "C:\temp\demo-logs"          # 滚动聚合模式
$env:MORAY_AIO_LOG_GLOB_PATTERN      = "moray.err.log*"
$env:MORAY_AIO_AUTO_APPROVE_READONLY = "false"   # 让 demo 展示审批环节
python -m uvicorn app.main:app --host 127.0.0.1 --port 8020
python scripts\demo.py
```

## v0.3.0：为接入 MoRay 做的三步升级

**1. 异步 episode 执行**。`POST /episodes` 立即返回 `202 {"episode_id", "phase": "created"}`，
内核丢进线程池后台执行（`MORAY_AIO_ASYNC_WORKERS` 控制并发），HTTP 不再被 LLM 阻塞；
主项目用 `GET /episodes/{id}` 轮询状态即可。`POST /episodes/sync` 保留同步行为供调试。
状态机新增 `queued`（created → queued → observing，与同步路径 created → observing 兼容）。
SQLite 长效记忆加了连接级锁，HTTP 线程与后台线程并发安全。

**2. 日志滚动聚合**。真实日志会滚动（`moray.err.log` / `.1` / 带日期后缀），单文件读取会漏数据。
配置 `MORAY_AIO_LOG_DIR` + `MORAY_AIO_LOG_GLOB_PATTERN`（默认 `moray.err.log*`）后，
`_observe` 按 mtime 新→旧聚合最多 5 个文件、合并为时间正序；未配置则回退单文件模式。
工具层新增 `log_rolling_tail`（dir/pattern/n/max_files），同样受只读白名单与审批闸门约束。

**3. 错误聚类**。`app/tools/clusterer.py` 把时间戳/IP/端口/UUID/十六进制串/数字归一化为
`<TS>/<IP>/<PORT>/<UUID>/<HEX>/<NUM>` 占位符，同类错误折叠为一类（模板+次数+样本），
只把 top N 类喂给 LLM——同类错误重复 1000 行不再浪费 token。聚类数上限
`MORAY_AIO_CLUSTER_TOP_N`（默认 10）。

## v0.4.0：只读系统探针 + 历史经验复用

**1. 只读系统探针**（`app/tools/probes.py`，4 个工具全部只读、绝不修改系统状态）：
- `port_check(host, port)`：TCP 端口可连性 + 延迟；host 必须命中探针白名单（默认仅环回），
  超时/失败返回 `open=false` 不抛异常；
- `process_list(name_pattern)`：进程列表（pid/name/内存等，上限 50 条）；
  只读枚举，绝不 terminate/发信号；
- `disk_usage(path)`：磁盘占用；path 受只读白名单约束；
- `windows_service_status(service_name)`：服务状态查询（只读，绝不启停）；
  service_name 必须匹配 `^[A-Za-z0-9_\-\s]{1,64}$`（防命令注入），subprocess 一律
  list 形式 + shell=False，raw_output 截断 4KB；psutil 不可用时优雅降级。

**2. planner 智能选工具**：错误含 `Connection refused 127.0.0.1:PORT` 时自动派
port_check + process_list（端口→服务名推测表：6379→redis、5432→postgres 等）；
含 disk/No space/quota 派 disk_usage；含 service/daemon 派 windows_service_status。
非本机地址、非法 service_name、非法正则的候选一律丢弃（fail-closed）。

**3. 历史相似 episode 检索**：memory 新增 episodes_index（关键词+结论），保存 episode 时
自动提取关键词；`find_similar` 用 Jaccard 相似度找 top-k 历史案例（不依赖 embedding），
分析时作为「历史类似案例」注入 LLM 上下文；调试可查 `GET /memory/similar`。

探针执行结果以 `kind=probe` 记入 observations（进程列表注入 LLM 前会摘要为
count + 前 3 个名字，防止撑爆上下文）。

## Docker 一键启动

```powershell
docker compose up --build
# 服务: http://127.0.0.1:8020 ；日志目录 ./logs 只读挂载到容器 /logs
```

镜像基于 python:3.10-slim、非 root 用户运行；compose 只把端口绑到本机回环。

## v0.5.0：可观测性 + 插件化 + CLI + 容器化

**1. Trace & Metrics**。Episode 新增 `trace` 字段：observe/analyze/propose/execute 四个
核心阶段逐一打点（阶段、迭代号、耗时 duration_ms、时间戳、LLM token 用量）。
Ollama 返回的 `prompt_eval_count`/`eval_count` 会被统计进对应阶段（线程隔离，避免并发串扰）。
`GET /episodes/{id}` 返回完整 trace；`GET /metrics` 给出全局统计（episode 数、成功率、
总 token、平均耗时、任务状态分布）。

**2. 插件化工具架构**。`app/tools/plugin_base.py` 定义 `BaseTool`（name/description/
risk_level/params_schema + `invoke(params)` 单字典入口）；8 个内置工具全部重构为
`app/tools/builtin/` 下的 BaseTool 实现；`ToolRegistry.register_tool()` 支持动态注册，
`MORAY_AIO_PLUGIN_DIRS`（分号分隔目录）启动时自动扫描加载 `*.py` 中的 BaseTool 子类，
坏插件只记录到 `plugin_errors`、绝不拖垮进程。示例见 `examples/dummy_echo_plugin.py`。

**3. CLI**。`python cli.py serve|run|health`：run 不经 HTTP 直接跑完整分析并在终端
彩色输出 trace/任务/结论（纯 ANSI，NO_COLOR=1 可关）；health 检查服务状态。

**4. Docker**。Dockerfile（python:3.10-slim、非 root、依赖层缓存）+ docker-compose.yml
（日志目录只读挂载、端口只绑 127.0.0.1）。

## v0.6.0：生产级基础设施

**1. Repository 持久化抽象**。`app/kernel/repository.py` 定义 `EpisodeRepository` 接口
（save/load/list_recent/metrics/record_reflection/find_similar...）；原 memory.py 迁移为
`sqlite_repository.py`（默认单机模式），新增 `postgres_repository.py`（配置
`MORAY_AIO_DB_DSN` 启用，依赖 `requirements-optional.txt`）。关键词提取/相似度/指标聚合
下沉为共享纯函数，两实现复用；所有 SQL 参数绑定。

**2. Token 预算熔断**。`MORAY_AIO_MAX_TOKENS_PER_EPISODE`（默认 50000）限制单 episode
LLM 用量；analyze/propose 每次调用后累计，超限立即 `FAILED`（error=
`Token budget exceeded: {used} / {max}`），trace 记录 `circuit_break` 事件，同步/异步同受保护。

**3. API Key 鉴权**。`MORAY_AIO_API_KEY` 非空时中间件拦截所有请求（`GET /health` 除外），
`X-API-Key` 不匹配返回 401（常量时间比较）；密钥只从环境变量读取。

**4. 网络重试**。Ollama 调用带指数退避（1s/2s/4s，最多 3 次）：网络超时/连接错误/5xx 重试，
4xx 不重试直接抛出；重试次数进 trace（`llm.retries`）与日志。

## 版本

- **v0.2.0**（tag）：MVP——Ollama 接入、自我迭代内核、批量审批、demo CLI。
- **v0.3.0**（tag）：异步 episodes、日志滚动聚合、错误聚类，面向 MoRay 主项目 HTTP 接入就绪。
- **v0.4.0**（tag）：只读系统探针、planner 智能派发、历史相似案例检索。
- **v0.5.0**（tag）：trace/metrics 可观测性、BaseTool 插件化、CLI、Docker。
- **v0.6.0**（tag）：Repository 抽象（SQLite/Postgres）、Token 熔断、API 鉴权、网络重试。

## Episode 生命周期

`created → observing → analyzing → proposing → (awaiting_approval ⇄) executing → analyzing → … → concluded`
任何阶段异常进入 `failed`（终态）。任务状态：`proposed`（等人工）→ `approved/rejected` →
`done/failed/empty`（empty = 执行成功但匹配为空，记 `No matches found`，会把无效 pattern
反馈给下一轮 LLM 规划避开）。
只读任务在 `MORAY_AIO_AUTO_APPROVE_READONLY=true` 时自动批准；启发式规则会针对
连接被拒 / 超时 / 文件缺失自动提出 `log_grep` 侦查任务，并按 (tool, params) 去重。

## 与 MoRay 的关系

moray-aio 是独立进程、独立仓库、独立数据（`data/memory.sqlite3`）。
MoRay 将来只需通过 HTTP 调用本服务即可接入分析能力；本仓库永不反向依赖 MoRay 代码。
