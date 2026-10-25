"""v1.2.0 tests: SSE live stream, event bus, multiline stack merging and the
single-pass precompiled clusterer regex.

The SSE generator is tested directly (TestClient/httpx ASGITransport buffers
whole streaming responses, which would deadlock an infinite SSE stream); the
endpoint itself is proven live with curl against a real uvicorn server."""

from __future__ import annotations

import asyncio
import re
import time

from app.kernel.events import EventBus
from app.kernel.state import Episode
from app.kernel.watcher import LogWatcher
from app.tools.clusterer import _COMBINED_RE, cluster, normalize
from app.tools.log_reader import CONTINUATION_RE, group_multiline

JAVA_STACK = [
    "2026-10-24 14:20:01 ERROR worker crashed id=a1b2c3d4e5",
    "    at com.moray.worker.JobRunner.run(JobRunner.java:88)",
    "    at java.base/java.lang.Thread.run(Thread.java:829)",
    "2026-10-24 14:20:02 ERROR worker crashed id=b2c3d4e5f6",
    "    at com.moray.worker.JobRunner.run(JobRunner.java:88)",
    "    at java.base/java.lang.Thread.run(Thread.java:829)",
]


# ---------- event bus ----------

def test_event_bus_pubsub():
    bus = EventBus(max_queue=10)
    q = bus.subscribe()
    assert bus.subscriber_count() == 1
    bus.publish("log_captured", {"lines": ["ERROR x"]})
    event_type, payload = q.get_nowait()
    assert event_type == "log_captured"
    assert payload == {"lines": ["ERROR x"]}
    bus.unsubscribe(q)
    bus.unsubscribe(q)  # double unsubscribe is safe
    assert bus.subscriber_count() == 0


def test_event_bus_evicts_oldest_when_full():
    bus = EventBus(max_queue=2)
    q = bus.subscribe()
    bus.publish("e1", {"n": 1})
    bus.publish("e2", {"n": 2})
    bus.publish("e3", {"n": 3})  # queue full -> evicts e1
    drained = [q.get_nowait() for _ in range(2)]
    assert [payload for _t, payload in drained] == [{"n": 2}, {"n": 3}]


def test_state_transition_publishes_events():
    from app.kernel.events import EVENT_BUS

    q = EVENT_BUS.subscribe()
    try:
        ep = Episode.new(trigger="unit")
        ep.transition("queued")
        ep.transition("observing")
        first = q.get_nowait()
        second = q.get_nowait()
        assert first[0] == "episode_phase" and first[1]["phase"] == "queued"
        assert second[1]["phase"] == "observing"
        assert first[1]["episode_id"] == ep.id
    finally:
        EVENT_BUS.unsubscribe(q)


# ---------- SSE stream (generator-level; see module docstring) ----------

def test_sse_generator_hello_events_and_heartbeat():
    from app.api.dashboard import _sse_generator
    from app.kernel.events import EVENT_BUS

    async def scenario() -> str:
        subscriber = EVENT_BUS.subscribe()

        async def never_disconnected() -> bool:
            return False

        gen = _sse_generator(subscriber, never_disconnected)
        await asyncio.sleep(0.05)  # generator subscribes before we publish
        EVENT_BUS.publish("log_captured", {"lines": ["ERROR gen-check"]})

        chunks: list = []
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 8
        async for chunk in gen:
            chunks.append(chunk)
            joined = "".join(chunks)
            if "hello" in joined and "gen-check" in joined and "heartbeat" in joined:
                break
            if loop.time() > deadline:
                raise AssertionError("SSE generator did not produce the expected frames")
        await gen.aclose()  # triggers the finally -> unsubscribe
        return "".join(chunks)

    joined = asyncio.run(scenario())
    assert "event: hello" in joined                      # connect confirmation
    assert "event: log_captured" in joined               # bus event round-trips
    assert "ERROR gen-check" in joined                   # payload is JSON-encoded
    assert "data: {\"ok\": true}" in joined              # hello payload shape
    assert ": heartbeat" in joined                       # idle keep-alive frame
    assert EVENT_BUS.subscriber_count() == 0             # unsubscribe on close


def test_sse_generator_stops_on_disconnect():
    from app.api.dashboard import _sse_generator
    from app.kernel.events import EVENT_BUS

    async def scenario() -> int:
        subscriber = EVENT_BUS.subscribe()
        state = {"disconnected": False}

        async def maybe_disconnected() -> bool:
            return state["disconnected"]

        gen = _sse_generator(subscriber, maybe_disconnected)
        seen = 0
        async for _chunk in gen:
            seen += 1
            if seen >= 3:
                state["disconnected"] = True
        return seen

    seen = asyncio.run(scenario())
    assert seen >= 3  # hello + a heartbeat cycle, then a clean stop
    assert EVENT_BUS.subscriber_count() == 0


# ---------- multiline merging ----------

