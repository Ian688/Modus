"""Background process tools: spawn/list/tail/kill + durable registry.

These give the agent a process handle across tool calls (function-map #1) and
carry the persistence half of the power-loss guard: the registry lives under
~/.modus/processes so a crash leaves the entry recoverable, and list_processes
reports a survived process as ``orphaned`` when its owner pid is gone.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

from modus.config import ModusConfig
from modus.tools.base import ToolContext
from modus.tools.builtins import get_builtin_tools
from modus.tools.process_tools import (
    _MAX_REGISTRY_ROWS,
    _pid_alive,
    _process_dir,
    _process_status,
    _read_meta,
    _write_meta,
    kill_process,
    list_processes,
    spawn_process,
    tail_process,
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


# ── declaration contract ──


def test_process_tools_declared():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    assert "spawn_process" in tools
    assert "list_processes" in tools
    assert "tail_process" in tools
    assert "kill_process" in tools
    assert tools["spawn_process"].capabilities == ("exec",)
    assert tools["spawn_process"].danger_level == "high"
    assert tools["spawn_process"].requires_approval is True
    assert tools["list_processes"].is_read_only is True
    assert tools["tail_process"].is_read_only is True
    assert tools["kill_process"].requires_approval is True


def test_process_tools_not_visible_to_subagents():
    from modus.desktop import peri

    names = set(getattr(peri, "SUBAGENT_ALLOWED_TOOL_NAMES", ()))
    assert "spawn_process" not in names


# ── registry durability ──


def test_meta_round_trips():
    _write_meta("abc123", {"pid": 1, "command": "x", "status": "running"})
    meta = _read_meta("abc123")
    assert meta["pid"] == 1
    assert meta["status"] == "running"
    assert (_process_dir("abc123") / "meta.json").exists()


def test_pid_alive_probes():
    # Our own pid is always alive.
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(999_999_999) is False
    assert _pid_alive(None) is False


# ── lifecycle ──


@pytest.mark.asyncio
async def test_spawn_list_tail_kill_round_trip():
    ctx = _ctx()
    result = await spawn_process(
        {"command": "echo hello-from-spawn; sleep 0.3; echo done"}, ctx,
    )
    assert not result.is_error
    process_id = result.metadata["process_id"]
    assert result.metadata["pid"] > 0

    await asyncio.sleep(0.4)
    listed = await list_processes({}, ctx)
    rows = json.loads(listed.content)["processes"]
    assert any(r["process_id"] == process_id for r in rows)

    tailed = await tail_process({"process_id": process_id}, ctx)
    assert "hello-from-spawn" in tailed.content
    assert "done" in tailed.content

    # A finished process is reported as stopped (pid gone).
    killed = await kill_process({"process_id": process_id}, ctx)
    assert not killed.is_error
    meta = _read_meta(process_id)
    assert meta["status"] == "stopped"


@pytest.mark.asyncio
async def test_kill_twice_is_idempotent():
    ctx = _ctx()
    result = await spawn_process({"command": "sleep 20"}, ctx)
    process_id = result.metadata["process_id"]
    assert not (await kill_process({"process_id": process_id}, ctx)).is_error
    again = await kill_process({"process_id": process_id}, ctx)
    assert not again.is_error  # already stopped -> non-error no-op


@pytest.mark.asyncio
async def test_kill_live_process_marks_exited():
    """Killing a still-running process records status=exited."""
    ctx = _ctx()
    result = await spawn_process({"command": "sleep 20"}, ctx)
    process_id = result.metadata["process_id"]

    killed = await kill_process({"process_id": process_id}, ctx)
    assert not killed.is_error
    meta = _read_meta(process_id)
    assert meta["status"] == "exited"


@pytest.mark.asyncio
async def test_list_bounded_and_schema():
    ctx = _ctx()
    for _ in range(3):
        await spawn_process({"command": "sleep 5"}, ctx)
    listed = await list_processes({"limit": 2}, ctx)
    data = json.loads(listed.content)
    assert data["schema"] == "modus.processes.v1"
    assert len(data["processes"]) <= 2


# ── power-loss: orphaned detection ──


def test_orphaned_when_owner_pid_changed():
    """A registry entry whose owner pid differs is orphaned while alive."""
    process_id = "orphan1"
    _write_meta(process_id, {
        "process_id": process_id, "pid": os.getpid(), "status": "running",
        "spawned_by": os.getpid() + 1,  # a different (older) Modus process
    })
    status = _process_status(_read_meta(process_id))
    assert status == "orphaned"


def test_running_when_owned_and_alive():
    process_id = "own1"
    _write_meta(process_id, {
        "process_id": process_id, "pid": os.getpid(), "status": "running",
        "spawned_by": os.getpid(),
    })
    assert _process_status(_read_meta(process_id)) == "running"


def test_stopped_when_pid_gone():
    process_id = "stop1"
    _write_meta(process_id, {
        "process_id": process_id, "pid": 999_999_999, "status": "running",
        "spawned_by": os.getpid(),
    })
    assert _process_status(_read_meta(process_id)) == "stopped"


def test_exited_status_preserved():
    process_id = "exit1"
    _write_meta(process_id, {
        "process_id": process_id, "pid": os.getpid(), "status": "exited",
        "exit_code": 0, "spawned_by": os.getpid(),
    })
    assert _process_status(_read_meta(process_id)) == "exited"


@pytest.mark.asyncio
async def test_list_reports_orphan_after_simulated_restart():
    ctx = _ctx()
    result = await spawn_process({"command": "sleep 20"}, ctx)
    process_id = result.metadata["process_id"]

    # Simulate Desktop restart: another Modus pid owns the registry entry.
    meta = _read_meta(process_id)
    meta["spawned_by"] = meta["spawned_by"] + 1
    _write_meta(process_id, meta)

    data = json.loads((await list_processes({}, ctx)).content)
    row = next(r for r in data["processes"] if r["process_id"] == process_id)
    assert row["status"] == "orphaned"


# ── command policy still applies ──


@pytest.mark.asyncio
async def test_spawn_rejects_destructive_command():
    ctx = _ctx()
    result = await spawn_process({"command": "rm -rf /"}, ctx)
    assert result.is_error
    assert "policy" in result.content


@pytest.mark.asyncio
async def test_spawn_rejects_wrapper_bypass():
    ctx = _ctx()
    result = await spawn_process({"command": 'sh -c "rm -rf /"'}, ctx)
    assert result.is_error
    assert "policy" in result.content


# ── background task model (Paicli runtime tasks, deepened) ──


@pytest.mark.asyncio
async def test_spawn_process_records_task_name():
    ctx = _ctx()
    result = await spawn_process(
        {"command": "sleep 20", "task_name": "gen-report", "description": "生成报告"}, ctx,
    )
    meta = _read_meta(result.metadata["process_id"])
    assert meta["task_name"] == "gen-report"
    assert meta["description"] == "生成报告"


@pytest.mark.asyncio
async def test_natural_exit_records_completed():
    """A process that exits 0 becomes completed via the background reaper."""
    ctx = _ctx()
    result = await spawn_process({"command": "exit 0", "task_name": "ok-task"}, ctx)
    pid = result.metadata["process_id"]
    # Give the reaper time to observe the exit.
    deadline = 0
    while deadline < 40:
        meta = _read_meta(pid)
        if meta["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.1)
        deadline += 1
    meta = _read_meta(pid)
    assert meta["status"] == "completed"
    assert meta["exit_code"] == 0


@pytest.mark.asyncio
async def test_natural_exit_records_failed():
    ctx = _ctx()
    result = await spawn_process({"command": "exit 3", "task_name": "flakey"}, ctx)
    pid = result.metadata["process_id"]
    deadline = 0
    while deadline < 40:
        meta = _read_meta(pid)
        if meta["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.1)
        deadline += 1
    meta = _read_meta(pid)
    assert meta["status"] == "failed"
    assert meta["exit_code"] == 3


@pytest.mark.asyncio
async def test_list_processes_carries_task_fields():
    ctx = _ctx()
    await spawn_process({"command": "exit 0", "task_name": "listed-task"}, ctx)
    data = json.loads((await list_processes({}, ctx)).content)
    row = next(r for r in data["processes"] if r["task_name"] == "listed-task")
    assert row["task_name"] == "listed-task"
    assert "description" in row
    assert "exit_code" in row


# ── T1: process identity / PID-reuse guard (Wave 1) ──


def test_spawn_records_born_at():
    """spawn_process records the child's start time for identity checks."""
    import modus.tools.process_tools as pt

    result = _run_spawn_sync("sleep 30")
    pid = result.metadata["process_id"]
    meta = _read_meta(pid)
    assert meta.get("born_at") is not None
    # The recorded born_at is close to the actual start time read back.
    actual = pt._read_born_at(meta["pid"])
    assert actual is not None
    assert abs(actual - meta["born_at"]) < 2.0


