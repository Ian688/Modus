"""Modus Desktop SQLite persistence."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import suppress
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modus.redact import redact_dict, redact_text
from modus.modes import DEFAULT_MODE, normalize_mode
from modus.paths import data_dir

logger = logging.getLogger(__name__)

DB_DIR = data_dir()
DB_PATH = DB_DIR / "desktop.db"

# Current schema revision, tracked with ``PRAGMA user_version``.  Every schema
# change is an entry in ``_MIGRATIONS`` keyed by the version it introduces;
# ``init_db`` walks forward from the database's stored version, backing up
# before each step.  The first numbered version ``2`` captures every ALTER the
# codebase historically applied ad hoc through ``_ensure_column``.  Version 1
# (the pre-history base schema) is never stamped: an unversioned database is
# treated as already at version 1 so old data migrates rather than being
# rebuilt.  A database from a *newer* version is refused, never downgraded.
#
# Version 4 (Wave5 E3) turns ``messages`` into a session tree: every row gains
# a ``parent_message_id`` link and a ``branch_root_id`` (NULL = mainline), and
# branch/revert operations are recorded in the new ``session_branches`` table
# whose latest row is the current leaf pointer.  Switching branches never
# copies history and never rewrites existing rows.
SCHEMA_VERSION = 4

# Each forward migration is (target_version, fn(conn)).  Steps run inside one
# transaction each and bump ``user_version`` on success, so a crash never
# leaves a half-applied step at its target version.  ``_MIGRATIONS`` itself is
# defined after the migration functions it references (see below).

# Lock file guarding exclusive write access to desktop.db across processes.
# Writers (CLI/Desktop/MCP/spawn subprocesses) hold a shared file descriptor
# flock for their lifetime; a second writer is refused with an explicit
# "another instance is running" error.  Read-only queries never touch the
# lease: WAL already allows many readers with one writer, so recall / index
# reads stay concurrent with a live writer.
_LOCK_FILENAME = "instance.lock"


class WriterLeaseError(RuntimeError):
    """Raised when another Modus instance holds the writer lease on desktop.db."""


class _WriterLease:
    """An advisory exclusive writer lease on the shared desktop.db.

    Holds an open handle on ``<DB_DIR>/instance.lock`` with an exclusive
    ``fcntl.flock`` / ``msvcrt.locking`` for the life of the object.  The
    lock is released automatically when the handle closes or the process
    exits (the OS drops the lock), so a crash cannot leave the database
    permanently locked.  ``__del__`` is a best-effort backstop.
    """

    def __init__(self, lock_path: Path) -> None:
        self._path = lock_path
        self._file: Any = None
        self._acquired = False
        self._os = os.name
        self._no_locking = False

    def _try_lock(self, f: Any) -> bool:
        if self._no_locking:
            return False
        if self._os == "nt":
            try:
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        try:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except ImportError:
            # No advisory-lock primitive on this platform: fail soft by
            # treating the lease as "not contested" so a headless/server
            # context is never blocked by an unavailable mechanism.
            self._no_locking = True
            logger.warning(
                "no fcntl/msvcrt on this platform; writer lease disabled (%s)",
                self._path,
            )
            return False
        except OSError:
            return False

    def acquire(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            f = open(self._path, "a+", encoding="utf-8")
        except OSError:
            # Cannot create the lock file at all: fail soft rather than block
            # legitimate single-instance use (for example an unwritable or
            # read-only data directory).
            logger.warning("cannot open writer lease %s; continuing without lock", self._path)
            return True
        # Ensure the lock file carries at least one byte: ``msvcrt.locking``
        # is unreliable on an empty region on Windows.
        with suppress(OSError):
            if f.tell() == 0:
                f.write("modus\n")
                f.flush()
            f.seek(0)
        if self._try_lock(f):
            self._file = f
            self._acquired = True
            return True
        if self._no_locking:
            # Platform has no locking primitive: proceed unlocked but keep the
            # handle for symmetry (release becomes a no-op).
            self._file = f
            self._acquired = True
            return True
        # A real contention: another instance holds the lease.
        f.close()
        return False

    def release(self) -> None:
        if self._file is not None:
            try:
                if self._os == "nt":
                    import msvcrt

                    with suppress(OSError):
                        self._file.seek(0)
                        msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    with suppress(Exception):
                        import fcntl

                        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            with suppress(Exception):
                self._file.close()
            self._file = None
        self._acquired = False

    def close(self) -> None:
        self.release()

    def __enter__(self) -> "_WriterLease":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover - best-effort backstop
        try:
            self.release()
        except Exception:
            pass


# Current process's lease object.  ``acquire_writer_lease`` / ``release_*``
# are idempotent so CLI/Desktop startup can share the acquired lease without
# double-locking.
_writer_lease: _WriterLease | None = None


def _lock_path() -> Path:
    return DB_DIR / _LOCK_FILENAME


def acquire_writer_lease() -> bool:
    """Try to take the exclusive writer lease on desktop.db.

    Returns True when this process now holds the lease (or already holds it).
    Returns False when another Modus instance holds it, which the caller must
    surface as "another instance is running".  Fail-soft: if the lease cannot
    be taken for environmental reasons (no fcntl/msvcrt, unwritable dir) it
    returns True so a headless/server context is never blocked.
    """
    global _writer_lease
    if _writer_lease is not None:
        return True
    lease = _WriterLease(_lock_path())
    if not lease.acquire():
        return False
    _writer_lease = lease
    return True


def release_writer_lease() -> None:
    """Drop the process's writer lease, if it holds one (idempotent)."""
    global _writer_lease
    if _writer_lease is not None:
        lease = _writer_lease
        _writer_lease = None
        lease.release()


