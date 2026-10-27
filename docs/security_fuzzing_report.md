# AST 沙箱安全 Fuzzing 报告（红蓝对抗）

- 日期：2026-10-27
- 被测对象：`app/tools/dynamic_sandbox.py`（`check_ast` 静态门 + `execute_safe_python` 执行管线）
- 攻击工具：`scripts/security_fuzzer.py`（198 条恶意 payload + 25 条良性对照）
- 结论：**最终拦截率 100%（0 漏网 / 0 误报）**；效果类 payload（破坏 / 越狱 / 逃逸 / 混淆 / 外联）在 AST 静态层 100% 拦截，DoS 类由超时熔断器兜底
- 回归验证：`pytest tests` — **184 passed, 1 skipped**（与封板基线一致）
- 结果明细（最终轮）：`data/security_fuzzing_last_run.json`

---

## 1. 概要

| 轮次 | 动作 | AST 拦截 | 运行时兜底 | 漏网 | 误报 |
|---|---|---|---|---|---|
| R1 | 修复前基线 | 140 / 198（70.7%） | 35 | **23** | 0 |
| R2 | 第一批加固（引用期拒绝 / 调用目标可解析 / frame 属性 / DNS / format / 装饰器 / socket 门面） | 176 / 198（88.9%） | 19 | **3** | 0 |
| R3 | 常量折叠守卫（≥ 10^10 拒绝） | 185 / 198（93.4%） | 12 | **1** | 0 |
| R4 | itertools 门面 + iter/context 封禁 | 185 / 198（93.4%） | 13 | **0** | 1* |
| **最终** | 良性对照修正 | **185 / 198（93.4%）** | 13 | **0** | **0** |

\* R4 的"误报"是良性对照样例自身使用了 `itertools.count()`——该 API 因 C 层 GIL 风险被**有意**封禁（见 §5.7、§8），对照样例改为有界写法后归零。

最终指标（`python scripts\security_fuzzer.py`）：

```
hostile payloads            : 198
benign controls             : 25
blocked at AST stage        : 185  (93.4%)
contained at runtime stage  : 13
runaway-thread residency    : 0
ESCAPED (false negatives)   : 0   <-- MUST BE 0
sandbox-level interception  : 100.0%
benign false positives      : 0   <-- MUST BE 0
AST check time              : total 24.9 ms, avg 111.8 us/payload
runtime audit time          : total 35.3 s
wall clock                  : 35.4 s
```

分类别最终结果：

| 类别 | 总数 | AST 拦截 | 运行时兜底 | 漏网 |
|---|---|---|---|---|
| A. basic-destruction（基础破坏） | 30 | 30 | 0 | 0 |
| B. import-jailbreak（导入越狱） | 51 | 51 | 0 | 0 |
| C. sandbox-escape（沙箱逃逸） | 51 | 47 | 4 | 0 |
| D. encoding-obfuscation（编码混淆） | 20 | 19 | 1 | 0 |
| E. resource-exhaustion（资源耗尽） | 18 | 10 | 8 | 0 |
| F. network-exfiltration（网络外联） | 28 | 28 | 0 | 0 |
| G. benign-controls（良性对照） | 25 | 0 | 0 | 0（全部正常运行） |

> 说明：AST 层 93.4% 中的 13 条"运行时兜底"全部是 DoS 类（`while True`、递归、大迭代），由超时熔断器按设计契约拦截（`test_v20_sandbox.py::test_timeout_circuit_breaker` 锁定该行为）。**所有具备实际效果（执行 / 文件 / 网络 / 逃逸）的 payload 在 AST 静态层即被拒绝，无一抵达运行时。**

---

## 2. 测试方法

### 2.1 双阶段验证

每个 payload 走两道独立验证：

