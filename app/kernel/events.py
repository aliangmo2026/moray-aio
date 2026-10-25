"""In-process event bus: the backbone of the SSE live stream.

Producers (watcher, kernel loop) publish typed events; the SSE endpoint subscribes
with one bounded queue per client. Delivery is best-effort within the process:
when a client queue is full, the oldest event is evicted so the stream never
blocks producers or grows unbounded.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

Event = Tuple[str, Dict[str, Any]]


class EventBus:
    """Thread-safe publish/subscribe bus with per-subscriber bounded queues."""

    def __init__(self, max_queue: int = 200):
        self._max = max(1, int(max_queue))
        self._subs: Set["queue.Queue"] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> "queue.Queue":
        """Register a new subscriber queue."""
        q: "queue.Queue" = queue.Queue(maxsize=self._max)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        """Remove a subscriber queue (safe to call twice)."""
        with self._lock:
            self._subs.discard(q)

    def publish(self, event_type: str, payload: Optional[dict] = None) -> None:
        """Publish an event to every subscriber (never blocks, never raises)."""
        with self._lock:
            subscribers: List["queue.Queue"] = list(self._subs)
        for q in subscribers:
            try:
                q.put_nowait((event_type, dict(payload or {})))
            except queue.Full:
                # evict the oldest event and retry once: live streams prefer fresh data
                try:
                    q.get_nowait()
                    q.put_nowait((event_type, dict(payload or {})))
                except (queue.Full, queue.Empty):
                    pass

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


EVENT_BUS = EventBus()


def publish(event_type: str, payload: Optional[dict] = None) -> None:
    """Module-level convenience: publish to the process-wide bus (never raises)."""
    try:
        EVENT_BUS.publish(event_type, payload)
    except Exception:  # pragma: no cover - telemetry must never break the kernel
        pass
