"""SQLite repository implementation (default lightweight / single-node mode).

Migrated from the former app/kernel/memory.py. All SQL is parameterized (?),
and a connection-level threading.Lock serializes concurrent access from HTTP
and background worker threads.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import List, Optional, Sequence, Union

from app.kernel.repository import (
    EpisodeRepository,
    _now,
    compute_metrics,
    extract_keywords,
    rank_similar,
)
from app.kernel.state import Episode


class SqliteEpisodeRepository(EpisodeRepository):
    """SQLite persistence. Creates parent directories automatically;
    every operation goes through the connection-level lock."""

    def __init__(self, db_path: Union[str, os.PathLike]):
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
        """Upsert by id; created_at keeps the original creation time;
        refreshes the episodes_index keyword row."""
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

    def metrics(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT payload FROM episodes").fetchall()
            n_reflections = self._conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        return compute_metrics([r[0] for r in rows], n_reflections)

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
        self,
        keywords: Sequence[str],
        top_k: int = 3,
        threshold: float = 0.3,
        exclude_episode_id: Optional[str] = None,
    ) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT episode_id, keywords, conclusion FROM episodes_index"
            ).fetchall()
        return rank_similar(
            rows, keywords, top_k=top_k, threshold=threshold,
            exclude_episode_id=exclude_episode_id,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# Backward-compatible alias (since v0.6.0 memory.py migrated to sqlite_repository.py)
Memory = SqliteEpisodeRepository
