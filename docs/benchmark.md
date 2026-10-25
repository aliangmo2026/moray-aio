# moray-aio Benchmark

> 由 `python scripts/benchmark.py` 自动生成。数据文件在 `data/benchmark/`（不入库）。

## 环境

| 项 | 值 |
| --- | --- |
| Python | 3.8.10 |
| Platform | Windows-10-10.0.26100-SP0 |

## 结果

| 项目 | 指标 |
| --- | --- |
| 生成 101.54MB 日志 | 24.31s |
| read_rolling 全量扫描 | 1445000 行 / 16.601s / **6.1 MB/s** / 峰值内存 552.3MB |
| cluster 聚类 | 1445000 行 -> **4 类** / 67.969s / 21259 lines/s / 峰值内存 0.0MB |
| 并发启发式分析 | 20 workers x 200 ops / 0.742s / **269.6 QPS**（无 LLM，聚类+归因+规划全链路） |

## 聚类 top 模板

| 模板 | 命中数 |
| --- | --- |
| `<TS> ERROR request timeout after <NUM>ms: http://<IP>:<PORT>/api/tasks` | 361250 |
| `<TS> ERROR FileNotFoundError: No such file: data/cache_<NUM>.bin` | 361250 |
| `<TS> ERROR dial tcp <IP>:<PORT> refused id=<HEX>` | 361250 |
| `<TS> INFO job tick ok seq=<NUM>` | 361250 |

## 备注

- read_rolling/cluster 覆盖全量 101.54MB（max_scan_bytes 放开），反映最坏情况吞吐；
- 并发压测直接调用分析内核（cluster + heuristic analysis + propose_tasks），
  不含 HTTP 与 LLM 延迟，度量的是纯 Python 内核的并发天花板；
- 结果随机器浮动，CI 中不设硬性阈值。
