"""Postgres 仓储实现（MORAY_AIO_DB_DSN 配置时启用，多进程/高并发模式）。

- 驱动：psycopg2（psycopg2-binary），惰性导入——未安装时只有显式使用才会报错；
- SQL 一律参数绑定（%s），绝不拼接；ON CONFLICT upsert；
- 连接级 threading.Lock 串行化（与 SQLite 实现同策略；如需更高并发可换连接池）。
"""

from __future__ import annotations

import json
import threading
from typing import List, Optional, Sequence

from app.kernel.repository import (
    EpisodeRepository,
    _now,
    compute_metrics,
    extract_keywords,
    rank_similar,
)
from app.kernel.state import Episode


class PostgresEpisodeRepository(EpisodeRepository):
    """Postgres 持久化。DSN 形如 postgresql://user:pass@host:5432/db（从环境变量读取）。"""

    def __init__(self, dsn: str):
        try:
            import psycopg2  # 惰性导入：默认 SQLite 模式不依赖该驱动
        except ImportError as exc:
            raise RuntimeError(
                "Postgres mode requires psycopg2: pip install -r requirements-optional.txt"
            ) from exc
        self._conn = psycopg2.connect(str(dsn))
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
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
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS reflections (
                        id BIGSERIAL PRIMARY KEY,
                        episode_id TEXT NOT NULL,
                        lesson TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS episodes_index (
                        episode_id TEXT PRIMARY KEY,
                        keywords TEXT NOT NULL,
                        conclusion TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
            self._conn.commit()

    def save(self, ep: Episode) -> None:
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
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO episodes (id, trigger, phase, payload, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        trigger = EXCLUDED.trigger,
                        phase = EXCLUDED.phase,
                        payload = EXCLUDED.payload,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (ep.id, ep.trigger, ep.phase.value, payload, ep.created_at, _now()),
                )
                cur.execute(
                    """
                    INSERT INTO episodes_index (episode_id, keywords, conclusion, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (episode_id) DO UPDATE SET
                        keywords = EXCLUDED.keywords,
                        conclusion = EXCLUDED.conclusion,
                        created_at = EXCLUDED.created_at
                    """,
                    (
                        ep.id,
                        json.dumps(keywords, ensure_ascii=False),
                        ep.conclusion,
                        ep.created_at,
                    ),
                )
            self._conn.commit()

    def load(self, episode_id: str) -> Optional[Episode]:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("SELECT payload FROM episodes WHERE id = %s", (episode_id,))
                row = cur.fetchone()
        if row is None:
            return None
        return Episode.from_dict(json.loads(row[0]))

    def list_recent(self, limit: int = 20) -> List[Episode]:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM episodes ORDER BY created_at DESC, id DESC LIMIT %s",
                    (int(limit),),
                )
                rows = cur.fetchall()
        return [Episode.from_dict(json.loads(r[0])) for r in rows]

    def metrics(self) -> dict:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("SELECT payload FROM episodes")
                payloads = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT COUNT(*) FROM reflections")
                n_reflections = cur.fetchone()[0]
        return compute_metrics(payloads, n_reflections)

    def record_reflection(self, episode_id: str, lesson: str) -> None:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO reflections (episode_id, lesson, created_at) VALUES (%s, %s, %s)",
                    (episode_id, lesson, _now()),
                )
            self._conn.commit()

    def recent_reflections(self, limit: int = 5) -> List[dict]:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT id, episode_id, lesson, created_at FROM reflections "
                    "ORDER BY id DESC LIMIT %s",
                    (int(limit),),
                )
                rows = cur.fetchall()
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
            with self._conn.cursor() as cur:
                cur.execute("SELECT episode_id, keywords, conclusion FROM episodes_index")
                rows = cur.fetchall()
        return rank_similar(rows, keywords, top_k=top_k, threshold=threshold,
                            exclude_episode_id=exclude_episode_id)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
