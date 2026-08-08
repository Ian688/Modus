"""C3 result bridge: oversized tool results -> artifact handles + content-addressed cache.

Covers Wave2 C3 验收清单:
- ``test_oversized_result_bridged``: >100KB content leaves the conversation; a
  compact ``{path, sha256, size, preview}`` handle is returned and the full text
  is persisted under Modus's private artifact store.
- ``test_cache_hit_on_same_args``: the same read-only tool+args reuses the
  persisted handle without re-executing.
- ``test_write_invalidates_cache``: a mutating tool clears the cache so the next
  identical read re-executes.
- ``test_artifact_intact_check``: a persisted file that was modified in place
  fails the SHA-256 check and the reuse is refused.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from modus.config import ModusConfig
from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
from modus.tools.builtins import grep, search_code
from modus.tools.executor import ToolExecutor
from modus.tools.registry import ToolRegistry


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    """Point home and the Modus data dir at temp dirs (path guard + store)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("MODUS_DATA_DIR", str(tmp_path / "data"))
    return home


@pytest.fixture
def bridge_store(tmp_path, monkeypatch):
    from modus.desktop import artifacts as artifacts_module

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(artifacts_module, "artifact_root", lambda: data / "artifacts")
    return data / "artifacts"


def _ctx(home: Path) -> ToolContext:
    ws = home / "ws"
    ws.mkdir(exist_ok=True)
    return ToolContext(cwd=str(ws), workspace_root=str(ws), config=ModusConfig())


def _oversized_workspace(home: Path, n: int = 600, fill: int = 500) -> Path:
    ws = home / "ws"
    ws.mkdir(exist_ok=True)
    for i in range(n):
        (ws / f"f{i}.py").write_text(f"needle {'y' * fill}\n", encoding="utf-8")
    return ws


# ── unit: cache_key canonicalization ──


def test_cache_key_order_and_secret_independent():
    from modus.desktop.artifacts import cache_key

    a = cache_key("grep", {"pattern": "needle", "path": "src", "limit": 100})
    b = cache_key("grep", {"limit": 100, "path": "src", "pattern": "needle"})
    assert a == b  # key order does not matter
    c = cache_key("grep", {"pattern": "needle", "path": "src", "limit": 100, "api_key": "sk-abc"})
    assert a == c  # secret-bearing args are stripped before hashing
    d = cache_key("grep", {"pattern": "needle", "path": "src", "limit": 999})
    assert a != d  # a different limit is a different result — never reused
    e = cache_key("search_code", {"pattern": "needle", "path": "src", "limit": 100})
    assert a != e  # the tool name binds the key


def test_cache_key_strips_nested_and_typed_secrets():
    from modus.desktop.artifacts import cache_key

    base = cache_key("grep", {"pattern": "needle", "path": "src"})
    assert cache_key("grep", {"pattern": "needle", "path": "src", "token": "abc"}) == base
    assert cache_key("grep", {"pattern": "needle", "path": "src", "password": "x"}) == base
    assert cache_key("grep", {"pattern": "needle", "path": "src", "authorization": "y"}) == base


# ── unit: persist_oversized + artifact_is_intact ──


def test_small_content_returns_none(bridge_store):
    from modus.desktop.artifacts import persist_oversized

    assert persist_oversized("grep", "small result", "txt") is None


def test_oversized_persists_and_returns_handle(bridge_store):
    from modus.desktop.artifacts import artifact_is_intact, persist_oversized

    content = "needle " + "y" * 150_000
    handle = persist_oversized("grep", content, "txt", args={"pattern": "y"}, cache=True)
    assert handle is not None
    assert set(("path", "sha256", "size", "preview")) <= set(handle)
    assert handle["size"] > 100 * 1024
    assert len(handle["preview"]) < 5_000  # preview stays compact
    assert artifact_is_intact(handle["path"], handle["sha256"])

    restored = Path(handle["path"]).read_bytes()
    assert hashlib.sha256(restored).hexdigest() == handle["sha256"]
    assert "needle" in restored.decode("utf-8")


def test_artifact_intact_check_rejects_modified_file(bridge_store):
    from modus.desktop.artifacts import artifact_is_intact, persist_oversized

    content = "needle " + "y" * 150_000
    handle = persist_oversized("grep", content, "txt", args={"pattern": "y"}, cache=True)
    assert artifact_is_intact(handle["path"], handle["sha256"]) is True

    Path(handle["path"]).write_bytes(b"corrupted by another writer")
    assert artifact_is_intact(handle["path"], handle["sha256"]) is False


def test_persist_oversized_never_writes_into_user_workspace(bridge_store, isolated_data):
    from modus.desktop.artifacts import persist_oversized

    content = "z" * 150_000
    handle = persist_oversized("grep", content, "txt", args={"pattern": "z"}, cache=True)
    path = Path(handle["path"])
    assert path.is_relative_to(bridge_store)
    # The user workspace (under home) never receives the persisted result.
    assert not path.is_relative_to(isolated_data)


# ── unit: cached_handle + invalidate_cache ──