# True once the process has validated the database at least once (startup
# quick_check).  Tests that swap ``DB_PATH`` per-case reset it explicitly.
_integrity_checked = False
# Reentrancy guard so corruption recovery never recursively recovers itself.
_recovering = False
# Periodic WAL checkpoint cadence (approximate, per _get_conn open).
_checkpoint_calls = 0
_CHECKPOINT_EVERY = 128


def _trajectory_default(value: Any) -> Any:
    """JSON fallback for trajectory serialization (dataclass-aware).

    Keeps ``persist_trajectory`` and the tool-call fingerprint writer safe for
    dataclass payloads (e.g. ``ToolResult``) without leaking a repr that a later
    ``json.dumps`` could not round-trip.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: getattr(value, field.name)
            for field in fields(value)
            if hasattr(value, field.name)
        }
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, (Path,)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def _summarize_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    """Fold one tool call into a bounded, fingerprintable trajectory summary.

    The raw input/output payloads are redacted and capped so the durable ledger
    stays bounded; the ``sha256`` of the *original* call makes the summary
    verifiable without re-running the agent.  Used by ``upsert_run_event`` /
    ``settle_run_event`` to give every run_events row an evaluable ``tool_calls``
    column.
    """
    if not isinstance(call, dict):
        call = {"raw": call}
    name = str(call.get("name") or call.get("function", {}).get("name") or call.get("id") or "tool")
    raw_input = call.get("input")
    if not isinstance(raw_input, dict) and isinstance(raw_input, str):
        try:
            raw_input = json.loads(raw_input)
        except (TypeError, json.JSONDecodeError):
            raw_input = {"value": raw_input}
    if not isinstance(raw_input, dict):
        raw_input = {"value": raw_input}
    result = call.get("result")
    if isinstance(result, dict):
        display = str(result.get("display_summary") or result.get("text") or result.get("output") or "")
    elif isinstance(result, str):
        display = result
    else:
        display = str(result or "")
    output = str(call.get("output") or call.get("display_summary") or display or "")
    fingerprint = hashlib.sha256(
        json.dumps(call, ensure_ascii=False, sort_keys=True, default=_trajectory_default).encode("utf-8"),
    ).hexdigest()
    return {
        "name": name,
        "input_summary": str(redact_dict(raw_input) if isinstance(raw_input, dict) else raw_input)[:1000],
        "output_summary": str(redact_text(output))[:1000],
        "sha256": fingerprint,
    }


def _extract_tool_calls(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect evaluable tool-call summaries for one Agent event.

    Prefers the explicit ``payload.tool_calls`` list (already summarized by the
    emitter) and falls back to deriving one summary from a tool_call/tool_result
    payload, so a minimal hand-built event still yields an evaluable trajectory.
    """
    payload = event.get("payload")
    if isinstance(payload, dict):
        explicit = payload.get("tool_calls")
        if isinstance(explicit, list):
            return [_summarize_tool_call(call) for call in explicit]
        if "tool_call_id" in payload:
            return [_summarize_tool_call(payload)]
    return []


