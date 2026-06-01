import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS workers (
    id              TEXT PRIMARY KEY,
    docker_id       TEXT,
    image           TEXT NOT NULL,
    privileged      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'initializing',
    socket_path     TEXT,
    label           TEXT,
    timeout_seconds INTEGER NOT NULL,
    last_seen       TEXT,
    created_at      TEXT NOT NULL,
    stopped_at      TEXT,
    error_code      TEXT
);

CREATE TABLE IF NOT EXISTS commands (
    id              TEXT PRIMARY KEY,
    worker_id       TEXT NOT NULL,
    command         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    exit_code       INTEGER,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (worker_id) REFERENCES workers(id)
);

CREATE INDEX IF NOT EXISTS idx_commands_worker_id
    ON commands(worker_id);

CREATE TABLE IF NOT EXISTS command_messages (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id      TEXT NOT NULL,
    stream          TEXT NOT NULL,
    data            TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    FOREIGN KEY (command_id) REFERENCES commands(id)
);

CREATE INDEX IF NOT EXISTS idx_command_messages_command_id
    ON command_messages(command_id);
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
