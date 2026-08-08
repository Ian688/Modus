"""Modus Desktop SQLite persistence."""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modus.redact import redact_dict
from modus.modes import DEFAULT_MODE, normalize_mode
from modus.paths import data_dir

logger = logging.getLogger(__name__)

DB_DIR = data_dir()
DB_PATH = DB_DIR / "desktop.db"

# True once the process has validated the database at least once (startup
# quick_check).  Tests that swap ``DB_PATH`` per-case reset it explicitly.
_integrity_checked = False
# Reentrancy guard so corruption recovery never recursively recovers itself.
_recovering = False
# Periodic WAL checkpoint cadence (approximate, per _get_conn open).
_checkpoint_calls = 0
_CHECKPOINT_EVERY = 128


def _raw_conn() -> sqlite3.Connection:
    """Open a plain connection without integrity/recovery wrappers."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _wal_checkpoint(conn: sqlite3.Connection) -> None:
    """Best-effort WAL truncate so the WAL never grows without bound."""
    with suppress(sqlite3.Error):
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def checkpoint_now() -> int:
    """Force a WAL checkpoint(TRUNCATE) now.  Returns frames checkpointed."""
    if not DB_PATH.exists():
        return 0
    try:
        with _raw_conn() as conn:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return int(row[2]) if row else 0
    except sqlite3.Error:
        return 0


def _get_conn() -> sqlite3.Connection:
    """Open a connection, validating and (once) repairing database integrity.

    Startup validation: ``PRAGMA quick_check`` runs once per process.  A
    corrupted database is backed up to ``~/.modus/backup/``, reopened fresh,
    and its run ledger is salvaged from the recoverable ``run_events`` (reusing
    ``interrupt_nonterminal_runs`` semantics so dead work settles as
    ``interrupted/process_restart``, never as running).  WAL checkpoint runs
    periodically so a long-lived Desktop keeps its WAL bounded.
    """
    global _integrity_checked
    try:
        conn = _raw_conn()
    except sqlite3.DatabaseError:
        _recover_corrupt_db()
        conn = _raw_conn()
    conn.row_factory = sqlite3.Row
    if not _integrity_checked and DB_PATH.exists():
        _integrity_checked = True
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
            healthy = bool(rows) and str(rows[0][0]).lower() == "ok"
        except sqlite3.DatabaseError:
            healthy = False
        if not healthy:
            conn.close()
            _recover_corrupt_db()
            conn = _raw_conn()
            conn.row_factory = sqlite3.Row
    global _checkpoint_calls
    _checkpoint_calls += 1
    if _checkpoint_calls % _CHECKPOINT_EVERY == 0:
        _wal_checkpoint(conn)
    return conn


def _rename_quiet(src: Path, dst: Path) -> None:
    with suppress(OSError):
        src.rename(dst)


def _recover_corrupt_db() -> None:
    """Back up a corrupted database and reopen a fresh, valid one.

    Only Modus's own data directory is touched.  The corrupt file is preserved
    both in-place (``desktop.db.corrupt.<ts>``) and in ``~/.modus/backup/``.
    Best-effort salvage then replays the recoverable tables into the fresh DB,
    settling any non-terminal runs as ``interrupted/process_restart``.
    """
    global _recovering, _integrity_checked
    if _recovering:
        return
    _recovering = True
    try:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup_dir = DB_DIR / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        salvaged = _salvage_from_db(DB_PATH)
        corrupt_main = DB_DIR / f"desktop.db.corrupt.{ts}"
        _rename_quiet(DB_PATH, corrupt_main)
        for suffix in ("-wal", "-shm"):
            _rename_quiet(Path(str(DB_PATH) + suffix), DB_DIR / f"desktop.db{suffix}.corrupt.{ts}")
        if corrupt_main.exists():
            with suppress(OSError):
                shutil.copy2(corrupt_main, backup_dir / f"desktop.db.corrupt.{ts}")
        # Fresh database with the full schema.
        init_db()
        _replay_salvaged(salvaged)
        logger.warning(
            "corrupted database recovered: %s moved aside; fresh desktop.db opened",
            corrupt_main,
        )
    finally:
        _recovering = False
        _integrity_checked = True


# Tables salvaged in FK-safe order on corruption recovery.
_SALVAGE_ORDER = [
    "workspaces", "users", "account_workspaces", "sessions", "runs",
    "run_tasks", "run_events", "context_compactions", "approvals",
    "artifacts", "memories", "messages",
]


def _salvage_from_db(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Best-effort read of the recoverable tables from a damaged database."""
    result: dict[str, list[dict[str, Any]]] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        return result
    try:
        for table in _SALVAGE_ORDER:
            try:
                cols = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                if not cols:
                    continue
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                result[table] = [dict(zip(cols, row)) for row in rows]
            except sqlite3.DatabaseError:
                # Partial salvage is better than none; keep what we have.
                logger.warning("salvage could not read table %s from corrupt db", table)
                continue
    finally:
        with suppress(Exception):
            conn.close()
    return result


def _replay_salvaged(salvaged: dict[str, list[dict[str, Any]]]) -> int:
    """Replay salvaged rows into the fresh database, settling dead runs.

    Running runs become ``interrupted/process_restart``, undecided approvals
    become ``deny/process_restart``, and non-terminal tasks become
    ``cancelled`` — the same settlement ``interrupt_nonterminal_runs`` applies
    so a restored Desktop never presents dead work as running.
    """
    now = time.time()
    replayed = 0
    for table in _SALVAGE_ORDER:
        rows = salvaged.get(table) or []
        if not rows:
            continue
        with _raw_conn() as conn:
            # Best-effort replay: the fresh DB enforces FKs by default, but a
            # partially salvaged source may have orphaned rows.  Orphans are
            # skipped rather than aborting the whole recovery.
            conn.execute("PRAGMA foreign_keys=OFF")
            cols = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if not cols:
                continue
            for row in rows:
                if table == "runs" and str(row.get("state") or "") == "running":
                    row = dict(row)
                    row["state"] = "interrupted"
                    row["stop_reason"] = "process_restart"
                    row["error"] = "database recovered from corruption"
                    row["updated_at"] = now
                    row["ended_at"] = now
                elif table == "approvals" and row.get("decision") is None:
                    row = dict(row)
                    row["decision"] = "deny"
                    row["resolution_reason"] = "process_restart"
                    row["decided_at"] = now
                elif table == "run_tasks" and str(row.get("status") or "") not in (
                    "completed", "failed", "cancelled",
                ):
                    row = dict(row)
                    row["status"] = "cancelled"
                    row["updated_at"] = now
                values = {key: row.get(key) for key in cols if key in row}
                if not values:
                    continue
                try:
                    placeholders = ", ".join("?" for _ in values)
                    conn.execute(
                        f"INSERT OR IGNORE INTO {table} ({', '.join(values)}) VALUES ({placeholders})",
                        [values[key] for key in values],
                    )
                    replayed += 1
                except sqlite3.Error:
                    # One bad row must not block the remaining salvage.
                    logger.warning("replay skipped a %s row during recovery", table)
                    continue
    return replayed


def _parse_event_timestamp(value: str | None) -> float | None:
    """Parse a run_events ``timestamp`` ISO string into an epoch float."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _remove_artifact_file(storage_path: str) -> bool:
    """Delete an artifact file only when it lives inside Modus's private store."""
    try:
        root = (DB_DIR / "artifacts").resolve()
        path = Path(storage_path).resolve()
        if root in path.parents:
            path.unlink(missing_ok=True)
            return True
    except OSError:
        pass
    return False


