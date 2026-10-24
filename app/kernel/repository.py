"""持久化抽象层：EpisodeRepository 接口 + 各实现共享的纯函数。

设计目标：
- 内核（loop/routes）只依赖本接口，不感知具体存储（SQLite/Postgres/...）；
- 关键词提取、相似度排序、指标聚合都是纯函数，供各实现复用；
- 所有实现必须参数化查询（禁止拼接 SQL），并保证并发安全。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from app.kernel.state import Episode


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- 共享纯函数

# 关键词提取：字母开头、长度>=3 的 token，停用词过滤，词频取 top_n
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


def extract_keywords(texts, top_n: int = 10) -> List[str]:
    """从日志文本提取关键词（小写、去停用词、词频 top_n）。"""
    counter: Counter = Counter()
    for text in texts or []:
        for word in _WORD_RE.findall(str(text).lower()):
            if word in _STOPWORDS:
                continue
            counter[word] += 1
    return [word for word, _count in counter.most_common(max(1, int(top_n)))]


def compute_metrics(payloads: Iterable[str], n_reflections: int = 0) -> dict:
    """从 episode 载荷序列聚合全局指标（各存储实现共用）。"""
    total = 0
    concluded = 0
    failed = 0
    awaiting = 0
    running = 0
    prompt_tokens = 0
    completion_tokens = 0
    total_tasks = 0
    tasks_by_status: Counter = Counter()
    episode_durations = []
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
    """Jaccard 相似度排序；rows 为 (episode_id, keywords_json, conclusion)。"""
    query = {str(k).strip().lower() for k in (keywords or []) if str(k).strip()}
    if not query:
        return []
    scored: List[dict] = []
    for episode_id, keywords_json, conclusion in rows:
        if exclude_episode_id and episode_id == exclude_episode_id:
            continue  # 不把当前 episode 自己当历史案例
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


# ---------------------------------------------------------------- 抽象接口

class EpisodeRepository(ABC):
    """Episode 持久化接口。实现必须：参数化查询、并发安全、只存不改外部世界。"""

    @abstractmethod
    def save(self, ep: Episode) -> None:
        """按 id upsert；同时刷新关键词索引。"""

    @abstractmethod
    def load(self, episode_id: str) -> Optional[Episode]:
        """读取单个 episode；不存在返回 None。"""

    @abstractmethod
    def list_recent(self, limit: int = 20) -> List[Episode]:
        """按创建时间倒序列出最近 episode。"""

    @abstractmethod
    def metrics(self) -> dict:
        """全局统计（episode 数/成功率/Token/耗时/任务分布）。"""

    @abstractmethod
    def record_reflection(self, episode_id: str, lesson: str) -> None:
        """记录一条反思/教训。"""

    @abstractmethod
    def recent_reflections(self, limit: int = 5) -> List[dict]:
        """最近的反思记录。"""

    @abstractmethod
    def find_similar(
        self,
        keywords: Sequence[str],
        top_k: int = 3,
        threshold: float = 0.3,
        exclude_episode_id: Optional[str] = None,
    ) -> List[dict]:
        """按关键词 Jaccard 检索历史相似 episode。"""

    def close(self) -> None:
        """释放底层资源（可选实现）。"""