def _final_result_text(event_type: str, payload: dict[str, Any],
                       *, conn: sqlite3.Connection | None = None,
                       run_id: str = "") -> str:
    """Derive the run's textual outcome for the ``final_result`` column.

    Priority:
    1. the terminal payload's ``message`` (run_error message, or a completion
       message when the caller supplied one);
    2. the latest ``host_response`` ``markdown``/``text`` already persisted in
       run_events (the agent's answer to the user's request) — queried on the
       settlement connection when provided;
    3. a ``text`` fallback on the completed payload itself.
    """
    message = str(payload.get("message") or "").strip()
    if message:
        return message
    if conn is not None and run_id:
        try:
            row = conn.execute(
                """SELECT payload FROM run_events
                   WHERE run_id=? AND type='host_response'
                   ORDER BY sequence ASC, event_id ASC""",
                (run_id,),
            ).fetchall()
            for r in reversed(row):
                try:
                    data = json.loads(r["payload"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    data = {}
                if isinstance(data, dict):
                    text = str(data.get("markdown") or data.get("text") or "").strip()
                    if text:
                        return text
        except sqlite3.Error:
            pass
    if event_type == "run_completed":
        return str(payload.get("text") or "").strip()
    return ""


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


def _backup_before_migrate(target_version: int) -> None:
    """Atomically snapshot desktop.db before a migration.

    Uses SQLite's online backup API so the copy is guaranteed consistent even
    mid-WAL.  A partial copy is never renamed into place, so the previous good
    backup is preserved on failure.  Best-effort: a migration that cannot back
    up still proceeds so a read-only data directory never blocks the migration
    itself.
    """
    if not DB_PATH.exists():
        return
    backup_dir = DB_DIR / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"db-v{target_version}.bak"
    if dest.exists():
        return
    # A brand-new database (opened but never populated) has nothing worth
    # backing up; only snapshot once schema tables already exist.
    try:
        probe = sqlite3.connect(str(DB_PATH))
        try:
            has_tables = bool(probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1",
            ).fetchone())
        finally:
            probe.close()
    except sqlite3.Error:
        has_tables = True
    if not has_tables:
        return
    tmp = dest.with_suffix(".bak.tmp")
    try:
        with suppress(OSError):
            tmp.unlink()
        src = sqlite3.connect(str(DB_PATH))
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.replace(tmp, dest)
    except (sqlite3.Error, OSError) as exc:
        with suppress(OSError):
            tmp.unlink()
        logger.warning(
            "schema migration backup to %s failed (%s); continuing", dest, exc,
        )


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Version 2: absorb every column evolution from the pre-version era.

    The base schema script creates the full modern table shapes, so on a fresh
    database these adds are all no-ops.  On a genuine v1 (pre-version)
    database they add the columns the codebase used to add ad hoc, then
    rebuild the secondary indexes and backfill the workspace/owner identity
    the same way ``init_db`` always did — preserving user history in place.
    """
    for table, column, declaration in _MIGRATIONS_V2_COLUMNS:
        _ensure_column(conn, table, column, declaration)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_events_task ON run_events(task_id, sequence)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_id, updated_at DESC)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_workspace ON runs(workspace_id, started_at DESC)",
    )
    conn.execute("DROP INDEX IF EXISTS idx_runs_client_request")
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
    _backfill_identity(conn)


_MIGRATIONS_V2_COLUMNS: list[tuple[str, str, str]] = [
    ("sessions", "workspace_id", "TEXT"),
    ("runs", "workspace_id", "TEXT"),
    ("runs", "client_request_id", "TEXT"),
    ("runs", "client_request_fingerprint", "TEXT"),
    ("runs", "projection_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("run_events", "workspace_id", "TEXT"),
    ("run_events", "task_id", "TEXT"),
    ("run_events", "artifact_ids", "TEXT NOT NULL DEFAULT '[]'"),
    ("run_events", "schema", "TEXT NOT NULL DEFAULT 'modus.agent-event.v2'"),
    ("run_tasks", "task_kind", "TEXT NOT NULL DEFAULT 'worker'"),
    ("run_tasks", "depth", "INTEGER NOT NULL DEFAULT 0"),
    ("run_tasks", "actor_id", "TEXT NOT NULL DEFAULT ''"),
    ("run_tasks", "actor_label", "TEXT NOT NULL DEFAULT ''"),
    ("memories", "embedding", "TEXT"),
    ("context_compactions", "cutoff_message_id", "INTEGER"),
    ("messages", "tool_call_id", "TEXT"),
    ("sessions", "owner_id", "TEXT"),
    ("runs", "owner_id", "TEXT"),
    ("workspaces", "owner_id", "TEXT"),
    ("account_workspaces", "is_default", "INTEGER NOT NULL DEFAULT 0"),
]

def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Version 3: make run_events an evaluable trajectory (Wave5 E1).

    ``run_events`` already records the ``modus.agent-event.v2`` stream; v3 adds
    the fields that let an offline Evaluator join a scenario against a run
    without re-running the agent:
    - ``tool_calls``: a JSON summary of the tool calls that produced this event
      (name / input / output摘要 + sha256 fingerprint of the full call), so a
      trajectory is self-describing even after the terminal budget snapshot
      replaces individual tool payloads.
    - ``objective`` (runs): the user request the run was admitted for, used as
      the scenario reference point by ``modus evaluate``.
    - ``final_result`` (runs): the run's textual outcome (the terminal event
      message or the final host_response) so a scenario can score the answer
      even when the run failed.
    """
    for table, column, declaration in _MIGRATIONS_V3_COLUMNS:
        _ensure_column(conn, table, column, declaration)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_events_toolcalls ON run_events(tool_calls)",
    )


