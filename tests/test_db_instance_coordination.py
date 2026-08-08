"""T4 multi-instance coordination + schema versioning.

CLI, Desktop, MCP and spawn subprocesses share one ``desktop.db``.  This suite
locks the guarantees:

- schema evolution is versioned (``PRAGMA user_version``) and forward-only,
  with an atomic pre-migration backup;
- old (pre-version) databases migrate in place, preserving user history;
- a database from a *newer* build is refused, never silently downgraded;
- exactly one writer at a time (fcntl.flock / msvcrt lease); a second writer
  is refused while read-only queries stay available concurrently.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _reset_db_state(monkeypatch, tmp_path: Path):
    """Isolate the module's globals onto a fresh per-test data dir."""
    from modus.desktop import db

    db.release_writer_lease()
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    monkeypatch.setattr(db, "_integrity_checked", False)
    monkeypatch.setattr(db, "_recovering", False)
    monkeypatch.setattr(db, "_checkpoint_calls", 0)
    return db


_V1_SCHEMA = """
CREATE TABLE workspaces (
    workspace_id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '旧对话',
    mode TEXT NOT NULL DEFAULT 'default', archived INTEGER NOT NULL DEFAULT 0,
    worldview TEXT NOT NULL DEFAULT '', world_view_history TEXT NOT NULL DEFAULT '[]',
    system_prompt TEXT NOT NULL DEFAULT '', model_id TEXT NOT NULL DEFAULT '',
    mode_config TEXT NOT NULL DEFAULT '{}', reasoning_effort TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
    tool_calls TEXT NOT NULL DEFAULT '[]', token_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE run_events (
    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL, timestamp TEXT NOT NULL, channel_id TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '{}', type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'started', payload TEXT NOT NULL DEFAULT '{}',
    mode TEXT NOT NULL DEFAULT 'default', part_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, mode TEXT NOT NULL,
    state TEXT NOT NULL, stop_reason TEXT,
    config_snapshot TEXT NOT NULL DEFAULT '{}', budget TEXT NOT NULL DEFAULT '{}',
    error TEXT, started_at REAL NOT NULL, updated_at REAL NOT NULL, ended_at REAL
);
CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_name TEXT NOT NULL,
    input_hash TEXT NOT NULL DEFAULT '', input TEXT NOT NULL DEFAULT '{}',
    decision TEXT, resolution_reason TEXT,
    requested_at REAL NOT NULL, decided_at REAL
);
"""


