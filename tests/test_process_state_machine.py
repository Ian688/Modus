"""T5: supervisor-style process state machine + backoff restart.

Ported semantics (not code) from supervisor's process lifecycle:
STARTING → RUNNING once the child survives ``startsecs``; a faster non-zero
exit is a startup failure (too_quickly) → BACKOFF with exponential backoff,
retried up to ``startretries`` then given up on (``failed``).  A faster exit-0
is a completed one-shot, not a crash.  kill/cleanup set manual terminal states
(``exited``/``stopped``) the supervisor never overwrites; restart_process is a
manual, approval-gated explicit restart for non-fatal states.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from modus.config import ModusConfig
from modus.tools.base import ToolContext
from modus.tools.process_tools import (
    _read_meta,
    _write_meta,
    get_process_tools,
    kill_process,
    list_processes,
    restart_process,
    spawn_process,
)


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch):
    """Isolate every test to a temp registry directory."""
    import modus.tools.process_tools as pt

    monkeypatch.setattr(pt, "_PROCESSES_DIR", tmp_path / "processes")
    yield
    import shutil

    shutil.rmtree(tmp_path / "processes", ignore_errors=True)


def _ctx():
    return ToolContext(cwd=str(os.path.expanduser("~")), config=ModusConfig())


async def _wait_for_status(process_id: str, statuses: set[str], *, timeout=8.0) -> str:
    """Poll the registry until the entry reaches one of ``statuses``."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = _read_meta(process_id)
        if meta is not None and meta.get("status") in statuses:
            return meta["status"]
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"process {process_id} did not reach {statuses} in {timeout}s; "
        f"last meta={_read_meta(process_id)}"
    )


# ── declaration contract ──


def test_restart_process_tool_declared():
    tools = {tool.name: tool for tool in get_process_tools()}
    assert "restart_process" in tools
    tool = tools["restart_process"]
    assert tool.danger_level == "medium"
    assert tool.requires_approval is True
    assert tool.is_read_only is False
    assert "process_id" in tool.required_keys
    assert tool.capabilities == ("exec",)


# ── spawn params are persisted ──


@pytest.mark.asyncio
async def test_spawn_persists_startsecs_and_startretries():
    ctx = _ctx()
    result = await spawn_process(
        {"command": "sleep 20", "startsecs": 2, "startretries": 5}, ctx,
    )
    meta = _read_meta(result.metadata["process_id"])
    assert meta["status"] == "starting"
    assert meta["startsecs"] == 2
    assert meta["startretries"] == 5
    assert meta["retries_left"] == 5
    assert meta["retry_count"] == 0
    # A live, owned child transitions to running after startsecs.
    status = await _wait_for_status(result.metadata["process_id"], {"running"})
    assert status == "running"
    assert _read_meta(result.metadata["process_id"])["exit_code"] is None
    await kill_process({"process_id": result.metadata["process_id"]}, ctx)


@pytest.mark.asyncio
async def test_spawn_invalid_params_fall_back_to_defaults():
    ctx = _ctx()
    result = await spawn_process(
        {"command": "sleep 20", "startsecs": "x", "startretries": -3}, ctx,
    )
    meta = _read_meta(result.metadata["process_id"])
    assert meta["startsecs"] == 1
    assert meta["startretries"] == 3
    await kill_process({"process_id": result.metadata["process_id"]}, ctx)


# ── too_quickly → backoff → failed (startup-failure retry) ──


@pytest.mark.asyncio
async def test_spawn_too_quickly_backoff(tmp_path):
    """A process that exits non-zero before startsecs is a startup failure."""
    ctx = _ctx()
    # First child fails fast (no marker yet) → BACKOFF; the retry sees the
    # marker and survives → RUNNING.
    command = (
        f'[ -e marker-ok ] && sleep 20 || {{ touch marker-ok; exit 1; }}'
    )
    result = await spawn_process(
        {"command": command, "cwd": str(tmp_path),
         "startsecs": 1, "startretries": 3}, ctx,
    )
    process_id = result.metadata["process_id"]

    status = await _wait_for_status(process_id, {"backoff", "running"})
    assert status == "backoff"
    meta = _read_meta(process_id)
    assert meta["exit_code"] == 1
    assert meta["retries_left"] == 2
    assert meta["retry_count"] == 0
    assert meta["backoff_until"] is not None and meta["backoff_until"] > time.time()

    status = await _wait_for_status(process_id, {"running"})
    assert status == "running"
    assert _read_meta(process_id)["retry_count"] == 1
    await kill_process({"process_id": process_id}, ctx)


@pytest.mark.asyncio
async def test_restart_retries_with_backoff():
    """startretries exhausted → failed; retry count grows; backoff increases."""
    ctx = _ctx()
    # Every retry fails fast: 3 attempts (1 initial + 2 retries) then failed.
    result = await spawn_process(
        {"command": "exit 1", "startsecs": 1, "startretries": 2}, ctx,
    )
    process_id = result.metadata["process_id"]

    status = await _wait_for_status(process_id, {"failed"})
    assert status == "failed"
    meta = _read_meta(process_id)
    assert meta["retry_count"] == 2
    assert meta["retries_left"] == 0
    assert meta["exit_code"] == 1
    assert _pid_gone(meta["pid"])  # no child left to kill