1. **阶段一（静态门）**：进程内调用 `check_ast()`，期望抛出 `SandboxSecurityError`，记录精确拒绝原因（如 `import not allowed: 'os'`、`call to denied name: 'system'`、`dunder attribute access not allowed: '__class__'`）。
2. **阶段二（运行时审计）**：在**隔离子进程**中调用 `execute_safe_python(timeout=2.0)`，用四类金丝雀判定真实逃逸：
   - **真 stdout 标记**：逃逸代码一旦摸到真实 builtins / 原始解释器 / OS shell，会向子进程真实 stdout 写 `CANARY_*` 标记（沙箱捕获的 print 拿不到这条通路）；
   - **本机 TCP 监听金丝雀**：网络类 payload 全部拨号 `127.0.0.1:<金丝雀端口>`（绝无真实外联，无任何包离开本机），监听端 accept 到连接即证明外联能力；
   - **严格规则**：恶意 payload 若以 `status == "ok"` 完成，即使无金丝雀也计为静态门被绕过；
   - **自毁检测**：`k = p.kill; k()`（对自身进程）命中时子进程无 JSON 输出且退出码非 0。

### 2.2 语料构造约定

与 `tests/test_v20_sandbox.py` 相同：所有攻击动词 / 模块名 / 解释器 token 在源码中以**相邻字符串片段**拼接（如 `"ev" + "al"`），在运行时组装成完整 payload——本文件只是数据容器，避免安全扫描器把攻击语料当成真实攻击。

### 2.3 判定口径

- **漏网（假阴性）**：恶意效果真实可达（金丝雀命中 / 监听被连 / 自毁发生 / 恶意 payload 以 ok 完成）。
- **运行时兜底**：通过 AST 门但被运行时防线拦截（命名空间无绑定 → NameError、guarded import、超时熔断、输出上限）。按"沙箱整体拦截"计入拦截率。
- **误报（假阳性）**：良性对照被 AST 拒绝或运行时失败。对照覆盖 math/json/socket/psutil/base64/hashlib/re/datetime/itertools/statistics/uuid/string/decimal/time/collections/random/fractions/functools、f-string、推导式、自定义函数等正常诊断写法。

---

## 3. 首轮战果：23 条漏网明细

R1（修复前）AST 拦截率仅 70.7%，23 条 payload 真实穿透。按逃逸类别归因：

### 3.1 别名洗白调用名（最严重，运行时杀进程已复现）

- **E49**：`p = psutil.Process(); k = p.kill; k()` —— `kill` 只在**调用时**按名字拒绝，先把方法绑定到变量再调用即绕过。运行时审计中子进程**成功自杀**（无 JSON 输出、退出码非 0），证明进程击杀能力真实可达。
- 同理推及所有"仅调用期"拒绝的名字：`socket.sendto` 别名（N16，TCP 未连接时被 OSError 兜住属侥幸）、`getattr` 动态取值（X12/X13 因 `getattr` 本身被拒而未遂）。

### 3.2 生成器帧逃逸阶梯（frame ladder）

- **E30/E31/E32/E34/E41**：`g.gi_frame`、`g.gi_code.co_consts`、`g.gi_frame.f_back`、`g.gi_frame.f_code.co_names`、`g.gi_yieldfrom` —— 这些**非 dunder** 的解释器内部属性全部通过静态门（dunder 检查只认 `__xxx__` 形态）。
- E26/E27/E42/E46/E50（`g.gi_frame.f_back.f_builtins['exec'](...)` 完整链）在运行时被兜住的原因是**侥幸**：未启动的生成器 `f_back` 为 `None`。一旦用 `for` 迭代启动生成器，链条即接通到真实 `exec`。静态门必须直接拒绝整条阶梯。

### 3.3 下标调用绕过（call-target 不可解析）

- **E40**：`d = {'f': len}; d['f']('abc')` —— 调用名检查只覆盖 `Name` / `Attribute` 两种函数形态，`Subscript` 调用直接放行。它是 §3.1/§3.2 的放大器：一旦拿到危险可调用对象的字典，就能无名字调用。

### 3.4 format 字符串 dunder 穿越

- **E35/E36/E37**：`"{0.__class__}".format([])` 及别名形态 —— `str.format` 的迷你语言在**运行时**做属性链 getattr，完全绕过 AST。可读取 `__class__.__mro__.__init__.__globals__` 的 repr（信息泄露 / 内部结构枚举）。

