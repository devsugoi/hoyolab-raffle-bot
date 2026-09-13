"""SQLite persistence for seen HoYoLAB posts and bot settings."""

from __future__ import annotations

import asyncio
import sqlite3
import json
from dataclasses import asdict
from contextlib import contextmanager
from typing import Iterator
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

CREATE TABLE IF NOT EXISTS raffle_reminders (
    post_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    gids INTEGER NOT NULL,
    post_json TEXT NOT NULL,
    historical INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    end_at REAL,
    winner_at REAL,
    reminder_at REAL,
    reason TEXT,
    created_at REAL NOT NULL,
    sent_at REAL,
    destination TEXT,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    claim_token TEXT,
    lease_until REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS raffle_reminders_due ON raffle_reminders(status, reminder_at);
CREATE INDEX IF NOT EXISTS raffle_reminders_analysis ON raffle_reminders(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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

    async def reminder_query(self, sql: str, params=()):
        """Short SQLite transactions, also shared by standalone backfill."""
        def run():
            with self._connect() as conn:
                return [dict(row) for row in conn.execute(sql, params).fetchall()]
        return await asyncio.to_thread(run)

    async def enqueue_raffle(self, post: HoyolabPost, *, historical=False):
        payload = asdict(post)
        for key in ('created_at', 'end_at'):
            payload[key] = payload[key].isoformat() if payload[key] else None
        rows = await self.reminder_query(
            '''INSERT INTO raffle_reminders
               (post_id, title, url, gids, post_json, historical, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'analysis_pending', ?) ON CONFLICT(post_id) DO NOTHING RETURNING post_id''',
            (post.post_id, post.title, post.url, post.gids, json.dumps(payload), int(historical),
             datetime.now(timezone.utc).timestamp()))
        return bool(rows)

    async def reminder_exists(self, post_id):
        return bool(await self.reminder_query('SELECT 1 FROM raffle_reminders WHERE post_id=?', (post_id,)))

    async def claim_reminder(self, phase, now, token):
        status = 'analysis_pending' if phase == 'analysis' else 'pending'
        due = 'next_attempt_at' if phase == 'analysis' else 'reminder_at'
        rows = await self.reminder_query(
            f'''UPDATE raffle_reminders SET claim_token=?, lease_until=?
                WHERE post_id=(SELECT post_id FROM raffle_reminders
                    WHERE status=? AND {due}<=? AND next_attempt_at<=?
                    AND lease_until<=? ORDER BY {due}, post_id LIMIT 1)
                RETURNING *''', (token, now + 300, status, now, now, now))
        return rows[0] if rows else None

    async def finish_reminder(self, post_id, token, **values):
        allowed = {'status', 'end_at', 'winner_at', 'reminder_at', 'reason',
                   'sent_at', 'next_attempt_at', 'destination', 'attempts'}
        if not values or not set(values) <= allowed:
            raise ValueError('Invalid reminder update')
        assignments = ', '.join(f'{key}=?' for key in values)
        await self.reminder_query(
            f'''UPDATE raffle_reminders SET {assignments}, claim_token=NULL, lease_until=0
                WHERE post_id=? AND claim_token=?''', (*values.values(), post_id, token))