def prune_expired(
    *, config: Any = None, env: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Report (and, when enabled, apply) retention for Modus's own data plane.

    Conservative and reversible: unless ``config.storage.enable_prune`` is true,
    nothing is deleted or modified — every candidate is merely reported.  All
    deletion is bounded to Modus's private data directory (desktop.db rows,
    artifact files under the private store, side-repo snapshots); user
    workspace files are never candidates.  Idempotent.
    """
    from modus.config import load_config

    cfg = config if config is not None else load_config(env=env)
    storage = cfg.storage
    report: dict[str, Any] = {
        "run_events_candidates": 0,
        "run_events_deleted": 0,
        "artifacts_candidates": 0,
        "artifacts_bytes_candidates": 0,
        "artifacts_deleted": 0,
        "memories_soft_expire_candidates": 0,
        "memories_archived": 0,
        "snapshot_dropped": 0,
    }
    if not DB_PATH.exists():
        return report
    now = time.time()

    # ── run_events retention ──
    with _raw_conn() as conn:
        expired_ids: list[str] = []
        for row in conn.execute("SELECT event_id, timestamp FROM run_events").fetchall():
            ts = _parse_event_timestamp(str(row["timestamp"] or ""))
            if ts is not None and now - ts > storage.run_events_retain_days * 86400:
                expired_ids.append(str(row["event_id"]))
        report["run_events_candidates"] = len(expired_ids)
        if storage.enable_prune and expired_ids:
            placeholders = ",".join("?" for _ in expired_ids)
            cursor = conn.execute(
                f"DELETE FROM run_events WHERE event_id IN ({placeholders})", expired_ids,
            )
            report["run_events_deleted"] = cursor.rowcount

        # ── artifact quota (bytes + count), oldest first ──
        arts = conn.execute(
            "SELECT artifact_id, storage_path, size_bytes, created_at "
            "FROM artifacts ORDER BY created_at, artifact_id",
        ).fetchall()
        total_bytes = sum(int(a["size_bytes"] or 0) for a in arts)
        delete_count = 0
        running = total_bytes
        for art in arts:
            if running <= storage.artifacts_max_bytes and (
                len(arts) - delete_count
            ) <= storage.artifacts_max_count:
                break
            delete_count += 1
            running -= int(art["size_bytes"] or 0)
        to_delete = arts[:delete_count]
        report["artifacts_candidates"] = len(to_delete)
        report["artifacts_bytes_candidates"] = sum(
            int(a["size_bytes"] or 0) for a in to_delete
        )
        if storage.enable_prune and to_delete:
            deleted = 0
            for art in to_delete:
                _remove_artifact_file(str(art["storage_path"] or ""))
                conn.execute(
                    "DELETE FROM artifacts WHERE artifact_id=?", (str(art["artifact_id"]),),
                )
                deleted += 1
            report["artifacts_deleted"] = deleted

        # ── memories soft-expire (archive, never delete) ──
        archive_ids: list[str] = []
        for row in conn.execute(
            "SELECT memory_id, updated_at FROM memories WHERE status='active'",
        ).fetchall():
            updated = row["updated_at"]
            if updated is not None and now - float(updated) > storage.memories_soft_expire_days * 86400:
                archive_ids.append(str(row["memory_id"]))
        report["memories_soft_expire_candidates"] = len(archive_ids)
        if storage.enable_prune and archive_ids:
            placeholders = ",".join("?" for _ in archive_ids)
            cursor = conn.execute(
                f"UPDATE memories SET status='archived', updated_at=? "
                f"WHERE memory_id IN ({placeholders})",
                [now, *archive_ids],
            )
            report["memories_archived"] = cursor.rowcount

    # ── side-git snapshot retention (Modus's side repos only) ──
    if storage.enable_prune:
        from modus.tools.snapshot import prune_snapshots

        report["snapshot_dropped"] = prune_snapshots(
            retain_per_run=storage.snapshot_retain_per_run,
        )
    return report


def init_db() -> None:
    """建表（幂等）"""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workspaces (
                workspace_id TEXT PRIMARY KEY,
                root TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                workspace_id TEXT REFERENCES workspaces(workspace_id) ON DELETE SET NULL,
                title TEXT NOT NULL DEFAULT '新对话',
                mode TEXT NOT NULL DEFAULT 'default',
                archived INTEGER NOT NULL DEFAULT 0,
                worldview TEXT NOT NULL DEFAULT '',
                world_view_history TEXT NOT NULL DEFAULT '[]',
                system_prompt TEXT NOT NULL DEFAULT '',
                model_id TEXT NOT NULL DEFAULT '',
                mode_config TEXT NOT NULL DEFAULT '{}',
                reasoning_effort TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                tool_calls TEXT NOT NULL DEFAULT '[]',
                token_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS context_compactions (
                compaction_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES runs(run_id) ON DELETE SET NULL,
                summary TEXT NOT NULL,
                omitted_count INTEGER NOT NULL,
                tail_count INTEGER NOT NULL,
                cutoff_message_id INTEGER,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                workspace_id TEXT REFERENCES workspaces(workspace_id) ON DELETE SET NULL,
                task_id TEXT REFERENCES run_tasks(task_id) ON DELETE SET NULL,
                sequence INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                parent_event_id TEXT,
                actor TEXT NOT NULL DEFAULT '{}',
                type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'started',
                payload TEXT NOT NULL DEFAULT '{}',
                mode TEXT NOT NULL DEFAULT 'default',
                part_id TEXT NOT NULL DEFAULT '',
                artifact_ids TEXT NOT NULL DEFAULT '[]',
                schema TEXT NOT NULL DEFAULT 'modus.agent-event.v2',
                revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                client_request_id TEXT,
                client_request_fingerprint TEXT,
                workspace_id TEXT REFERENCES workspaces(workspace_id) ON DELETE SET NULL,
                mode TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'running',
                projection_revision INTEGER NOT NULL DEFAULT 0,
                stop_reason TEXT,
                config_snapshot TEXT NOT NULL DEFAULT '{}',
                budget TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                ended_at REAL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                tool_name TEXT NOT NULL,
                input_hash TEXT NOT NULL DEFAULT '',
                input TEXT NOT NULL DEFAULT '{}',
                decision TEXT,
                resolution_reason TEXT,
                requested_at REAL NOT NULL,
                decided_at REAL
            );
            CREATE TABLE IF NOT EXISTS run_tasks (
                task_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                parent_task_id TEXT REFERENCES run_tasks(task_id) ON DELETE SET NULL,
                depth INTEGER NOT NULL DEFAULT 0,
                task_kind TEXT NOT NULL DEFAULT 'worker',
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                success_criteria TEXT NOT NULL DEFAULT '',
                context_artifact_id TEXT,
                actor_id TEXT NOT NULL DEFAULT '',
                actor_label TEXT NOT NULL DEFAULT '',
                assigned_model_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                attempt INTEGER NOT NULL DEFAULT 0,
                dependencies TEXT NOT NULL DEFAULT '[]',
                result_artifact_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                task_id TEXT REFERENCES run_tasks(task_id) ON DELETE SET NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
                task_id TEXT REFERENCES run_tasks(task_id) ON DELETE SET NULL,
                scope TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                content TEXT NOT NULL,
                source_ids TEXT NOT NULL DEFAULT '[]',
                reference_only INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_run_events_session ON run_events(session_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_compactions_session
                ON context_compactions(session_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, requested_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_run ON run_tasks(run_id, ordinal);
            CREATE INDEX IF NOT EXISTS idx_tasks_session ON run_tasks(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id, scope, updated_at);
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL DEFAULT '',
                salt TEXT NOT NULL DEFAULT '',
                is_local_default INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_workspaces (
                owner_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (owner_id, workspace_id)
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accounts (
                user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                balance_cents INTEGER NOT NULL DEFAULT 0,
                lifetime_cents INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS billing_ledger (
                ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                delta_cents INTEGER NOT NULL,
                balance_after_cents INTEGER NOT NULL,
                model_id TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recharge_records (
                recharge_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
        """)
        # Existing Desktop databases predate workspace/task identity. SQLite's
        # CREATE TABLE IF NOT EXISTS does not add columns, so evolve them here
        # without replacing user-owned history.
        _ensure_column(conn, "sessions", "workspace_id", "TEXT")
        _ensure_column(conn, "runs", "workspace_id", "TEXT")
        _ensure_column(conn, "runs", "client_request_id", "TEXT")
        _ensure_column(conn, "runs", "client_request_fingerprint", "TEXT")
        _ensure_column(
            conn, "runs", "projection_revision", "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(conn, "run_events", "workspace_id", "TEXT")
        _ensure_column(conn, "run_events", "task_id", "TEXT")
        _ensure_column(conn, "run_events", "artifact_ids", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(
            conn, "run_events", "schema",
            "TEXT NOT NULL DEFAULT 'modus.agent-event.v2'",
        )
        _ensure_column(conn, "run_tasks", "task_kind", "TEXT NOT NULL DEFAULT 'worker'")
        _ensure_column(conn, "run_tasks", "depth", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "run_tasks", "actor_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "run_tasks", "actor_label", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "memories", "embedding", "TEXT")
        _ensure_column(conn, "context_compactions", "cutoff_message_id", "INTEGER")
        # tool results must keep the id of the assistant tool_call they answer;
        # OpenAI-compatible providers reject a tool message without a matching
        # tool_call_id (HTTP 400). Older rows predate the column and are paired
        # positionally at restore time.
        _ensure_column(conn, "messages", "tool_call_id", "TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_events_task ON run_events(task_id, sequence)",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_id, updated_at DESC)",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_workspace ON runs(workspace_id, started_at DESC)",
        )
        # Request idempotency is account-local. The former global index let an
        # unrelated account block the same client request ID.
        conn.execute("DROP INDEX IF EXISTS idx_runs_client_request")

        from modus.desktop.workspace import WorkspaceIdentity

        workspace = WorkspaceIdentity.current()
        now = time.time()
        conn.execute(
            """INSERT INTO workspaces (workspace_id, root, name, created_at, updated_at)
               VALUES (?,?,?,?,?) ON CONFLICT(root) DO UPDATE SET
               name=excluded.name, updated_at=excluded.updated_at""",
            (workspace.workspace_id, workspace.root, workspace.name, now, now),
        )
        conn.execute(
            "UPDATE sessions SET workspace_id=? WHERE workspace_id IS NULL OR workspace_id=''",
            (workspace.workspace_id,),
        )
        conn.execute(
            """UPDATE runs SET workspace_id=(
                   SELECT sessions.workspace_id FROM sessions WHERE sessions.id=runs.session_id
               ) WHERE workspace_id IS NULL OR workspace_id=''""",
        )
        conn.execute(
            """UPDATE run_events SET workspace_id=(
                   SELECT runs.workspace_id FROM runs WHERE runs.run_id=run_events.run_id
               ) WHERE workspace_id IS NULL OR workspace_id=''""",
        )

        # ── User ownership (local multi-account). No foreign keys: isolation is
        # enforced at the query layer (session catalog / run admission filter by
        # owner_id). Existing rows are backfilled to the local default user.
        _ensure_column(conn, "sessions", "owner_id", "TEXT")
        _ensure_column(conn, "runs", "owner_id", "TEXT")
        _ensure_column(conn, "workspaces", "owner_id", "TEXT")
        _ensure_column(conn, "account_workspaces", "is_default", "INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_id, updated_at DESC)",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_owner ON runs(owner_id, started_at DESC)",
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_client_request
               ON runs(owner_id, client_request_id)
               WHERE client_request_id IS NOT NULL AND client_request_id != ''""",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_billing_user ON billing_ledger(user_id, created_at)",
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_run
               ON billing_ledger(user_id, run_id)
               WHERE kind='charge'""",
        )

        from modus.desktop import accounts

        default_user = accounts.ensure_default_user(conn=conn)
        default_owner = str(default_user["user_id"])
        conn.execute(
            "UPDATE sessions SET owner_id=? WHERE owner_id IS NULL OR owner_id=''",
            (default_owner,),
        )
        conn.execute(
            "UPDATE runs SET owner_id=? WHERE owner_id IS NULL OR owner_id=''",
            (default_owner,),
        )
        conn.execute(
            "UPDATE workspaces SET owner_id=? WHERE owner_id IS NULL OR owner_id=''",
            (default_owner,),
        )
        conn.execute(
            """INSERT OR IGNORE INTO account_workspaces
               (owner_id, workspace_id, created_at, updated_at)
               SELECT owner_id, workspace_id, created_at, updated_at
               FROM workspaces WHERE owner_id IS NOT NULL AND owner_id != ''""",
        )


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, declaration: str,
) -> None:
    columns = {
        str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def upsert_workspace(workspace: Any, *, owner_id: str = "") -> dict[str, Any]:
    """Persist one canonical WorkspaceIdentity and return its public record."""
    from modus.desktop.workspace import WorkspaceIdentity

    identity = (
        workspace if isinstance(workspace, WorkspaceIdentity)
        else WorkspaceIdentity.from_path(workspace)
    )
    now = time.time()
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO workspaces (workspace_id, root, name, created_at, updated_at)
               VALUES (?,?,?,?,?) ON CONFLICT(root) DO UPDATE SET
               name=excluded.name, updated_at=excluded.updated_at""",
            (identity.workspace_id, identity.root, identity.name, now, now),
        )
        if owner_id:
            conn.execute(
                """INSERT INTO account_workspaces
                   (owner_id, workspace_id, created_at, updated_at)
                   VALUES (?,?,?,?) ON CONFLICT(owner_id, workspace_id) DO UPDATE SET
                   updated_at=excluded.updated_at""",
                (owner_id, identity.workspace_id, now, now),
            )
    return get_workspace(identity.workspace_id, owner_id=owner_id) or identity.to_wire()


def get_workspace(workspace_id: str, *, owner_id: str = "") -> dict[str, Any] | None:
    if not workspace_id:
        return None
    with _get_conn() as conn:
        if owner_id:
            row = conn.execute(
                """SELECT w.* FROM workspaces w
                   JOIN account_workspaces aw ON aw.workspace_id=w.workspace_id
                   WHERE w.workspace_id=? AND aw.owner_id=?""",
                (workspace_id, owner_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,),
            ).fetchone()
    if row is None:
        return None
    value = dict(row)
    value["schema"] = "modus.workspace.v1"
    return value


def list_workspaces(limit: int = 100, *, owner_id: str = "") -> list[dict[str, Any]]:
    """Return every persisted workspace, most recently used first."""
    bounded = max(1, min(int(limit), 500))
    with _get_conn() as conn:
        if owner_id:
            rows = conn.execute(
                """SELECT w.*, aw.is_default FROM workspaces w
                   JOIN account_workspaces aw ON aw.workspace_id=w.workspace_id
                   WHERE aw.owner_id=? ORDER BY aw.is_default DESC, aw.updated_at DESC LIMIT ?""",
                (owner_id, bounded),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workspaces ORDER BY updated_at DESC LIMIT ?", (bounded,),
            ).fetchall()
    result = []
    for row in rows:
        value = dict(row)
        value["schema"] = "modus.workspace.v1"
        result.append(value)
    return result


def set_default_workspace(owner_id: str, workspace_id: str) -> dict[str, Any] | None:
    """Set one remembered workspace as the account default for new sessions."""
    owner = str(owner_id or "").strip()
    target = str(workspace_id or "").strip()
    if not owner or not target or get_workspace(target, owner_id=owner) is None:
        return None
    now = time.time()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE account_workspaces SET is_default=0, updated_at=? WHERE owner_id=?",
            (now, owner),
        )
        cursor = conn.execute(
            """UPDATE account_workspaces SET is_default=1, updated_at=?
               WHERE owner_id=? AND workspace_id=?""",
            (now, owner, target),
        )
    return get_workspace(target, owner_id=owner) if cursor.rowcount else None


def get_default_workspace(owner_id: str) -> dict[str, Any] | None:
    owner = str(owner_id or "").strip()
    if not owner:
        return None
    with _get_conn() as conn:
        row = conn.execute(
            """SELECT w.*, aw.is_default FROM workspaces w
               JOIN account_workspaces aw ON aw.workspace_id=w.workspace_id
               WHERE aw.owner_id=? AND aw.is_default=1
               ORDER BY aw.updated_at DESC LIMIT 1""",
            (owner,),
        ).fetchone()
    if row is None:
        return None
    value = dict(row)
    value["schema"] = "modus.workspace.v1"
    return value


def forget_workspace(owner_id: str, workspace_id: str) -> bool:
    """Forget an account/workspace association without touching source files.

    The canonical workspace row and historical Run evidence remain intact.
    Re-adding the same directory recreates the account association.
    """
    owner = str(owner_id or "").strip()
    target = str(workspace_id or "").strip()
    if not owner or not target:
        return False
    with _get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM account_workspaces WHERE owner_id=? AND workspace_id=?",
            (owner, target),
        )
    return cursor.rowcount > 0
# ── Run event audit ledger ──

TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})
ADMISSION_FAILURE_STOP_REASONS = frozenset({
    "admission_persistence_failed",
    "admission_transport_failed",
    "admission_conflict",
})


def _bump_run_projection(conn: sqlite3.Connection, run_id: str) -> None:
    """Advance the unified Workbench projection version in this transaction."""
    conn.execute(
        "UPDATE runs SET projection_revision=projection_revision+1 WHERE run_id=?",
        (run_id,),
    )


def create_run(
    run_id: str, session_id: str, mode: str, *,
    config_snapshot: dict[str, Any] | None = None,
    workspace_id: str = "",
    client_request_id: str = "",
    client_request_fingerprint: str = "",
) -> dict[str, Any]:
    """Create one run and freeze its start-time configuration exactly once."""
    now = time.time()
    snapshot = json.dumps(
        redact_dict(config_snapshot or {}), ensure_ascii=False, sort_keys=True,
    )
    with _get_conn() as conn:
        if not workspace_id:
            row = conn.execute(
                "SELECT workspace_id FROM sessions WHERE id=?", (session_id,),
            ).fetchone()
            workspace_id = str(row["workspace_id"] or "") if row else ""
        owner_row = conn.execute(
            "SELECT owner_id FROM sessions WHERE id=?", (session_id,),
        ).fetchone()
        owner_id = str(owner_row["owner_id"] or "") if owner_row else ""
        conn.execute(
            """INSERT OR IGNORE INTO runs
               (run_id, session_id, client_request_id, client_request_fingerprint,
                workspace_id, owner_id, mode, state, projection_revision,
                config_snapshot, started_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, session_id, client_request_id or None,
                client_request_fingerprint or None, workspace_id or None,
                owner_id or None, normalize_mode(mode),
                "running", 0, snapshot, now, now,
            ),
        )
    return get_run(run_id) or {}


def create_run_admission(
    run_id: str, session_id: str, mode: str, *,
    config_snapshot: dict[str, Any] | None = None,
    workspace_id: str = "",
    client_request_id: str = "",
    client_request_fingerprint: str = "",
    root_title: str = "User task",
    root_description: str = "Run admission root task",
    root_actor_id: str = "primary",
    root_actor_label: str = "Host",
    assigned_model_id: str = "",
) -> dict[str, Any]:
    """Atomically create the durable Run and its running canonical root.

    A provider may only start after this function succeeds.  Identity
    conflicts are reported as an empty result and leave the database
    untouched; operational failures propagate after SQLite rolls the whole
    transaction back.  A failed admission therefore cannot expose a
    ``running`` Run without the root task required by replay and settlement.
    """
    now = time.time()
    normalized_mode = normalize_mode(mode)
    snapshot = json.dumps(
        redact_dict(config_snapshot or {}), ensure_ascii=False, sort_keys=True,
    )
    root_task_id = f"task_{run_id}_root"
    admitted: dict[str, Any] | None = None
    with _get_conn() as conn:
        # Reserve both identities before inspecting conflicts.  This also
        # serializes a duplicate request ID racing a distinct generated Run.
        conn.execute("BEGIN IMMEDIATE")
        if not workspace_id:
            row = conn.execute(
                "SELECT workspace_id FROM sessions WHERE id=?", (session_id,),
            ).fetchone()
            workspace_id = str(row["workspace_id"] or "") if row else ""
        owner_row = conn.execute(
            "SELECT owner_id FROM sessions WHERE id=?", (session_id,),
        ).fetchone()
        owner_id = str(owner_row["owner_id"] or "") if owner_row else ""
        # Local billing gate: when enabled, refuse admission once the owner's
        # balance is exhausted. Reuses the existing ``{}`` rejection path so a
        # server-side admission failure is surfaced exactly like a conflict.
        try:
            from modus.config import load_config
            from modus.desktop import billing

            if owner_id and load_config().features.billing and not billing.sufficient_balance(owner_id):
                conn.rollback()
                return {}
        except Exception:
            pass
        try:
            run_cursor = conn.execute(
                """INSERT INTO runs
                   (run_id, session_id, client_request_id,
                    client_request_fingerprint, workspace_id, owner_id, mode, state,
                    projection_revision, config_snapshot, started_at,
                    updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, session_id, client_request_id or None,
                    client_request_fingerprint or None, workspace_id or None,
                    owner_id or None, normalized_mode, "running", 0, snapshot, now, now,
                ),
            )
            root_cursor = conn.execute(
                """INSERT INTO run_tasks
                   (task_id, run_id, session_id, ordinal, parent_task_id,
                    task_kind, title, description, success_criteria,
                    context_artifact_id, actor_id, actor_label,
                    assigned_model_id, status, attempt, dependencies,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    root_task_id, run_id, session_id, -1, None, "root",
                    root_title, root_description, "", None,
                    root_actor_id, root_actor_label, assigned_model_id,
                    "running", 1, "[]", now, now,
                ),
            )
        except sqlite3.IntegrityError:
            # A run ID, request ID, canonical task ID, or foreign-key conflict
            # is a rejected admission rather than a partial durable record.
            conn.rollback()
            return {}
        if run_cursor.rowcount != 1 or root_cursor.rowcount != 1:
            conn.rollback()
            return {}
        # Preserve the projection progression of the former create-task then
        # start-task sequence even though no observer can see the midpoint.
        _bump_run_projection(conn, run_id)
        _bump_run_projection(conn, run_id)
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return {}
        admitted = _decode_run_row(row)
    # Do not perform a second connection/read after commit: a transient read
    # failure there would make a successful admission look unpersisted to the
    # caller and unnecessarily enter compensation.
    return admitted or {}


def get_run_by_client_request_id(
    client_request_id: str, *, owner_id: str = "",
) -> dict[str, Any] | None:
    """Resolve one explicit client submission without storing its message body."""
    request_id = str(client_request_id or "")
    if not request_id:
        return None
    with _get_conn() as conn:
        if owner_id:
            row = conn.execute(
                "SELECT run_id FROM runs WHERE client_request_id=? AND owner_id=?",
                (request_id, owner_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT run_id FROM runs WHERE client_request_id=?", (request_id,),
            ).fetchone()
    return get_run(str(row["run_id"])) if row else None


def update_run(
    run_id: str, *, state: str, stop_reason: str | None = None,
    budget: dict[str, Any] | None = None, error: str | None = None,
) -> bool:
    """Advance a run unless its persisted state is already terminal."""
    if state not in {"running", *TERMINAL_RUN_STATES}:
        raise ValueError(f"invalid run state: {state}")
    now = time.time()
    ended_at = now if state in TERMINAL_RUN_STATES else None
    with _get_conn() as conn:
        current = conn.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if current is None:
            return False
        if str(current["state"]) in TERMINAL_RUN_STATES:
            return False
        conn.execute(
            """UPDATE runs SET state=?, stop_reason=?, budget=?, error=?,
               updated_at=?, ended_at=?, projection_revision=projection_revision+1
               WHERE run_id=?""",
            (
                state, stop_reason,
                json.dumps(redact_dict(budget or {}), ensure_ascii=False, sort_keys=True),
                error, now, ended_at, run_id,
            ),
        )
    return True


def fail_run_admission(
    run_id: str, *, stop_reason: str = "admission_persistence_failed",
    expected_session_id: str = "",
) -> bool:
    """Fail a partially persisted Run before it can be admitted.

    Run admission creates the durable Run before its root task and transport
    acknowledgement so a request ID can remain idempotent. If either boundary
    fails, keep the partial ledger internally consistent in one transaction:
    the Run becomes terminal and any root row becomes failed too. No terminal
    Agent event is synthesized because provider execution never started.
    """
    reason = str(stop_reason or "admission_persistence_failed")
    if reason not in ADMISSION_FAILURE_STOP_REASONS:
        raise ValueError(f"invalid admission failure stop reason: {reason}")
    now = time.time()
    with _get_conn() as conn:
        # A transport failure, a settlement, and a duplicate recovery may be
        # reported by different tasks.  Claim the ledger before validating
        # that this is still an admission-only Run.
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT state, session_id FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if (
            current is None
            or str(current["state"]) in TERMINAL_RUN_STATES
            or (
                expected_session_id
                and str(current["session_id"]) != str(expected_session_id)
            )
        ):
            return False
        # Once an Agent event exists, provider execution crossed admission.
        # Never relabel such a Run as an event-free admission failure.
        event = conn.execute(
            "SELECT 1 FROM run_events WHERE run_id=? LIMIT 1", (run_id,),
        ).fetchone()
        if event is not None:
            return False
        roots = conn.execute(
            """SELECT task_id, session_id, status FROM run_tasks
               WHERE run_id=? AND task_kind='root'""",
            (run_id,),
        ).fetchall()
        canonical_root_id = f"task_{run_id}_root"
        if roots:
            if len(roots) != 1:
                return False
            root = roots[0]
            if (
                str(root["task_id"]) != canonical_root_id
                or str(root["session_id"]) != str(current["session_id"])
                or str(root["status"]) in TERMINAL_TASK_STATES
            ):
                return False
        cursor = conn.execute(
            """UPDATE runs SET state='failed', stop_reason=?,
               error='Run admission did not reach provider execution',
               updated_at=?, ended_at=?, projection_revision=projection_revision+1
               WHERE run_id=?
                 AND state NOT IN ('completed','failed','cancelled','interrupted')""",
            (reason, now, now, run_id),
        )
        if cursor.rowcount != 1:
            return False
        if roots:
            root_cursor = conn.execute(
                """UPDATE run_tasks SET status='failed', updated_at=?
                   WHERE task_id=? AND run_id=? AND session_id=?
                     AND task_kind='root'
                     AND status NOT IN ('completed','failed','cancelled')""",
                (
                    now, canonical_root_id, run_id,
                    str(current["session_id"]),
                ),
            )
            if root_cursor.rowcount != 1:
                conn.rollback()
                return False
    return True


def settle_run_event(session_id: str, event: dict[str, Any]) -> bool:
    """Atomically claim and persist exactly one terminal event for a Run.

    The terminal event, Run state and root task are one durable fact.  This
    function deliberately contains its own SQL instead of composing helpers
    that open separate connections.
    """
    event_type = str(event.get("type") or "")
    if event_type not in {"run_completed", "run_error"}:
        raise ValueError("settle_run_event requires a terminal event")
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return False
    stop_reason = str(
        payload.get("stop_reason")
        or payload.get("code")
        or ("completed" if event_type == "run_completed" else "failed")
    )
    event_status = str(event.get("status") or "")
    if event_type == "run_completed":
        if stop_reason != "completed" or event_status not in {"", "completed"}:
            return False
        run_state = task_state = "completed"
    else:
        if event_status and event_status not in {"failed", "cancelled"}:
            return False
        if stop_reason == "cancelled" and event_status not in {"", "cancelled"}:
            return False
        if stop_reason != "cancelled" and event_status == "cancelled":
            return False
        run_state = task_state = (
            "cancelled" if stop_reason == "cancelled" else "failed"
        )
    run_id = str(event["run_id"])
    canonical_root_id = f"task_{run_id}_root"
    provided_task_id = str(event.get("task_id") or "")
    if provided_task_id and provided_task_id != canonical_root_id:
        return False
    now = time.time()
    with _get_conn() as conn:
        # Serialize terminal claimants before reading either half of the
        # Run/root invariant.  A deferred transaction would allow two callers
        # to observe the same non-terminal rows and only discover the race
        # after one of them had started mutating the ledger.
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            "SELECT state, session_id FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if run is None or str(run["session_id"]) != str(session_id):
            return False
        if str(run["state"]) in TERMINAL_RUN_STATES:
            return False

        root = conn.execute(
            """SELECT task_id, run_id, session_id, task_kind, status
               FROM run_tasks WHERE task_id=?""",
            (canonical_root_id,),
        ).fetchone()
        if (
            root is None
            or str(root["task_id"]) != canonical_root_id
            or str(root["run_id"]) != run_id
            or str(root["session_id"]) != str(session_id)
            or str(root["task_kind"]) != "root"
            or str(root["status"]) in TERMINAL_TASK_STATES
        ):
            return False

        task_cursor = conn.execute(
            """UPDATE run_tasks SET status=?, updated_at=?
               WHERE task_id=? AND run_id=? AND session_id=? AND task_kind='root'
                 AND status NOT IN ('completed','failed','cancelled')""",
            (task_state, now, canonical_root_id, run_id, str(session_id)),
        )
        if task_cursor.rowcount != 1:
            return False
        cursor = conn.execute(
            """UPDATE runs SET state=?, stop_reason=?, budget=?, error=?,
               updated_at=?, ended_at=?, projection_revision=projection_revision+1
               WHERE run_id=? AND state NOT IN ('completed','failed','cancelled','interrupted')""",
            (
                run_state, stop_reason,
                json.dumps(redact_dict(payload.get("budget") or {}), ensure_ascii=False, sort_keys=True),
                str(payload.get("message") or "") or None,
                now, now, run_id,
            ),
        )
        if cursor.rowcount != 1:
            # BEGIN IMMEDIATE makes this unreachable under normal operation,
            # but never commit a root-only settlement if the Run row was
            # concurrently or externally corrupted.
            conn.rollback()
            return False
        # ── Local billing: debit balance in the same transaction as the
        # terminal state so a run's completion and its cost are one durable
        # fact.  Idempotent via the UNIQUE(user_id, run_id) charge index.
        try:
            from modus.desktop import billing

            budget = payload.get("budget") or {}
            run_row = conn.execute(
                "SELECT owner_id, config_snapshot FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            owner_id = str(run_row["owner_id"] or "") if run_row else ""
            if owner_id:
                try:
                    run_config = json.loads(run_row["config_snapshot"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    run_config = {}
                billing.charge_run(
                    user_id=owner_id, run_id=run_id,
                    model_id=str(run_config.get("host_model_id", "") or ""),
                    input_tokens=int(budget.get("input_tokens") or 0),
                    output_tokens=int(budget.get("output_tokens") or 0),
                    conn=conn,
                )
        except Exception:
            # Billing is best-effort within settlement; a pricing/DB failure
            # must never block the durable terminal state.
            pass
        conn.execute(
            """UPDATE run_tasks SET status=?, updated_at=?
               WHERE run_id=? AND task_kind!='root'
                 AND status NOT IN ('completed','failed','cancelled')""",
            (task_state, now, run_id),
        )
        conn.execute(
            """UPDATE approvals SET decision='deny', resolution_reason='run_settled',
               decided_at=? WHERE run_id=? AND decision IS NULL""",
            (now, run_id),
        )
        conn.execute(
            """INSERT INTO run_events
               (event_id, run_id, session_id, workspace_id, task_id, sequence,
                timestamp, channel_id, parent_event_id, actor, type, status,
                payload, mode, part_id, artifact_ids, schema, revision)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(event["event_id"]), run_id, str(session_id),
                str(event.get("workspace_id") or "") or None,
                canonical_root_id, int(event["sequence"]), str(event["timestamp"]),
                str(event["channel_id"]), event.get("parent_event_id"),
                json.dumps(redact_dict(event.get("actor") or {}), ensure_ascii=False, sort_keys=True),
                event_type, event_status or task_state,
                json.dumps(redact_dict(payload), ensure_ascii=False, sort_keys=True),
                normalize_mode(event.get("mode")), str(event.get("part_id") or ""),
                json.dumps(list(event.get("artifact_ids") or []), ensure_ascii=False),
                str(event.get("schema") or "modus.agent-event.v2"),
                int(event.get("revision") or 0),
            ),
        )
        if task_cursor.rowcount:
            _bump_run_projection(conn, run_id)
        _bump_run_projection(conn, run_id)
    return True


def _decode_run_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["mode"] = normalize_mode(result.get("mode"))
    for field in ("config_snapshot", "budget"):
        try:
            value = json.loads(result.get(field) or "{}")
        except (TypeError, json.JSONDecodeError):
            value = {}
        result[field] = value if isinstance(value, dict) else {}
    return result


def get_run(run_id: str) -> dict[str, Any] | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return _decode_run_row(row) if row is not None else None


def latest_run_for_session(session_id: str) -> dict[str, Any] | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT run_id FROM runs WHERE session_id=? ORDER BY started_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return get_run(str(row["run_id"])) if row else None


def list_runs_for_session(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent bounded run history in chronological order."""
    bounded = max(1, min(int(limit), 100))
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT run_id FROM runs WHERE session_id=? ORDER BY started_at DESC LIMIT ?",
            (session_id, bounded),
        ).fetchall()
    runs = [get_run(str(row["run_id"])) for row in reversed(rows)]
    return [run for run in runs if run is not None]


def get_session_run_history(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return typed events grouped by run for deterministic replay."""
    return [
        {"run": run, "events": get_run_events(str(run["run_id"]))}
        for run in list_runs_for_session(session_id, limit=limit)
    ]


def interrupt_nonterminal_runs() -> int:
    """Atomically settle runs whose provider streams died with the process.

    A Run row alone is not enough to restore the Desktop.  The transcript is
    rebuilt from ``run_events`` and the Workbench task tree is rebuilt from
    ``run_tasks``.  Leaving either ledger non-terminal makes a restored page
    restart its timers and continue to present dead work as running.

    ``cancelled`` is the closest terminal task/event status in their schemas;
    the authoritative Run outcome remains ``interrupted/process_restart`` so a
    restart can never be mistaken for either user cancellation or completion.
    Task attempt counters are deliberately preserved.
    """
    now = time.time()
    event_timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z",
    )
    message = "Desktop process restarted before the run reached a terminal state"
    with _get_conn() as conn:
        interrupted = conn.execute(
            """SELECT run_id, session_id, workspace_id, mode
               FROM runs WHERE state='running' ORDER BY started_at, run_id""",
        ).fetchall()
        if not interrupted:
            return 0

        conn.execute(
            """UPDATE approvals SET decision='deny', resolution_reason='process_restart', decided_at=?
               WHERE decision IS NULL AND run_id IN (SELECT run_id FROM runs WHERE state='running')""",
            (now,),
        )
        task_runs = conn.execute(
            """SELECT DISTINCT run_id FROM run_tasks
               WHERE status NOT IN ('completed', 'failed', 'cancelled')
                 AND run_id IN (SELECT run_id FROM runs WHERE state='running')""",
        ).fetchall()
        conn.execute(
            """UPDATE run_tasks SET status='cancelled', updated_at=?
               WHERE status NOT IN ('completed', 'failed', 'cancelled')
                 AND run_id IN (SELECT run_id FROM runs WHERE state='running')""",
            (now,),
        )
        for task_run in task_runs:
            _bump_run_projection(conn, str(task_run["run_id"]))

        # RunEventEmitter cannot survive the process boundary, so synthesize
        # the same terminal envelope it would have audited before shutdown.
        # A UUIDv5 derived from the Run makes the repair idempotent even if a
        # caller restores a damaged database whose Run state was reset later.
        for run in interrupted:
            run_id = str(run["run_id"])
            sequence_row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM run_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            sequence = int(sequence_row["sequence"] or 0) + 1
            root_task = conn.execute(
                """SELECT task_id FROM run_tasks
                   WHERE run_id=? AND task_kind='root'
                   ORDER BY ordinal, created_at LIMIT 1""",
                (run_id,),
            ).fetchone()
            event_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"modus:process_restart:{run_id}").hex
            event_cursor = conn.execute(
                """INSERT OR IGNORE INTO run_events
                   (event_id, run_id, session_id, workspace_id, task_id, sequence,
                    timestamp, channel_id, parent_event_id, actor, type, status,
                    payload, mode, part_id, artifact_ids, schema, revision)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"evt_restart_{event_uuid}", run_id, str(run["session_id"]),
                    str(run["workspace_id"] or "") or None,
                    str(root_task["task_id"]) if root_task else None,
                    sequence, event_timestamp, "user_host", None,
                    json.dumps(
                        {"kind": "system", "id": "system", "label": "系统"},
                        ensure_ascii=False, sort_keys=True,
                    ),
                    "run_error", "cancelled",
                    json.dumps(
                        {
                            "code": "process_restart", "message": message,
                            "retryable": True, "stop_reason": "process_restart",
                        },
                        ensure_ascii=False, sort_keys=True,
                    ),
                    normalize_mode(run["mode"]), f"part_restart_{event_uuid}",
                    "[]", "modus.agent-event.v2", 0,
                ),
            )
            if event_cursor.rowcount == 1:
                _bump_run_projection(conn, run_id)
        cursor = conn.execute(
            """UPDATE runs SET state='interrupted', stop_reason='process_restart',
               error=?, updated_at=?, ended_at=?,
               projection_revision=projection_revision+1 WHERE state='running'""",
            (message, now, now),
        )
    return cursor.rowcount


def create_approval(
    *, approval_id: str, run_id: str, tool_name: str, input_hash: str,
    input_data: dict[str, Any],
) -> None:
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO approvals
               (approval_id, run_id, tool_name, input_hash, input, requested_at)
               VALUES (?,?,?,?,?,?)""",
            (
                approval_id, run_id, tool_name, input_hash,
                json.dumps(redact_dict(input_data), ensure_ascii=False, sort_keys=True), time.time(),
            ),
        )


def resolve_approval_record(
    approval_id: str, decision: str, resolution_reason: str = "user_decision",
) -> bool:
    with _get_conn() as conn:
        cursor = conn.execute(
            """UPDATE approvals SET decision=?, resolution_reason=?, decided_at=?
               WHERE approval_id=? AND decision IS NULL""",
            (decision, resolution_reason, time.time(), approval_id),
        )
    return cursor.rowcount == 1


# ── Multi-agent task, artifact and memory ledger ──

TASK_STATES = frozenset({"pending", "running", "blocked", "revision", "completed", "failed", "cancelled"})


def create_run_task(
    *, run_id: str, session_id: str, ordinal: int, title: str,
    description: str = "", success_criteria: str = "",
    context_artifact_id: str | None = None, assigned_model_id: str = "",
    parent_task_id: str | None = None, dependencies: list[str] | None = None,
    task_kind: str = "worker", actor_id: str = "", actor_label: str = "",
    task_id: str | None = None, depth: int = 0,
) -> dict[str, Any]:
    # Known kinds are canonical, but the set is open so a future AGI can
    # register new task kinds (e.g. "agi_agent") without a schema change.
    if not isinstance(task_kind, str) or not task_kind.strip():
        raise ValueError("task_kind must be a non-empty string")
    task_id = task_id or f"task_{uuid.uuid4().hex}"
    now = time.time()
    with _get_conn() as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO run_tasks
               (task_id, run_id, session_id, ordinal, parent_task_id, depth, task_kind, title,
                description, success_criteria, context_artifact_id,
                actor_id, actor_label, assigned_model_id, status, dependencies, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id, run_id, session_id, int(ordinal), parent_task_id,
                int(depth), task_kind, title, description, success_criteria, context_artifact_id,
                actor_id, actor_label, assigned_model_id, "pending",
                json.dumps(list(dependencies or []), ensure_ascii=False), now, now,
            ),
        )
        if cursor.rowcount == 1:
            _bump_run_projection(conn, run_id)
    return get_run_task(task_id) or {}


def get_run_task(task_id: str) -> dict[str, Any] | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM run_tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["dependencies"] = json.loads(result.get("dependencies") or "[]")
    except (TypeError, json.JSONDecodeError):
        result["dependencies"] = []
    return result


def list_run_tasks(run_id: str) -> list[dict[str, Any]]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT task_id FROM run_tasks WHERE run_id=? ORDER BY ordinal, created_at", (run_id,),
        ).fetchall()
    return [task for row in rows if (task := get_run_task(str(row["task_id"]))) is not None]


def update_run_task(
    task_id: str, *, status: str | None = None, result_artifact_id: str | None = None,
    context_artifact_id: str | None = None, increment_attempt: bool = False,
) -> bool:
    if status is not None and status not in TASK_STATES:
        raise ValueError(f"invalid task state: {status}")
    assignments: list[str] = ["updated_at=?"]
    values: list[Any] = [time.time()]
    if status is not None:
        assignments.append("status=?")
        values.append(status)
    if result_artifact_id is not None:
        assignments.append("result_artifact_id=?")
        values.append(result_artifact_id)
    if context_artifact_id is not None:
        assignments.append("context_artifact_id=?")
        values.append(context_artifact_id)
    if increment_attempt:
        assignments.append("attempt=attempt+1")
    values.append(task_id)
    with _get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE run_tasks SET {', '.join(assignments)} WHERE task_id=?", values,
        )
        if cursor.rowcount == 1:
            conn.execute(
                """UPDATE runs SET projection_revision=projection_revision+1
                   WHERE run_id=(SELECT run_id FROM run_tasks WHERE task_id=?)""",
                (task_id,),
            )
    return cursor.rowcount == 1


def create_artifact_record(
    *, artifact_id: str, run_id: str, session_id: str, kind: str,
    title: str, storage_path: str, content_hash: str, size_bytes: int,
    task_id: str | None = None, summary: str = "",
) -> dict[str, Any]:
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO artifacts
               (artifact_id, run_id, session_id, task_id, kind, title,
                storage_path, content_hash, size_bytes, summary, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                artifact_id, run_id, session_id, task_id, kind, title,
                storage_path, content_hash, int(size_bytes), summary, time.time(),
            ),
        )
        _bump_run_projection(conn, run_id)
    return get_artifact(artifact_id) or {}


def get_artifact(artifact_id: str) -> dict[str, Any] | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
    return dict(row) if row else None


def list_run_artifacts(run_id: str) -> list[dict[str, Any]]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at, artifact_id", (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _memory_overlap(existing: str, incoming: str) -> float:
    """Token-overlap ratio between two memory contents (0..1).

    CJK is tokenized per character so a single shared character is weak; the
    caller requires a high threshold (>= 0.90) before treating them as the same
    fact, and that threshold applies to the smaller token set.
    """
    import re as _re
    token_re = _re.compile(r"[A-Za-z0-9_\-]{2,}|[一-鿿]")
    a = set(token_re.findall(existing or ""))
    b = set(token_re.findall(incoming or ""))
    if not a or not b:
        return 0.0
    smaller = min(len(a), len(b))
    if smaller == 0:
        return 0.0
    return len(a & b) / smaller


def add_memory_record(
    *, session_id: str, scope: str, content: str, category: str = "general",
    run_id: str | None = None, task_id: str | None = None,
    source_ids: list[str] | None = None, reference_only: bool = True,
    dedup: bool = True,
) -> dict[str, Any]:
    if scope not in {"run", "task", "session", "project"}:
        raise ValueError("memory scope must be run, task, session or project")
    content = str(content or "").strip()
    if not content:
        raise ValueError("memory content is required")

    if dedup:
        # Idempotent writes: an exact or near-identical memory in the same
        # scope+category is not duplicated; its updated_at is refreshed and
        # its provenance (source_ids) merged.  This makes auto-memorize /
        # working-memory persistence safe to run repeatedly.
        existing = _find_duplicate_memory(
            session_id, scope, category, content, run_id=run_id, task_id=task_id,
        )
        if existing is not None:
            merged_sources = list(dict.fromkeys([
                *(existing.get("source_ids") or []),
                *(source_ids or []),
            ]))
            _update_memory_provenance(
                existing["memory_id"], merged_sources,
            )
            refreshed = get_memory(existing["memory_id"])
            return refreshed or existing

    memory_id = f"mem_{uuid.uuid4().hex}"
    now = time.time()
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO memories
               (memory_id, session_id, run_id, task_id, scope, category,
                content, source_ids, reference_only, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                memory_id, session_id, run_id, task_id, scope, category,
                str(redact_dict({"content": content})["content"]),
                json.dumps(list(source_ids or []), ensure_ascii=False),
                1 if reference_only else 0, "active", now, now,
            ),
        )
    return get_memory(memory_id) or {}


def _find_duplicate_memory(
    session_id: str, scope: str, category: str, content: str,
    *, run_id: str | None = None, task_id: str | None = None,
) -> dict[str, Any] | None:
    """Return an existing active memory in the same scope+category that is
    exact or highly overlapping, or None."""
    clauses = ["session_id=?", "status='active'", "scope=?", "category=?"]
    values: list[Any] = [session_id, scope, category]
    if scope in {"run", "task"}:
        if run_id is not None:
            clauses.append("run_id=?")
            values.append(run_id)
        if task_id is not None:
            clauses.append("task_id=?")
            values.append(task_id)
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT memory_id FROM memories WHERE {' AND '.join(clauses)} LIMIT 50",
            values,
        ).fetchall()
    for row in rows:
        mem = get_memory(str(row["memory_id"]))
        if mem is None:
            continue
        if str(mem.get("content") or "") == content:
            return mem
        if _memory_overlap(str(mem.get("content") or ""), content) >= 0.90:
            return mem
    return None


def _update_memory_provenance(memory_id: str, source_ids: list[str]) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE memories SET source_ids=?, updated_at=? WHERE memory_id=?",
            (
                json.dumps(source_ids, ensure_ascii=False),
                time.time(), memory_id,
            ),
        )


def get_memory(memory_id: str) -> dict[str, Any] | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["source_ids"] = json.loads(result.get("source_ids") or "[]")
    except (TypeError, json.JSONDecodeError):
        result["source_ids"] = []
    result["reference_only"] = bool(result.get("reference_only"))
    return result


def list_memories(
    session_id: str, *, scope: str | None = None, run_id: str | None = None,
    task_id: str | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = ["session_id=?", "status='active'"]
    values: list[Any] = [session_id]
    for field, value in (("scope", scope), ("run_id", run_id), ("task_id", task_id)):
        if value is not None:
            clauses.append(f"{field}=?")
            values.append(value)
    values.append(max(1, min(int(limit), 500)))
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT memory_id FROM memories WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
            values,
        ).fetchall()
    return [item for row in rows if (item := get_memory(str(row["memory_id"]))) is not None]

def upsert_run_event(session_id: str, event: dict[str, Any]) -> None:
    """Persist the latest immutable envelope for one typed event ID.

    Streaming events intentionally reuse an ``event_id`` while their payload and
    status advance.  ``ON CONFLICT`` makes that stream an idempotent ledger row
    rather than a sequence of duplicate UI deltas.
    """
    with _get_conn() as conn:
        run = conn.execute(
            "SELECT state, session_id FROM runs WHERE run_id=?",
            (str(event["run_id"]),),
        ).fetchone()
        if run is not None and (
            str(run["session_id"]) != str(session_id)
            or str(run["state"]) in TERMINAL_RUN_STATES
        ):
            # A terminal event is the transcript boundary.  Never admit a
            # detached provider/tool delta after settlement, and never attach
            # an event to a Run through another conversation identity.
            return
        prior = conn.execute(
            "SELECT run_id FROM run_events WHERE event_id=?",
            (str(event["event_id"]),),
        ).fetchone()
        cursor = conn.execute(
            """INSERT INTO run_events
               (event_id, run_id, session_id, workspace_id, task_id, sequence,
                timestamp, channel_id, parent_event_id, actor, type, status,
                payload, mode, part_id, artifact_ids, schema, revision)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET
                 run_id=excluded.run_id,
                 session_id=excluded.session_id,
                 workspace_id=excluded.workspace_id,
                 task_id=excluded.task_id,
                 sequence=excluded.sequence,
                 timestamp=excluded.timestamp,
                 channel_id=excluded.channel_id,
                 parent_event_id=excluded.parent_event_id,
                 actor=excluded.actor,
                 type=excluded.type,
                 status=excluded.status,
                 payload=excluded.payload,
                 mode=excluded.mode,
                 part_id=excluded.part_id,
                 artifact_ids=excluded.artifact_ids,
                 schema=excluded.schema,
                 revision=excluded.revision
               WHERE excluded.revision >= run_events.revision
            """,
            (
                str(event["event_id"]), str(event["run_id"]), session_id,
                str(event.get("workspace_id") or "") or None,
                str(event.get("task_id") or "") or None,
                int(event["sequence"]), str(event["timestamp"]), str(event["channel_id"]),
                event.get("parent_event_id"),
                json.dumps(redact_dict(event.get("actor") or {}), ensure_ascii=False, sort_keys=True),
                str(event["type"]), str(event.get("status") or "started"),
                json.dumps(redact_dict(event.get("payload") or {}), ensure_ascii=False, sort_keys=True),
                normalize_mode(event.get("mode")),
                str(event.get("part_id") or ""),
                json.dumps(list(event.get("artifact_ids") or []), ensure_ascii=False),
                str(event.get("schema") or "modus.agent-event.v2"),
                int(event.get("revision") or 0),
            ),
        )
        if cursor.rowcount == 1:
            run_id = str(event["run_id"])
            _bump_run_projection(conn, run_id)
            prior_run_id = str(prior["run_id"]) if prior else ""
            if prior_run_id and prior_run_id != run_id:
                _bump_run_projection(conn, prior_run_id)


def get_run_events(run_id: str) -> list[dict[str, Any]]:
    """Return the latest audited event state in deterministic timeline order."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM run_events WHERE run_id=? ORDER BY sequence ASC, event_id ASC", (run_id,),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        event = dict(row)
        event["mode"] = normalize_mode(event.get("mode"))
        for key in ("actor", "payload", "artifact_ids"):
            try:
                event[key] = json.loads(event[key] or ("[]" if key == "artifact_ids" else "{}"))
            except (TypeError, json.JSONDecodeError):
                event[key] = [] if key == "artifact_ids" else {}
        events.append(event)
    return events


def get_run_events_since(run_id: str, since_sequence: int = 0) -> list[dict[str, Any]]:
    """Return a bounded transcript suffix, including the cursor sequence.

    The lower bound is intentionally inclusive.  A streaming part keeps its
    sequence while its ``revision`` advances, so replaying the last observed
    sequence lets the client receive a final replacement that may have been
    committed just after a transport disconnect.  The browser EventStore
    de-duplicates that overlap by ``event_id``/``revision``.
    """
    bounded = max(1, int(since_sequence or 0))
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM run_events WHERE run_id=? AND sequence>=? "
            "ORDER BY sequence ASC, event_id ASC",
            (run_id, bounded),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        event = dict(row)
        event["mode"] = normalize_mode(event.get("mode"))
        for key in ("actor", "payload", "artifact_ids"):
            try:
                event[key] = json.loads(event[key] or ("[]" if key == "artifact_ids" else "{}"))
            except (TypeError, json.JSONDecodeError):
                event[key] = [] if key == "artifact_ids" else {}
        events.append(event)
    return events


def get_run_event_cursor(run_id: str) -> int:
    """Return the greatest sequence persisted for a run, or zero if unknown."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS cursor FROM run_events WHERE run_id=?",
            (run_id,),
        ).fetchone()
    return int(row["cursor"] if row is not None else 0)


def get_latest_session_run_events(session_id: str) -> list[dict[str, Any]]:
    run = latest_run_for_session(session_id)
    return get_run_events(str(run["run_id"])) if run else []


def create_context_compaction(
    *, session_id: str, summary: str, omitted_count: int, tail_count: int,
    run_id: str | None = None, cutoff_message_id: int | None = None,
) -> dict[str, Any]:
    """Persist the exact model-context boundary selected after one Run.

    Message rows remain immutable audit history.  This record only controls
    how the next in-memory model context is reconstructed after a restart.
    """
    compaction_id = f"cmp_{uuid.uuid4().hex}"
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO context_compactions
               (compaction_id, session_id, run_id, summary, omitted_count,
                tail_count, cutoff_message_id, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                compaction_id, session_id, run_id,
                str(redact_dict({"summary": summary})["summary"]),
                max(0, int(omitted_count)), max(1, int(tail_count)),
                int(cutoff_message_id) if cutoff_message_id is not None else None,
                time.time(),
            ),
        )
    return get_latest_context_compaction(session_id) or {}


def get_latest_context_compaction(session_id: str) -> dict[str, Any] | None:
    with _get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM context_compactions WHERE session_id=?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None

def create_session(
    title: str = "新对话", system_prompt: str = "", mode: str = DEFAULT_MODE,
    model_id: str = "", mode_config: dict[str, Any] | None = None,
    reasoning_effort: str = "", workspace_id: str = "",
    owner_id: str = "",
    default_to_current_workspace: bool = True,
) -> dict:
    sess_id = uuid.uuid4().hex[:12]
    now = time.time()
    if not owner_id:
        from modus.desktop import accounts

        owner_id = str(accounts.ensure_default_user()["user_id"])
    with _get_conn() as conn:
        if not workspace_id and default_to_current_workspace:
            from modus.desktop.workspace import WorkspaceIdentity

            workspace = WorkspaceIdentity.current()
            conn.execute(
                """INSERT INTO workspaces (workspace_id, root, name, created_at, updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(root) DO UPDATE SET updated_at=excluded.updated_at""",
                (workspace.workspace_id, workspace.root, workspace.name, now, now),
            )
            workspace_id = workspace.workspace_id
        conn.execute(
            """INSERT INTO sessions
               (id, workspace_id, owner_id, title, mode, worldview, system_prompt, model_id, mode_config, reasoning_effort, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sess_id, workspace_id or None, owner_id, title, normalize_mode(mode), "", system_prompt, model_id,
                json.dumps(redact_dict(mode_config or {}), ensure_ascii=False, sort_keys=True),
                str(reasoning_effort or ""),
                now, now,
            ),
        )
    return get_session(sess_id)


def get_session(session_id: str, *, owner_id: str = "") -> dict | None:
    with _get_conn() as conn:
        if owner_id:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id=? AND owner_id=?",
                (session_id, owner_id),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["mode"] = normalize_mode(result.get("mode"))
    try:
        result["mode_config"] = json.loads(result.get("mode_config") or "{}")
    except (TypeError, json.JSONDecodeError):
        result["mode_config"] = {}
    return result


def _session_catalog_filter(
    *, include_archived: bool, query: str, owner_id: str = "",
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if owner_id:
        clauses.append("s.owner_id=?")
        params.append(owner_id)
    if not include_archived:
        clauses.append("s.archived=0")
    normalized_query = str(query or "").strip().lower()
    if normalized_query:
        # Search text is literal user input.  Without escaping, SQLite treats
        # ``%`` and ``_`` as wildcards, so a query for a path, identifier or
        # percentage can silently match unrelated conversations.
        escaped_query = (
            normalized_query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped_query}%"
        clauses.append(
            "(LOWER(s.title) LIKE ? ESCAPE '\\' OR EXISTS ("
            "SELECT 1 FROM messages searched "
            "WHERE searched.session_id=s.id "
            "AND LOWER(searched.content) LIKE ? ESCAPE '\\'))"
        )
        params.extend((pattern, pattern))
    return (" AND ".join(clauses) or "1=1"), params


def session_catalog_page(
    limit: int = 50, *, include_archived: bool = False, query: str = "",
    cursor: tuple[float, str] | None = None, owner_id: str = "",
) -> dict[str, Any]:
    """Return one stable, filtered session-catalog page and exact totals.

    The keyset cursor follows ``updated_at DESC, id DESC``.  Applying archive
    and full-message search filters before the cursor and limit prevents hidden
    archived rows from consuming active-session capacity.
    """
    bounded_limit = max(1, min(int(limit), 100))
    where, params = _session_catalog_filter(
        include_archived=include_archived, query=query, owner_id=owner_id,
    )
    all_where, all_params = _session_catalog_filter(
        include_archived=True, query=query, owner_id=owner_id,
    )
    page_where = where
    page_params = list(params)
    if cursor is not None:
        cursor_updated_at, cursor_id = cursor
        page_where += " AND (s.updated_at<? OR (s.updated_at=? AND s.id<?))"
        page_params.extend((float(cursor_updated_at), float(cursor_updated_at), str(cursor_id)))
    select = """SELECT s.id, s.title, s.mode, s.archived, s.model_id,
                       s.worldview, s.created_at, s.updated_at,
                       (SELECT content FROM messages latest
                        WHERE latest.session_id=s.id
                        ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1)
                       AS last_message
                FROM sessions s"""
    with _get_conn() as conn:
        rows = conn.execute(
            f"{select} WHERE {page_where} "
            "ORDER BY s.updated_at DESC, s.id DESC LIMIT ?",
            (*page_params, bounded_limit + 1),
        ).fetchall()
        total = int(conn.execute(
            f"SELECT COUNT(*) AS total FROM sessions s WHERE {where}", params,
        ).fetchone()["total"])
        active_total = int(conn.execute(
            f"SELECT COUNT(*) AS total FROM sessions s "
            f"WHERE ({all_where}) AND s.archived=0",
            all_params,
        ).fetchone()["total"])
        archived_total = int(conn.execute(
            f"SELECT COUNT(*) AS total FROM sessions s "
            f"WHERE ({all_where}) AND s.archived=1",
            all_params,
        ).fetchone()["total"])
    has_more = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    sessions = [
        dict(row) | {"mode": normalize_mode(row["mode"])} for row in rows
    ]
    next_cursor = None
    if has_more and sessions:
        last = sessions[-1]
        next_cursor = (float(last["updated_at"]), str(last["id"]))
    return {
        "sessions": sessions, "total": total,
        "active_total": active_total, "archived_total": archived_total,
        "has_more": has_more, "next_cursor": next_cursor,
    }


def list_sessions(
    limit: int = 50, *, include_archived: bool = False, owner_id: str = "",
) -> list[dict]:
    """Compatibility helper for callers that do not need catalog pagination.

    The public catalog deliberately caps each page at 100 rows, while internal
    maintenance callers may ask for a larger bounded snapshot (for example,
    repairing every persisted session after a model is removed).  Walk the
    keyset cursor here so those callers do not silently receive only page one.
    """
    requested = max(0, int(limit))
    sessions: list[dict] = []
    cursor: tuple[float, str] | None = None
    while len(sessions) < requested:
        page = session_catalog_page(
            min(100, requested - len(sessions)),
            include_archived=include_archived, cursor=cursor, owner_id=owner_id,
        )
        sessions.extend(page["sessions"])
        cursor = page["next_cursor"]
        if not page["has_more"] or cursor is None:
            break
    return sessions


def update_session(session_id: str, **kwargs) -> None:
    if not kwargs:
        return
    if "mode_config" in kwargs and not isinstance(kwargs["mode_config"], str):
        kwargs["mode_config"] = json.dumps(
            redact_dict(kwargs["mode_config"] or {}), ensure_ascii=False, sort_keys=True,
        )
    if "mode" in kwargs:
        kwargs["mode"] = normalize_mode(kwargs["mode"])
    allowed = {"workspace_id", "title", "mode", "archived", "worldview", "world_view_history", "system_prompt", "model_id", "mode_config", "reasoning_effort"}
    unknown = set(kwargs) - allowed
    if unknown:
        raise ValueError("unsupported session fields: " + ", ".join(sorted(unknown)))
    kwargs["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [session_id]
    with _get_conn() as conn:
        conn.execute(f"UPDATE sessions SET {sets} WHERE id=?", vals)


def delete_session(session_id: str) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


# ── Message CRUD ──

def add_message(session_id: str, role: str, content: str = "", tool_calls: list | None = None, token_count: int = 0, tool_call_id: str | None = None) -> None:
    if token_count <= 0:
        # Populate the audit token estimate so the durable ledger is usable
        # for budgeting and restore decisions without trusting caller call
        # sites to remember to fill it.  A zero/omitted count means "estimate".
        token_count = _estimate_message_tokens(role, content, tool_calls or [])
    with _get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, token_count, created_at, tool_call_id) VALUES (?,?,?,?,?,?,?)",
                (session_id, role, content, json.dumps(tool_calls or []), token_count, time.time(), tool_call_id or None),
            )
            conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id))
        except sqlite3.IntegrityError:
            # 忽略外键约束错误
            pass


def _estimate_message_tokens(role: str, content: str, tool_calls: list) -> int:
    """Rough chars/4 token estimate for one persisted message row."""
    total = len(str(content or ""))
    for tc in tool_calls:
        total += len(json.dumps(tc))
    return max(1, total // 4)


def get_messages(session_id: str, limit: int = 200) -> list[dict]:
    """Return the newest bounded context window in chronological order.

    The inner descending query chooses the recent tail.  The outer ascending
    query restores model-context order; ``id`` makes equal timestamps stable
    in both directions.
    """
    bounded = max(1, min(int(limit), 1_000))
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM (
                   SELECT * FROM messages WHERE session_id=?
                   ORDER BY created_at DESC, id DESC LIMIT ?
               ) AS recent ORDER BY created_at ASC, id ASC""",
            (session_id, bounded),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["tool_calls"] = json.loads(d.get("tool_calls", "[]"))
        result.append(d)
    return result


def get_latest_assistant_message(session_id: str) -> str:
    """Return the most recent assistant message content, or '' if none."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='assistant' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        return ""
    return str(row["content"] or "")


def get_context_compaction_boundary(
    session_id: str, tail_count: int,
) -> dict[str, int]:
    """Return one ID-ordered boundary and count from the durable message log."""
    tail = max(1, int(tail_count))
    with _get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE((
                          SELECT id FROM messages
                          WHERE session_id=?
                          ORDER BY id DESC LIMIT 1 OFFSET ?
                      ), 0) AS cutoff_message_id
               FROM messages WHERE session_id=?""",
            (session_id, tail, session_id),
        ).fetchone()
        system_rows = conn.execute(
            """SELECT id, content FROM messages
               WHERE session_id=? AND role='system' ORDER BY id ASC""",
            (session_id,),
        ).fetchall()
    total = int(row["total"] if row is not None else 0)
    cutoff = int(row["cutoff_message_id"] if row is not None else 0)
    from modus.agent.compressor import SUMMARY_PREFIX

    contract_id = next(
        (
            int(item["id"]) for item in system_rows
            if not str(item["content"] or "").startswith(SUMMARY_PREFIX)
        ),
        0,
    )
    return {
        "omitted_count": max(0, total - tail - int(0 < contract_id <= cutoff)),
        "cutoff_message_id": cutoff,
    }


def get_messages_after_context_compaction(
    session_id: str, cutoff_message_id: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return the original system contract and every message after an ID boundary.

    There is intentionally no silent limit: these rows were the active model
    context immediately before restart. The normal compression gate will bound
    them again before the next provider request when required.
    """
    from modus.agent.compressor import SUMMARY_PREFIX

    cutoff = max(0, int(cutoff_message_id))
    with _get_conn() as conn:
        system_rows = conn.execute(
            """SELECT * FROM messages
               WHERE session_id=? AND role='system' ORDER BY id ASC""",
            (session_id,),
        ).fetchall()
        head = next(
            (
                row for row in system_rows
                if not str(row["content"] or "").startswith(SUMMARY_PREFIX)
            ),
            None,
        )
        rows = conn.execute(
            """SELECT * FROM messages WHERE session_id=? AND id>?
               ORDER BY id ASC""",
            (session_id, cutoff),
        ).fetchall()

    def decode(row: sqlite3.Row) -> dict[str, Any]:
        message = dict(row)
        try:
            message["tool_calls"] = json.loads(message.get("tool_calls") or "[]")
        except (TypeError, json.JSONDecodeError):
            message["tool_calls"] = []
        return message

    head_id = int(head["id"]) if head is not None else 0
    return (
        decode(head) if head is not None else None,
        [decode(row) for row in rows if int(row["id"]) != head_id],
    )


def get_legacy_messages(session_id: str, limit: int = 200) -> list[dict]:
    """Return only message rows that predate this session's typed transcript.

    New Agent runs intentionally persist both model-context ``messages`` and a
    user-visible ``run_events`` transcript.  Replaying all message rows beside
    typed events duplicates modern runs, while replaying none hides the legacy
    prefix of a conversation migrated in place.  The first Run that owns a
    typed event is the durable migration boundary because its Run row is
    created before any messages from that Run are persisted.
    """
    bounded = max(1, min(int(limit), 1_000))
    with _get_conn() as conn:
        cutoff_row = conn.execute(
            """SELECT MIN(r.started_at) AS started_at
               FROM runs r JOIN run_events e ON e.run_id=r.run_id
               WHERE r.session_id=?""",
            (session_id,),
        ).fetchone()
        cutoff = cutoff_row["started_at"] if cutoff_row is not None else None
        if cutoff is None:
            rows = conn.execute(
                """SELECT * FROM (
                       SELECT * FROM messages WHERE session_id=?
                       ORDER BY created_at DESC, id DESC LIMIT ?
                   ) AS recent ORDER BY created_at ASC, id ASC""",
                (session_id, bounded),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM (
                       SELECT * FROM messages
                       WHERE session_id=? AND created_at<?
                       ORDER BY created_at DESC, id DESC LIMIT ?
                   ) AS recent ORDER BY created_at ASC, id ASC""",
                (session_id, float(cutoff), bounded),
            ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        message = dict(row)
        try:
            message["tool_calls"] = json.loads(message.get("tool_calls") or "[]")
        except (TypeError, json.JSONDecodeError):
            message["tool_calls"] = []
        result.append(message)
    return result


# ── 会话恢复：从 DB 重建内存状态 ──

def restore_session(session_id: str, *, owner_id: str = "") -> dict | None:
    """从 DB 恢复完整的会话状态，用于断线重连"""
    sess = get_session(session_id, owner_id=owner_id)
    if not sess:
        return None
    compaction = get_latest_context_compaction(session_id)
    sess["context_compaction"] = compaction
    cutoff_message_id = (
        compaction.get("cutoff_message_id") if isinstance(compaction, dict) else None
    )
    sess["messages"] = get_messages(session_id)
    if cutoff_message_id is not None:
        try:
            cutoff = int(cutoff_message_id)
        except (TypeError, ValueError):
            cutoff = None
        if cutoff is not None:
            head, retained = get_messages_after_context_compaction(session_id, cutoff)
            sess["context_messages"] = ([head] if head is not None else []) + retained
    return sess


def delete_user_data(user_id: str) -> None:
    """级联删除某用户 owner 的全部数据（会话/运行/工作区/计费）。

    会话删除由 sessions 外键级联到 messages/run_events/runs/run_tasks/
    approvals/artifacts/memories/context_compactions。billing_ledger 与
    recharge_records 无外键，须显式删除。
    """
    uid = str(user_id or "")
    if not uid:
        return
    with _get_conn() as conn:
        for table in ("billing_ledger", "recharge_records"):
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM sessions WHERE owner_id=?", (uid,))
        conn.execute("DELETE FROM runs WHERE owner_id=?", (uid,))
        conn.execute("DELETE FROM workspaces WHERE owner_id=?", (uid,))
        conn.execute("DELETE FROM account_workspaces WHERE owner_id=?", (uid,))
