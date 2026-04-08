from datetime import datetime, timezone

import pytest


async def test_connect_creates_schema(db):
    """Schema tables should exist after connect."""
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = {row["name"] for row in rows}
    assert "containers" in names
    assert "commands" in names
    assert "command_messages" in names


async def test_insert_and_fetch_container(db):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_insert(
        """INSERT INTO containers
           (id, docker_id, image, privileged, status, socket_path,
            label, timeout_seconds, last_seen, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("C001", "docker_abc", "python-runner", 0, "running", "/tmp/s.sock",
         "test", 300, now, now),
    )
    row = await db.fetchone("SELECT * FROM containers WHERE id = ?", ("C001",))
    assert row is not None
    assert row["id"] == "C001"
    assert row["image"] == "python-runner"
    assert row["status"] == "running"
    assert row["privileged"] == 0
    assert row["timeout_seconds"] == 300


async def test_insert_and_fetch_command(db):
    now = datetime.now(timezone.utc).isoformat()
    # Need a container first (foreign key)
    await db.execute_insert(
        """INSERT INTO containers
           (id, docker_id, image, privileged, status, timeout_seconds, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("C002", "docker_def", "node", 0, "running", 300, now),
    )
    await db.execute_insert(
        """INSERT INTO commands (id, container_id, command, status, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        ("CMD01", "C002", "echo hello", "pending", now),
    )
    row = await db.fetchone("SELECT * FROM commands WHERE id = ?", ("CMD01",))
    assert row is not None
    assert row["container_id"] == "C002"
    assert row["command"] == "echo hello"
    assert row["status"] == "pending"
    assert row["exit_code"] is None


async def test_insert_and_fetch_command_messages(db):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_insert(
        """INSERT INTO containers
           (id, docker_id, image, privileged, status, timeout_seconds, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("C003", "docker_ghi", "test", 0, "running", 300, now),
    )
    await db.execute_insert(
        """INSERT INTO commands (id, container_id, command, status, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        ("CMD02", "C003", "ls", "running", now),
    )
    await db.execute_insert(
        """INSERT INTO command_messages (command_id, stream, data, received_at)
           VALUES (?, ?, ?, ?)""",
        ("CMD02", "stdout", "file1.txt\n", now),
    )
    await db.execute_insert(
        """INSERT INTO command_messages (command_id, stream, data, received_at)
           VALUES (?, ?, ?, ?)""",
        ("CMD02", "stderr", "warning\n", now),
    )
    rows = await db.fetchall(
        "SELECT * FROM command_messages WHERE command_id = ? ORDER BY seq",
        ("CMD02",),
    )
    assert len(rows) == 2
    assert rows[0]["stream"] == "stdout"
    assert rows[1]["stream"] == "stderr"


async def test_update_container_status(db):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_insert(
        """INSERT INTO containers
           (id, docker_id, image, privileged, status, timeout_seconds, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("C004", "docker_jkl", "test", 0, "running", 300, now),
    )
    await db.execute_insert(
        "UPDATE containers SET status = 'stopped' WHERE id = ?",
        ("C004",),
    )
    row = await db.fetchone("SELECT status FROM containers WHERE id = ?", ("C004",))
    assert row["status"] == "stopped"


async def test_fetchall_empty(db):
    rows = await db.fetchall("SELECT * FROM containers WHERE id = 'nonexistent'")
    assert rows == []


async def test_fetchone_missing(db):
    row = await db.fetchone("SELECT * FROM containers WHERE id = 'nonexistent'")
    assert row is None


async def test_execute_context_manager(db):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute_insert(
        """INSERT INTO containers
           (id, docker_id, image, privileged, status, timeout_seconds, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("C005", "docker_mno", "test", 0, "running", 300, now),
    )
    async with db.execute(
        "SELECT * FROM containers WHERE id = ?", ("C005",)
    ) as cursor:
        row = await cursor.fetchone()
        assert row["id"] == "C005"
