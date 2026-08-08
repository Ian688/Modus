"""Coverage matrix — "what (objective, operation, capability) work is still undone" (Wave3 A3).

Modus persists run budgets on the *resource* axis (tokens, turns, wall time) but
never on the *work-completion* axis.  After a multi-step task the agent has no
objective record of which ``(objective, operation, capability)`` combinations it
already tried/passed/failed/skipped, so it blindly repeats.  This module is that
missing dimension:

- ``CoverageStore`` is a keyed matrix ``(objective, operation, capability) -> state``
  (``tried|passed|failed|skipped``).
- ``untested()`` crosses candidate objectives x capabilities against the matrix and
  returns the tuples that have never been touched — the "what's left to do" list.
- Writes coalesce (a burst of ``mark`` calls flushes the session JSONL at most once
  per window) and are bounded (5000 rows cap with LRU eviction), so a long-lived
  session cannot grow without limit.

The store is a read-only, pure record.  It never affects approval or the
capability gate: ``mark`` merely records what already happened; it cannot grant,
deny, or skip anything.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modus.tools.base import ToolContext, ToolResult

# Coverage states, mirroring PentesterFlow's tried|passed|failed|skipped.
VALID_STATES = ("tried", "passed", "failed", "skipped")

# Hard row cap per session (PentesterFlow store.ts:196-204).  Above this the
# least-recently-used entries are evicted so a long-lived session cannot grow
# without bound.
MAX_ROWS = 5000

# Write-coalescing interval: a burst of ``mark`` calls flushes at most once per
# window.  Writes are best-effort — a disk failure never fails the tool.
_WRITE_WINDOW_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    objective: str
    operation: str
    capability: str
    state: str
    timestamp: float

    def as_row(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "operation": self.operation,
            "capability": self.capability,
            "state": self.state,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "CoverageEntry":
        return cls(
            objective=str(row.get("objective") or ""),
            operation=str(row.get("operation") or ""),
            capability=str(row.get("capability") or ""),
            state=str(row.get("state") or ""),
            timestamp=float(row.get("timestamp") or 0.0),
        )


def _coverage_dir() -> Path:
    """Return ``~/.modus/coverage`` (honours ``MODUS_DATA_DIR`` in tests)."""
    from modus.paths import data_dir

    return data_dir() / "coverage"


def _coverage_path(session_id: str) -> Path:
    """Return the JSONL path for one session.  ``None``-safe for ad-hoc runs."""
    session_id = str(session_id or "").strip()
    if not session_id:
        # An unscoped session (CLI/embedder) shares the transient ledger; the
        # store still works fully in memory and writes are skipped.
        return _coverage_dir() / "transient.jsonl"
    return _coverage_dir() / f"{session_id}.jsonl"


def _row_key(entry: CoverageEntry) -> tuple[str, str, str]:
    return (entry.objective, entry.operation, entry.capability)


class CoverageStore:
    """In-memory coverage matrix with coalescing JSONL persistence.

    One store per session: ``session_id`` scopes the ledger file under
    ``~/.modus/coverage/``.  Thread-safe for the Desktop's mixed callers (tool
    handlers on the event loop, board reads on request threads).
    """

    def __init__(self, session_id: str | None = None, *, path: Path | None = None) -> None:
        self.session_id = str(session_id or "").strip()
        self._path = path or _coverage_path(self.session_id)
        self._lock = threading.Lock()
        # key -> entry; insertion order doubles as LRU order for eviction.
        self._entries: dict[tuple[str, str, str], CoverageEntry] = {}
        # Declared coverage space: objectives and capabilities the session has
        # worked on or proposed.  ``summary().untested_count`` crosses these.
        self._objectives: set[str] = set()
        self._capabilities: set[str] = set()
        self._dirty = False
        self._last_flush = 0.0
        self._load()

    # ── persistence ──

    def _load(self) -> None:
        """Rehydrate from the session JSONL ledger, newest row wins."""
        try:
            if not self._path.exists():
                return
            loaded: dict[tuple[str, str, str], CoverageEntry] = {}
            with self._path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    entry = CoverageEntry.from_row(row)
                    if entry.state not in VALID_STATES:
                        continue
                    loaded[_row_key(entry)] = entry
            self._entries = loaded
        except OSError:
            self._entries = {}
        self._load_space()

    def _load_space(self) -> None:
        """Restore the declared objective/capability space from its sidecar."""
        try:
            space_path = self._path.with_suffix(self._path.suffix + ".space.json")
            if space_path.exists():
                data = json.loads(space_path.read_text(encoding="utf-8") or "{}")
                self._objectives = {str(o) for o in (data.get("objectives") or []) if str(o)}
                self._capabilities = {str(c) for c in (data.get("capabilities") or []) if str(c)}
        except (OSError, ValueError, TypeError):
            self._objectives = set()
            self._capabilities = set()

    def _save_space(self) -> None:
        """Persist the declared space; best-effort."""
        try:
            space_path = self._path.with_suffix(self._path.suffix + ".space.json")
            space_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "objectives": sorted(self._objectives),
                "capabilities": sorted(self._capabilities),
            }
            space_path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        except OSError:
            pass

    def _flush(self, *, force: bool = False) -> None:
        """Persist current rows (coalesced).  Best-effort; never raises.

        The full matrix is rewritten on each flush so evicted (LRU-dropped)
        rows do not linger on disk.
        """
        with self._lock:
            if not self._dirty:
                return
            now = time.monotonic()
            if not force and (now - self._last_flush) < _WRITE_WINDOW_SECONDS:
                return
            rows = [entry.as_row() for entry in self._entries.values()]
            self._dirty = False
            self._last_flush = now
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            pass

    # ── mutation ──

    def mark(
        self,
        objective: str,
        operation: str,
        capability: str,
        state: str = "tried",
        *,
        flush: bool = False,
    ) -> CoverageEntry:
        """Record one (objective, operation, capability) combination.

        A repeat ``mark`` of the same key overwrites the earlier row (a cell is
        a single state).  Invalid states are coerced to ``tried`` so a
        malformed caller can never inject an unknown matrix state.  Pure
        recording — no approval or capability side effects.
        """
        objective = str(objective or "").strip()
        operation = str(operation or "").strip()
        capability = str(capability or "").strip()
        if state not in VALID_STATES:
            state = "tried"
        entry = CoverageEntry(
            objective=objective,
            operation=operation,
            capability=capability,
            state=state,
            timestamp=time.time(),
        )
        with self._lock:
            self._entries[_row_key(entry)] = entry
            if objective:
                self._objectives.add(objective)
            if capability:
                self._capabilities.add(capability)
            self._dirty = True
            self._evict_locked()
        self._flush(force=flush)
        self._save_space()
        return entry

    def clear(self) -> int:
        """Drop every row for this session and truncate the ledger."""
        with self._lock:
            removed = len(self._entries)
            self._entries.clear()
            self._objectives.clear()
            self._capabilities.clear()
            self._dirty = True
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("w", encoding="utf-8") as handle:
                handle.write("")
        except OSError:
            pass
        with self._lock:
            self._dirty = False
        self._save_space()
        return removed

    def _evict_locked(self) -> None:
        """LRU eviction once the row cap is exceeded (oldest insert first)."""
        overflow = len(self._entries) - MAX_ROWS
        if overflow <= 0:
            return
        for key in list(self._entries.keys())[:overflow]:
            self._entries.pop(key, None)

    # ── queries (all pure / read-only) ──

    def get(
        self, objective: str, operation: str, capability: str,
    ) -> CoverageEntry | None:
        objective = str(objective or "").strip()
        operation = str(operation or "").strip()
        capability = str(capability or "").strip()
        with self._lock:
            return self._entries.get((objective, operation, capability))

    def list(self) -> list[CoverageEntry]:
        """All rows, most recently marked first (stable)."""
        with self._lock:
            entries = list(self._entries.values())
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries

    def untested(
        self,
        objectives: list[str],
        capabilities: list[str],
    ) -> list[tuple[str, str]]:
        """Cross candidate objectives x capabilities and return never-tried pairs.

        Mirrors PentesterFlow ``coverage/store.ts:151-165``: the caller proposes
        the candidate objective space (e.g. extracted goals) and the capability
        set; any pair that has no matrix row of any state counts as "untested"
        (a cell whose state is ``skipped`` is still *tested* — it was attempted
        and deliberately skipped, so it is not re-proposed).
        """
        objectives = [str(o or "").strip() for o in (objectives or [])]
        capabilities = [str(c or "").strip() for c in (capabilities or [])]
        objectives = [o for o in objectives if o]
        capabilities = [c for c in capabilities if c]
        with self._lock:
            rows = list(self._entries.values())
            self._objectives.update(objectives)
            self._capabilities.update(capabilities)
        self._save_space()
        # A candidate (objective, capability) pair is covered if ANY operation
        # was marked under that objective with that capability.
        covered_pairs: set[tuple[str, str]] = {
            (entry.objective, entry.capability) for entry in rows
        }
        missing: list[tuple[str, str]] = []
        for objective in objectives:
            for capability in capabilities:
                if (objective, capability) not in covered_pairs:
                    missing.append((objective, capability))
        return missing

    def summary(self) -> dict[str, Any]:
        """Aggregate the matrix: totals by state + the outstanding count.

        ``untested_count`` crosses the declared coverage space (objectives x
        capabilities) against the covered pairs, giving the board a real
        "what's left" number.  With no declared space it reports 0.
        """
        with self._lock:
            rows = list(self._entries.values())
            objectives = set(self._objectives)
            capabilities = set(self._capabilities)
        by_state: dict[str, int] = {state: 0 for state in VALID_STATES}
        by_capability: dict[str, int] = {}
        covered_pairs: set[tuple[str, str]] = set()
        for entry in rows:
            by_state[entry.state] = by_state.get(entry.state, 0) + 1
            by_capability[entry.capability] = by_capability.get(entry.capability, 0) + 1
            covered_pairs.add((entry.objective, entry.capability))
        total = len(rows)
        passed = by_state["passed"]
        untested_count = 0
        for objective in objectives:
            for capability in capabilities:
                if (objective, capability) not in covered_pairs:
                    untested_count += 1
        return {
            "schema": "modus.coverage-summary.v1",
            "session_id": self.session_id,
            "total": total,
            "by_state": by_state,
            "by_capability": dict(sorted(by_capability.items())),
            "objectives": sorted(objectives),
            "capabilities": sorted(capabilities),
            "passed": passed,
            "coverage_rate": round(passed / total, 3) if total else 0.0,
            "untested_count": untested_count,
        }

    def close(self) -> None:
        """Force a final flush so the ledger is durable before teardown."""
        self._flush(force=True)


# ACTIONS mirror PentesterFlow's coverage tool (tools/coverage.ts:13).
ACTIONS = ("mark", "list", "untested", "summary", "clear")

# Module-level store registry so repeated tool calls within one session share
# the same in-memory matrix (and its coalescing flush) instead of re-reading
# the ledger each call.  Read/write guarded by a lock; stores are cheap.
_store_registry: dict[str, CoverageStore] = {}
_store_registry_lock = threading.Lock()


def _coverage_store(session_id: str | None) -> CoverageStore:
    key = str(session_id or "").strip() or "transient"
    with _store_registry_lock:
        store = _store_registry.get(key)
        if store is None:
            store = CoverageStore(session_id)
            _store_registry[key] = store
        return store


def mark_coverage_call(
    session_id: str | None, tool_name: str, payload: dict[str, Any],
    is_error: bool,
) -> None:
    """Record one tool call into the coverage matrix (Wave3 A3, sync helper).

    Called from the agent loop (react.py) after each tool result so the board
    can show what has been tried (state) versus what is left (``untested``).
    ``objective`` is derived from the tool name + a coarse capability class, so
    the matrix stays coarse but useful without needing the run's user prompt.
    Best-effort and bounded: never raises, never blocks the loop.
    """
    try:
        from modus.tools.capabilities import Capability

        capability = "filesystem"
        for cap in Capability:
            if cap.value in str(payload or {}).get("capabilities", "") or (
                tool_name in {"web_search", "web_fetch"} and cap.value == "network"
            ):
                capability = cap.value
                break
        objective = f"{tool_name}"
        state = "passed" if not is_error else "failed"
        store = _coverage_store(session_id)
        store.mark(objective, "tool_call", capability, state)
    except Exception:
        return


async def coverage(
    payload: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    """Record and query the work-completion coverage matrix (Wave3 A3).

    The matrix is keyed ``(objective, operation, capability)`` with state
    ``tried|passed|failed|skipped``.  ``untested`` crosses candidate objectives
    against capabilities and returns the pairs never attempted — the "what's
    left to do" list.  Purely a record: it never gates or grants anything.
    """
    action = str(payload.get("action") or "summary").strip().lower()
    if action not in ACTIONS:
        return ToolResult(
            f"coverage action must be one of: {', '.join(ACTIONS)}; got {action!r}",
            is_error=True,
        )
    store = _coverage_store(context.session_id)

    if action == "mark":
        objective = str(payload.get("objective") or "").strip()
        operation = str(payload.get("operation") or "").strip()
        capability = str(payload.get("capability") or "").strip()
        if not objective or not capability:
            return ToolResult(
                "coverage mark requires 'objective' and 'capability' "
                "(operation is optional).",
                is_error=True,
            )
        state = str(payload.get("state") or "tried").strip().lower()
        entry = store.mark(objective, operation, capability, state, flush=True)
        return ToolResult(
            f"Coverage marked: ({entry.objective}, {entry.operation}, "
            f"{entry.capability}) -> {entry.state}",
            display_summary=f"覆盖记录：{entry.objective} · {entry.capability}",
            metadata={
                "operation": "coverage-mark",
                "coverage": entry.as_row(),
                "changed": True,
            },
        )

    if action == "list":
        entries = store.list()
        if not entries:
            return ToolResult("Coverage matrix is empty.")
        rows = [
            f"[{e.state}] {e.objective} · {e.operation or '-'} · {e.capability}"
            for e in entries[:200]
        ]
        return ToolResult(
            "Coverage matrix:\n" + "\n".join(rows),
            display_summary=f"覆盖记录 {len(entries)} 条",
            metadata={"operation": "coverage-list", "count": len(entries)},
        )

    if action == "untested":
        objectives = list(payload.get("objectives") or [])
        capabilities = list(payload.get("capabilities") or [])
        if not objectives or not capabilities:
            return ToolResult(
                "coverage untested requires non-empty 'objectives' and "
                "'capabilities' lists to cross.",
                is_error=True,
            )
        missing = store.untested(objectives, capabilities)
        if not missing:
            return ToolResult(
                "No untested combinations: all candidate objectives x "
                "capabilities are covered.",
                metadata={"operation": "coverage-untested", "untested_count": 0},
            )
        lines = [f"{o} · {c}" for o, c in missing]
        return ToolResult(
            f"Untested combinations ({len(missing)}):\n" + "\n".join(lines),
            display_summary=f"未覆盖 {len(missing)} 项",
            metadata={
                "operation": "coverage-untested",
                "untested": [{"objective": o, "capability": c} for o, c in missing],
                "untested_count": len(missing),
            },
        )

    if action == "summary":
        summary = store.summary()
        bits = " · ".join(f"{count} {state}" for state, count in summary["by_state"].items() if count)
        text = f"Coverage summary ({summary['total']} rows): {bits or 'empty'}"
        return ToolResult(
            text,
            display_summary=f"覆盖 {summary['total']} 条",
            metadata={
                "operation": "coverage-summary",
                "coverage_summary": {
                    "total": summary["total"],
                    "by_state": summary["by_state"],
                },
            },
        )

    # action == "clear"
    removed = store.clear()
    return ToolResult(
        f"Coverage matrix cleared ({removed} rows).",
        display_summary=f"清除覆盖记录 {removed} 条",
        metadata={"operation": "coverage-clear", "removed": removed, "changed": True},
    )