_MIGRATIONS_V3_COLUMNS: list[tuple[str, str, str]] = [
    ("run_events", "tool_calls", "TEXT NOT NULL DEFAULT '[]'"),
    ("runs", "objective", "TEXT NOT NULL DEFAULT ''"),
    ("runs", "final_result", "TEXT NOT NULL DEFAULT ''"),
]


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Version 4: session tree fields on ``messages`` + branch leaf pointer.

    Wave5 E3 turns the linear message log into a tree.  On a fresh database
    the base schema script already creates these columns and the
    ``session_branches`` table, so every ALTER below is a no-op; on a genuine
    v3 database they are added in place.  No rows are rewritten and no
    per-session anchor is seeded: ``current_session_leaf`` falls back to the
    newest message when no branch row exists, which keeps every pre-existing
    linear history on the implicit mainline.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS session_branches (
               branch_id TEXT PRIMARY KEY,
               session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
               message_id INTEGER NOT NULL,
               branch_root_id INTEGER,
               created_at REAL NOT NULL
           )""",
    )
    for table, column, declaration in _MIGRATIONS_V4_COLUMNS:
        _ensure_column(conn, table, column, declaration)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_branches_session "
        "ON session_branches(session_id, created_at DESC)",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_branch ON messages(branch_root_id, id)",
    )


_MIGRATIONS_V4_COLUMNS: list[tuple[str, str, str]] = [
    ("messages", "parent_message_id", "INTEGER"),
    ("messages", "branch_root_id", "INTEGER"),
]


_MIGRATIONS: list[tuple[int, Any]] = [
    (2, _migrate_v2),
    (3, _migrate_v3),
    (4, _migrate_v4),
]


def _backfill_identity(conn: sqlite3.Connection) -> None:
    """Backfill workspace/owner identity onto pre-version rows, as init_db did."""
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


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Walk desktop.db forward to ``SCHEMA_VERSION`` using ``PRAGMA user_version``.

    Version 0 or 1 (an unversioned pre-T4 database) is treated as version 1
    so existing history migrates in place rather than being rebuilt.  Each
    step is backed up to ``~/.modus/backup/db-v{n}.bak`` before it runs and is
    committed in one transaction together with its ``user_version`` bump, so a
    crash never leaves a database stamped with a version whose schema half
    applied.  A database stamped newer than the current code is refused with an
    explicit error — never silently downgraded.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            "database schema version {} is newer than this build supports ({}) — "
            "拒绝打开：数据库来自更新版本，请升级 Modus 后再打开".format(version, SCHEMA_VERSION)
        )
    if version >= SCHEMA_VERSION:
        return
    starting = version or 1
    for target, apply in _MIGRATIONS:
        if target <= starting:
            continue
        _backup_before_migrate(target)
        conn.execute("BEGIN")
        try:
            apply(conn)
            conn.execute(f"PRAGMA user_version={int(target)}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        logger.info("desktop.db migrated to schema v%s", target)


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
    """建表（幂等），并把 schema 演进收敛到版本化迁移。

    ``PRAGMA user_version`` 是演进权威：启动先拒绝来自更新版本的数据库
    （绝不静默降级），再建表，最后 ``migrate_schema`` 逐个前向迁移并迁移前
    备份到 ``~/.modus/backup/db-v{n}.bak``。
    """
    if DB_PATH.exists():
        try:
            probe = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            try:
                version = int(probe.execute("PRAGMA user_version").fetchone()[0] or 0)
            finally:
                probe.close()
        except sqlite3.DatabaseError:
            version = 0
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                "database schema version {} is newer than this build supports ({}) — "
                "拒绝打开：数据库来自更新版本，请升级 Modus 后再打开".format(version, SCHEMA_VERSION)
            )
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
                created_at REAL NOT NULL,
                parent_message_id INTEGER,
                branch_root_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS session_branches (
                branch_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL,
                branch_root_id INTEGER,
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
                revision INTEGER NOT NULL DEFAULT 0,
                tool_calls TEXT NOT NULL DEFAULT '[]'
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
                objective TEXT NOT NULL DEFAULT '',
                final_result TEXT NOT NULL DEFAULT '',
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
            CREATE INDEX IF NOT EXISTS idx_session_branches_session
                ON session_branches(session_id, created_at DESC);
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
        # Schema evolution is versioned and transactional: ``migrate_schema``
        # walks ``PRAGMA user_version`` forward through ``_MIGRATIONS``,
        # backing up before each step, and backfills workspace/owner identity
        # exactly as the ad-hoc ``_ensure_column`` calls below used to.
        migrate_schema(conn)


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
    objective: str = "",
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
                config_snapshot, objective, started_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, session_id, client_request_id or None,
                client_request_fingerprint or None, workspace_id or None,
                owner_id or None, normalize_mode(mode),
                "running", 0, snapshot, str(objective or ""), now, now,
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
    objective: str = "",
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
                    projection_revision, config_snapshot, objective, started_at,
                    updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, session_id, client_request_id or None,
                    client_request_fingerprint or None, workspace_id or None,
                    owner_id or None, normalized_mode, "running", 0, snapshot,
                    str(objective or ""), now, now,
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
        final_result = _final_result_text(event_type, payload, conn=conn, run_id=run_id)
        cursor = conn.execute(
            """UPDATE runs SET state=?, stop_reason=?, budget=?, error=?,
               final_result=?, updated_at=?, ended_at=?,
               projection_revision=projection_revision+1
               WHERE run_id=? AND state NOT IN ('completed','failed','cancelled','interrupted')""",
            (
                run_state, stop_reason,
                json.dumps(redact_dict(payload.get("budget") or {}), ensure_ascii=False, sort_keys=True),
                str(payload.get("message") or "") or None,
                final_result, now, now, run_id,
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
                payload, mode, part_id, artifact_ids, schema, revision, tool_calls)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                json.dumps(_extract_tool_calls(event), ensure_ascii=False),
            ),
        )
        if task_cursor.rowcount:
            _bump_run_projection(conn, run_id)
        _bump_run_projection(conn, run_id)
    # Sink the evaluable trajectory once the terminal state is durable.  A
    # storage failure must never make a successfully settled run look unsettled.
    try:
        persist_trajectory(run_id)
    except Exception:
        logger.warning("trajectory sink failed for settled run %s", run_id)
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
                    payload, mode, part_id, artifact_ids, schema, revision,
                    tool_calls)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    "[]", "modus.agent-event.v2", 0, "[]",
                ),
            )
            if event_cursor.rowcount == 1:
                _bump_run_projection(conn, run_id)
        cursor = conn.execute(
            """UPDATE runs SET state='interrupted', stop_reason='process_restart',
               error=?, final_result=?, updated_at=?, ended_at=?,
               projection_revision=projection_revision+1 WHERE state='running'""",
            (message, message, now, now),
        )
    # Sink the interrupted runs' trajectories after the transaction commits so
    # an evaluator can still score where the work died.  Best-effort: a storage
    # failure never affects the settlement already committed.
    for run in interrupted:
        try:
            persist_trajectory(str(run["run_id"]))
        except Exception:
            logger.warning("trajectory sink failed for interrupted run %s", run["run_id"])
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
                payload, mode, part_id, artifact_ids, schema, revision, tool_calls)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                 revision=excluded.revision,
                 tool_calls=excluded.tool_calls
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
                json.dumps(_extract_tool_calls(event), ensure_ascii=False),
            ),
        )
        if cursor.rowcount == 1:
            run_id = str(event["run_id"])
            _bump_run_projection(conn, run_id)
            prior_run_id = str(prior["run_id"]) if prior else ""
            if prior_run_id and prior_run_id != run_id:
                _bump_run_projection(conn, prior_run_id)


def _decode_run_event(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Decode one run_events row into its typed wire shape.

    Decodes the JSON columns (actor / payload / artifact_ids / tool_calls) and
    normalizes the mode so consumers — including the offline Evaluator — see the
    same shape regardless of whether the row was read from SQLite or from a
    persisted trajectory file.
    """
    event = dict(row)
    event["mode"] = normalize_mode(event.get("mode"))
    for key in ("actor", "payload", "artifact_ids", "tool_calls"):
        default = "[]" if key in ("artifact_ids", "tool_calls") else "{}"
        try:
            event[key] = json.loads(event[key] or default)
        except (TypeError, json.JSONDecodeError):
            event[key] = [] if key in ("artifact_ids", "tool_calls") else {}
    return event


