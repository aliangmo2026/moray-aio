"""Persistence abstraction: the EpisodeRepository interface plus shared pure functions.

Design goals:
- The kernel (loop/routes) depends only on this interface, never on a concrete store
  (SQLite / Postgres / ...);
- keyword extraction, similarity ranking and metric aggregation are pure functions
  reused by every implementation;
- implementations must use parameterized SQL (never string concatenation) and be
  concurrency-safe.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

from app.kernel.state import Episode


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- shared pure functions

# Keyword extraction: tokens starting with a letter, length >= 3, stop-word filtered,
# top_n by frequency
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]{2,}")
_STOPWORDS = frozenset(
    {
        "error", "errors", "info", "warning", "warn", "debug", "critical", "fatal",
        "trace", "log", "logs", "line", "the", "and", "for", "with", "from", "this",
        "that", "not", "was", "were", "are", "is", "be", "been", "to", "in", "on",
        "at", "by", "of", "as", "it", "its", "after", "before", "while", "when",
        "then", "than", "but", "has", "have", "had", "did", "does", "do", "can",
        "could", "will", "would", "should", "may", "might", "new", "all", "one",
    }
)


def extract_keywords(texts: Iterable[str], top_n: int = 10) -> List[str]:
    """Extract keywords from log texts (lowercase, stop-word filtered, top_n by count)."""
    counter: Counter = Counter()
    for text in texts or []:
        for word in _WORD_RE.findall(str(text).lower()):
            if word in _STOPWORDS:
                continue
            counter[word] += 1
    return [word for word, _count in counter.most_common(max(1, int(top_n)))]


def compute_metrics(payloads: Iterable[str], n_reflections: int = 0) -> dict:
    """Aggregate global metrics from a sequence of episode payloads (shared by impls)."""
    total = 0
    concluded = 0
    failed = 0
    awaiting = 0
    running = 0
    prompt_tokens = 0
    completion_tokens = 0
    total_tasks = 0
    tasks_by_status: Counter = Counter()
    episode_durations: List[float] = []
    for payload in payloads:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            continue
        total += 1
        phase = data.get("phase")
        if phase == "concluded":
            concluded += 1
        elif phase == "failed":
            failed += 1
        elif phase == "awaiting_approval":
            awaiting += 1
        else:
            running += 1
        duration = 0.0
        for entry in data.get("trace") or []:
            duration += float(entry.get("duration_ms") or 0)
            usage = entry.get("llm") or {}
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("completion_tokens") or 0)
        episode_durations.append(duration)
        for task in data.get("tasks") or []:
            total_tasks += 1
            tasks_by_status[str(task.get("status") or "unknown")] += 1
    avg_ms = (
        round(sum(episode_durations) / len(episode_durations), 2) if episode_durations else 0.0
    )
    return {
        "total_episodes": total,
        "concluded": concluded,
        "failed": failed,
        "awaiting_approval": awaiting,
        "in_progress": running,
        "success_rate": round(concluded / total, 4) if total else 0.0,
        "total_prompt_tokens": prompt_tokens,
        "total_completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "avg_episode_duration_ms": avg_ms,
        "total_tasks": total_tasks,
        "tasks_by_status": dict(tasks_by_status),
        "reflections": int(n_reflections),
    }


def rank_similar(
    rows: Sequence[Tuple[str, str, Optional[str]]],
    keywords: Sequence[str],
    top_k: int = 3,
    threshold: float = 0.3,
    exclude_episode_id: Optional[str] = None,
) -> List[dict]:
    """Jaccard similarity ranking; rows are (episode_id, keywords_json, conclusion)."""
    query = {str(k).strip().lower() for k in (keywords or []) if str(k).strip()}
    if not query:
        return []
    scored: List[dict] = []
    for episode_id, keywords_json, conclusion in rows:
        if exclude_episode_id and episode_id == exclude_episode_id:
            continue  # never treat the current episode as its own history
        try:
            stored = {str(k).lower() for k in json.loads(keywords_json)}
        except (TypeError, ValueError):
            continue
        if not stored:
            continue
        union = len(query | stored)
        similarity = (len(query & stored) / union) if union else 0.0
        if similarity >= float(threshold):
            scored.append(
                {
                    "episode_id": episode_id,
                    "conclusion": conclusion,
                    "similarity": round(similarity, 4),
                }
            )
    scored.sort(key=lambda item: item["similarity"], reverse=True)
    return scored[: max(0, int(top_k))]


# ---------------------------------------------------------------- abstract interface

class EpisodeRepository(ABC):
    """Episode persistence interface. Implementations must: use parameterized SQL,
    be concurrency-safe, and never mutate the outside world."""

    @abstractmethod
    def save(self, ep: Episode) -> None:
        """Upsert by id; also refreshes the keyword index."""

    @abstractmethod
    def load(self, episode_id: str) -> Optional[Episode]:
        """Load one episode; returns None when missing."""

    @abstractmethod
    def list_recent(self, limit: int = 20) -> List[Episode]:
        """Most recent episodes, newest first."""

    @abstractmethod
    def metrics(self) -> dict:
        """Global metrics (episode count/success rate/tokens/duration/task mix)."""

    @abstractmethod
    def record_reflection(self, episode_id: str, lesson: str) -> None:
        """Record one reflection / lesson learned."""

    @abstractmethod
    def recent_reflections(self, limit: int = 5) -> List[dict]:
        """Most recent reflections."""

    @abstractmethod
    def find_similar(
        self,
        keywords: Sequence[str],
        top_k: int = 3,
        threshold: float = 0.3,
        exclude_episode_id: Optional[str] = None,
    ) -> List[dict]:
        """Retrieve history episodes similar to the keywords (Jaccard)."""

    def close(self) -> None:
        """Release underlying resources (optional)."""
