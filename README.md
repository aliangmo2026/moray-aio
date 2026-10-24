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
│   │   ├── state.py        # Phase 枚举 + Episode + 合法转移表
│   │   ├── memory.py       # SQLite 长效记忆（参数绑定）
│   │   ├── approval.py     # 审批闸门（classify/decide）
│   │   ├── planner.py      # 自驱任务生成（LLM 优先 / 启发式兜底）
│   │   └── loop.py         # 主循环 Observe→Analyze→Propose→(审批)→Execute
│   ├── tools\              # 只读工具集
│   │   ├── log_reader.py   # tail/recent_errors/grep/check_readable（"rb" 只读）
│   │   └── registry.py     # ToolRegistry：白名单校验 + 分发
│   └── llm\
│       ├── prompts.py      # ANALYZE_*/PLAN_*（只读、白名单、严格 JSON）
│       └── ollama_client.py# /api/generate，format=json，不可用自动兜底
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

# 拉取本地模型（Ollama 不可用时服务自动走启发式兜底，不阻塞启动）
ollama pull qwen2.5:7b

# 起服务
python -m uvicorn app.main:app --host 127.0.0.1 --port 8020
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
| GET | `/health` | 健康检查；`log` 字段为目标日志的只读探测（exists/readable/size_bytes） |
| GET | `/logs/recent-errors?limit=10` | 最近 N 条 ERROR（时间正序）；文件不存在 404，白名单外 403 |
| POST | `/episodes?trigger=recent_errors` | 启动一次自我迭代，返回 episode（含 phase/tasks） |
| GET | `/episodes?limit=20` | 最近的 episode 列表 |
| GET | `/episodes/{id}` | 单个 episode 详情 |
| POST | `/episodes/{id}/tasks/{task_id}/approve` | 批准待审批任务；非只读工具 403 |
| POST | `/episodes/{id}/tasks/{task_id}/reject` | 否决待审批任务 |
| POST | `/episodes/{id}/tasks/approve-all` | 批量批准：仅 proposed 且只读白名单内的任务 |
| POST | `/episodes/{id}/tasks/reject-all` | 批量拒绝：所有 proposed 任务 |
| POST | `/episodes/{id}/resume` | 恢复迭代：无待审批任务时执行 approved 任务并继续循环 |
| GET | `/memory/reflections?limit=5` | 最近反思/教训记录（长效记忆，只读） |

交互文档：服务启动后访问 `/docs`（Swagger UI）。

## 一键演示

服务运行期间，另开一个终端：

```powershell
python scripts/demo.py
# 或指定服务地址：python scripts/demo.py http://127.0.0.1:8020
# 或 set MORAY_AIO_DEMO_URL=http://127.0.0.1:8020
```

脚本（仅标准库 urllib）会自动：POST /episodes → 打印 phase 与待审批任务 →
approve-all（服务端只批只读白名单内的任务）→ resume → 循环直到
`concluded/failed`，最后打印结论与长效记忆（反思记录），方便一键观看
Agent 自驱分析的全过程。

想在本地临时日志上体验（不动 D:\MoRay）：

```powershell
$env:MORAY_AIO_READONLY_ROOTS        = "C:\temp\demo-logs"
$env:MORAY_AIO_LOG_PATH              = "C:\temp\demo-logs\moray.err.log"
$env:MORAY_AIO_AUTO_APPROVE_READONLY = "false"   # 让 demo 展示审批环节
python -m uvicorn app.main:app --host 127.0.0.1 --port 8020
python scripts\demo.py
```

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