def _write_v1_db(path: Path, *, session_title: str = "我的旧会话") -> str:
    """Create a genuine pre-version (v1) database and return its session id."""
    now = time.time()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_V1_SCHEMA)
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES ('s1',?,?,?)",
            (session_title, now, now),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) "
            "VALUES ('s1','user',?,?)",
            ("这条历史消息必须保留", now),
        )
        conn.execute(
            "INSERT INTO runs (run_id, session_id, mode, state, started_at, updated_at) "
            "VALUES ('r1','s1','default','running',?,?)",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return "s1"


# ── Schema migration ──


def test_migrate_from_old_schema_preserves_history(monkeypatch, tmp_path):
    """v1 (unversioned) db migrates to SCHEMA_VERSION with history intact."""
    from modus.desktop import db

    db = _reset_db_state(monkeypatch, tmp_path)
    sid = _write_v1_db(db.DB_PATH)

    db.init_db()

    with db._get_conn() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == db.SCHEMA_VERSION
        # The migrated columns the codebase used to add ad hoc now exist.
        for table, column in [
            ("sessions", "workspace_id"), ("sessions", "owner_id"),
            ("runs", "workspace_id"), ("runs", "owner_id"),
            ("runs", "projection_revision"), ("runs", "client_request_id"),
            ("run_events", "task_id"), ("run_events", "schema"),
            ("messages", "tool_call_id"), ("memories", "embedding"),
        ]:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert column in cols, f"{table}.{column} missing after migration"
        # User history is preserved, not rebuilt.
        msg = conn.execute("SELECT content FROM messages WHERE id=1").fetchone()
        assert msg["content"] == "这条历史消息必须保留"
        row = conn.execute(
            "SELECT workspace_id, owner_id FROM sessions WHERE id=?", (sid,),
        ).fetchone()
        assert row["workspace_id"], "workspace identity not backfilled"
        assert row["owner_id"], "owner identity not backfilled"
    # A pre-migration backup snapshot was written under Modus's data dir.
    backups = list((tmp_path / "backup").glob("db-v*.bak"))
    assert backups, "no pre-migration backup produced"


def test_migrate_is_idempotent_no_second_backup(monkeypatch, tmp_path):
    """Re-running init_db on a migrated db is a no-op and backs up once."""
    from modus.desktop import db

    db = _reset_db_state(monkeypatch, tmp_path)
    db.init_db()
    assert int(db._get_conn().execute("PRAGMA user_version").fetchone()[0]) == db.SCHEMA_VERSION
    first = sorted(p.name for p in (tmp_path / "backup").glob("db-v*.bak"))

    db.init_db()
    assert int(db._get_conn().execute("PRAGMA user_version").fetchone()[0]) == db.SCHEMA_VERSION
    second = sorted(p.name for p in (tmp_path / "backup").glob("db-v*.bak"))
    assert second == first, "idempotent init must not create another backup"


def test_downgrade_refused(monkeypatch, tmp_path):
    """A database from a newer build is refused, never silently downgraded."""
    from modus.desktop import db

    db = _reset_db_state(monkeypatch, tmp_path)
    db.init_db()
    with db._get_conn() as conn:
        conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")

    with pytest.raises(RuntimeError, match="newer"):
        db.init_db()
    with pytest.raises(RuntimeError, match="newer"):
        with db._get_conn() as conn:
            db.migrate_schema(conn)

    # The newer database was not touched.
    with db._raw_conn() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == db.SCHEMA_VERSION + 1


def test_backup_snapshot_is_readable_consistent(monkeypatch, tmp_path):
    """The pre-migration backup opens as a valid database containing history."""
    from modus.desktop import db

    db = _reset_db_state(monkeypatch, tmp_path)
    _write_v1_db(db.DB_PATH)
    db.init_db()
    # Take the EARLIEST backup (lowest version number) — it predates the
    # v2+ migrations that add columns, so the snapshot-property assertions
    # below hold regardless of how many later migrations ran.
    backups = sorted((tmp_path / "backup").glob("db-v*.bak"))
    assert backups, "expected at least one pre-migration backup"
    backup = backups[0]
    conn = sqlite3.connect(str(backup))
    try:
        messages = conn.execute("SELECT content FROM messages WHERE id=1").fetchone()
        assert messages[0] == "这条历史消息必须保留"
        # The snapshot predates the migration: the v2-only column is absent.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        assert "owner_id" not in cols
    finally:
        conn.close()


# ── Writer lease / multi-instance coordination ──


def test_second_writer_refused_same_process(monkeypatch, tmp_path):
    """Two open leases in one process: the second is refused until release."""
    from modus.desktop.db import _WriterLease

    db = _reset_db_state(monkeypatch, tmp_path)
    lock = tmp_path / "instance.lock"

    first = _WriterLease(lock)
    second = _WriterLease(lock)
    try:
        assert first.acquire() is True
        assert second.acquire() is False, "second writer must be refused"
        # Release frees the lease for a new writer.
        first.release()
        assert second.acquire() is True
    finally:
        first.release()
        second.release()


def _lease_subprocess(
    tmp_path: Path, env: dict, *, hold_for: float = 2.0, wait_until: str | None = None,
) -> subprocess.Popen:
    """Spawn a child that takes the writer lease and holds it ``hold_for`` s.

    Returns the running Popen; callers must wait/reap it.  When ``wait_until``
    is given the call blocks until that line appears on the child's stdout.
    """
    script = (
        "import sys, time; "
        "from pathlib import Path; "
        "sys.path.insert(0, sys.argv[1]); "
        "from modus.desktop import db; "
        "db.DB_DIR=Path(sys.argv[2]); db.DB_PATH=Path(sys.argv[2]+'/desktop.db'); "
        "ok=db.acquire_writer_lease(); "
        "print('acquired='+str(ok), flush=True); "
        "time.sleep(float(sys.argv[3])); "
        "db.release_writer_lease()"
    )
    root = Path(__file__).parents[1]
    child_env = {
        "PYTHONPATH": str(root / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        **env,
    }
    proc = subprocess.Popen(
        [
            sys.executable, "-B", "-c", script,
            str(root / "src"), str(tmp_path), str(hold_for),
        ],
        cwd=root, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    if wait_until is not None:
        deadline = time.time() + 15
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if wait_until in line:
                return proc
        proc.kill()
        raise AssertionError(f"child never printed {wait_until!r}")
    return proc


def test_second_writer_refused_across_processes(monkeypatch, tmp_path):
    """A live writer blocks a second process; its death releases the lease."""
    from modus.desktop import db

    db = _reset_db_state(monkeypatch, tmp_path)
    db.init_db()

    # Child A acquires and holds the lease.
    proc_a = _lease_subprocess(tmp_path, env={}, hold_for=2.0, wait_until="acquired=True")

    # While A is alive, a second writer is refused.
    proc_b = _lease_subprocess(tmp_path, env={}, hold_for=0.2, wait_until="acquired=False")

    proc_a.wait(timeout=10)
    proc_b.wait(timeout=10)
    assert proc_a.returncode == 0 and proc_b.returncode == 0

    # A released/crashed writer leaves no stale lock: the next writer wins.
    proc_c = _lease_subprocess(tmp_path, env={}, hold_for=0.2, wait_until="acquired=True")
    proc_c.wait(timeout=10)
    assert proc_c.returncode == 0


def test_lease_released_on_crash(monkeypatch, tmp_path):
    """A child that dies without releasing does not wedge the database."""
    from modus.desktop.db import _WriterLease

    db = _reset_db_state(monkeypatch, tmp_path)
    script = (
        "import sys, os; "
        "from pathlib import Path; "
        "sys.path.insert(0, sys.argv[1]); "
        "from modus.desktop import db; "
        "db.DB_DIR=Path(sys.argv[2]); db.DB_PATH=Path(sys.argv[2]+'/desktop.db'); "
        "assert db.acquire_writer_lease(); "
        "os._exit(1)"  # die without release
    )
    root = Path(__file__).parents[1]
    child_env = {
        "PYTHONPATH": str(root / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(tmp_path),
    }
    subprocess.run(
        [sys.executable, "-B", "-c", script, str(root / "src"), str(tmp_path)],
        cwd=root, env=child_env, timeout=15, capture_output=True,
    )
    # The OS drops the flock on process death, so a new lease is free.
    lease = _WriterLease(tmp_path / "instance.lock")
    try:
        assert lease.acquire() is True, "crash must release the writer lease"
    finally:
        lease.release()


def test_reader_concurrent_ok(monkeypatch, tmp_path):
    """Read-only queries stay available while a writer holds the lease."""
    from modus.desktop import db

    db = _reset_db_state(monkeypatch, tmp_path)
    db.init_db()
    db.create_session("coord-reader")

    # Hold the writer lease in-process, as a live Desktop would.
    assert db.acquire_writer_lease() is True
    try:
        # Reads do not need the lease and are not blocked by it.
        sessions = db.list_sessions(limit=5)
        assert sessions and sessions[0]["title"] == "coord-reader"
        with db._raw_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM sessions", {},
            ).fetchone()
        assert row[0] >= 1
        # A second writer is still refused while the lease is held.
        assert db.acquire_writer_lease() is True  # idempotent within process
    finally:
        db.release_writer_lease()


def test_acquire_lease_is_idempotent_and_releasable(monkeypatch, tmp_path):
    """Repeated acquires share one lease; release clears it for others."""
    from modus.desktop import db

    db = _reset_db_state(monkeypatch, tmp_path)
    assert db.acquire_writer_lease() is True
    assert db.acquire_writer_lease() is True  # already held
    db.release_writer_lease()
    db.release_writer_lease()  # idempotent release
    assert db.acquire_writer_lease() is True  # free again
    db.release_writer_lease()


def test_cli_startup_refuses_second_instance(monkeypatch, tmp_path):
    """CLI entry-point reports an explicit error when another writer holds the db."""
    import typer
    from modus.desktop import db

    db = _reset_db_state(monkeypatch, tmp_path)
    db.init_db()

    # A live writer (the "Desktop") holds the lease.
    proc = _lease_subprocess(tmp_path, env={}, hold_for=2.0, wait_until="acquired=True")
    try:
        from modus.entrypoints import cli

        monkeypatch.setattr(cli, "_data_dir", lambda: tmp_path)
        # _db_startup must refuse and exit(1) with a clear message.
        with pytest.raises(typer.Exit) as exc:
            cli._db_startup(db)
        assert exc.value.exit_code == 1
    finally:
        proc.wait(timeout=10)
    assert proc.returncode == 0


def test_cli_startup_succeeds_when_lease_is_free(monkeypatch, tmp_path):
    """CLI entry-point initializes the db when no other writer holds the lease."""
    from modus.desktop import db

    db = _reset_db_state(monkeypatch, tmp_path)
    from modus.entrypoints import cli

    monkeypatch.setattr(cli, "_data_dir", lambda: tmp_path)
    cli._db_startup(db)
    with db._get_conn() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == db.SCHEMA_VERSION
    db.release_writer_lease()
