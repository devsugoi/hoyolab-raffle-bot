"""SQLite persistence for seen HoYoLAB posts and bot settings."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from utils.api import HoyolabPost

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_posts (
    post_id TEXT PRIMARY KEY,
    gids INTEGER,
    title TEXT,
    url TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def _init() -> None:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)

        await asyncio.to_thread(_init)

    async def has_any_seen(self) -> bool:
        def _query() -> bool:
            with self._connect() as conn:
                row = conn.execute("SELECT 1 FROM seen_posts LIMIT 1").fetchone()
                return row is not None

        return await asyncio.to_thread(_query)

    async def seen_game_ids(self) -> set[int]:
        def _query() -> set[int]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT gids FROM seen_posts WHERE gids IS NOT NULL"
                ).fetchall()
                return {int(row["gids"]) for row in rows}

        return await asyncio.to_thread(_query)

    async def is_seen(self, post_id: str) -> bool:
        def _query() -> bool:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM seen_posts WHERE post_id = ?",
                    (post_id,),
                ).fetchone()
                return row is not None

        return await asyncio.to_thread(_query)

    async def mark_seen(self, post: HoyolabPost, notified: bool) -> None:
        seen_at = datetime.now(timezone.utc).isoformat()

        def _write() -> None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO seen_posts (post_id, gids, title, url, notified, seen_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(post_id) DO UPDATE SET
                        gids = excluded.gids,
                        title = excluded.title,
                        url = excluded.url,
                        notified = MAX(seen_posts.notified, excluded.notified),
                        seen_at = excluded.seen_at
                    """,
                    (
                        post.post_id,
                        post.gids,
                        post.title,
                        post.url,
                        int(notified),
                        seen_at,
                    ),
                )

        await asyncio.to_thread(_write)

    async def mark_many_seen(self, posts: list[HoyolabPost], notified: bool) -> None:
        if not posts:
            return
        seen_at = datetime.now(timezone.utc).isoformat()

        def _write() -> None:
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO seen_posts
                        (post_id, gids, title, url, notified, seen_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            post.post_id,
                            post.gids,
                            post.title,
                            post.url,
                            int(notified),
                            seen_at,
                        )
                        for post in posts
                    ],
                )

        await asyncio.to_thread(_write)

    async def tracked_count(self) -> int:
        def _query() -> int:
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(*) AS n FROM seen_posts").fetchone()
                return int(row["n"] if row else 0)

        return await asyncio.to_thread(_query)

    async def get_setting(self, key: str) -> str | None:
        def _query() -> str | None:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    return None
                return str(row["value"])

        return await asyncio.to_thread(_query)

    async def set_setting(self, key: str, value: str) -> None:
        def _write() -> None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO settings (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )

        await asyncio.to_thread(_write)