def get_run_events(run_id: str) -> list[dict[str, Any]]:
    """Return the latest audited event state in deterministic timeline order."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM run_events WHERE run_id=? ORDER BY sequence ASC, event_id ASC", (run_id,),
        ).fetchall()
    return [_decode_run_event(row) for row in rows]


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
    return [_decode_run_event(row) for row in rows]


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


# ── Trajectory persistence (Wave5 E1) ──

_TRAJECTORY_SUBDIR = "trajectories"
_TRAJECTORY_FILE_VERSION = 1


def trajectory_dir() -> Path:
    """Return Modus's private trajectory store (``~/.modus/trajectories``)."""
    return DB_DIR / _TRAJECTORY_SUBDIR


def _trajectory_file(run_id: str) -> Path:
    return trajectory_dir() / f"{run_id}.json"


def _trajectory_event(sequence: int, event: dict[str, Any]) -> dict[str, Any]:
    """Project one decoded run_event onto the on-disk trajectory shape.

    Only the fields the offline Evaluator needs are kept, and everything is
    JSON-safe: the actor/payload are redacted on write, tool_calls is already a
    summarized list, and non-JSON payload values are coerced through
    ``_trajectory_default`` so a dataclass-bearing payload never breaks the
    sink.
    """
    actor = event.get("actor") or {}
    payload = event.get("payload") or {}
    return {
        "sequence": int(sequence),
        "event_id": str(event.get("event_id") or ""),
        "type": str(event.get("type") or ""),
        "status": str(event.get("status") or ""),
        "timestamp": str(event.get("timestamp") or ""),
        "actor": actor,
        "tool_calls": list(event.get("tool_calls") or []),
        "payload": json.loads(
            json.dumps(redact_dict(payload), ensure_ascii=False, default=_trajectory_default)
        ) if isinstance(payload, dict) else payload,
    }


def _trajectory_objective(run: dict[str, Any] | None, events: list[dict[str, Any]]) -> str:
    """Resolve the trajectory's objective.

    Prefers the durable ``runs.objective`` (written by ``create_run`` when the
    caller supplies it) and falls back to the first ``user_message`` payload —
    the user's own request — so a trajectory is self-describing even when the
    run admission did not pass an explicit objective.
    """
    objective = str((run or {}).get("objective") or "").strip()
    if objective:
        return objective
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("type") or "") != "user_message":
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        text = str(payload.get("markdown") or payload.get("text") or "").strip()
        if text:
            return text[:500]
    return ""


def persist_trajectory(run_id: str, *, run: dict[str, Any] | None = None,
                       events: list[dict[str, Any]] | None = None) -> Path | None:
    """Sink one run's evaluable trajectory to ``~/.modus/trajectories/{run_id}.json``.

    Called after a run reaches a terminal state.  Re-serialization is
    idempotent (same run_id overwrites the same file), best-effort (a storage
    failure never raises), and bounded to Modus's own data directory — the
    trajectory store never touches the user's workspace.

    ``run``/``events`` default to a live read of the ledger so callers can pass
    either a freshly settled envelope or nothing and still get a correct file.
    """
    if not run_id:
        return None
    if run is None:
        run = get_run(run_id)
    if events is None:
        events = get_run_events(run_id)
    try:
        _trajectory_file(run_id).parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "schema": "modus.trajectory.v1",
            "file_version": _TRAJECTORY_FILE_VERSION,
            "run_id": run_id,
            "state": str((run or {}).get("state") or ""),
            "stop_reason": str((run or {}).get("stop_reason") or "") or None,
            "mode": normalize_mode((run or {}).get("mode")),
            "objective": _trajectory_objective(run, events),
            "final_result": str((run or {}).get("final_result") or ""),
            "budget": (run or {}).get("budget") or {},
            "created_at": time.time(),
            "events": [
                _trajectory_event(int(event.get("sequence") or index), event)
                for index, event in enumerate(events)
            ],
        }
        with open(_trajectory_file(run_id), "w", encoding="utf-8") as handle:
            json.dump(doc, handle, ensure_ascii=False, indent=2, default=_trajectory_default)
        return _trajectory_file(run_id)
    except OSError:
        logger.warning("trajectory sink failed for run %s", run_id)
        return None


