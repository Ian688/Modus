"""G3 background-process completion → wake-up / bounded resume.

Covers:
- ``spawn_process`` persisting ``resume_on_complete`` / the owning ``run_id``.
- the reaper emitting a ``process_completed`` event on a terminal transition
  (and only once, via the durable marker).
- ``consume_process_resume`` enforcing the one-resume bound.
- the Desktop push / resume admission shape (server-side helpers).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from modus.config import ModusConfig
from modus.tools.base import ToolContext
from modus.tools.process_tools import (
    _MAX_PROCESS_RESUMES,
    _read_meta,
    _write_meta,
    consume_process_resume,
    set_process_event_sink,
    spawn_process,
)


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch):
    """Isolate every test to a temp registry directory and a fresh sink."""
    import modus.tools.process_tools as pt

    monkeypatch.setattr(pt, "_PROCESSES_DIR", tmp_path / "processes")
    monkeypatch.setattr(pt, "_MAX_PROCESS_RESUMES", 1)
    set_process_event_sink(None)
    yield
    set_process_event_sink(None)
    import shutil

    shutil.rmtree(tmp_path / "processes", ignore_errors=True)


def _ctx(*, run_id: str | None = None, session_id: str | None = None) -> ToolContext:
    import os

    return ToolContext(
        cwd=os.path.expanduser("~"),
        config=ModusConfig(),
        run_id=run_id,
        session_id=session_id,
    )


# ── spawn persists the resume contract ──


@pytest.mark.asyncio
async def test_spawn_records_resume_on_complete_default_false():
    ctx = _ctx(run_id="run-spawn", session_id="sess")
    result = await spawn_process({"command": "exit 0"}, ctx)
    meta = _read_meta(result.metadata["process_id"])
    assert meta["resume_on_complete"] is False
    assert meta["resume_count"] == 0
    assert meta["run_id"] == "run-spawn"
    assert meta["session_id"] == "sess"
    # A no-op default must not request a resume.
    assert result.metadata["resume_on_complete"] is False


@pytest.mark.asyncio
async def test_spawn_records_resume_on_complete_true():
    ctx = _ctx(run_id="run-spawn", session_id="sess")
    result = await spawn_process(
        {"command": "sleep 20", "resume_on_complete": True}, ctx,
    )
    meta = _read_meta(result.metadata["process_id"])
    assert meta["resume_on_complete"] is True
    assert meta["resume_count"] == 0
    assert result.metadata["resume_on_complete"] is True


@pytest.mark.asyncio
async def test_spawn_without_run_id_records_none():
    ctx = _ctx()
    result = await spawn_process({"command": "exit 0"}, ctx)
    meta = _read_meta(result.metadata["process_id"])
    assert meta["run_id"] is None
    assert meta["session_id"] is None


# ── reaper emits the process_completed event ──


@pytest.mark.asyncio
async def test_reaper_emits_process_completed_event():
    events = []

    def sink(event):
        events.append(event)

    set_process_event_sink(sink)
    ctx = _ctx(run_id="run-owning", session_id="sess")
    result = await spawn_process(
        {"command": "exit 0", "task_name": "build", "resume_on_complete": True}, ctx,
    )
    process_id = result.metadata["process_id"]
    deadline = 0
    while deadline < 40 and not events:
        await asyncio.sleep(0.1)
        deadline += 1
    assert events, "reaper never fired a process_completed event"
    event = events[0]
    assert event["type"] == "process_completed"
    assert event["process_id"] == process_id
    assert event["run_id"] == "run-owning"
    assert event["exit_code"] == 0
    assert event["status"] == "completed"
    assert event["resume_on_complete"] is True


@pytest.mark.asyncio
async def test_reaper_emits_failed_on_nonzero_exit():
    events = []
    set_process_event_sink(lambda e: events.append(e))
    result = await spawn_process({"command": "exit 3", "task_name": "flake"}, _ctx())
    deadline = 0
    while deadline < 40 and not events:
        await asyncio.sleep(0.1)
        deadline += 1
    assert events
    event = events[0]
    assert event["status"] == "failed"
    assert event["exit_code"] == 3
    assert event["resume_on_complete"] is False


@pytest.mark.asyncio
async def test_long_running_process_reaped_and_notified():
    """A process that outlives startsecs is still reaped to a terminal state."""
    events = []
    set_process_event_sink(lambda e: events.append(e))
    result = await spawn_process({"command": "sleep 0.3", "task_name": "job"}, _ctx())
    process_id = result.metadata["process_id"]
    deadline = 0
    while deadline < 60:
        meta = _read_meta(process_id)
        if meta is not None and meta.get("status") in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)
        deadline += 1
    meta = _read_meta(process_id)
    assert meta["status"] == "completed"
    assert meta["exit_code"] == 0
    assert events and events[0]["process_id"] == process_id
    assert events[0]["exit_code"] == 0


@pytest.mark.asyncio
async def test_event_fires_at_most_once_per_process():
    """Idempotent notification: repeated registry reads never re-emit."""
    events = []
    set_process_event_sink(lambda e: events.append(e))
    result = await spawn_process({"command": "exit 0"}, _ctx())
    process_id = result.metadata["process_id"]
    deadline = 0
    while deadline < 40 and not events:
        await asyncio.sleep(0.1)
        deadline += 1
    assert events
    await asyncio.sleep(0.2)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_completion_marker_is_durable():
    import modus.tools.process_tools as pt

    result = await spawn_process({"command": "exit 0"}, _ctx())
    process_id = result.metadata["process_id"]
    deadline = 0
    while deadline < 40:
        meta = _read_meta(process_id)
        if meta is not None and meta.get("status") in {"completed", "failed"}:
            break
        await asyncio.sleep(0.1)
        deadline += 1
    assert pt._read_marker(process_id, "completed")
    assert pt._read_marker(process_id, "notified")


# ── consume_process_resume enforces the bounded resume ──


def test_consume_resume_requires_resume_on_complete():
    _write_meta("p1", {
        "process_id": "p1", "pid": 1, "command": "echo hi", "status": "completed",
        "exit_code": 0, "resume_on_complete": False, "resume_count": 0,
    })
    assert consume_process_resume("p1") is None
    # untouched
    assert _read_meta("p1")["resume_count"] == 0


def test_consume_resume_requires_terminal_state():
    _write_meta("p2", {
        "process_id": "p2", "pid": 1, "command": "sleep 9", "status": "running",
        "resume_on_complete": True, "resume_count": 0,
    })
    assert consume_process_resume("p2") is None


def test_consume_resume_claims_exactly_once():
    _write_meta("p3", {
        "process_id": "p3", "pid": 1, "command": "make", "status": "completed",
        "exit_code": 0, "resume_on_complete": True, "resume_count": 0,
    })
    first = consume_process_resume("p3")
    assert first is not None
    assert first["resume_count"] == 1
    # The single bounded resume is now exhausted.
    assert consume_process_resume("p3") is None
    assert _read_meta("p3")["resume_count"] == 1


def test_consume_resume_bound_cap():
    _write_meta("p4", {
        "process_id": "p4", "pid": 1, "command": "make", "status": "failed",
        "exit_code": 1, "resume_on_complete": True, "resume_count": _MAX_PROCESS_RESUMES,
    })
    assert consume_process_resume("p4") is None


def test_consume_resume_missing_process():
    assert consume_process_resume("nope") is None


# ── server-side resume shape ──


@pytest.mark.asyncio
async def test_resume_context_message_carries_completion():
    from modus.desktop.server import _resume_context_message

    meta = {
        "command": "npm run build", "task_name": "编译", "status": "completed",
        "exit_code": 0,
    }
    text = _resume_context_message("abc123", meta, 0)
    assert "abc123" in text
    assert "编译" in text
    assert "npm run build" in text
    assert "成功" in text
    failed = _resume_context_message("abc123", meta, 2)
    assert "退出码 2" in failed


def test_resume_budget_halved():
    from modus.desktop.server import _resume_budget_halved
    from modus.runtime.budget import RunBudget, RunLimits
    from modus.runtime.controller import RunController

    controller = RunController(
        run_id="run-r", mode="default",
        budget=RunBudget(RunLimits(max_turns=20, max_tokens=200_000, max_wall_seconds=600)),
    )
    _resume_budget_halved(controller)
    assert controller.budget.limits.max_turns == 10
    assert controller.budget.limits.max_tokens == 100_000
    assert controller.budget.limits.max_wall_seconds == 300.0
    # Never zero — the continuation always keeps at least one turn / token.
    tiny = RunController(
        run_id="run-t", mode="default",
        budget=RunBudget(RunLimits(max_turns=1, max_tokens=2, max_wall_seconds=1.0)),
    )
    _resume_budget_halved(tiny)
    assert tiny.budget.limits.max_turns == 1
    assert tiny.budget.limits.max_tokens == 1


def _fake_session(db_id: str) -> object:
    return type("S", (), {
        "id": "rt-1", "db_id": db_id, "owner_id": "",
        "workspace_id": "", "workspace_root": "", "workspace_name": "",
        "mode": "default", "model_id": "", "mode_config": {},
        "reasoning_effort": None, "worldview": "",
    })()


@pytest.mark.asyncio
async def test_resume_process_handler_rejects_unknown_process():
    from modus.desktop.server import _handle_resume_process

    sent = []

    class FakeWS:
        async def send_json(self, payload):
            sent.append(payload)

    session = _fake_session("sess-1")
    await _handle_resume_process(FakeWS(), session, {"process_id": "nope"})
    assert sent and sent[0]["code"] == "process_not_found"


@pytest.mark.asyncio
async def test_resume_process_handler_rejects_nonterminal():
    from modus.desktop.server import _handle_resume_process

    sent = []
    _write_meta("rp1", {
        "process_id": "rp1", "pid": 1, "command": "sleep 9", "status": "running",
        "session_id": "sess-1", "resume_on_complete": False, "resume_count": 0,
    })

    class FakeWS:
        async def send_json(self, payload):
            sent.append(payload)

    session = _fake_session("sess-1")
    await _handle_resume_process(FakeWS(), session, {"process_id": "rp1"})
    assert sent and sent[0]["code"] == "process_not_terminal"


@pytest.mark.asyncio
async def test_resume_process_handler_requires_session_match():
    from modus.desktop.server import _handle_resume_process

    sent = []
    _write_meta("rp2", {
        "process_id": "rp2", "pid": 1, "command": "make", "status": "completed",
        "exit_code": 0, "session_id": "sess-A", "resume_on_complete": False,
        "resume_count": 0,
    })

    class FakeWS:
        async def send_json(self, payload):
            sent.append(payload)

    session = _fake_session("sess-B")
    await _handle_resume_process(FakeWS(), session, {"process_id": "rp2"})
    assert sent and sent[0]["code"] == "process_session_mismatch"


@pytest.mark.asyncio
async def test_process_completed_event_without_resume_does_not_start_run(tmp_path, monkeypatch):
    """A default (resume_on_complete=False) process never auto-resumes."""
    import modus.desktop.server as server

    started = []
    monkeypatch.setattr(server, "_start_resume_run", async_noop(started))
    # No persisted run row: without a spawn run the event routes nowhere.
    await server._handle_process_completed_event({
        "type": "process_completed", "process_id": "p-x",
        "run_id": "", "task_name": None, "exit_code": 0,
        "status": "completed", "resume_on_complete": False,
    })
    assert started == []


@pytest.mark.asyncio
async def test_process_completed_failure_never_auto_resumes(monkeypatch):
    """A failed background job is never auto-continued, even with the flag."""
    import modus.desktop.server as server

    started = []
    monkeypatch.setattr(server, "_start_resume_run", async_noop(started))
    await server._handle_process_completed_event({
        "type": "process_completed", "process_id": "p-fail",
        "run_id": "", "task_name": None, "exit_code": 1,
        "status": "failed", "resume_on_complete": True,
    })
    assert started == []


def async_noop(started):
    async def _noop(*args, **kwargs):
        started.append(True)

    return _noop
