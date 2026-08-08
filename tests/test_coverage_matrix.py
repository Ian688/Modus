"""Wave3 A3 — coverage matrix: mark / list / untested / summary / clear.

Covers ``CoverageStore`` (in-memory + JSONL persistence, write coalescing,
5000-row LRU cap), the ``coverage`` builtin tool declaration, and the board
aggregation "未覆盖" (untested) section.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from modus.config import ModusConfig
from modus.desktop.board_aggregation import aggregate_board
from modus.tools.base import ToolContext
from modus.tools.builtins import get_builtin_tools
from modus.tools.coverage import (
    ACTIONS,
    MAX_ROWS,
    CoverageStore,
    _coverage_path,
)
from modus.tools.executor import ToolExecutor
from modus.tools.registry import ToolRegistry


# ── store: mark / summary / untested ──


def _store(tmp_path: Path, session_id: str = "sess1") -> CoverageStore:
    return CoverageStore(session_id, path=tmp_path / "coverage" / f"{session_id}.jsonl")


def test_coverage_mark_and_summary(tmp_path):
    store = _store(tmp_path)
    store.mark("fix-login", "read", "filesystem", "passed")
    store.mark("fix-login", "edit", "filesystem", "passed")
    store.mark("fix-login", "command", "exec", "tried")
    store.mark("fix-login", "command", "network", "failed")

    summary = store.summary()
    assert summary["total"] == 4
    assert summary["by_state"]["passed"] == 2
    assert summary["by_state"]["tried"] == 1
    assert summary["by_state"]["failed"] == 1
    assert summary["by_state"]["skipped"] == 0
    assert summary["coverage_rate"] == 0.5
    assert summary["objectives"] == ["fix-login"]

    entries = store.list()
    assert len(entries) == 4
    assert {e.state for e in entries} == {"passed", "tried", "failed"}


def test_untested_cross_product_excludes_tried(tmp_path):
    store = _store(tmp_path)
    store.mark("o1", "read", "filesystem")
    store.mark("o1", "command", "exec")
    store.mark("o2", "read", "filesystem", "passed")

    missing = store.untested(
        ["o1", "o2", "o3"],
        ["filesystem", "exec", "network"],
    )
    missing_set = set(missing)
    # o1: filesystem + exec covered -> network outstanding.
    assert ("o1", "network") in missing_set
    assert ("o1", "filesystem") not in missing_set
    assert ("o1", "exec") not in missing_set
    # o2: filesystem covered (passed counts as tested).
    assert ("o2", "filesystem") not in missing_set
    assert ("o2", "exec") in missing_set
    # o3: nothing covered.
    assert ("o3", "filesystem") in missing_set
    assert ("o3", "exec") in missing_set
    assert ("o3", "network") in missing_set


def test_untested_skipped_state_is_still_tested(tmp_path):
    store = _store(tmp_path)
    store.mark("o1", "read", "filesystem", "skipped")
    missing = store.untested(["o1"], ["filesystem", "exec"])
    assert ("o1", "filesystem") not in missing
    assert ("o1", "exec") in missing


def test_coverage_persists(tmp_path):
    store = _store(tmp_path)
    store.mark("persist", "write", "filesystem", "passed", flush=True)
    store.mark("persist", "command", "exec", "tried", flush=True)

    # Fresh store reads the same ledger back.
    restored = CoverageStore("sess1", path=tmp_path / "coverage" / "sess1.jsonl")
    assert restored.get("persist", "write", "filesystem").state == "passed"
    assert restored.get("persist", "command", "exec").state == "tried"
    assert restored.summary()["total"] == 2
    # The declared objective/capability space persisted too.
    assert "persist" in restored.summary()["objectives"]


def test_coverage_persist_round_trip_untested_count(tmp_path):
    store = _store(tmp_path)
    store.mark("o1", "read", "filesystem", "passed", flush=True)
    store.untested(["o1", "o2"], ["filesystem", "exec"])

    restored = CoverageStore("sess1", path=tmp_path / "coverage" / "sess1.jsonl")
    summary = restored.summary()
    assert summary["untested_count"] == 3  # o1.exec, o2.filesystem, o2.exec
    assert set(summary["capabilities"]) == {"filesystem", "exec"}


def test_coverage_clear(tmp_path):
    store = _store(tmp_path)
    store.mark("o1", "read", "filesystem")
    store.mark("o2", "command", "exec")
    assert store.clear() == 2
    assert store.summary()["total"] == 0
    assert store.list() == []

    restored = CoverageStore("sess1", path=tmp_path / "coverage" / "sess1.jsonl")
    assert restored.summary()["total"] == 0


def test_mark_overwrite_same_cell(tmp_path):
    store = _store(tmp_path)
    store.mark("o1", "op", "filesystem", "tried")
    store.mark("o1", "op", "filesystem", "passed")
    assert store.summary()["total"] == 1
    assert store.get("o1", "op", "filesystem").state == "passed"


def test_invalid_state_coerced_to_tried(tmp_path):
    store = _store(tmp_path)
    store.mark("o1", "op", "filesystem", "not-a-state")
    assert store.get("o1", "op", "filesystem").state == "tried"


def test_lru_eviction_bounds_matrix(tmp_path):
    store = _store(tmp_path)
    for index in range(MAX_ROWS + 200):
        store.mark(f"obj-{index}", "op", "filesystem")
    assert store.summary()["total"] == MAX_ROWS
    # Oldest inserted rows were evicted first (LRU by insertion order).
    assert store.get("obj-0", "op", "filesystem") is None
    assert store.get(f"obj-{MAX_ROWS + 199}", "op", "filesystem") is not None


def test_store_no_session_id_writes_transient(tmp_path, monkeypatch):
    monkeypatch.setenv("MODUS_DATA_DIR", str(tmp_path / "data"))
    store = CoverageStore(None)
    store.mark("o1", "op", "filesystem")
    assert store.summary()["total"] == 1
    assert str(_coverage_path("")) == str(tmp_path / "data" / "coverage" / "transient.jsonl")


# ── coverage tool (declaration + handler through the executor) ──


def test_coverage_tool_declared():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    tool = tools["coverage"]
    assert tool.is_read_only is True
    assert tool.is_concurrency_safe is True
    assert tool.danger_level == "safe"
    assert tool.requires_approval is False
    assert tool.capabilities == ("filesystem",)
    props = tool.parameters["properties"]
    assert "action" in props and "objective" in props and "capability" in props
    assert "untested" in tool.description


@pytest.mark.asyncio
async def test_coverage_tool_mark_summary_untested(tmp_path, monkeypatch):
    monkeypatch.setenv("MODUS_DATA_DIR", str(tmp_path / "data"))
    registry = ToolRegistry()
    registry.register(next(t for t in get_builtin_tools() if t.name == "coverage"))
    executor = ToolExecutor(registry)
    ctx = ToolContext(cwd=".", config=ModusConfig(), session_id="sess_tool", run_id="r1")

    def call(args: dict) -> dict:
        return {"id": "c1", "type": "function",
                "function": {"name": "coverage", "arguments": json.dumps(args)}}

    results = await executor.execute_all([
        call({"action": "mark", "objective": "o1", "capability": "filesystem", "operation": "read", "state": "passed"}),
        call({"action": "mark", "objective": "o1", "capability": "exec", "operation": "command"}),
        call({"action": "untested", "objectives": ["o1", "o2"], "capabilities": ["filesystem", "exec", "network"]}),
    ], ctx)

    assert results[0].is_error is False
    assert "Coverage marked" in results[0].content
    assert results[2].is_error is False
    assert "Untested" in results[2].content
    assert results[2].metadata["untested_count"] == 4  # o1.network + o2*3

    summary = await executor.execute_all([call({"action": "summary"})], ctx)
    assert summary[0].is_error is False
    assert "Coverage summary (2 rows)" in summary[0].content


@pytest.mark.asyncio
async def test_coverage_tool_clear_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("MODUS_DATA_DIR", str(tmp_path / "data"))
    registry = ToolRegistry()
    registry.register(next(t for t in get_builtin_tools() if t.name == "coverage"))
    executor = ToolExecutor(registry)
    ctx = ToolContext(cwd=".", config=ModusConfig(), session_id="sess_tool2", run_id="r1")

    def call(args: dict) -> dict:
        return {"id": "c1", "type": "function",
                "function": {"name": "coverage", "arguments": json.dumps(args)}}

    bad = await executor.execute_all([call({"action": "bogus"})], ctx)
    assert bad[0].is_error is True
    assert "must be one of" in bad[0].content

    missing = await executor.execute_all([call({"action": "untested", "objectives": ["o1"]})], ctx)
    assert missing[0].is_error is True

    cleared = await executor.execute_all([call({"action": "clear"})], ctx)
    assert cleared[0].is_error is False
    assert "cleared (0 rows)" in cleared[0].content

    empty = await executor.execute_all([call({"action": "list"})], ctx)
    assert empty[0].is_error is False
    assert "empty" in empty[0].content


def test_coverage_read_only_passes_capability_gate():
    """A3 invariant: coverage is a pure record — never gated by grants."""
    from modus.tools.capabilities import capabilities_granted

    tool = next(t for t in get_builtin_tools() if t.name == "coverage")
    assert capabilities_granted(tool.capabilities, None) is True
    # Under a filesystem-only lockdown it is still allowed; under exec-only it
    # is denied like any other filesystem tool (no special casing).
    assert capabilities_granted(tool.capabilities, ["filesystem"]) is True
    assert capabilities_granted(tool.capabilities, ["exec"]) is False


# ── board aggregation: 未覆盖 count ──


def test_board_aggregation_coverage(tmp_path):
    store = CoverageStore("sess_board", path=tmp_path / "coverage" / "sess_board.jsonl")
    store.mark("o1", "read", "filesystem", "passed")
    store.mark("o1", "command", "exec", "passed")
    store.untested(["o1", "o2"], ["filesystem", "exec", "network"])

    board = aggregate_board([], coverage_summary=store.summary())
    section = board["coverage"]
    assert section["schema"] == "modus.board-coverage.v1"
    assert section["total"] == 2
    assert section["untested_count"] == 4  # o1.network + o2 x3
    assert section["by_state"]["passed"] == 2


def test_board_aggregation_coverage_empty_default():
    board = aggregate_board([])
    assert board["coverage"]["total"] == 0
    assert board["coverage"]["untested_count"] == 0


def test_board_aggregation_coverage_malformed_is_total():
    board = aggregate_board([], coverage_summary=None)
    assert board["coverage"]["untested_count"] == 0
    board = aggregate_board([], coverage_summary={"total": "x", "by_state": None})
    assert board["coverage"]["total"] == 0
    assert board["coverage"]["by_state"] == {"tried": 0, "passed": 0, "failed": 0, "skipped": 0}


def test_board_coverage_does_not_break_legacy_columns():
    run = {
        "run_id": "r1", "state": "completed", "mode": "default",
        "semantic": {"goal": {"summary": "g"}, "activities": [],
                     "outcome": {"status": "completed", "attention": "none"},
                     "metrics": {"tokens": 10, "turns": 1, "duration_seconds": 5.0}},
    }
    board = aggregate_board([run], coverage_summary={"total": 3, "untested_count": 1,
                                                     "by_state": {"passed": 2, "tried": 1},
                                                     "coverage_rate": 0.667})
    assert board["summary"]["completed"] == 1
    assert board["columns"]["completed"]["count"] == 1
    assert board["coverage"]["untested_count"] == 1


# ── server wiring: coverage command + kanban board 未覆盖 ──


@pytest.mark.asyncio
async def test_server_coverage_command_and_kanban_board(tmp_path, monkeypatch):
    from modus.desktop import db, server

    monkeypatch.setenv("MODUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    current = db.create_session("Current")

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    socket = Socket()
    session = server.DaoSession(id="runtime", db_id=current["id"])

    # Seed the coverage ledger for this session.
    from modus.tools.coverage import _coverage_store

    store = _coverage_store(current["id"])
    store.mark("o1", "read", "filesystem", "passed")
    store.mark("o1", "command", "exec", "passed")
    store.untested(["o1", "o2"], ["filesystem", "exec", "network"])

    # coverage command (summary).
    assert await server.command_router.dispatch(
        socket, session, {"type": "coverage", "operation": "summary", "request_id": "cr1"},
    ) is True
    coverage_packet = socket.sent[-1]
    assert coverage_packet["type"] == "coverage"
    assert coverage_packet["operation"] == "summary"
    assert coverage_packet["data"]["total"] == 2
    assert coverage_packet["data"]["untested_count"] == 4

    # coverage command (untested cross).
    assert await server.command_router.dispatch(
        socket, session, {
            "type": "coverage", "operation": "untested",
            "objectives": ["o1", "o2"],
            "capabilities": ["filesystem", "exec", "network"],
            "request_id": "cr2",
        },
    ) is True
    untested_packet = socket.sent[-1]
    assert untested_packet["type"] == "coverage"
    assert untested_packet["operation"] == "untested"
    assert untested_packet["data"]["untested_count"] == 4

    # kanban_board carries the 未覆盖 count for the frontend badge.
    assert await server.command_router.dispatch(
        socket, session, {"type": "kanban_board", "request_id": "kb1"},
    ) is True
    board_packet = socket.sent[-1]
    assert board_packet["type"] == "kanban_board"
    assert board_packet["board"]["coverage"]["total"] == 2
    assert board_packet["board"]["coverage"]["untested_count"] == 4


@pytest.mark.asyncio
async def test_server_coverage_requires_persisted_session(tmp_path, monkeypatch):
    from modus.desktop import server

    monkeypatch.setenv("MODUS_DATA_DIR", str(tmp_path / "data"))

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    socket = Socket()
    session = server.DaoSession(id="runtime", db_id="")
    assert await server.command_router.dispatch(
        socket, session, {"type": "coverage", "operation": "summary", "request_id": "cr1"},
    ) is True
    packet = socket.sent[-1]
    assert packet["type"] == "coverage"
    assert packet["data"] is None