def test_cache_hit_on_same_args(bridge_store):
    from modus.desktop.artifacts import cached_handle, persist_oversized

    content = "needle " + "y" * 150_000
    persist_oversized("grep", content, "txt", args={"path": ".", "pattern": "needle"}, cache=True)
    handle = cached_handle("grep", {"pattern": "needle", "path": "."})
    assert handle is not None
    assert handle.get("cached") is True
    assert handle["sha256"] == hashlib.sha256(
        (content if content.endswith("\n") else content + "\n").encode("utf-8"),
    ).hexdigest()


def test_cache_miss_on_different_args(bridge_store):
    from modus.desktop.artifacts import cached_handle, persist_oversized

    persist_oversized(
        "grep", "needle " + "y" * 150_000, "txt",
        args={"path": ".", "pattern": "needle"}, cache=True,
    )
    assert cached_handle("grep", {"pattern": "needle", "path": "other"}) is None


def test_cache_miss_when_file_corrupted(bridge_store):
    from modus.desktop.artifacts import cached_handle, persist_oversized

    handle = persist_oversized(
        "grep", "needle " + "y" * 150_000, "txt",
        args={"path": ".", "pattern": "needle"}, cache=True,
    )
    Path(handle["path"]).write_bytes(b"tampered")
    assert cached_handle("grep", {"pattern": "needle", "path": "."}) is None


def test_write_invalidates_cache(bridge_store):
    from modus.desktop.artifacts import cached_handle, invalidate_cache, persist_oversized

    persist_oversized(
        "grep", "needle " + "y" * 150_000, "txt",
        args={"path": ".", "pattern": "needle"}, cache=True,
    )
    assert cached_handle("grep", {"pattern": "needle", "path": "."}) is not None
    # A write tool clears the whole content-addressed cache.
    invalidate_cache("", None)
    assert cached_handle("grep", {"pattern": "needle", "path": "."}) is None


def test_arg_scoped_invalidation_only_touches_matching_tool(bridge_store):
    from modus.desktop.artifacts import cached_handle, invalidate_cache, persist_oversized

    persist_oversized(
        "grep", "needle " + "y" * 150_000, "txt",
        args={"path": "src", "pattern": "needle"}, cache=True,
    )
    # A bash mutation invalidates only bash-cached reads, never grep's.
    invalidate_cache("bash", {"command": "echo hi"})
    assert cached_handle("grep", {"pattern": "needle", "path": "src"}) is not None


def test_mutating_tools_set_covers_write_file_and_process_tools():
    from modus.desktop.artifacts import _MUTATING_TOOLS

    for name in ("write_file", "edit_file", "patch", "spawn_process", "kill_process"):
        assert name in _MUTATING_TOOLS


# ── integration: grep / search_code bridging ──


@pytest.mark.asyncio
async def test_grep_oversized_bridged(isolated_data):
    _oversized_workspace(isolated_data)
    ctx = _ctx(isolated_data)
    result = await grep({"pattern": "needle", "path": ".", "limit": 100_000}, ctx)

    assert not result.is_error
    assert result.model_payload is not None  # the model reads the handle
    assert "完整内容已落盘" in result.content
    assert "path:" in result.content
    assert "sha256:" in result.content
    assert result.disclosure.get("oversized") is True
    assert result.disclosure.get("raw_content_sent") is False
    assert len(result.artifacts) == 1
    assert set(("path", "sha256", "size", "preview")) <= set(result.artifacts[0])
    # The raw full text stays local, never in the model payload.
    assert "y" * 500 not in result.model_text()


@pytest.mark.asyncio
async def test_grep_small_result_unchanged(isolated_data):
    _oversized_workspace(isolated_data)
    ctx = _ctx(isolated_data)
    result = await grep({"pattern": "needle", "path": ".", "limit": 2}, ctx)

    assert not result.is_error
    assert result.model_payload is None  # inline, legacy shape
    assert "needle" in result.content
    assert "完整内容已落盘" not in result.content


@pytest.mark.asyncio
async def test_grep_cache_reuse_on_same_args(isolated_data):
    _oversized_workspace(isolated_data)
    ctx = _ctx(isolated_data)
    first = await grep({"pattern": "needle", "path": ".", "limit": 100_000}, ctx)
    second = await grep({"pattern": "needle", "path": ".", "limit": 100_000}, ctx)

    assert not first.is_error and not second.is_error
    assert "完整内容已落盘" in first.content
    assert "缓存复用" in second.content
    assert second.disclosure.get("cached") is True


@pytest.mark.asyncio
async def test_search_code_oversized_bridged(isolated_data):
    _oversized_workspace(isolated_data)
    ctx = _ctx(isolated_data)
    result = await search_code({"query": "needle", "path": ".", "limit": 1000}, ctx)

    assert not result.is_error
    assert result.model_payload is not None
    assert "完整内容已落盘" in result.content
    assert result.disclosure.get("oversized") is True
    assert len(result.artifacts) == 1