def load_trajectory(run_id: str) -> dict[str, Any] | None:
    """Read a persisted trajectory back as a plain dict, or None when absent."""
    path = _trajectory_file(run_id)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    doc["events"] = [
        event for event in doc.get("events") or []
        if isinstance(event, dict)
    ]
    return doc


def list_trajectories() -> list[dict[str, Any]]:
    """Return every persisted trajectory in Modus's private store.

    Each entry is the trajectory doc (run_id, state, objective, final_result,
    event count); malformed files are skipped, never raised.
    """
    store = trajectory_dir()
    if not store.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(store.glob("*.json")):
        doc = load_trajectory(path.stem)
        if doc is None:
            continue
        result.append(doc)
    return result





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

def add_message(session_id: str, role: str, content: str = "", tool_calls: list | None = None, token_count: int = 0, tool_call_id: str | None = None, *, parent_id: int | None = None, parent_message_id: int | None = None) -> None:
    """Persist one message row, optionally on a session-tree branch.

    ``parent_id`` (aliased ``parent_message_id`` for callers that speak the
    column name) is the row this message continues from.  When omitted, the
    message is appended to the session's current leaf — which is the mainline
    for a linear history and the active branch's leaf after a branch/revert.
    ``parent_message_id`` defaults to that resolved parent; ``branch_root_id``
    is inherited from the parent row (NULL stays mainline).  A message that
    carries tool results is never chosen as a branch root, so a branch always
    starts at a user/assistant boundary (invertible and tool-call-safe).
    """
    if token_count <= 0:
        # Populate the audit token estimate so the durable ledger is usable
        # for budgeting and restore decisions without trusting caller call
        # sites to remember to fill it.  A zero/omitted count means "estimate".
        token_count = _estimate_message_tokens(role, content, tool_calls or [])
    parent = parent_id if parent_id is not None else parent_message_id
    with _get_conn() as conn:
        try:
            if parent is not None:
                parent_row = conn.execute(
                    "SELECT id, branch_root_id FROM messages WHERE id=? AND session_id=?",
                    (int(parent), session_id),
                ).fetchone()
                if parent_row is None:
                    return
                parent_id_resolved = int(parent_row["id"])
                branch_root = parent_row["branch_root_id"]
            else:
                leaf = _session_leaf_for_append(conn, session_id)
                parent_id_resolved = leaf
                branch_root = None
                if leaf is not None:
                    parent_row = conn.execute(
                        "SELECT branch_root_id FROM messages WHERE id=?",
                        (int(leaf),),
                    ).fetchone()
                    branch_root = parent_row["branch_root_id"] if parent_row else None
            # A branch's root id lives on the session_branches pointer, not on
            # the anchor message row (which predates the fork and still carries
            # NULL).  Inherit it from the current pointer so branch appends
            # carry the branch marker exactly like their sibling rows.
            if branch_root is None:
                pointer = conn.execute(
                    """SELECT branch_root_id FROM session_branches
                       WHERE session_id=? ORDER BY created_at DESC, branch_id DESC LIMIT 1""",
                    (session_id,),
                ).fetchone()
                if pointer is not None and pointer["branch_root_id"] is not None:
                    branch_root = pointer["branch_root_id"]
            cursor = conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, token_count, created_at, tool_call_id, parent_message_id, branch_root_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id, role, content, json.dumps(tool_calls or []), token_count, time.time(), tool_call_id or None, parent_id_resolved, branch_root),
            )
            # The current leaf pointer advances only when this message continues
            # the session's active lineage (implicit append, or an explicit
            # parent that is exactly the current leaf).  An explicit parent off
            # the active lineage leaves the leaf pointer untouched so the
            # active branch is not silently switched by a side insertion.
            if cursor.lastrowid is not None:
                current_leaf = _session_leaf_for_append(conn, session_id)
                if parent is None or parent_id_resolved == current_leaf:
                    _advance_session_leaf(conn, session_id, int(cursor.lastrowid))
            conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id))
        except sqlite3.IntegrityError:
            # 忽略外键约束错误
            pass


