"""Local semantic long-term memory: a pure-Python vector store plus Ollama
embeddings, used to retrieve "last time a similar error happened, the
conclusion was ..." cases by meaning instead of by keyword overlap.

Why pure Python instead of chromadb / lancedb: this product supports Python
3.8 (CI runs test-py38 in a python:3.8 container) where both packages refuse
to install; a zero-native-dependency store keeps the offline enterprise
posture (no telemetry, no onnxruntime, no compiler toolchain) and is fully
deterministic in tests. The interface mirrors a vector DB: upsert / search /
count.

Design:
- Storage: SQLite single table; embeddings serialized as JSON float arrays;
- Similarity: cosine similarity computed in Python, top-K by score, with a
  minimum threshold and an exclude-episode guard (an episode is never its own
  history);
- Embeddings: Ollama POST /api/embeddings (nomic-embed-text by default),
  injected as a plain ``embed_fn`` callable — the same SSRF-validated client
  used for chat, so no second trust boundary exists;
- Degradation (never crashes, never blocks analysis): any embedding failure
  marks the embedder down; while down, remember()/search() fail soft (return
  False / []) so callers fall back to keyword Jaccard; one successful embed
  after ``retry_after_seconds`` re-arms the embedder (self-healing).
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Union

logger = logging.getLogger("moray_aio.vector_memory")

# Embed fn contract: text -> dense vector; raised exceptions are treated as
# "embedding unavailable" (fail-soft, never propagate to the kernel)
EmbedFn = Callable[[str], List[float]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in pure Python; 0.0 for empty/zero vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        norm_a += fx * fx
        norm_b += fy * fy
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


class VectorStore:
    """SQLite-backed vector collection; thread-safe via a connection lock.

    One row per episode (upsert by id) so long-term memory stays bounded and
    an episode's conclusion can be refreshed when it is re-analyzed.
    """

    def __init__(self, db_path: Union[str, os.PathLike], max_items: int = 500):
        self.db_path = Path(db_path)
        parent = self.db_path.parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        self.max_items = max(1, int(max_items))
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vectors (
                        episode_id TEXT PRIMARY KEY,
                        embedding TEXT NOT NULL,
                        conclusion TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )

    def upsert(
        self,
        episode_id: str,
        embedding: Sequence[float],
        conclusion: str = "",
        created_at: Optional[str] = None,
    ) -> None:
        payload = json.dumps([float(v) for v in embedding])
        row = (str(episode_id), payload, str(conclusion or ""), created_at or _now())
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO vectors (episode_id, embedding, conclusion, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(episode_id) DO UPDATE SET
                        embedding = excluded.embedding,
                        conclusion = excluded.conclusion
                    """,
                    row,
                )
                # Bound growth: keep only the newest max_items episodes
                self._conn.execute(
                    """
                    DELETE FROM vectors WHERE episode_id NOT IN (
                        SELECT episode_id FROM vectors
                        ORDER BY created_at DESC, episode_id DESC
                        LIMIT ?
                    )
                    """,
                    (self.max_items,),
                )

    def rows(self) -> List[tuple]:
        """All rows as (episode_id, embedding_list, conclusion); newest first."""
        with self._lock:
            raw = self._conn.execute(
                "SELECT episode_id, embedding, conclusion FROM vectors "
                "ORDER BY created_at DESC, episode_id DESC"
            ).fetchall()
        out: List[tuple] = []
        for episode_id, embedding_json, conclusion in raw:
            try:
                vector = [float(v) for v in json.loads(embedding_json)]
            except (TypeError, ValueError):
                continue
            out.append((episode_id, vector, conclusion))
        return out

    def search(
        self,
        query_embedding: Sequence[float],
        top_k: int = 3,
        threshold: float = 0.35,
        exclude_episode_id: Optional[str] = None,
    ) -> List[dict]:
        scored: List[dict] = []
        for episode_id, vector, conclusion in self.rows():
            if exclude_episode_id and episode_id == exclude_episode_id:
                continue
            score = cosine_similarity(query_embedding, vector)
            if score >= float(threshold):
                scored.append(
                    {
                        "episode_id": episode_id,
                        "conclusion": conclusion,
                        "similarity": round(score, 4),
                    }
                )
        scored.sort(key=lambda item: item["similarity"], reverse=True)
        return scored[: max(0, int(top_k))]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class VectorMemory:
    """Semantic memory facade: embed -> store -> top-K cosine retrieval, with a
    hard fail-soft contract so the analysis kernel can never be broken by it.

    The embed_fn is injected (normally ``OllamaClient.embed`` with the
    embedding model); tests inject deterministic fakes.
    """

    def __init__(
        self,
        db_path: Union[str, os.PathLike],
        embed_fn: Optional[EmbedFn] = None,
        enabled: bool = True,
        top_k: int = 3,
        threshold: float = 0.35,
        max_items: int = 500,
        retry_after_seconds: float = 300.0,
        max_query_chars: int = 4000,
    ):
        self._store = VectorStore(db_path, max_items=max_items)
        self._embed_fn = embed_fn
        self.enabled = bool(enabled) and embed_fn is not None
        self.top_k = int(top_k)
        self.threshold = float(threshold)
        self.retry_after_seconds = float(retry_after_seconds)
        self.max_query_chars = int(max_query_chars)
        self._down_until = 0.0  # monotonic timestamp; 0 -> eligible now

    # ---- availability ----

    def is_available(self) -> bool:
        """True when enabled and the embedder is not in a failure backoff."""
        return self.enabled and time.monotonic() >= self._down_until

    def _mark_down(self, exc: Exception) -> None:
        self._down_until = time.monotonic() + self.retry_after_seconds
        logger.warning(
            "vector memory embedding unavailable (%s: %s); falling back to keyword retrieval "
            "for the next %.0fs",
            type(exc).__name__,
            exc,
            self.retry_after_seconds,
        )

    def _embed(self, text: str) -> Optional[List[float]]:
        """Embed one text; returns None (and enters backoff) on any failure."""
        if not self.is_available() or self._embed_fn is None:
            return None
        try:
            vector = self._embed_fn(text[: self.max_query_chars])
        except Exception as exc:  # noqa: BLE001 — fail-soft by contract
            self._mark_down(exc)
            return None
        if not isinstance(vector, list) or not vector:
            self._mark_down(ValueError("empty embedding vector"))
            return None
        self._down_until = 0.0  # success re-arms immediately (self-healing)
        return [float(v) for v in vector]

    # ---- public API (fail-soft) ----

    def remember(self, episode_id: str, text: str, conclusion: str) -> bool:
        """Vectorize (error features + conclusion) and upsert into the store.

        Returns True when stored; any embedding failure returns False without
        raising — the episode flow must never break on memory writes.
        """
        if not self.enabled or not str(text).strip():
            return False
        vector = self._embed(text)
        if vector is None:
            return False
        try:
            self._store.upsert(str(episode_id), vector, str(conclusion or ""))
        except Exception as exc:  # noqa: BLE001 — storage failure is also fail-soft
            logger.warning("vector memory upsert failed: %s: %s", type(exc).__name__, exc)
            return False
        logger.info(
            "vector memory stored episode=%s conclusion_chars=%d", episode_id, len(str(conclusion or ""))
        )
        return True

    def search(
        self, text: str, top_k: Optional[int] = None, exclude_episode_id: Optional[str] = None
    ) -> List[dict]:
        """Top-K most similar historical conclusions; [] when unavailable or
        empty (callers fall back to keyword Jaccard)."""
        if not self.enabled or not str(text).strip():
            return []
        vector = self._embed(text)
        if vector is None:
            return []
        try:
            return self._store.search(
                vector,
                top_k=self.top_k if top_k is None else top_k,
                threshold=self.threshold,
                exclude_episode_id=exclude_episode_id,
            )
        except Exception as exc:  # noqa: BLE001 — fail-soft by contract
            logger.warning("vector memory search failed: %s: %s", type(exc).__name__, exc)
            return []

    def count(self) -> int:
        try:
            return self._store.count()
        except Exception:  # noqa: BLE001
            return 0

    def close(self) -> None:
        try:
            self._store.close()
        except Exception:  # noqa: BLE001
            pass