@pytest.mark.asyncio
async def test_write_invalidates_grep_cache_integration(bridge_store, isolated_data):
    """A write through the executor clears grep's cached handle (写后读一致)."""
    from modus.desktop.artifacts import cached_handle, persist_oversized

    persist_oversized(
        "grep", "needle " + "y" * 150_000, "txt",
        args={"path": ".", "pattern": "needle"}, cache=True,
    )
    assert cached_handle("grep", {"pattern": "needle", "path": "."}) is not None

    async def write_handler(_payload, _context):
        return ToolResult("wrote")

    tool = Tool(
        name="write_file", description="write",
        parameters=object_schema({"path": {"type": "string"}}, ["path"]),
        handler=write_handler, required_keys=["path"],
        is_read_only=False, danger_level="medium",
    )
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry)
    ctx = ToolContext(
        cwd=str(isolated_data), config=ModusConfig(),
        approval_callback=lambda _request: "approve",
    )
    result = (await executor.execute_all(
        [{"id": "w", "function": {"name": "write_file", "arguments": '{"path": "a.py"}'}}], ctx,
    ))[0]
    assert result.content == "wrote"
    assert cached_handle("grep", {"pattern": "needle", "path": "."}) is None


# ── integration: executor fallback bridge ──


@pytest.mark.asyncio
async def test_executor_fallback_bridges_oversized(isolated_data, bridge_store):
    async def big_handler(_payload, _context):
        return ToolResult("L" * 150_000)

    tool = Tool("big_probe", "desc", object_schema({}, []), big_handler, is_read_only=True)
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry)
    ctx = ToolContext(cwd=str(isolated_data), config=ModusConfig())
    result = (await executor.execute_all(
        [{"id": "c", "function": {"name": "big_probe", "arguments": "{}"}}], ctx,
    ))[0]

    assert not result.is_error
    assert result.model_payload is not None
    assert "完整内容已落盘" in result.content
    assert result.raw_result == "L" * 150_000  # raw stays local
    assert result.disclosure.get("oversized") is True
    assert len(result.artifacts) == 1


@pytest.mark.asyncio
async def test_executor_fallback_leaves_small_and_payload_results(isolated_data):
    async def small_handler(_payload, _context):
        return ToolResult("tiny")

    async def payload_handler(_payload, _context):
        return ToolResult("legacy", model_payload="bounded", raw_result="RAW")

    registry = ToolRegistry()
    registry.register(Tool("small_probe", "desc", object_schema({}, []), small_handler, is_read_only=True))
    registry.register(Tool("payload_probe", "desc", object_schema({}, []), payload_handler, is_read_only=True))
    executor = ToolExecutor(registry)
    ctx = ToolContext(cwd=str(isolated_data), config=ModusConfig())

    small = (await executor.execute_all(
        [{"id": "s", "function": {"name": "small_probe", "arguments": "{}"}}], ctx,
    ))[0]
    assert small.content == "tiny"
    assert small.model_payload is None
    assert small.artifacts == []

    payload = (await executor.execute_all(
        [{"id": "p", "function": {"name": "payload_probe", "arguments": "{}"}}], ctx,
    ))[0]
    # A tool that already bounded its own result is never double-bridged.
    assert payload.model_payload == "bounded"
    assert payload.raw_result == "RAW"


@pytest.mark.asyncio
async def test_deny_skip_results_never_bridged(isolated_data):
    """A2 deny/skip structured results (is_error=False) are information, not
    large results — the executor must never bridge them, even with big content."""
    async def deny_handler(_payload, _context):
        return ToolResult.denied("read_file", reason="blocked by user", tool_use_id="d")

    async def skip_handler(_payload, _context):
        return ToolResult.skipped("read_file", reason="deferred", tool_use_id="s")

    registry = ToolRegistry()
    registry.register(Tool("read_file", "desc", object_schema({}, []), deny_handler, is_read_only=True))
    executor = ToolExecutor(registry)
    ctx = ToolContext(cwd=str(isolated_data), config=ModusConfig())

    denied = (await executor.execute_all(
        [{"id": "d", "function": {"name": "read_file", "arguments": "{}"}}], ctx,
    ))[0]
    assert denied.is_error is False
    assert denied.metadata.get("operation") == "approval-denied"
    assert denied.model_payload is None
    assert "完整内容已落盘" not in denied.content

    registry.register(Tool("search_code", "desc", object_schema({}, []), skip_handler, is_read_only=True))
    skipped = (await executor.execute_all(
        [{"id": "s", "function": {"name": "search_code", "arguments": "{}"}}], ctx,
    ))[0]
    assert skipped.is_error is False
    assert skipped.metadata.get("operation") == "skipped"
    assert skipped.model_payload is None


@pytest.mark.asyncio
async def test_executor_error_result_not_bridged(isolated_data):
    async def err_handler(_payload, _context):
        return ToolResult("big " * 50_000, is_error=True)

    registry = ToolRegistry()
    registry.register(Tool("fail_probe", "desc", object_schema({}, []), err_handler, is_read_only=True))
    executor = ToolExecutor(registry)
    ctx = ToolContext(cwd=str(isolated_data), config=ModusConfig())
    result = (await executor.execute_all(
        [{"id": "e", "function": {"name": "fail_probe", "arguments": "{}"}}], ctx,
    ))[0]
    assert result.is_error is True
    assert result.model_payload is None  # errors pass through untouched