def _session_leaf_for_append(conn: sqlite3.Connection, session_id: str) -> int | None:
    """Resolve the current branch leaf for an implicit (no parent) append.

    The latest ``session_branches`` row is the leaf pointer (it advances as
    messages append).  When no branch/revert ever happened the log is linear
    mainline, so the leaf falls back to the newest message.
    """
    row = conn.execute(
        """SELECT message_id FROM session_branches
           WHERE session_id=? ORDER BY created_at DESC, branch_id DESC LIMIT 1""",
        (session_id,),
    ).fetchone()
    if row is not None and row["message_id"] is not None:
        return int(row["message_id"]) or None
    leaf = conn.execute(
        "SELECT MAX(id) AS leaf FROM messages WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return int(leaf["leaf"]) if leaf is not None and leaf["leaf"] is not None else None


def _advance_session_leaf(conn: sqlite3.Connection, session_id: str, message_id: int) -> None:
    """Move the latest branch pointer's leaf onto ``message_id`` (no new row)."""
    conn.execute(
        """UPDATE session_branches SET message_id=?
           WHERE session_id=?
             AND branch_id=(
                 SELECT branch_id FROM session_branches
                 WHERE session_id=? ORDER BY created_at DESC, branch_id DESC LIMIT 1
             )""",
        (int(message_id), session_id, session_id),
    )


# ── Session tree: branch / revert / tree (Wave5 E3) ──────────────────────

def _session_leaf_conn(conn: sqlite3.Connection, session_id: str) -> int:
    """The current branch leaf for one session on an open connection."""
    leaf = _session_leaf_for_append(conn, session_id)
    return int(leaf) if leaf is not None else 0


def current_session_leaf(session_id: str) -> int:
    """Return the id of the current branch leaf message (0 when empty)."""
    with _get_conn() as conn:
        return _session_leaf_conn(conn, session_id)


def _branch_walk(conn: sqlite3.Connection, message_id: int) -> list[int]:
    """Ancestor id chain from a message to its branch root (or mainline head).

    Walks ``parent_message_id`` upward so the active lineage is always exactly
    the messages reachable from the current leaf — shared ancestors are
    included, sibling/diverged messages are not.  Returned oldest first.
    """
    chain: list[int] = []
    current = int(message_id)
    seen: set[int] = set()
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        row = conn.execute(
            "SELECT parent_message_id FROM messages WHERE id=?", (current,),
        ).fetchone()
        parent = int(row["parent_message_id"]) if row is not None and row["parent_message_id"] is not None else 0
        if not parent:
            break
        current = parent
    return list(reversed(chain))


def _session_ancestor_ids(conn: sqlite3.Connection, session_id: str, leaf: int) -> list[int]:
    """Active-branch ancestor ids for one session (oldest first).

    Once any branch/revert pointer exists, the active lineage is the parent
    chain from the leaf.  A session that never branched is the linear mainline
    (pre-v4 history has no parent links at all), so its whole log by id order
    is returned.
    """
    if not leaf:
        return []
    anchor = conn.execute(
        """SELECT message_id FROM session_branches
           WHERE session_id=? ORDER BY created_at DESC, branch_id DESC LIMIT 1""",
        (session_id,),
    ).fetchone()
    if anchor is not None:
        return _branch_walk(conn, leaf)
    return [
        int(row["id"]) for row in conn.execute(
            "SELECT id FROM messages WHERE session_id=? ORDER BY id ASC", (session_id,),
        ).fetchall()
    ]


def _decode_message_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    try:
        d["tool_calls"] = json.loads(d.get("tool_calls") or "[]")
    except (TypeError, json.JSONDecodeError):
        d["tool_calls"] = []
    d["parent_message_id"] = (
        int(d["parent_message_id"]) if d.get("parent_message_id") is not None else None
    )
    d["branch_root_id"] = (
        int(d["branch_root_id"]) if d.get("branch_root_id") is not None else None
    )
    return d


def get_session_messages(session_id: str, limit: int = 200) -> list[dict]:
    """Return the active-branch messages (chronological) for one session.

    The active branch is the parent lineage from the current leaf (or the
    linear mainline when the session never branched).  Each row carries its
    tree fields (``parent_message_id`` / ``branch_root_id``), so the consumer
    can rebuild model context from the branch exactly as it was left.
    """
    bounded = max(1, min(int(limit), 1_000))
    with _get_conn() as conn:
        leaf = _session_leaf_conn(conn, session_id)
        ancestor_ids = _session_ancestor_ids(conn, session_id, leaf)[-bounded:]
        if ancestor_ids:
            placeholders = ", ".join("?" for _ in ancestor_ids)
            rows = conn.execute(
                f"SELECT * FROM messages WHERE session_id=? AND id IN ({placeholders}) "
                "ORDER BY id ASC",
                [session_id, *ancestor_ids],
            ).fetchall()
        else:
            rows = []
    return [_decode_message_row(r) for r in rows]


def session_branch(session_id: str, message_id: int) -> dict[str, Any] | None:
    """Fork a new branch from one message: new leaf pointer, history untouched.

    The new leaf pointer names ``message_id`` as the branch point; future
    implicit ``add_message`` calls append onto this branch.  No message row is
    copied or rewritten, so the fork is O(1) and fully reversible.
    Returns the new ``session_branches`` row, or None when the anchor does not
    exist / belongs to another session.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, branch_root_id FROM messages WHERE id=? AND session_id=?",
            (int(message_id), session_id),
        ).fetchone()
        if row is None:
            return None
        branch_root = (
            int(row["branch_root_id"]) if row["branch_root_id"] is not None
            else (int(row["id"]) if _branch_candidate(conn, session_id, int(row["id"])) else None)
        )
        branch_id = f"br_{uuid.uuid4().hex}"
        now = time.time()
        conn.execute(
            """INSERT INTO session_branches
               (branch_id, session_id, message_id, branch_root_id, created_at)
               VALUES (?,?,?,?,?)""",
            (branch_id, session_id, int(message_id), branch_root, now),
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
    return get_session_branch(branch_id)


def _branch_candidate(conn: sqlite3.Connection, session_id: str, message_id: int) -> bool:
    """A message starts a NEW branch root only when no branch already anchors it.

    Re-branching from a message that already roots a branch keeps that
    branch's root so the divergence stays grouped under one id.
    """
    existing = conn.execute(
        "SELECT 1 FROM session_branches WHERE session_id=? AND branch_root_id=? LIMIT 1",
        (session_id, message_id),
    ).fetchone()
    return existing is None


def session_revert(session_id: str, message_id: int) -> dict[str, Any] | None:
    """Move the leaf pointer back to one message without deleting history.

    A pure rewind: no message row is touched or removed.  New appends continue
    from ``message_id``; every downstream message stays on disk and
    ``session_tree`` still shows it.  The new pointer inherits the target
    message's own lineage (``branch_root_id``), so reverting into a branch
    keeps that branch's grouping.  Returns the new row, or None when the
    target does not exist / belongs to another session.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, branch_root_id FROM messages WHERE id=? AND session_id=?",
            (int(message_id), session_id),
        ).fetchone()
        if row is None:
            return None
        branch_id = f"br_{uuid.uuid4().hex}"
        now = time.time()
        conn.execute(
            """INSERT INTO session_branches
               (branch_id, session_id, message_id, branch_root_id, created_at)
               VALUES (?,?,?,?,?)""",
            (branch_id, session_id, int(message_id), row["branch_root_id"], now),
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
    return get_session_branch(branch_id)


def get_session_branch(branch_id: str) -> dict[str, Any] | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM session_branches WHERE branch_id=?", (branch_id,),
        ).fetchone()
    return dict(row) if row else None


