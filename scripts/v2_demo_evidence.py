"""v2.0.0 live evidence generator: exercises the four Enterprise features with
REAL components (real local Ollama embeddings, real chat model as Critic, real
MCP dispatcher, real AST gate) and prints the raw evidence blocks to stdout.

Run: python scripts/v2_demo_evidence.py   (needs local Ollama for blocks 2/3;
block 1 and 4 work fully offline). Nothing is written to disk except a temp
log fixture via pathlib.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.kernel.loop import EvolutionLoop  # noqa: E402
from app.kernel.sqlite_repository import Memory  # noqa: E402
from app.kernel.vector_memory import VectorMemory  # noqa: E402
from app.llm.ollama_client import OllamaClient  # noqa: E402
from app.tools.dynamic_sandbox import execute_safe_python  # noqa: E402
from app.tools.mcp_server import McpServer  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

DIVIDER = "=" * 72


def block(title: str) -> None:
    print("\n{}\n### {}\n{}".format(DIVIDER, title, DIVIDER))


def main() -> None:
    # ------------------------------------------------------------------ 1. AST
    block("EVIDENCE 1: AST sandbox intercepts a prompt-injected kill payload")
    hostile = "import os\nos.system('rm -rf /')"
    print("input snippet       : {!r}".format(hostile))
    outcome = execute_safe_python(hostile)
    print("sandbox result      : {}".format(json.dumps(outcome, ensure_ascii=False)))
    benign = "import math, psutil\ncpu = psutil.cpu_count()\n{'cpu_count': cpu, 'sqrt16': math.sqrt(16)}"
    print("benign snippet      : {}".format(json.dumps(execute_safe_python(benign), ensure_ascii=False)))

    # ------------------------------------------------------------- 2. Vector
    block("EVIDENCE 2: real Ollama embedding (nomic-embed-text) vector retrieval")
    tmp = Path(tempfile.mkdtemp(prefix="moray_evidence_"))
    client = OllamaClient("http://127.0.0.1:11434", "qwen2.5:7b", 120)
    try:
        vector = client.embed("Connection refused while connecting to redis 127.0.0.1:6379",
                              model="nomic-embed-text")
        print("embedded dims       : {} (model=nomic-embed-text, real /api/embeddings)".format(len(vector)))
        print("embedding head      : {}".format([round(v, 6) for v in vector[:6]]))
    except Exception as exc:
        print("embedding UNAVAILABLE: {}: {}".format(type(exc).__name__, exc))
        vector = None

    if vector is not None:
        def _embed(text):
            return client.embed(text, model="nomic-embed-text")

        vm = VectorMemory(tmp / "vector_memory.sqlite3", embed_fn=_embed, threshold=0.3)
        ok1 = vm.remember(
            "case-redis-001",
            "ERROR Connection refused to 127.0.0.1:6379\nredis pool exhausted",
            "根因：本地 Redis(6379) 未监听导致连接池耗尽；建议：人工确认 Redis 进程与端口后重启。",
        )
        ok2 = vm.remember(
            "case-disk-002",
            "ERROR No space left on device while writing checkpoint",
            "根因：磁盘写满导致 checkpoint 落盘失败；建议：人工清理后重启写入进程。",
        )
        print("stored cases        : redis={} disk={}".format(ok1, ok2))
        hits = vm.search("app cannot connect to redis, connection refused on port 6379", top_k=3)
        print("query               : 'app cannot connect to redis, connection refused on port 6379'")
        print("top-3 hits          : {}".format(json.dumps(hits, ensure_ascii=False, indent=2)))
        vm.close()

    # ---------------------------------------------------------------- 3. Critic
    block("EVIDENCE 3: real Critic Agent (qwen2.5:7b) rebuts an over-confident analysis")
    review = None
    try:
        review = client.critic_review(
            errors=[
                "[x5] 2026-10-25 10:01:00 ERROR Connection refused to 127.0.0.1:6379",
                "[x2] 2026-10-25 10:03:00 WARNING retry scheduled (attempt 2)",
            ],
            analysis={
                "summary": "redis 挂了",
                "likely_root_causes": ["Redis 服务已崩溃，磁盘故障是唯一原因"],
                "information_gaps": [],
                "conclusion": "确定是 Redis 崩溃 + 磁盘故障，重启服务器即可恢复。",
            },
        )
    except Exception as exc:
        print("critic call FAILED  : {}: {}".format(type(exc).__name__, exc))
    if review is not None:
        print("critic raw verdict  :")
        print(json.dumps(review, ensure_ascii=False, indent=2))

    # full kernel run with critic + vector memory wired (real LLM, heuristic plan)
    log = tmp / "moray.err.log"
    log.write_text(
        "\n".join(
            [
                "2026-10-25 10:00:00 INFO service started",
                "2026-10-25 10:01:00 ERROR Connection refused to 127.0.0.1:6379",
                "2026-10-25 10:01:01 ERROR Connection refused to 127.0.0.1:6379",
                "2026-10-25 10:02:00 WARNING retry scheduled",
            ]
        ),
        encoding="utf-8",
    )
    settings = dataclasses.replace(
        Settings(),
        log_path=str(log),
        readonly_roots_raw=str(tmp),
        max_iterations=1,
        auto_approve_readonly=True,
        rate_limit_per_minute=0,
    )
    memory = Memory(str(tmp / "memory.sqlite3"))
    tools = ToolRegistry(settings.readonly_roots, settings.max_scan_bytes)
    embed_client = OllamaClient("http://127.0.0.1:11434", "qwen2.5:7b", 120)
    vector_memory = VectorMemory(
        tmp / "loop_vectors.sqlite3",
        embed_fn=lambda t: embed_client.embed(t, model="nomic-embed-text"),
        threshold=0.3,
    )
    loop = EvolutionLoop(
        settings=settings,
        memory=memory,
        tools=tools,
        llm=client,
        auto_approve_readonly=True,
        vector_memory=vector_memory,
    )
    print("\n--- full kernel episode (real qwen2.5:7b Analyst+Critic) ---")
    started = time.perf_counter()
    ep = loop.start()
    print("episode             : {} phase={} ({:.1f}s)".format(
        ep.id, ep.phase.value, time.perf_counter() - started
    ))
    for entry in ep.trace:
        if entry["stage"] in ("critic_review", "reflexion", "memory_retrieval", "memory_write"):
            print("trace[{}]         : {}".format(
                entry["stage"], json.dumps(entry.get("detail"), ensure_ascii=False)
            ))
    if isinstance(ep.analysis, dict):
        print("analysis.critic_review : {}".format(
            json.dumps(ep.analysis.get("critic_review"), ensure_ascii=False)
        ))
        print("analysis.conclusion    : {}".format(ep.analysis.get("conclusion")))
    vector_memory.close()

    # ------------------------------------------------------------------- 4. MCP
    block("EVIDENCE 4: MCP JSON-RPC handshake over the real dispatcher")
    registry = ToolRegistry([str(tmp)], max_scan_bytes=1024 * 1024)
    server = McpServer(registry, version="2.0.0")
    for label, raw in (
        ("initialize", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-03-26"}})),
        ("tools/list", json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})),
        ("tools/call process_list", json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "process_list", "arguments": {"name_pattern": "python"}}})),
        ("tools/call whitelist violation", json.dumps({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "log_tail", "arguments": {"path": r"C:\Windows\System32", "n": 5}}})),
        ("tools/call AST sandbox", json.dumps({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "execute_safe_python", "arguments": {"code": "sum(range(10))"}}})),
    ):
        response = server.handle_message(raw)
        print("{:26s} -> {}".format(label, response))

    print("\n{}".format(DIVIDER))
    print("evidence collection done")


if __name__ == "__main__":
    main()
