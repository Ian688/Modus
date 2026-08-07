"""Persistent code-search index (Paicli code_index, deepened for Modus).

The index persists the bounded walker's output as SQLite so search_code's
use_index path queries pre-indexed lines instead of re-scanning.  The indexed
path must preserve every search semantic (word-boundary / regex / case) and
fall back to the live scan when no index exists.
"""

from __future__ import annotations

import pytest

from modus.config import ModusConfig
from modus.tools.base import ToolContext
from modus.tools.builtins import get_builtin_tools, rebuild_code_index, search_code
from modus.tools.code_index import CodeIndex


@pytest.fixture
def indexed_ws(tmp_path, monkeypatch):
    """A workspace with a built index, isolated from the real ~/.modus."""
    from pathlib import Path

    # The bounded walker is home-anchored (PathGuard); point home at tmp_path
    # so the workspace under it is inside the boundary.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("MODUS_DATA_DIR", str(tmp_path / ".modus"))
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(5):
        (ws / f"mod{i}.py").write_text(
            f"def handler_{i}():\n    return {i}\nhandler_{i}_extra()\n",
            encoding="utf-8",
        )
    return ws, tmp_path


def _ctx(ws):
    return ToolContext(cwd=str(ws), workspace_root=str(ws), config=ModusConfig())


@pytest.mark.asyncio
async def test_rebuild_indexes_workspace(indexed_ws):
    ws, _ = indexed_ws
    ctx = _ctx(ws)
    result = await rebuild_code_index({}, ctx)
    assert not result.is_error
    assert "5 files" in result.content
    assert "lines" in result.content


@pytest.mark.asyncio
async def test_use_index_returns_matches(indexed_ws):
    ws, _ = indexed_ws
    ctx = _ctx(ws)
    await rebuild_code_index({}, ctx)
    result = await search_code(
        {"query": "handler_1", "path": ".", "limit": 50, "use_index": True}, ctx,
    )
    assert not result.is_error
    assert "handler_1" in result.content
    # Substring semantics preserved: handler_1_extra and handler_10 also match.
    assert "handler_1_extra" in result.content


@pytest.mark.asyncio
async def test_use_index_preserves_word_boundary(indexed_ws):
    ws, _ = indexed_ws
    ctx = _ctx(ws)
    await rebuild_code_index({}, ctx)
    result = await search_code(
        {"query": "handler_1", "path": ".", "limit": 50,
         "use_index": True, "word_boundary": True}, ctx,
    )
    assert "handler_1_extra" not in result.content  # whole identifier only
    assert "handler_1" in result.content


@pytest.mark.asyncio
async def test_use_index_falls_back_to_scan(indexed_ws):
    """No index built -> use_index silently falls back to the live scan."""
    ws, _ = indexed_ws
    ctx = _ctx(ws)
    # Do NOT rebuild; the scan path must still answer.
    result = await search_code(
        {"query": "handler_2", "path": ".", "limit": 50, "use_index": True}, ctx,
    )
    assert not result.is_error
    assert "handler_2" in result.content


@pytest.mark.asyncio
async def test_use_index_case_sensitive(indexed_ws):
    ws, _ = indexed_ws
    ctx = _ctx(ws)
    await rebuild_code_index({}, ctx)
    (ws / "case.py").write_text("USER = 1\nuser = 2\n", encoding="utf-8")
    await rebuild_code_index({}, ctx)  # refresh to include case.py
    upper = await search_code(
        {"query": "USER", "path": ".", "limit": 50,
         "use_index": True, "case_sensitive": True}, ctx,
    )
    assert "USER = 1" in upper.content
    assert "user = 2" not in upper.content


def test_rebuild_code_index_declared():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    tool = tools["rebuild_code_index"]
    assert tool.is_read_only is True
    assert "filesystem" in tool.capabilities


def test_code_index_isolation_per_root():
    """Different roots map to different index DBs."""
    a = CodeIndex("/tmp/root-a", "/tmp/base")
    b = CodeIndex("/tmp/root-b", "/tmp/base")
    assert a.db_path != b.db_path
    # Same root -> same DB (idempotent rebuild target).
    c = CodeIndex("/tmp/root-a", "/tmp/base")
    assert a.db_path == c.db_path
