# moray-aio · WorkBuddy 连接器接入包

将 [moray-aio](https://github.com/aliangmo2026/moray-aio)（只读 AIOps 探针底座）接入腾讯 WorkBuddy 的 MCP 连接器体系。

## 目录结构

```
workbuddy-connector/
├── connector-meta.json   # 连接器元信息（名称、简介、作者、版本、安全声明）
├── mcp.json              # MCP Server 启动配置（stdio：command + args + env）
├── icon.svg              # 连接器图标（发光盾牌包裹终端提示符）
├── SKILL.md              # 安装后可用的 9 个工具的自然语言使用说明
└── README.md             # 本文件：打包与上传流程
```

## 工作原理

WorkBuddy 自定义连接器基于标准 MCP 协议。平台读取 `mcp.json`，在用户机器上以 stdio 方式拉起 `moray-mcp` 命令（由 PyPI 包 `moray-aio` 提供），随后双方通过换行分隔的 JSON-RPC 2.0 通信（协议版本 `2025-03-26`）。

```
WorkBuddy 客户端 ──stdio JSON-RPC──> moray-mcp ──> 只读探针工具（日志/端口/进程/磁盘/沙箱）
```

## 前置条件

1. Python ≥ 3.8
2. `pip install "moray-aio>=3.0.0"`，并确认 `moray-mcp` 在 PATH 中：
   ```bash
   moray-mcp --help  # 或直接 echo，服务将进入 stdio 等待；Ctrl+C 退出
   ```
   若命令不存在，检查 pip 的 scripts 目录是否在 PATH 中。

## 打包上传流程

### 方式一：对话式安装（推荐先用这个验证）

WorkBuddy 支持在对话中管理连接器。把 `mcp.json` 的内容发给 Buddy，例如：

> 帮我添加一个自定义 MCP 连接器，配置如下：`{"mcpServers": {"moray-aio": {"type": "stdio", "command": "moray-mcp", "args": [], ...}}}`

### 方式二：设置界面手动添加

1. 打开 WorkBuddy → 右上角侧边栏 → **设置** → **连接器 / MCP** 页签。
2. 点击 **添加 MCP**，粘贴 `mcp.json` 中 `mcpServers` 对象的内容并保存。
3. 状态灯变绿即接入成功；变红则查看客户端日志排查（常见原因：`moray-mcp` 不在 PATH、Python 环境不对）。

### 方式三：作为上架连接器市场（如有审核通道）

1. 确认 `connector-meta.json` 中 `version` 与 PyPI 上 `moray-aio` 版本一致（当前 `3.0.0`）。
2. 将整个 `workbuddy-connector/` 目录打成 zip（保持相对路径不变）。
3. 通过 WorkBuddy 开放平台的连接器提交入口上传 zip 包，等待平台拉取 `connector-meta.json` 校验。
4. 审核通过后，用户可在连接器市场搜索 "moray-aio" 一键安装。

> 注：WorkBuddy 官方文档对自定义连接器的描述是"配置方式与 MCP 接入一致"，未公开独立的市场包 schema；本包的 `connector-meta.json` 采用通用惯例字段，若平台后续公布正式 schema，按官方字段名微调即可。

## 安全配置（`mcp.json` → `env`）

平台支持传递环境变量，以下为暴露的关键配置（全部有安全默认值，fail-closed）：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MORAY_AIO_READONLY_ROOTS` | `D:\MoRay\logs` | **路径白名单**：分号分隔；所有文件/目录读取必须落在此根内，否则直接拒绝。部署时改成用户实际日志目录 |
| `MORAY_AIO_PROBE_HOST_WHITELIST` | `127.0.0.1;localhost;::1` | 端口探测白名单，默认仅回环，防 SSRF |
| `MORAY_AIO_SANDBOX_ENABLED` | `true` | Python 沙箱总开关，建议保持 true |
| `MORAY_AIO_SANDBOX_TIMEOUT_SECONDS` | `3.0` | 沙箱执行硬超时 |
| `MORAY_AIO_MAX_SCAN_BYTES` | `5242880` | 单次日志扫描上限（5 MB） |
| `MORAY_AIO_RATE_LIMIT_PER_MINUTE` | `5` | 每分钟工具调用限流，0 为关闭 |
| `MORAY_AIO_RECENT_ERROR_LIMIT` | `10` | "最近 N 条 ERROR" 的 N |
| `MORAY_AIO_LOG_LEVEL` | `INFO` | 引擎自身日志级别 |

**部署建议**：把 `MORAY_AIO_READONLY_ROOTS` 改为目标机器的真实日志目录，多个目录用 `;` 分隔；除非确有必要，不要放开 `MORAY_AIO_PROBE_HOST_WHITELIST`。

## 安装后的体验

参见 [SKILL.md](./SKILL.md)。用户可直接对 Buddy 说：

- "帮我查一下本地 6379 端口通不通"
- "最近 10 条 ERROR 日志是什么"
- "现在内存占用最高的 5 个进程是什么"

## 版本同步

| 组件 | 版本 |
|---|---|
| connector-meta.json | 3.0.0 |
| PyPI 包 `moray-aio` | 3.0.0 |
| MCP 协议版本 | 2025-03-26 |

发布新版本时：先发 PyPI，再同步更新 `connector-meta.json` 的 `version` 和 `requirements.install`。

## License

Apache-2.0 with Commons Clause — © Mo Guangliang (莫光良)
