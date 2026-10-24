"""moray-aio 硬核压测：日志读取 / 错误聚类 / 并发启发式分析。

用法：
    python scripts/benchmark.py [--size-mb 100] [--workers 20] [--ops 10]
                                [--out docs/benchmark.md]

产出：终端表格 + docs/benchmark.md。被压测数据写在 data/benchmark/（已 gitignore）。
全程只写自己的压测数据与报告，不碰任何被监控日志。
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.kernel.loop import EvolutionLoop  # noqa: E402
from app.kernel.planner import propose_tasks  # noqa: E402
from app.tools import clusterer  # noqa: E402
from app.tools.log_reader import read_rolling  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

BENCH_DIR = REPO_ROOT / "data" / "benchmark"

ERROR_TEMPLATES = (
    "2026-10-24 {h:02d}:{m:02d}:{s:02d} ERROR dial tcp 10.0.{a}.{b}:{port} refused id=beef{hex:06x}",
    "2026-10-24 {h:02d}:{m:02d}:{s:02d} ERROR request timeout after {ms}ms: http://127.0.0.1:{port}/api/tasks",
    "2026-10-24 {h:02d}:{m:02d}:{s:02d} ERROR FileNotFoundError: No such file: data/cache_{n}.bin",
)
NOISE_TEMPLATE = "2026-10-24 {h:02d}:{m:02d}:{s:02d} INFO job tick ok seq={n}"


def generate_log(size_mb: int) -> Path:
    """生成约 size_mb 的模拟日志（3 类 ERROR 模板 + INFO 噪声，逐块写入）。"""
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    path = BENCH_DIR / "moray.err.log"
    target_bytes = size_mb * 1024 * 1024
    written = 0
    i = 0
    with path.open("w", encoding="utf-8") as fh:
        while written < target_bytes:
            chunk_lines = []
            for _ in range(5000):
                i += 1
                if i % 4 == 0:
                    line = NOISE_TEMPLATE.format(h=i % 24, m=i % 60, s=i % 60, n=i)
                else:
                    template = ERROR_TEMPLATES[i % len(ERROR_TEMPLATES)]
                    line = template.format(
                        h=i % 24, m=i % 60, s=i % 60,
                        a=i % 250, b=(i // 7) % 250, port=6000 + i % 300,
                        ms=100 + i % 900, n=100000 + i, hex=i % 0xFFFFFF,
                    )
                chunk_lines.append(line)
            block = "\n".join(chunk_lines) + "\n"
            fh.write(block)
            written += len(block.encode("utf-8"))
    return path


def _timed(fn):
    tracemalloc.start()
    started = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, elapsed, peak


def _analysis_op(lines, tools, log_path):
    """一次「分析内核」操作：聚类 + 启发式归因 + 任务规划（无 LLM）。"""
    clusters = clusterer.cluster(lines, top_n=10)
    formatted = ["[x{}] {} | 样本: {}".format(c["count"], c["template"], c["sample_line"])
                 for c in clusters]
    analysis = EvolutionLoop._heuristic_analysis(formatted)
    tasks = propose_tasks(
        errors=formatted, existing_tasks=[], log_path=str(log_path),
        tools=tools, llm=None, auto_approve_readonly=True,
    )
    return len(clusters) + len(tasks) + len(analysis or {})


def run_benchmark(size_mb: int, workers: int, ops: int) -> dict:
    results = {"env": {"python": platform.python_version(), "platform": platform.platform()}}

    print("== moray-aio benchmark ==")
    print("[1/4] 生成 {:.0f}MB 模拟日志 ...".format(size_mb))
    log_path, gen_time, _ = _timed(lambda: generate_log(size_mb))
    size_actual = log_path.stat().st_size / 1024 / 1024
    results["generate"] = {"seconds": round(gen_time, 2), "size_mb": round(size_actual, 2)}
    print("      done: {:.1f}MB in {:.2f}s".format(size_actual, gen_time))

    print("[2/4] read_rolling 全量扫描 ...")
    lines, read_time, read_peak = _timed(
        lambda: read_rolling(
            BENCH_DIR, "moray.err.log*", n=2 ** 31 - 1,
            max_files=5, max_scan_bytes=max(size_mb + 10, 120) * 1024 * 1024,
        )
    )
    results["read_rolling"] = {
        "lines": len(lines), "seconds": round(read_time, 3),
        "peak_mb": round(read_peak / 1024 / 1024, 1),
        "throughput_mb_per_s": round(size_actual / read_time, 1) if read_time else 0,
    }
    print("      {} lines in {:.2f}s ({:.1f} MB/s), peak mem {:.1f}MB".format(
        len(lines), read_time, results["read_rolling"]["throughput_mb_per_s"],
        results["read_rolling"]["peak_mb"]))

    print("[3/4] cluster 聚类 ...")
    clusters, cluster_time, cluster_peak = _timed(lambda: clusterer.cluster(lines, top_n=10))
    results["cluster"] = {
        "classes": len(clusters), "seconds": round(cluster_time, 3),
        "peak_mb": round(cluster_peak / 1024 / 1024, 1),
        "lines_per_s": int(len(lines) / cluster_time) if cluster_time else 0,
        "top": [{"template": c["template"][:80], "count": c["count"]} for c in clusters[:5]],
    }
    print("      {} lines -> {} classes in {:.2f}s ({:d} lines/s), peak mem {:.1f}MB".format(
        len(lines), len(clusters), cluster_time,
        results["cluster"]["lines_per_s"], results["cluster"]["peak_mb"]))

    print("[4/4] 并发启发式分析 ({} workers x {} ops) ...".format(workers, ops))
    sample = lines[-50:] or ["ERROR sample"]
    tools = ToolRegistry([str(BENCH_DIR)], 64 * 1024 * 1024)
    total_ops = workers * ops

    def worker(_idx):
        for _ in range(ops):
            _analysis_op(sample, tools, log_path)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, range(workers)))
    elapsed = time.perf_counter() - started
    results["concurrency"] = {
        "workers": workers, "ops": total_ops, "seconds": round(elapsed, 3),
        "qps": round(total_ops / elapsed, 1) if elapsed else 0,
    }
    print("      {} ops in {:.2f}s -> {:.1f} QPS".format(total_ops, elapsed, results["concurrency"]["qps"]))
    return results


def render_report(results: dict) -> str:
    env = results["env"]
    gen = results["generate"]
    read = results["read_rolling"]
    cl = results["cluster"]
    conc = results["concurrency"]
    top_rows = "\n".join(
        "| `{}` | {} |".format(t["template"], t["count"]) for t in cl["top"]
    )
    return """# moray-aio Benchmark