def test_pid_identity_ok_matching():
    """A live pid whose start time matches born_at passes identity."""
    import modus.tools.process_tools as pt

    # Use our own process as a stable, live subject.
    pid = os.getpid()
    born = pt._read_born_at(pid)
    assert born is not None
    meta = {"pid": pid, "born_at": born}
    assert pt._pid_identity_ok(meta) is True


def test_pid_identity_ok_reused_rejected(monkeypatch):
    """A pid alive but with a DIFFERENT start time is rejected (PID reuse)."""
    import modus.tools.process_tools as pt

    pid = os.getpid()
    born = pt._read_born_at(pid)
    assert born is not None
    # Simulate a reused pid: alive, but the registry's born_at is way older.
    meta = {"pid": pid, "born_at": born - 10_000.0}
    assert pt._pid_identity_ok(meta) is False


def test_process_status_pid_reused(monkeypatch):
    """A reused pid is reported as pid_reused, never 'running'."""
    import modus.tools.process_tools as pt

    monkeypatch.setattr(pt, "os", _os_with_kill0_ok())
    pid = os.getpid()
    born = pt._read_born_at(pid)
    meta = {
        "pid": pid, "born_at": born - 10_000.0, "status": "running",
        "spawned_by": os.getpid(),
    }
    assert pt._process_status(meta) == "pid_reused"


