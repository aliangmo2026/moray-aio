"""SQLite 长效记忆：episode 持久化（upsert）+ reflection 教训沉淀。

所有 SQL 一律使用参数绑定（?），不做任何字符串拼接。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

from app.kernel.state import Episode


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Memory:
    """长效记忆。父目录不存在则自动创建。"""

    def __init__(self, db_path: Union[str, "os.PathLike"]):
        self.db_path = Path(db_path)
        parent = self.db_path.parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
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

    def save(self, ep: Episode) -> None:
        """按 id upsert；created_at 保留首次创建时间。"""
        payload = json.dumps(ep.to_dict(), ensure_ascii=False)
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

    def load(self, episode_id: str) -> Optional[Episode]:
        row = self._conn.execute(
            "SELECT payload FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            return None
        return Episode.from_dict(json.loads(row[0]))

    def list_recent(self, limit: int = 20) -> List[Episode]:
        rows = self._conn.execute(
            "SELECT payload FROM episodes ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [Episode.from_dict(json.loads(r[0])) for r in rows]

    def record_reflection(self, episode_id: str, lesson: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO reflections (episode_id, lesson, created_at) VALUES (?, ?, ?)",
                (episode_id, lesson, _now()),
            )

    def recent_reflections(self, limit: int = 5) -> List[dict]:
        rows = self._conn.execute(
            "SELECT id, episode_id, lesson, created_at FROM reflections "
            "ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            {"id": r[0], "episode_id": r[1], "lesson": r[2], "created_at": r[3]}
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()
