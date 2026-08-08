"""T2 data-plane governance: audit rotation/degradation, retention pruning,
corruption recovery, and snapshot pruning.

Every deletion is bounded to Modus's own data directory and gated behind
``storage.enable_prune`` (report-only by default, conservative and reversible).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from modus.config import ModusConfig, StorageConfig


# ── Audit log rotation + degradation ──


def test_audit_rotate_splits_file(tmp_path):
    from modus.policy.audit_log import AuditLog

    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, rotate_bytes=500, rotate_keep=3)
    for i in range(60):
        log.record(
            tool_name="echo", input_data={"n": i, "x": "y" * 20},
            outcome="ok", approver="auto", cwd=str(tmp_path),
        )
    assert path.exists()
    assert (tmp_path / "audit-1.jsonl").exists()
    # At most 3 rotated copies are kept.
    copies = [p for p in tmp_path.glob("audit-*.jsonl")]
    assert len(copies) <= 3
    # The most recent event always made it to the active file.
    tail = log.tail()
    assert tail and tail[-1]["tool_name"] == "echo"


def test_audit_rotate_shifts_copies(tmp_path):
    from modus.policy.audit_log import AuditLog

    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, rotate_bytes=200, rotate_keep=2)
    for i in range(40):
        log.record(
            tool_name="echo", input_data={"n": i, "x": "z" * 15},
            outcome="ok", approver="auto", cwd=str(tmp_path),
        )
    # Keep=2 => active + at most audit-1 + audit-2. Copies must be contiguous
    # suffixes (1..N), never with gaps.
    copies = sorted(
        int(p.stem.rsplit("-", 1)[1]) for p in tmp_path.glob("audit-*.jsonl")
    )
    assert len(copies) <= 2
    assert copies == list(range(1, len(copies) + 1))


def test_audit_write_fails_degrades(tmp_path, monkeypatch):
    from modus.policy.audit_log import AuditLog

    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)

    def _explode(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(AuditLog, "_append", _explode)
    # A failed write must NOT raise.
    log.record(
        tool_name="echo", input_data={"x": 1}, outcome="ok",
        approver="auto", cwd=str(tmp_path),
    )
    assert log.degraded is True
    # The event survives in the in-memory ring for observability.
    assert len(log.degraded_memory) == 1
    assert log.degraded_memory[0]["tool_name"] == "echo"


def test_audit_tail_reads_written_events(tmp_path):
    from modus.policy.audit_log import AuditLog

    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, rotate_bytes=10_000_000)
    log.record(
        tool_name="ls", input_data={"path": "/tmp"}, outcome="ok",
        approver="human", cwd=str(tmp_path),
    )
    events = log.tail()
    assert len(events) == 1
    assert events[0]["tool_name"] == "ls"


# ── Retention pruning (report-only by default) ──


def _fresh_db(tmp_path, monkeypatch) -> Path:
    import modus.desktop.db as db

    db_path = tmp_path / "desktop.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "_integrity_checked", False)
    db.init_db()
    return db_path


@pytest.fixture
def storage_config(tmp_path, monkeypatch):
    """A DB isolated to tmp_path plus a per-call config."""
    import modus.desktop.db as db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "_integrity_checked", False)
    return ModusConfig()


def _seed_run_events(db_path: Path, n: int = 3, days_old: int = 500) -> list[str]:
    import modus.desktop.db as db

    from modus.desktop.db import create_session, create_run, upsert_run_event

    session_id = create_session(title="prune-seed", mode="default")["id"]
    with db._raw_conn() as conn:
        conn.execute("UPDATE sessions SET created_at=? WHERE id=?", (1.0, session_id))
    run_id = f"run-{days_old}"
    create_run(run_id, session_id, "default")
    ids: list[str] = []
    from modus.desktop.events import Actor, ChannelId, EventType

    for i in range(n):
        event_id = f"evt-{run_id}-{i}"
        upsert_run_event(
            session_id,
            {
                "event_id": event_id, "run_id": run_id, "session_id": session_id,
                "sequence": i, "channel_id": ChannelId.USER_HOST.value,
                "timestamp": _iso_old(days_old), "actor": Actor.system().to_wire(),
                "type": EventType.USER_MESSAGE.value, "status": "completed",
                "payload": {}, "mode": "default",
                "schema": "modus.agent-event.v2", "revision": 0,
            },
        )
        ids.append(event_id)
    return ids


def _iso_old(days: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def test_prune_expired_reports_only(tmp_path, monkeypatch):
    """Default (enable_prune=False) reports candidates and deletes nothing."""
    import modus.desktop.db as db

    _fresh_db(tmp_path, monkeypatch)
    event_ids = _seed_run_events(db.DB_PATH, n=2, days_old=500)
    # Also add one recent event that must NOT be a candidate.
    from modus.desktop.db import create_session, create_run, upsert_run_event
    from modus.desktop.events import Actor, ChannelId, EventType

    session_id = create_session(title="recent", mode="default")["id"]
    create_run("run-recent", session_id, "default")
    upsert_run_event(
        session_id,
        {
            "event_id": "evt-recent", "run_id": "run-recent", "session_id": session_id,
            "sequence": 0, "channel_id": ChannelId.USER_HOST.value,
            "timestamp": _iso_old(1), "actor": Actor.system().to_wire(),
            "type": EventType.USER_MESSAGE.value, "status": "completed",
            "payload": {}, "mode": "default",
            "schema": "modus.agent-event.v2", "revision": 0,
        },
    )

    report = db.prune_expired(config=ModusConfig())
    assert report["run_events_candidates"] == len(event_ids)
    assert report["run_events_deleted"] == 0
    # Nothing was deleted (report-only).
    with db._raw_conn() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
    assert remaining == len(event_ids) + 1


def test_prune_expired_deletes_when_enabled(tmp_path, monkeypatch):
    import modus.desktop.db as db

    _fresh_db(tmp_path, monkeypatch)
    event_ids = _seed_run_events(db.DB_PATH, n=2, days_old=500)

    config = ModusConfig()
    config.storage.enable_prune = True
    report = db.prune_expired(config=config)

    assert report["run_events_deleted"] == len(event_ids)
    with db._raw_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0] == 0


def test_prune_archives_old_memories_never_deletes(tmp_path, monkeypatch):
    import modus.desktop.db as db

    _fresh_db(tmp_path, monkeypatch)
    from modus.desktop.db import add_memory_record, create_session, list_memories

    session_id = create_session(title="mem", mode="default")["id"]
    memory = add_memory_record(
        session_id=session_id, scope="session", category="general",
        content="old memory", source_ids=[], reference_only=True,
    )
    memory_id = memory["memory_id"]
    with db._raw_conn() as conn:
        conn.execute("UPDATE memories SET updated_at=? WHERE memory_id=?", (1.0, memory_id))

    report = db.prune_expired(config=ModusConfig())
    assert report["memories_soft_expire_candidates"] == 1

    config = ModusConfig()
    config.storage.enable_prune = True
    report = db.prune_expired(config=config)
    assert report["memories_archived"] == 1
    # Soft-expire archives, never deletes.
    with db._raw_conn() as conn:
        row = conn.execute("SELECT status FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
    assert row["status"] == "archived"
    assert list_memories(session_id) == []


def test_prune_expired_idempotent(tmp_path, monkeypatch):
    import modus.desktop.db as db

    _fresh_db(tmp_path, monkeypatch)
    _seed_run_events(db.DB_PATH, n=2, days_old=500)
    config = ModusConfig()
    config.storage.enable_prune = True
    first = db.prune_expired(config=config)
    second = db.prune_expired(config=config)
    assert first["run_events_deleted"] == 2
    assert second["run_events_deleted"] == 0
    assert second["run_events_candidates"] == 0


# ── Corruption recovery ──


def _corrupt_db(db_path: Path) -> None:
    """Flip bytes in a middle page so quick_check fails but most tables read."""
    data = bytearray(db_path.read_bytes())
    page = 4096
    offset = 6 * page
    if offset < len(data):
        data[offset:offset + 100] = b"\xde\xad\xbe\xef" * 25
    db_path.write_bytes(bytes(data))


def test_quick_check_corrupt_recovery(tmp_path, monkeypatch):
    """Corrupt DB -> backed up -> reopened fresh -> schema restored."""
    import modus.desktop.db as db

    _fresh_db(tmp_path, monkeypatch)
    from modus.desktop.db import create_session, create_run

    session_id = create_session(title="recover", mode="default")["id"]
    create_run("run-corrupt", session_id, "default")
    # Checkpoint so the data lives in the main file (not the WAL).
    with db._raw_conn() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    _corrupt_db(db.DB_PATH)

    # Simulate a fresh process opening the DB: quick_check runs at startup.
    db._integrity_checked = False
    # Any connection triggers recovery.
    conn = db._get_conn()
    try:
        # The reopened DB is healthy.
        rows = conn.execute("PRAGMA quick_check").fetchall()
        assert str(rows[0][0]).lower() == "ok"
    finally:
        conn.close()

    # Corrupt db was moved aside and backed up under Modus's own directory.
    assert list(tmp_path.glob("desktop.db.corrupt.*"))
    assert (tmp_path / "backup").is_dir()
    assert list((tmp_path / "backup").glob("desktop.db.corrupt.*"))

    # The fresh DB has the schema and the salvaged session/run.
    with db._raw_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 1


def test_recover_damaged_run_ledger_settles_running(tmp_path, monkeypatch):
    """A run stuck 'running' in a damaged DB settles to interrupted."""
    import modus.desktop.db as db

    _fresh_db(tmp_path, monkeypatch)
    from modus.desktop.db import create_session, create_run

    session_id = create_session(title="rec", mode="default")["id"]
    create_run("run-live", session_id, "default")
    with db._raw_conn() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert conn.execute(
            "SELECT state FROM runs WHERE run_id='run-live'"
        ).fetchone()["state"] == "running"

    _corrupt_db(db.DB_PATH)

    db._integrity_checked = False
    db._get_conn()  # triggers recovery + replay
    with db._raw_conn() as conn:
        row = conn.execute(
            "SELECT state, stop_reason FROM runs WHERE run_id='run-live'"
        ).fetchone()
    assert row["state"] == "interrupted"
    assert row["stop_reason"] == "process_restart"


# ── Config surface ──


def test_storage_config_env_maps(monkeypatch):
    from modus.config import load_config

    config = load_config(env={
        "MODUS_STORAGE_AUDIT_ROTATE_BYTES": "1024",
        "MODUS_STORAGE_RUN_EVENTS_RETAIN_DAYS": "7",
        "MODUS_STORAGE_ARTIFACTS_MAX_BYTES": "999",
        "MODUS_STORAGE_SNAPSHOT_RETAIN_PER_RUN": "3",
        "MODUS_STORAGE_ENABLE_PRUNE": "true",
    })
    assert config.storage.audit_rotate_bytes == 1024
    assert config.storage.run_events_retain_days == 7
    assert config.storage.artifacts_max_bytes == 999
    assert config.storage.snapshot_retain_per_run == 3
    assert config.storage.enable_prune is True


def test_prune_snapshots_drops_old_beyond_retain(tmp_path, monkeypatch):
    """Side-repo snapshots beyond the retention are dropped, restore still works."""
    from modus.tools.snapshot import (
        create_snapshot, list_snapshots, prune_snapshots, restore_snapshot,
    )

    monkeypatch.setattr("modus.paths.data_dir", lambda env=None: tmp_path / "modus-data")
    project = tmp_path / "project"
    project.mkdir()
    for i in range(1, 7):
        (project / "f.txt").write_text(f"version {i}\n", encoding="utf-8")
        create_snapshot(str(project), phase="pre-turn", summary=f"snap {i}")

    assert len(list_snapshots(str(project), limit=100)) == 6

    dropped = prune_snapshots([str(project)], retain_per_run=2)
    assert dropped == 4
    remaining = list_snapshots(str(project), limit=100)
    assert len(remaining) == 2

    # The newest retained snapshot still restores the workspace.
    (project / "f.txt").write_text("MUTATED\n", encoding="utf-8")
    restored, _removed = restore_snapshot(str(project), remaining[0].commit_id)
    assert restored >= 1
    assert (project / "f.txt").read_text(encoding="utf-8").startswith("version 6")


def test_prune_snapshots_noop_when_under_retain(tmp_path, monkeypatch):
    from modus.tools.snapshot import (
        create_snapshot, list_snapshots, prune_snapshots,
    )

    monkeypatch.setattr("modus.paths.data_dir", lambda env=None: tmp_path / "modus-data")
    project = tmp_path / "project"
    project.mkdir()
    (project / "f.txt").write_text("v1\n", encoding="utf-8")
    create_snapshot(str(project), phase="pre-turn", summary="one")

    assert prune_snapshots([str(project)], retain_per_run=5) == 0
    assert len(list_snapshots(str(project), limit=100)) == 1
