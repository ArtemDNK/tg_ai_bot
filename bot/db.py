from __future__ import annotations

import json
import time
from dataclasses import dataclass

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    provider      TEXT,
    model         TEXT,
    system_prompt TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    role     TEXT NOT NULL,
    parts    TEXT NOT NULL,
    created  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_user ON messages(user_id, id);
CREATE TABLE IF NOT EXISTS access (
    user_id  INTEGER PRIMARY KEY,
    role     TEXT NOT NULL,      -- 'user' | 'admin' | 'blocked'
    name     TEXT,
    added_by INTEGER,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    key      TEXT NOT NULL,
    name     TEXT,
    rpm      INTEGER NOT NULL DEFAULT 0,
    rpd      INTEGER NOT NULL DEFAULT 0,
    tpd      INTEGER NOT NULL DEFAULT 0,
    added_by INTEGER,
    added_at REAL NOT NULL,
    UNIQUE (provider, key)
);
CREATE TABLE IF NOT EXISTS usage (
    day      TEXT NOT NULL,
    scope    TEXT NOT NULL,      -- 'key' | 'user'
    ident    TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    tokens   INTEGER NOT NULL DEFAULT 0,
    images   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, scope, ident)
);
"""


@dataclass
class UserSettings:
    provider: str | None
    model: str | None
    system_prompt: str | None


@dataclass
class Usage:
    requests: int = 0
    tokens: int = 0
    images: int = 0


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ── пользователи ──────────────────────────────────────────
    async def get_user(self, user_id: int) -> UserSettings:
        async with self.conn.execute(
            "SELECT provider, model, system_prompt FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return UserSettings(*row) if row else UserSettings(None, None, None)

    async def set_user(self, user_id: int, **fields) -> None:
        await self.conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))
        for k, v in fields.items():
            if k not in {"provider", "model", "system_prompt"}:
                raise ValueError(k)
            await self.conn.execute(f"UPDATE users SET {k}=? WHERE user_id=?", (v, user_id))
        await self.conn.commit()

    # ── история ───────────────────────────────────────────────
    async def add_message(self, user_id: int, role: str, parts: list[dict]) -> None:
        await self.conn.execute(
            "INSERT INTO messages(user_id, role, parts, created) VALUES (?,?,?,?)",
            (user_id, role, json.dumps(parts, ensure_ascii=False), time.time()),
        )
        await self.conn.commit()

    async def get_history(self, user_id: int, limit: int) -> list[dict]:
        async with self.conn.execute(
            "SELECT role, parts FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [{"role": r, "parts": json.loads(p)} for r, p in reversed(rows)]

    async def all_file_paths(self, user_id: int) -> list[str]:
        async with self.conn.execute("SELECT parts FROM messages WHERE user_id=?", (user_id,)) as cur:
            rows = await cur.fetchall()
        paths = []
        for (p,) in rows:
            paths += [x["path"] for x in json.loads(p) if x.get("path")]
        return paths

    async def clear_history(self, user_id: int) -> None:
        await self.conn.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
        await self.conn.commit()

    async def drop_last_user_message(self, user_id: int) -> None:
        """Удаляет висящее сообщение пользователя, если ответа на него не случилось."""
        await self.conn.execute(
            "DELETE FROM messages WHERE id = (SELECT id FROM messages WHERE user_id=? "
            "ORDER BY id DESC LIMIT 1) AND role='user'",
            (user_id,),
        )
        await self.conn.commit()

    # ── доступ ────────────────────────────────────────────────
    async def list_access(self) -> list[tuple[int, str, str | None]]:
        async with self.conn.execute("SELECT user_id, role, name FROM access ORDER BY role, added_at") as cur:
            return [tuple(r) for r in await cur.fetchall()]

    async def set_access(self, user_id: int, role: str, name: str | None, added_by: int | None) -> None:
        await self.conn.execute(
            "INSERT INTO access(user_id, role, name, added_by, added_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, "
            "name=COALESCE(excluded.name, access.name), added_by=excluded.added_by, added_at=excluded.added_at",
            (user_id, role, name, added_by, time.time()),
        )
        await self.conn.commit()

    async def delete_access(self, user_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM access WHERE user_id=?", (user_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    # ── API-ключи, добавленные через бота ─────────────────────
    async def list_keys(self) -> list[dict]:
        async with self.conn.execute(
            "SELECT id, provider, key, name, rpm, rpd, tpd FROM api_keys ORDER BY provider, id"
        ) as cur:
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]

    async def add_key(self, provider: str, key: str, name: str | None,
                      rpm: int, rpd: int, tpd: int, added_by: int) -> bool:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO api_keys(provider, key, name, rpm, rpd, tpd, added_by, added_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (provider, key, name, rpm, rpd, tpd, added_by, time.time()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def update_key_limits(self, key_id: int, rpm: int, rpd: int, tpd: int) -> None:
        await self.conn.execute("UPDATE api_keys SET rpm=?, rpd=?, tpd=? WHERE id=?", (rpm, rpd, tpd, key_id))
        await self.conn.commit()

    async def delete_key(self, key_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    # ── учёт использования ────────────────────────────────────
    async def add_usage(self, day: str, scope: str, ident: str,
                        requests: int = 0, tokens: int = 0, images: int = 0) -> None:
        await self.conn.execute(
            "INSERT INTO usage(day, scope, ident, requests, tokens, images) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(day, scope, ident) DO UPDATE SET "
            "requests=requests+excluded.requests, tokens=tokens+excluded.tokens, images=images+excluded.images",
            (day, scope, ident, requests, tokens, images),
        )
        await self.conn.commit()

    async def get_usage(self, day: str, scope: str, ident: str) -> Usage:
        async with self.conn.execute(
            "SELECT requests, tokens, images FROM usage WHERE day=? AND scope=? AND ident=?",
            (day, scope, ident),
        ) as cur:
            row = await cur.fetchone()
        return Usage(*row) if row else Usage()