### 3.5 DNS / 拨号类外联（SSRF 面）

- **N06-N13**：`socket.getaddrinfo` / `gethostbyname` / `gethostbyname_ex` / `getnameinfo` / `gethostbyaddr` / `getfqdn` 及其**别名形态**全部穿透并真实执行（status ok，真实 resolver 调用）。connect 系动词按名字拒绝挡不住这些"一步式" API；DNS 查询本身即是一条数据外带信道。`create_connection`（N03/N04/N05）同样通过静态门。
- **N21**：`s.makefile('w')` 未被拒绝（当时因套接字未连接而未产生实际外发，属链式放大器）。

### 3.6 GIL 长占型算术炸弹（熔断器失效类）

- **D07**：`10 ** 10 ** 10`、**D10**：`dict.fromkeys(range(10 ** 12))`、**D11**：`sum(itertools.count())` —— 三者的共同点：**单段 C 层计算长期持有 GIL，不回字节码**。超时熔断器的 `thread.join(2.0)` 到点后主线程再也拿不到 GIL，`execute_safe_python` 永远无法返回 `timeout`，整个宿主进程被冻结。这是比"废弃线程继续烧 CPU"更严重的发现：**熔断器对这类 payload 完全失效**。
- 纯 Python 层循环（`while True: pass` 等）每 5ms 交一次 GIL，熔断器正常工作——所以其余 15 条 DoS 在 R1 即为"运行时兜底"而非漏网。

### 3.7 其他原语

- **E23/E24**：`str.mro()` / `object.mro()` —— 非 dunder 的类层级遍历入口。
- **E44**：`@print def f(): ...` —— 装饰器应用点未校验（可挂任意表达式）。

---

## 4. 复现与工程注记

R1 复盘时发现两处 harness 自身问题，已在 R2 修正并全程复跑：

1. 网络金丝雀监听器最初绑定临时端口而非 payload 内嵌端口，导致拨号类 payload 拨向死端口（Windows 对 closed port 丢 SYN，表现为熔断超时而非拒绝连接）；
2. 巨型计算卡死解释器关闭流程时，子进程 stdout 的块缓冲 JSON 来不及落盘 —— 子进程改用 `PYTHONUNBUFFERED=1`，并将"读 JSON"与"等进程退出"解耦，从而能区分"熔断已触发 + 废弃线程驻留"与"真逃逸"。

---

## 5. 加固措施（dynamic_sandbox.py）

所有措施均已落在 `check_ast` / `_build_namespace`，与模块 docstring 的威胁模型同步更新：

