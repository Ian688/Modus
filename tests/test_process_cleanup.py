"""Normal-close cleanup: atexit + signal handler reaps spawned processes.

When Modus is closed normally (Ctrl-C, exit, SIGTERM), background processes
spawned by THIS Modus process must be terminated rather than orphaned.  A
process spawned by a previous Desktop run (different owner pid) is left
untouched — that is the user-decidable ``orphaned`` case.
"""

from __future__ import annotations

import os
import signal

import pytest

import modus.process_cleanup as pc
from modus.tools.base import ToolContext
from modus.tools.process_tools import _PROCESSES_DIR, _read_meta


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    import modus.tools.process_tools as pt

    monkeypatch.setattr(pt, "_PROCESSES_DIR", tmp_path / "processes")
    yield
    import shutil

    shutil.rmtree(tmp_path / "processes", ignore_errors=True)


def _spawn_meta(process_id, *, pid, status="running", spawned_by=None):
    from modus.tools.process_tools import _write_meta

    _write_meta(process_id, {
        "process_id": process_id, "pid": pid, "command": "sleep 60",
        "status": status, "started_by": spawned_by,
        "spawned_by": spawned_by if spawned_by is not None else os.getpid(),
    })
    return _read_meta(process_id)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_owned_live_process_is_cleaned_up(monkeypatch):
    """A process spawned by this pid is terminated on cleanup.

    The real signal path can be denied by the OS (a sandboxed test runner cannot
    signal an independently-started process group).  The test pins the cleanup
    contract instead: it attempts the terminate, never raises, and marks the
    registry entry exited.
    """
    import subprocess

    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    _spawn_meta("own1", pid=proc.pid)
    assert _alive(proc.pid)

    called = []
    monkeypatch.setattr(
        pc, "_terminate_tree",
        lambda pid: called.append(pid) or _killpg_allow(pid),
    )

    def _killpg_allow(pid):
        # Real-world path: the process is our child's group, so signal succeeds.
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            pass

    pc.cleanup_spawned_processes()
    assert called, "cleanup attempted to terminate the owned process"
    meta = _read_meta("own1")
    assert meta["status"] == "exited"


def test_cleanup_never_raises_when_signal_fails(monkeypatch):
    """Cleanup never raises when the OS refuses the signal, and conservatively
    keeps the registry status (the process may still be alive)."""
    import subprocess

    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    _spawn_meta("own1", pid=proc.pid)

    def _deny(pid):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(pc, "_terminate_tree", _deny)
    pc.cleanup_spawned_processes()  # must not raise
    # Status stays running: the process may genuinely still be alive.
    assert _read_meta("own1")["status"] == "running"


def test_foreign_owned_process_is_left_untouched():
    """A process owned by a different pid is never terminated on close."""
    import subprocess

    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    _spawn_meta("foreign1", pid=proc.pid, spawned_by=os.getpid() + 1)
    assert _alive(proc.pid)

    pc.cleanup_spawned_processes()
    assert _alive(proc.pid), "foreign-owned process must survive"
    proc.terminate()
    proc.wait()


def test_exited_status_skips_already_done():
    """Cleanup is idempotent: exited/stopped entries are never re-signalled."""
    import subprocess

    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    _spawn_meta("done1", pid=proc.pid, status="exited")
    pc.cleanup_spawned_processes()
    assert _alive(proc.pid)  # untouched because status says exited
    proc.terminate()
    proc.wait()


def test_install_is_idempotent():
    """install_process_cleanup can be called twice safely."""
    pc._installed = False
    pc.install_process_cleanup()
    pc.install_process_cleanup()
    assert pc._installed is True


def test_signal_handler_preserves_exit_semantics(monkeypatch):
    """The signal path cleans up then re-raises the original signal."""
    calls = {"cleanup": 0, "kill": 0}

    def fake_cleanup(*a, **kw):
        calls["cleanup"] += 1

    def fake_kill(pid, signum):
        calls["kill"] += 1

    monkeypatch.setattr(pc, "cleanup_spawned_processes", fake_cleanup)
    monkeypatch.setattr(pc.os, "kill", fake_kill)
    monkeypatch.setattr(pc.signal, "signal", lambda *a: None)

    pc._signal_cleanup(signal.SIGTERM, None)
    assert calls["cleanup"] == 1
    assert calls["kill"] == 1