> 由 `python scripts/benchmark.py` 自动生成。数据文件在 `data/benchmark/`（不入库）。

## 环境

| 项 | 值 |
| --- | --- |
| Python | {python} |
| Platform | {platform} |

## 结果

| 项目 | 指标 |
| --- | --- |
| 生成 {gen_mb}MB 日志 | {gen_s}s |
| read_rolling 全量扫描 | {read_lines} 行 / {read_s}s / **{read_tp} MB/s** / 峰值内存 {read_mem}MB |
| cluster 聚类 | {cl_lines} 行 -> **{cl_classes} 类** / {cl_s}s / {cl_lps} lines/s / 峰值内存 {cl_mem}MB |
| 并发启发式分析 | {workers} workers x {ops} ops / {conc_s}s / **{qps} QPS**（无 LLM，聚类+归因+规划全链路） |

## 聚类 top 模板

| 模板 | 命中数 |
| --- | --- |
{top_rows}

## 备注

- read_rolling/cluster 覆盖全量 {gen_mb}MB（max_scan_bytes 放开），反映最坏情况吞吐；
- 并发压测直接调用分析内核（cluster + heuristic analysis + propose_tasks），
  不含 HTTP 与 LLM 延迟，度量的是纯 Python 内核的并发天花板；
- 结果随机器浮动，CI 中不设硬性阈值。
""".format(
        python=env["python"], platform=env["platform"],
        gen_mb=gen["size_mb"], gen_s=gen["seconds"],
        read_lines=read["lines"], read_s=read["seconds"], read_tp=read["throughput_mb_per_s"],
        read_mem=read["peak_mb"],
        cl_lines=cl["lines"] if "lines" in cl else read["lines"], cl_classes=cl["classes"],
        cl_s=cl["seconds"], cl_lps=cl["lines_per_s"], cl_mem=cl["peak_mb"],
        workers=conc["workers"], ops=conc["ops"], conc_s=conc["seconds"], qps=conc["qps"],
        top_rows=top_rows,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="moray-aio benchmark")
    parser.add_argument("--size-mb", type=int, default=100)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--ops", type=int, default=10)
    parser.add_argument("--out", default="docs/benchmark.md")
    args = parser.parse_args()

    results = run_benchmark(args.size_mb, args.workers, args.ops)
    report = render_report(results)

    out_path = REPO_ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print("\n报告已写入 {}".format(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