@pytest.mark.asyncio
async def test_exponential_backoff_increases():
    """Backoff delay doubles between retries (later window > earlier)."""
    ctx = _ctx()
    result = await spawn_process(
        {"command": "exit 1", "startsecs": 0.1, "startretries": 4}, ctx,
    )
    process_id = result.metadata["process_id"]

    await _wait_for_status(process_id, {"backoff"})
    first = _read_meta(process_id)["backoff_until"]

    # Wait for the loop to consume the first retry and open the next window:
    # retry_count increments, then a *new* (later) backoff_until is written.
    deadline = time.time() + 8.0
    second = None
    while time.time() < deadline:
        meta = _read_meta(process_id)
        if meta.get("status") == "backoff" and meta.get("retry_count", 0) >= 1:
            second = meta["backoff_until"]
            break
        await asyncio.sleep(0.05)
    assert second is not None, "expected a second backoff window"
    assert second > first
    # Both delays were scheduled for future moments.
    assert _read_meta(process_id)["retry_count"] >= 1


@pytest.mark.asyncio
async def test_quick_exit_zero_is_completed_not_backoff():
    """A one-shot that exits 0 within startsecs is completed, not a crash."""
    ctx = _ctx()
    result = await spawn_process(
        {"command": "exit 0", "startsecs": 1, "startretries": 3}, ctx,
    )
    status = await _wait_for_status(result.metadata["process_id"], {"completed"})
    assert status == "completed"
    meta = _read_meta(result.metadata["process_id"])
    assert meta["exit_code"] == 0
    assert meta["retry_count"] == 0
    assert meta["retries_left"] == 3


# ── list_processes reports the state machine ──


@pytest.mark.asyncio
async def test_list_reports_backoff_state():
    ctx = _ctx()
    result = await spawn_process(
        {"command": "exit 1", "startsecs": 1, "startretries": 3}, ctx,
    )
    process_id = result.metadata["process_id"]
    await _wait_for_status(process_id, {"backoff"})
    data = json.loads((await list_processes({}, ctx)).content)
    row = next(r for r in data["processes"] if r["process_id"] == process_id)
    assert row["status"] == "backoff"
    await _wait_for_status(process_id, {"failed"}, timeout=8.0)


@pytest.mark.asyncio
async def test_kill_during_backoff_never_resurrects():
    """Killing a process while it is in backoff stops the retry loop."""
    ctx = _ctx()
    result = await spawn_process(
        {"command": "exit 1", "startsecs": 1, "startretries": 5}, ctx,
    )
    process_id = result.metadata["process_id"]
    await _wait_for_status(process_id, {"backoff"})

    killed = await kill_process({"process_id": process_id}, ctx)
    assert not killed.is_error
    meta = _read_meta(process_id)
    assert meta["status"] == "exited" or _pid_gone(meta["pid"])
    # Give the loop time to wake; it must NOT flip the entry back to starting.
    await asyncio.sleep(0.3)
    assert _read_meta(process_id)["status"] != "starting"
    assert _read_meta(process_id)["status"] != "backoff"


# ── restart_process ──


@pytest.mark.asyncio
async def test_restart_process_resets_state_machine():
    ctx = _ctx()
    result = await spawn_process({"command": "sleep 20"}, ctx)
    process_id = result.metadata["process_id"]
    await _wait_for_status(process_id, {"running"})
    old_pid = _read_meta(process_id)["pid"]

    restarted = await restart_process({"process_id": process_id}, ctx)
    assert not restarted.is_error
    new_pid = restarted.metadata["pid"]
    assert new_pid != old_pid

    meta = _read_meta(process_id)
    assert meta["status"] == "starting"
    assert meta["pid"] == new_pid
    # Retry budget reset, count carried forward.
    assert meta["retries_left"] == meta["startretries"]
    assert meta["retry_count"] == 1

    await _wait_for_status(process_id, {"running"})
    await kill_process({"process_id": process_id}, ctx)


@pytest.mark.asyncio
async def test_restart_after_failed():
    """A failed process is explicitly restartable."""
    ctx = _ctx()
    result = await spawn_process(
        {"command": "exit 1", "startsecs": 1, "startretries": 1}, ctx,
    )
    process_id = result.metadata["process_id"]
    await _wait_for_status(process_id, {"failed"})

    restarted = await restart_process({"process_id": process_id}, ctx)
    assert not restarted.is_error
    meta = _read_meta(process_id)
    assert meta["status"] == "starting"
    await _wait_for_status(process_id, {"failed"}, timeout=8.0)
    await kill_process({"process_id": process_id}, ctx)


@pytest.mark.asyncio
async def test_restart_refused_for_fatal():
    ctx = _ctx()
    result = await spawn_process(
        {"command": "exit 1", "startsecs": 1, "startretries": 1}, ctx,
    )
    process_id = result.metadata["process_id"]
    await _wait_for_status(process_id, {"failed"})
    # Mark it fatal (the give-up state) to pin the refusal contract.
    meta = _read_meta(process_id)
    meta["status"] = "fatal"
    _write_meta(process_id, meta)

    restarted = await restart_process({"process_id": process_id}, ctx)
    assert restarted.is_error
    assert "fatal" in restarted.content.lower()


@pytest.mark.asyncio
async def test_restart_process_approval():
    """restart_process is declared medium + requires_approval (six invariants)."""
    tool = next(t for t in get_process_tools() if t.name == "restart_process")
    assert tool.danger_level == "medium"
    assert tool.requires_approval is True
    assert tool.is_read_only is False


@pytest.mark.asyncio
async def test_restart_refuses_missing_or_fatal(monkeypatch):
    ctx = _ctx()
    missing = await restart_process({"process_id": "nope"}, ctx)
    assert missing.is_error

    # A FATAL entry refuses restart without touching anything.
    _write_meta("fat1", {
        "process_id": "fat1", "pid": 1, "command": "echo hi",
        "status": "fatal", "spawned_by": os.getpid(),
    })
    refused = await restart_process({"process_id": "fat1"}, ctx)
    assert refused.is_error
    assert "fatal" in refused.content.lower()


def _pid_gone(pid) -> bool:
    if not pid or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
