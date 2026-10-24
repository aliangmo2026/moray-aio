"""SQLite 长效记忆：episode 持久化（upsert）+ reflection 教训沉淀 + 相似案例检索。

所有 SQL 一律使用参数绑定（?），不做任何字符串拼接。
线程安全：连接级 threading.Lock 包住全部读写（HTTP 线程与后台执行线程并发访问）。
相似检索用纯字符串 Jaccard，不引入 embedding 依赖。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

from app.kernel.state import Episode


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class Memory:
    """长效记忆。父目录不存在则自动创建；全部操作经连接级锁串行化。"""

    def __init__(self, db_path: Union[str, "os.PathLike"]):
        self.db_path = Path(db_path)
        parent = self.db_path.parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS episodes (
                        id TEXT PRIMARY KEY,
                        trigger TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS reflections (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        episode_id TEXT NOT NULL,
                        lesson TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS episodes_index (
                        episode_id TEXT PRIMARY KEY,
                        keywords TEXT NOT NULL,
                        conclusion TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )

    def save(self, ep: Episode) -> None:
        """按 id upsert；created_at 保留首次创建时间；同步刷新 episodes_index 关键词。"""
        payload = json.dumps(ep.to_dict(), ensure_ascii=False)
        keywords = extract_keywords(
            [
                e.get("text", "")
                for obs in ep.observations
                if obs.get("kind") == "recent_errors"
                for e in obs.get("errors", [])
            ],
            top_n=10,
        )
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO episodes (id, trigger, phase, payload, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        trigger = excluded.trigger,
                        phase = excluded.phase,
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    """,
                    (ep.id, ep.trigger, ep.phase.value, payload, ep.created_at, _now()),
                )
                self._conn.execute(
                    """
                    INSERT INTO episodes_index (episode_id, keywords, conclusion, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(episode_id) DO UPDATE SET
                        keywords = excluded.keywords,
                        conclusion = excluded.conclusion,
                        created_at = excluded.created_at
                    """,
                    (
                        ep.id,
                        json.dumps(keywords, ensure_ascii=False),
                        ep.conclusion,
                        ep.created_at,
                    ),
                )

    def load(self, episode_id: str) -> Optional[Episode]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
        if row is None:
            return None
        return Episode.from_dict(json.loads(row[0]))

    def list_recent(self, limit: int = 20) -> List[Episode]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM episodes ORDER BY created_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [Episode.from_dict(json.loads(r[0])) for r in rows]

    def record_reflection(self, episode_id: str, lesson: str) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO reflections (episode_id, lesson, created_at) VALUES (?, ?, ?)",
                    (episode_id, lesson, _now()),
                )

    def recent_reflections(self, limit: int = 5) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, episode_id, lesson, created_at FROM reflections "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {"id": r[0], "episode_id": r[1], "lesson": r[2], "created_at": r[3]}
            for r in rows
        ]

    def find_similar(
        self, keywords: List[str], top_k: int = 3, threshold: float = 0.3,
        exclude_episode_id: Optional[str] = None,
    ) -> List[dict]:
        """Jaccard 相似度检索历史 episode；返回 [{"episode_id","conclusion","similarity"}]。"""
        query = {str(k).strip().lower() for k in (keywords or []) if str(k).strip()}
        if not query:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT episode_id, keywords, conclusion FROM episodes_index"
            ).fetchall()
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

    def close(self) -> None:
        with self._lock:
            self._conn.close()