def session_tree(session_id: str, limit: int = 500) -> dict[str, Any]:
    """Return the full session message tree as nested children + lineage info.

    The structure keeps every message that belongs to the session (across all
    branches — branch/revert never deletes history), so the frontend can draw
    divergent paths and the compressor can rebuild context from a branch.
    Every node carries ``parent_message_id`` / ``branch_root_id`` /
    ``branch_name`` plus ``children`` (ids).  ``current_leaf`` names the
    active branch leaf; ``branches`` lists the recorded branch/revert
    pointers (latest first).
    """
    bounded = max(1, min(int(limit), 2_000))
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id ASC LIMIT ?",
            (session_id, bounded),
        ).fetchall()
        leaf = _session_leaf_conn(conn, session_id)
        branch_rows = conn.execute(
            """SELECT * FROM session_branches WHERE session_id=?
               ORDER BY created_at DESC, branch_id DESC LIMIT 200""",
            (session_id,),
        ).fetchall()
    children: dict[int, list[int]] = {}
    parents: dict[int, int | None] = {}
    nodes: dict[int, dict[str, Any]] = {}
    for r in rows:
        mid = int(r["id"])
        parent = int(r["parent_message_id"]) if r["parent_message_id"] is not None else None
        parents[mid] = parent
        d = _decode_message_row(r)
        d["children"] = children.setdefault(mid, [])
        d["branch_name"] = _branch_display_name(r["branch_root_id"])
        nodes[mid] = d
    for mid, parent in parents.items():
        if parent is not None and parent in nodes:
            children.setdefault(parent, []).append(mid)
    roots = [mid for mid, parent in parents.items() if parent is None or parent not in nodes]
    branches = [
        {
            "branch_id": str(b["branch_id"]),
            "message_id": int(b["message_id"] or 0) or None,
            "branch_root_id": b["branch_root_id"],
            "branch_name": _branch_display_name(b["branch_root_id"]),
            "created_at": float(b["created_at"] or 0),
        }
        for b in branch_rows
    ]
    return {
        "session_id": session_id,
        "current_leaf": leaf or None,
        "branches": branches,
        "roots": roots,
        "nodes": nodes,
        "message_count": len(nodes),
    }


def _branch_display_name(branch_root_id: Any) -> str:
    return "主线" if branch_root_id is None else f"分支@{branch_root_id}"


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
    """Return one ID-ordered boundary and count from the active-branch log.

    Wave5 E3: after a branch/revert the active lineage (``get_session_messages``)
    is the context the next compaction gates, so the boundary and cutoff are
    computed against that lineage instead of the whole session log.  A linear
    session behaves exactly as before.
    """
    tail = max(1, int(tail_count))
    lineage = get_session_messages(session_id, limit=10_000)
    with _get_conn() as conn:
        system_rows = conn.execute(
            """SELECT id, content FROM messages
               WHERE session_id=? AND role='system' ORDER BY id ASC""",
            (session_id,),
        ).fetchall()
    total = len(lineage)
    cutoff = 0
    if total > tail:
        # The cutoff is the last message OUTSIDE the retained tail (the
        # ``tail``-th newest message), matching the durable ``cutoff_message_id``
        # semantic: the retained context is every active-branch message with
        # id strictly greater than the cutoff.
        cutoff = int(lineage[-tail - 1]["id"])
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
    """Return the original system contract and every active-branch message after an ID boundary.

    Wave5 E3: the retained rows are the active-branch lineage after the cutoff
    (a branch/revert never deletes history, so this is what a branch-aware
    restore replays).  A linear session behaves exactly as before.

    There is intentionally no silent limit: these rows were the active model
    context immediately before restart. The normal compression gate will bound
    them again before the next provider request when required.
    """
    from modus.agent.compressor import SUMMARY_PREFIX

    cutoff = max(0, int(cutoff_message_id))
    lineage = get_session_messages(session_id, limit=10_000)
    rows = [
        message for message in lineage
        if int(message.get("id") or 0) > cutoff
    ]
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

    def decode(row: dict[str, Any]) -> dict[str, Any]:
        message = dict(row)
        try:
            message["tool_calls"] = json.loads(message.get("tool_calls") or "[]")
        except (TypeError, json.JSONDecodeError):
            message["tool_calls"] = []
        return message

    head_id = int(head["id"]) if head is not None else 0
    return (
        decode(dict(head)) if head is not None else None,
        [decode(row) for row in rows if int(row.get("id") or 0) != head_id],
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
    # The active-branch lineage (mainline by default; the current branch after
    # a branch/revert) is what restore replays into model context.
    sess["messages"] = get_session_messages(session_id)
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