def test_continuation_regex():
    assert CONTINUATION_RE.match("    at com.x.y(Z.java:1)")
    assert CONTINUATION_RE.match("\tat com.x.y(Z.java:1)")
    assert CONTINUATION_RE.match("  Caused by: java.lang.NullPointerException")
    assert not CONTINUATION_RE.match("2026-10-24 ERROR fresh record")
    assert not CONTINUATION_RE.match("INFO normal line")


def test_group_multiline_merges_stack_traces():
    records = group_multiline(JAVA_STACK)
    assert len(records) == 2  # two crashes, each with its stack folded in
    assert records[0].startswith("2026-10-24 14:20:01 ERROR worker crashed")
    assert "JobRunner.java:88" in records[0]
    assert "Thread.java:829" in records[0]
    assert records[1].startswith("2026-10-24 14:20:02 ERROR")


def test_group_multiline_respects_size_cap():
    fat = "x" * 100
    lines = ["ERROR start"] + ["    pad {}".format(fat)] * 400  # ~40KB of frames
    records = group_multiline(lines, max_record_bytes=4096)
    assert len(records) > 1  # cap kicks in instead of unbounded growth
    assert all(len(r) < 8192 for r in records)


def test_watcher_emits_merged_records(tmp_path):
    log = tmp_path / "w.log"
    log.write_text("INFO boot\n", encoding="utf-8")
    watcher = LogWatcher(str(log), on_lines=lambda lines: None, poll_interval=0.05)

    # one poll delivering an ERROR + stack trace closed by a plain INFO line:
    # the record flushes inside the chunk, whole and unshredded
    matched = watcher._process_lines(
        [
            "INFO boot",
            "2026-10-24 ERROR boom id=beef000001",
            "    at com.x.Y.run(Y.java:42)",
            "    at java.base/java.lang.Thread.run(Thread.java:829)",
            "INFO next record",
        ]
    )
    assert len(matched) == 1  # one merged ERROR record; INFO lines dropped by the filter
    assert "Y.java:42" in matched[0]
    assert "Thread.java:829" in matched[0]
    assert matched[0].startswith("2026-10-24 ERROR boom")


def test_watcher_carries_stack_across_polls():
    watcher = LogWatcher("unused.log", on_lines=lambda lines: None, poll_interval=0.05)

    # poll 1: record ends on a continuation frame -> held in carry, not emitted
    first = watcher._process_lines(
        ["ERROR exploded id=beef000009", "    at com.x.Z.run(Z.java:7)"]
    )
    assert first == []
    assert watcher._carry is not None

    # poll 2: more frames arrive -> merge into the same record, then flush on a
    # non-continuation line
    second = watcher._process_lines(
        ["    at java.base/java.lang.Thread.run(Thread.java:829)", "INFO next record"]
    )
    assert len(second) == 1
    assert "Z.java:7" in second[0] and "Thread.java:829" in second[0]
    assert watcher._carry is None

    # quiet poll flushes any pending carry
    watcher._process_lines(["ERROR tail id=beef000010", "    at com.x.W.run(W.java:9)"])
    assert watcher._carry is not None
    flushed = watcher._process_lines([])
    assert len(flushed) == 1 and "W.java:9" in flushed[0]


# ---------- clusterer single-pass regex ----------

def test_clusterer_precompiled_pattern():
    assert isinstance(_COMBINED_RE, re.Pattern)  # precompiled exactly once
    # semantics preserved after the single-pass rewrite
    assert normalize("2026-10-24 14:20:01 ERROR dial tcp 127.0.0.1:6379 refused id=a1b2c3") == (
        "<TS> ERROR dial tcp <IP>:<PORT> refused id=<HEX>"
    )
    assert normalize("request timeout after 304ms on port 8000") == (
        "request timeout after <NUM>ms on port <NUM>"
    )
    # multiline records normalize as a whole (digits after ':' fold to <PORT>,
    # matching the historical single-line semantics)
    record = group_multiline(JAVA_STACK)[0]
    assert normalize(record) == (
        "<TS> ERROR worker crashed id=<HEX>\n"
        "    at com.moray.worker.JobRunner.run(JobRunner.java:<PORT>)\n"
        "    at java.base/java.lang.Thread.run(Thread.java:<PORT>)"
    )


def test_clusterer_multiline_records_fold_to_one_template():
    records = group_multiline(JAVA_STACK)
    result = cluster(records, top_n=10)
    assert len(result) == 1
    assert result[0]["count"] == 2  # two identical crashes -> one template


def test_clusterer_throughput_smoke():
    # a 50k-line smoke run must finish comfortably fast with the single pass
    lines = [
        "2026-10-24 10:{:02d}:{:02d} ERROR dial tcp 10.0.0.{}:{} refused id=beef{:06x}".format(
            i % 60, i % 60, i % 250, 6000 + i % 300, i
        )
        for i in range(50000)
    ]
    started = time.perf_counter()
    result = cluster(lines, top_n=5)
    elapsed = time.perf_counter() - started
    assert len(result) == 1 and result[0]["count"] == 50000
    assert elapsed < 10.0  # generous CI ceiling; typically well under 2s