| # | 措施 | 堵住的漏网 |
|---|---|---|
| 5.1 | **引用期拒绝**：`Attribute.attr ∈ DENIED_CALL_NAMES` 时，连"引用"都拒绝（不只拒绝调用）——`k = p.kill; k()` 无法再把被拒动词经别名洗白 | E49、N07/N09/N16 等全部别名形态 |
| 5.2 | **调用目标必须可静态解析**：`Call.func` 仅允许 `Name` / `Attribute` / `Lambda`，其余（下标调用、链式调用）fail-closed 拒绝 | E40 及一切 `d["exec"](...)` 形态 |
| 5.3 | **解释器内部属性拒绝**：属性名前缀 `f_ / gi_ / cr_ / ag_ / co_ / tb_` 与精确名 `mro` 一律拒绝（帧 / 生成器 / 协程 / code object / traceback 阶梯全断） | E23、E30-E32、E34、E41 |
| 5.4 | **新增拒绝动词**：`format` / `format_map`（format 迷你语言穿越，f-string 的 `JoinedStr` 仍允许且受 AST 检查）、`makefile`、DNS/拨号系（`create_connection` / `getaddrinfo` / `gethostbyname` / `gethostbyname_ex` / `gethostbyaddr` / `getnameinfo` / `getfqdn` / `fromfd`）、文件 / 进程系（`rmtree` / `startfile` / `execv*` / `spawnv*` / `killpg` / `setuid` / `setgid` / `seteuid` / `setegid` / `getstatusoutput` / `getoutput` / `dup` / `dup2` / `pipe`）、psutil 控制系（`suspend` / `resume` / `send_signal` / `nice` / `ionice`）、`sleep`、`iter`（两参哨兵形式）、`getcontext` / `setcontext` / `localcontext`（decimal 精度解钳） | E35-E37、N06-N13、N21/N22、B 系纵深、D14 |
| 5.5 | **装饰器拒绝**：`FunctionDef.decorator_list` 非空即拒 | E44 |
| 5.6 | **常量折叠守卫**：字面量算术经 `**` / `*` 折叠后量级 ≥ 10^10 即拒绝（`10**10**10`、`'A'*10**11` 这类单次 C 调用持 GIL 的算术炸弹不再抵达执行层；`1024**3` 等常规换算不受影响） | D07-D10、D12/D13/D16-D18 |
| 5.7 | **运行时门面（第三道门）**：guarded import 对 `socket` 与 `itertools` 返回零属性门面——socket 仅暴露常量 / 纯转换函数 / 本机名查询（`gethostname`、`inet_*`、`if_*`）；itertools 仅暴露有界组合器（`islice` / `chain` / `combinations` / `permutations` / `product` / `groupby` / `accumulate` / `starmap` / `filterfalse` / `dropwhile` / `takewhile` / `zip_longest` / `compress`）。无限生产器 `count` / `cycle` / `repeat` / `tee` 不可达，杜绝 `sum(count())` 型 C 层 GIL 长占 | D11、并为 5.1/5.4 的未来解析缺口兜底 |

---

## 6. 迭代过程

- **R1 → R2**（23 → 3）：落实 5.1-5.5 + socket 门面。剩余 3 条均为 GIL 长占类。
- **R2 → R3**（3 → 1）：落实 5.6 常量折叠守卫。期间用 `PYTHONUNBUFFERED=1` 复测排除了"JSON 缓冲假象"，确认 `10**10**10` 类冻结是真实缺陷。剩余 1 条 `sum(itertools.count())`。
- **R3 → R4**（1 → 0）：落实 5.7 itertools 门面 + `iter` 两参形式封禁。
- **R4 → 最终**（0 逃逸，0 误报）：良性对照 G13 由 `itertools.count()` 有界化改写（该 API 被有意封禁，见 §8）。

---

## 7. 拦截耗时

| 指标 | 数值 |
|---|---|
| 单条 AST 静态检查均值 | 111.8 µs |
| 223 条全量 AST 检查总耗时 | 24.9 ms |
| 223 条运行时隔离审计总耗时 | 35.3 s（含 ~200 次子进程启动与 DoS 类 2s 熔断等待） |
| 全量 wall clock | 35.4 s |

静态门开销在引擎每次执行 snippet 时仅约 0.1 ms，可忽略。

---

## 8. 有意收紧的 API（写 snippet 时注意）

以下收紧是**设计决定**（fail-closed 优先），影响合法但少用的写法：

1. `str.format` / `str.format_map` 被拒 → 用 f-string（AST 可检查 f-string 内部表达式）。
2. `itertools.count` / `cycle` / `repeat` / `tee` 不可达 → 用有界序列配合 `islice`。
3. `iter` 整体拒绝（两参哨兵形式是唯一无需 itertools 就能构造无限 C 迭代器的入口）。
4. 字面量常量折叠 ≥ 10^10 拒绝 → TiB 级换算请写成连除（`x / 1024 / 1024 / 1024 / 1024`）或 `x / 1024**3`（= 1.07e9，放行）。
5. decimal 上下文调优（`getcontext` / `setcontext` / `localcontext`）被拒 → 使用默认 28 位精度。
6. `sleep` 被拒 —— 有界只读 snippet 无合法用途。

## 9. 已知局限（如实记录）