@pytest.mark.asyncio
async def test_kill_refuses_pid_reuse():
    """kill_process refuses to SIGKILL a pid whose identity changed."""
    import modus.tools.process_tools as pt

    ctx = _ctx()
    await spawn_process({"command": "sleep 30"}, ctx)
    # Grab the most recent registry entry.
    entries = pt._iter_registry()
    assert entries, "expected a spawned process"
    process_id, meta = entries[0]
    # Corrupt the recorded identity: same pid, wrong start time.
    meta["born_at"] = meta.get("born_at", time.time()) - 10_000.0
    _write_meta(process_id, meta)
    result = await kill_process({"process_id": process_id}, ctx)
    assert result.is_error
    assert "refusing" in result.content or "reused" in result.content
    assert _read_meta(process_id)["status"] == "pid_reused"


def _run_spawn_sync(command):
    """Synchronously run spawn_process via asyncio.run."""
    import modus.tools.process_tools as pt

    return asyncio.run(pt.spawn_process({"command": command}, _ctx()))


def _os_with_kill0_ok():
    """An os shim where os.kill(pid, 0) succeeds (pid alive)."""
    import modus.tools.process_tools as pt

    class _Shim:
        def __init__(self):
            self.kill = _kill0_ok

    def _kill0_ok(pid, sig):
        if sig == 0:
            return None
        raise ProcessLookupError

    shim = _Shim()
    # Keep the real attributes process_tools relies on.
    for attr in ("getpid", "name", "path"):
        if hasattr(os, attr):
            setattr(shim, attr, getattr(os, attr))
    shim.killpg = lambda *a, **k: None
    return shim
