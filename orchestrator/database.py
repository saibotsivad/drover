import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS containers (
    id              TEXT PRIMARY KEY,
    docker_id       TEXT,
    image           TEXT NOT NULL,
    privileged      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'running',
    socket_path     TEXT,
    label           TEXT,
    timeout_seconds INTEGER NOT NULL,
    last_seen       TEXT,
    created_at      TEXT NOT NULL,
    stopped_at      TEXT
);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def execute(
        self, sql: str, params: tuple = ()
    ) -> AsyncIterator[aiosqlite.Cursor]:
        assert self._conn is not None, "Database not connected"
        cursor = await self._conn.execute(sql, params)
        try:
            yield cursor
        finally:
            await self._conn.commit()

    async def execute_insert(self, sql: str, params: tuple = ()) -> None:
        assert self._conn is not None, "Database not connected"
        await self._conn.execute(sql, params)
        await self._conn.commit()

    async def fetchone(
        self, sql: str, params: tuple = ()
    ) -> aiosqlite.Row | None:
        assert self._conn is not None, "Database not connected"
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(
        self, sql: str, params: tuple = ()
    ) -> list[aiosqlite.Row]:
        assert self._conn is not None, "Database not connected"
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchall()