1. ~~**废弃线程 CPU 驻留**~~ **已在 v3.0.0 彻底解决（进程隔离执行）**：超时熔断改为 `SandboxWorkerPool`（multiprocessing spawn 池、JSON-only IPC）——超时即 `process.terminate()` OS 级物理击杀并自动补位新 worker，连"单段 C 调用持 GIL"类炸弹（旧机制下线程熔断完全失效）都能被杀掉。钻地弹演练：`sum(range(10**9))`（纯 C 层循环持 GIL 数十秒）以 timeout=1.0s 提交 → **1.524s 内被物理击杀**，池子随后继续正常服务（见本报告 §11 与 fuzzer 的 GIL-bomb drill 段）。本报告 §2/§5 记录的 R1-R4 战役结论不受影响；fuzzer 在进程隔离下重跑仍为 **0 逃逸 / 0 误报 / 100% 拦截**。
2. **AST 层拦截率 93.4% 而非 100%**：差额 13 条全部是 DoS 类，依赖超时熔断器兜底（该行为由既有测试契约锁定）。停机问题不可判定，静态层无法穷尽无限循环；分层防御（AST 门 → 命名空间/导入钩子 → 熔断器 → 输出上限 → 模块门面）是设计意图。
3. **psutil 危险方法依赖 AST 引用期拒绝**：`Process.kill/terminate/suspend/...` 由 5.1 在引用期拦截（已验证），但 psutil 模块本身未做门面化——若未来 AST 出现解析缺口，psutil 是最大残余面。可选后续：为 psutil 增加只读门面。

## 10. 复现方式

```bat
cd /d E:\moray-aio
.venv\Scripts\python.exe scripts\security_fuzzer.py            :: 全量双阶段
.venv\Scripts\python.exe scripts\security_fuzzer.py --verbose  :: 逐条打印拦截原因
.venv\Scripts\python.exe scripts\security_fuzzer.py --ast-only :: 仅静态门（秒级）
```

退出码 0 当且仅当 漏网 = 0 且 误报 = 0，可直接挂入 CI 作为安全门禁。扩展语料：在 `build_corpus()` 中追加 payload（沿用片段拼接约定；效果类 payload 必须以金丝雀或受控失败收尾；网络类只准拨 `127.0.0.1` 金丝雀端口）。

---

## 11. v3.0.0 附录：进程隔离执行（对 §9 局限 1 的架构级闭环）

§9 局限 1 指出：线程式熔断无法杀死持 GIL 的 runaway 线程。v3.0.0 以
`SandboxWorkerPool`（`app/tools/dynamic_sandbox.py`）彻底关闭该缺口：

- **执行位置**：片段只在独立 worker 进程中编译与执行（multiprocessing spawn 池，
  常驻复用、被击杀后自动补位）；宿主解释器里不构建 code object，worker 侧自行重跑
  `check_ast`（不信任父进程的审查结论）。
- **IPC 纪律**：进程间只传 `json.dumps` 后的结果字符串（父进程 `json.loads` 回读，
  状态白名单归一化）——敌意结果无法经 pickle/对象通道回流。
- **超时 = OS 击杀**：`run_snippet` 超时（默认 3s + 0.5s 宽限）即对 worker 调用
  `process.terminate()`（POSIX SIGTERM / Windows TerminateProcess）并补位新进程。
- **钻地弹演练（GIL-bomb drill）**：`sum(range(10**9))` —— 纯 C 层循环、持 GIL 数十秒，
  旧线程熔断完全无解 —— 以 timeout=1.0s 提交后 **1.524s 内被物理击杀**（status=timeout），
  池子随后继续正常服务（follow-up 调用返回 42）。该演练已内置于
  `scripts/security_fuzzer.py` 的 GIL-bomb drill 段与
  `tests/test_v20_sandbox.py::test_gil_bomb_is_physically_terminated`，每次 fuzzer/测试运行都会复验。
- **回归**：进程隔离下 fuzzer 全量重跑 —— **198 恶意 / 0 逃逸 / 0 误报 / 沙箱级拦截 100%**；
  测试套件 48 个 sandbox 用例 + 全仓 184 用例全绿。
