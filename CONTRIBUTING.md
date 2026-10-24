# Contributing to moray-aio

感谢关注 moray-aio！本项目的核心价值观是 **Safety by Design**，所有贡献必须延续这一底线。

## 开发环境

```powershell
git clone <repo> && cd moray-aio
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows；Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                            # 必须全绿再提交
```

Python 3.8+ / 3.10 为 CI 验证版本。

## 贡献流程

1. Fork 并从 `main` 切出特性分支（`feat/xxx`）。
2. 写代码 + 补测试（新增行为必须有 pytest 覆盖）。
3. `pytest -q` 全绿后提交 PR，说明动机、方案与测试证据。
4. CI（3.8/3.10 矩阵 + Docker build）通过后由维护者评审。

## 硬性安全约定（违反会被直接拒绝）

1. **只读日志**：日志文件只允许 `open(path, "rb")` 读取；绝不写、截断、重命名、删除。
2. **路径白名单**：任何工具的 path/dir 输入必须经 `Path.resolve() + relative_to` 白名单校验。
3. **绝不执行危险命令**：kill/reboot/restart/shutdown/rm/del/format 只能作为建议文本。
4. **凭据纪律**：密钥只从环境变量/密钥服务读取，源码、示例、测试不得出现凭据字面量。
5. **出站请求纪律**：URL 仅 http/https；请求前校验 host，拒绝环回/私有/保留地址；
   Webhook 类目标必须命中运维配置的域名白名单。
6. **SQL 一律参数绑定**，禁止字符串拼接。

## 代码风格

- 标准库优先；新增第三方依赖需在 PR 中说明理由。
- 保持 Python 3.8 兼容（不用 3.9+ 专属语法）。
- 插件/工具实现 `BaseTool` 时使用 `invoke(params: dict)` 单字典入口。

## 测试

- `tests/` 下 pytest；用 `tmp_path`/`monkeypatch`，不依赖外部网络与真实凭据。
- 时间相关逻辑请 mock 时间源（参考 `tests/test_v07.py`）。
- 压测脚本改动后请更新 `docs/benchmark.md`。

## 行为准则

保持友善与专业；一切讨论以代码和数据说话。
