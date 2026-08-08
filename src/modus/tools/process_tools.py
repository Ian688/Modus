"""Background process tools: spawn / list / tail / kill (function-map #1).

The single biggest capability gap: bash blocks until EOF, so nothing can be
monitored, restarted, or run in the background.  These tools give the agent a
bounded process handle across tool calls:

- ``spawn_process`` launches a command detached from the tool's stdout/stderr,
  redirecting to ``~/.modus/processes/<id>/{stdout,stderr}.log`` and recording
  a ``meta.json`` registry entry.
- ``list_processes`` reads the registry and probes each pid with ``os.kill(pid,
  0)`` so a process is reported running/stopped/orphaned.
- ``tail_process`` reads a bounded tail of the recorded logs (no blocking).
- ``kill_process`` terminates the process group and marks the registry entry.

Power-loss posture: the registry is durable on disk.  If Modus Desktop is
killed (crash / power loss / SIGKILL), the spawned process keeps running as an
orphan and its registry entry survives — a later ``list_processes`` reports it
as ``orphaned`` (registry says running, pid alive, but no live owner).  This is
the persistence half of the power-loss guard; reaping/daemon ownership is
explicitly out of scope here (see the process-guard design note).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from modus.tools.base import Tool, ToolContext, ToolResult, object_schema

# The registry lives under Modus's private data dir so it survives Desktop
# restarts and is never written into the user's workspace.
_PROCESSES_DIR = Path.home() / ".modus" / "processes"
_MAX_REGISTRY_ROWS = 50
_TAIL_BYTES = 64 * 1024

# G3 resume-on-complete bound: a finished background process may trigger at most
# this many automatic continuations (the server additionally halves the budget
# for a resumed run so a runaway loop cannot compound indefinitely).
_MAX_PROCESS_RESUMES = 1

# G3 process-completed event sink.  The Desktop server installs a callback at
# startup that turns a reaped ``process_completed`` transition into a live WS
# push + run_events ledger row.  Default None keeps CLI / embedder / tests
# side-effect free (a completion marker is still written to the registry dir).
_PROCESS_EVENT_SINK: "Callable[[dict[str, Any]], None] | None" = None

# ── supervisor-style lifecycle (T5) ───────────────────────────────────────
# Ported semantics (not code): STARTING → RUNNING once the child survives
# ``startsecs``; a quick non-zero exit (before startsecs) is a startup failure
# → BACKOFF, retried with exponential backoff up to ``startretries``, then given
# up on (``failed``).  A quick exit-0 is a completed job, never a crash.
# ``exited``/``stopped`` are manual terminal states the supervisor never
# overwrites.  ``completed``/``failed`` are kept (in addition to the design's
# ``fatal``) for backward compatibility with pre-state-machine registry entries.
_DEFAULT_STARTSECS = 1.0
_DEFAULT_STARTRETRIES = 3
_BACKOFF_BASE = 0.25        # seconds; doubles per retry (exponential backoff)
_BACKOFF_MAX = 30.0
# States a manual action (kill / cleanup) may set.  The supervisor must treat
# these as an external close and stop supervising.
_MANUAL_TERMINAL = frozenset({"exited", "stopped", "pid_reused", "cancelled"})


def _parse_float(value: Any, default: float, name: str) -> float:
    """Coerce a payload number to a finite non-negative float, else the default."""
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number < 0:  # NaN / negative
        return default
    return number


def _parse_int(value: Any, default: int, name: str) -> int:
    """Coerce a payload number to a non-negative int, else the default."""
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _process_dir(process_id: str) -> Path:
    return _PROCESSES_DIR / process_id


def _registry_path(process_id: str) -> Path:
    return _process_dir(process_id) / "meta.json"


def _read_meta(process_id: str) -> dict[str, Any] | None:
    path = _registry_path(process_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_meta(process_id: str, meta: dict[str, Any]) -> None:
    d = _process_dir(process_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def _pid_alive(pid: int | None) -> bool:
    """Probe whether a pid exists, without signaling it."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user


def _read_born_at(pid: int) -> float | None:
    """Read a process's start time (epoch seconds), cross-platform.

    Used to detect PID reuse: a pid whose start time differs from what the
    registry recorded is a *different* process that the OS handed the freed
    pid to — killing it would hit an innocent bystander.
    """
    try:
        # Linux: /proc/<pid>/stat field 22 is starttime in clock ticks since
        # boot; convert to epoch via /proc/stat btime.
        if os.path.exists(f"/proc/{pid}/stat") and os.path.exists("/proc/stat"):
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                # Comm field can contain spaces/parens; split after the last ')'.
                data = handle.read()
            comm_end = data.rfind(")")
            fields = data[comm_end + 2:].split()
            # After comm, field index 19 in the full stat is starttime.
            if len(fields) > 19:
                ticks = int(fields[19])
                btime = 0
                try:
                    with open("/proc/stat", encoding="utf-8") as handle:
                        for line in handle:
                            if line.startswith("btime "):
                                btime = int(line.split()[1])
                                break
                except OSError:
                    pass
                hertz = os.sysconf("SC_CLK_TCK")
                return btime + ticks / hertz
        # macOS/BSD: `ps -o lstart= -p <pid>` → parse ISO-ish timestamp.
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            raw = out.stdout.strip()
            # "Sat Aug  8 09:01:59 2026" → struct_time.  %d parses both
            # zero-padded and space-padded days (macOS prints "Aug  8").
            parsed = time.strptime(raw, "%a %b %d %H:%M:%S %Y")
            return time.mktime(parsed)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _pid_identity_ok(meta: dict[str, Any]) -> bool:
    """True when the recorded pid is alive AND its start time matches the
    registry's ``born_at`` (i.e. it is genuinely the same process).

    PID reuse: after Modus crashes, the OS may hand a freed pid to a new
    process.  ``_pid_alive`` alone would report it as our old process; the
    start-time check catches that the identity changed.
    """
    pid = meta.get("pid")
    if not _pid_alive(pid):
        return False
    recorded = meta.get("born_at")
    if not recorded:
        # No start-time recorded (legacy entry) — fall back to pid-alive only.
        return True
    actual = _read_born_at(pid)
    if actual is None:
        return True  # cannot verify — do not fail closed on a read problem
    # Allow a small skew: start time is captured after spawn, and ps rounding
    # may differ by a fraction of a second.
    return abs(actual - float(recorded)) < 1.0


def _process_status(meta: dict[str, Any]) -> str:
    """Derive a live status from the registry entry + a pid probe.

    Terminal states — recorded by the supervisor or a manual action — are
    returned as stored: ``completed``/``failed``/``fatal`` (supervisor) and
    ``exited``/``stopped``/``cancelled``/``pid_reused`` (kill / cleanup / reuse).

    A live pid always warrants the T1 identity and ownership probes before the
    stored supervisor state is reported, because the registry survives Modus
    restarts: a ``starting``/``backoff``/``running`` entry whose owner pid is
    gone is really an ``orphaned`` process, and a reused pid is a bystander.

    running           — registry says running AND the pid is alive AND its start
                        time matches the recorded born_at AND owned by us
    starting/backoff  — supervisor transition state, live pid owned by us
    stopped           — registry says running but the pid is gone
    orphaned          — registry says running/starting/backoff AND the pid is
                        alive, but no Modus process currently owns the registry
    pid_reused        — registry says running/starting/backoff AND the pid is
                        alive, but its start time differs from born_at
    """
    status = str(meta.get("status") or "running")
    if status in (
        "completed", "failed", "fatal",
        "exited", "stopped", "cancelled", "pid_reused",
    ):
        return status
    pid = meta.get("pid")
    alive = _pid_alive(pid)
    if alive:
        if not _pid_identity_ok(meta):
            return "pid_reused"
        if not _owned_by_this_process(meta):
            return "orphaned"
        return status
    # Dead pid.  ``running`` collapses to ``stopped`` (legacy probe); a
    # transition state (starting/backoff) is left as stored because the
    # supervisor still owns the cycle and will record the next transition.
    return "stopped" if status == "running" else status


def _owned_by_this_process(meta: dict[str, Any]) -> bool:
    """True when the recorded owner pid is the current Modus process.

    ``spawned_by`` records the Modus pid that spawned the process.  A registry
    entry whose owner pid differs from the current pid means a previous Desktop
    process spawned it — if it is still alive, it is an orphan.
    """
    owner = meta.get("spawned_by")
    return owner is None or owner == os.getpid()


def _iter_registry() -> list[tuple[str, dict[str, Any]]]:
    """All registry entries, most recent first, bounded."""
    if not _PROCESSES_DIR.is_dir():
        return []
    entries: list[tuple[str, dict[str, Any]]] = []
    for child in sorted(_PROCESSES_DIR.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        meta = _read_meta(child.name)
        if meta is not None:
            entries.append((child.name, meta))
    return entries[:_MAX_REGISTRY_ROWS]


# ── G3 process-completed event / resume-on-complete ───────────────────────


def set_process_event_sink(sink: Callable[[dict[str, Any]], None] | None) -> None:
    """Install the process-completed event sink (called once by Desktop).

    ``sink`` receives one event dict per terminal transition:
    ``{"type": "process_completed", "process_id", "run_id", "exit_code",
    "resume_on_complete"}``.  The server uses it to push a timeline row and to
    offer / trigger a bounded resume run.  Installing replaces any prior sink;
    ``None`` disables live delivery while the durable completion marker still
    lands on disk.
    """
    global _PROCESS_EVENT_SINK
    _PROCESS_EVENT_SINK = sink


def _read_marker(process_id: str, kind: str) -> bool:
    """Whether a completion marker exists in the process's registry directory.

    Markers are the durable half of G3: a crash after the process exits but
    before the live sink fires is recovered on the next Desktop start (the
    server drains markers at startup), and they make event delivery idempotent
    (one marker → one event, never a re-emission per list call).
    """
    path = _process_dir(process_id) / f".{kind}"
    try:
        return path.exists()
    except OSError:
        return False


def _write_marker(process_id: str, kind: str, payload: dict[str, Any] | None = None) -> None:
    """Persist a completion marker next to the registry entry."""
    try:
        d = _process_dir(process_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f".{kind}"
        if payload is not None:
            path.write_text(
                json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8",
            )
        else:
            path.touch(exist_ok=True)
    except OSError:
        # A marker must never break the process lifecycle it records.
        pass


def _notify_process_completed(
    process_id: str, meta: dict[str, Any], *, exit_code: int,
) -> None:
    """Emit the ``process_completed`` event after a terminal transition.

    Called exactly once per transition to a terminal state: a reaper that
    observes ``completed``/``failed`` (or a manual ``exited`` after the group
    is terminated) writes a durable ``.completed`` marker and, when a live sink
    is installed, forwards a process-completed event.  The marker makes the
    notification idempotent — repeated reads of the registry never re-emit.
    """
    resume_on_complete = bool(meta.get("resume_on_complete") or False)
    event = {
        "type": "process_completed",
        "process_id": process_id,
        "run_id": str(meta.get("run_id") or ""),
        "task_name": meta.get("task_name"),
        "exit_code": exit_code,
        "status": str(meta.get("status") or "completed"),
        "resume_on_complete": resume_on_complete,
    }
    _write_marker(
        process_id, "completed",
        {"status": event["status"], "exit_code": exit_code},
    )
    if _read_marker(process_id, "notified"):
        return
    _write_marker(process_id, "notified", {"at": time.time()})
    try:
        sink = _PROCESS_EVENT_SINK
        if sink is not None:
            sink(event)
    except Exception:
        # A failing notification must never break the process lifecycle.
        pass


def consume_process_resume(process_id: str) -> dict[str, Any] | None:
    """Atomically claim one bounded resume for a completed background process.

    G3 boundedness: a process may drive at most ``_MAX_PROCESS_RESUMES``
    continuations.  This returns the registry metadata when the entry is
    terminal (``completed``/``failed``), was spawned with
    ``resume_on_complete=True``, and has not exhausted its single resume; it
    bumps ``resume_count`` (a durable guard that survives a Desktop restart)
    and returns the meta.  Returns None when the resume is not authorized,
    not terminal, or exhausted — the caller then leaves the registry untouched
    and never starts a continuation.  The bound is enforced here, inside the
    process module, so no server-side counter can drift from the durable one.
    """
    meta = _read_meta(process_id)
    if meta is None:
        return None
    if not meta.get("resume_on_complete"):
        return None
    if str(meta.get("status") or "") not in {"completed", "failed"}:
        return None
    count = int(meta.get("resume_count") or 0)
    if count >= _MAX_PROCESS_RESUMES:
        return None
    meta["resume_count"] = count + 1
    try:
        _write_meta(process_id, meta)
    except OSError:
        # Fail closed: without a durable bound no continuation is started, so a
        # repeated claim cannot silently double-resume the same process.
        return None
    return meta


# ── tool handlers ──


async def spawn_process(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Launch a command detached from this tool call, logging to disk."""
    command = str(payload.get("command") or "").strip()
    if not command:
        return ToolResult("spawn_process requires a command", is_error=True)
    from modus.policy.command_guard import CommandGuard, CommandPolicyError

    try:
        CommandGuard(context.config.policy.command_blacklist).validate(command)
    except CommandPolicyError as exc:
        return ToolResult(str(exc), is_error=True)

    process_id = uuid.uuid4().hex[:8]
    startsecs = _parse_float(payload.get("startsecs"), _DEFAULT_STARTSECS, "startsecs")
    startretries = _parse_int(
        payload.get("startretries"), _DEFAULT_STARTRETRIES, "startretries",
    )
    # G3 resume-on-complete (default False, bounded).  True makes a finished
    # process trigger a bounded automatic continuation; the Desktop server
    # halves the budget of the resumed run and the registry caps it at one.
    resume_on_complete = bool(payload.get("resume_on_complete") or False)
    # The owning run (ToolContext.run_id), recorded so a completion event can
    # target the exact conversation that spawned the job.
    run_id = str(getattr(context, "run_id", None) or "") or None
    session_id = str(getattr(context, "session_id", None) or "") or None
    proc_dir = _process_dir(process_id)
    proc_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = proc_dir / "stdout.log"
    stderr_path = proc_dir / "stderr.log"
    # Imported lazily to avoid a circular import: builtins imports process_tools
    # for the tool declarations, and process_tools needs builtins' helpers.
    from modus.tools.builtins import _process_group_kwargs, _safe_shell_env

    # The handler's own timeout must not bound a background process; spawn
    # synchronously (create_subprocess_exec is fast) and return immediately.
    # The supervisor drives STARTING → RUNNING / BACKOFF asynchronously.
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(payload.get("cwd") or context.cwd or os.getcwd()),
        stdout=open(stdout_path, "a", encoding="utf-8"),
        stderr=open(stderr_path, "a", encoding="utf-8"),
        env=_safe_shell_env(),
        **_process_group_kwargs(),
    )
    now = time.time()
    meta = {
        "process_id": process_id,
        "pid": proc.pid,
        "command": command,
        "task_name": str(payload.get("task_name") or "").strip() or None,
        "description": str(payload.get("description") or "").strip() or None,
        "cwd": str(payload.get("cwd") or context.cwd or os.getcwd()),
        # G3 resume context: the owning run and the resume flag, both recorded
        # so a completion event can route back to the conversation that spawned
        # the job and trigger a bounded continuation when authorized.
        "run_id": run_id,
        "session_id": session_id,
        "resume_on_complete": resume_on_complete,
        "resume_count": 0,
        # T5 supervisor state machine.  ``starting`` until the child survives
        # ``startsecs``; ``running`` thereafter; ``backoff`` (retry pending) on
        # a startup failure; ``failed`` once startretries are exhausted.
        "status": "starting",
        "startsecs": startsecs,
        "startretries": startretries,
        "retries_left": startretries,
        "retry_count": 0,
        "backoff_until": None,
        "gen": 0,
        "started_at": now,
        "born_at": _read_born_at(proc.pid),
        "spawned_by": os.getpid(),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    _write_meta(process_id, meta)
    # Background supervisor (reaper + too_quickly + backoff/retry).  It is the
    # single writer of the lifecycle fields and never raises: any failure just
    # stops supervising and leaves the child as an ordinary orphaned process.
    # It re-reads the registry each step, so a manual kill / cleanup / explicit
    # restart (which bumps ``gen``) supersedes it cleanly.
    _supervise_process(process_id, proc)
    return ToolResult(
        f"Spawned {process_id}: pid {proc.pid} · {command[:80]}",
        metadata={
            "operation": "spawn_process",
            "process_id": process_id,
            "pid": proc.pid,
            "command": command[:200],
            "status": "starting",
            "startsecs": startsecs,
            "startretries": startretries,
            "resume_on_complete": resume_on_complete,
        },
    )


async def list_processes(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """List the bounded process registry with live status."""
    limit = max(1, min(int(payload.get("limit") or 20), _MAX_REGISTRY_ROWS))
    rows = []
    for process_id, meta in _iter_registry()[:limit]:
        status = _process_status(meta)
        rows.append({
            "process_id": process_id,
            "pid": meta.get("pid"),
            "command": str(meta.get("command") or "")[:120],
            "task_name": meta.get("task_name"),
            "description": meta.get("description"),
            "status": status,
            "started_at": round(float(meta.get("started_at") or 0), 1),
            "exit_code": meta.get("exit_code"),
        })
    import json as _json

    return ToolResult(
        _json.dumps({"schema": "modus.processes.v1", "processes": rows},
                    ensure_ascii=False),
        metadata={"operation": "list_processes", "process_count": len(rows)},
    )


async def tail_process(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Read a bounded tail of a spawned process's stdout/stderr logs."""
    process_id = str(payload.get("process_id") or "").strip()
    if not process_id:
        return ToolResult("tail_process requires a process_id", is_error=True)
    meta = _read_meta(process_id)
    if meta is None:
        return ToolResult(f"process not found: {process_id}", is_error=True)
    stream = str(payload.get("stream") or "stdout")
    log_path = meta.get(f"{stream}_log") if stream in ("stdout", "stderr") else None
    if not log_path or not Path(log_path).exists():
        return ToolResult(f"{process_id} has no {stream} log yet", is_error=False)
    try:
        size = os.path.getsize(log_path)
        offset = max(0, size - _TAIL_BYTES)
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            tail = handle.read()
    except OSError as exc:
        return ToolResult(f"tail_process failed: {exc}", is_error=True)
    if not tail.strip():
        return ToolResult(f"(no {stream} output yet)", is_error=False)
    return ToolResult(tail[-_TAIL_BYTES:])


async def kill_process(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Terminate a spawned process group and mark the registry entry."""
    process_id = str(payload.get("process_id") or "").strip()
    if not process_id:
        return ToolResult("kill_process requires a process_id", is_error=True)
    meta = _read_meta(process_id)
    if meta is None:
        return ToolResult(f"process not found: {process_id}", is_error=True)
    pid = meta.get("pid")
    if not _pid_alive(pid):
        meta["status"] = "stopped"
        _write_meta(process_id, meta)
        return ToolResult(f"process {process_id} is already stopped", is_error=False)
    # PID-reuse guard: the recorded pid must still be the *same* process.  If
    # the OS handed our old pid to a different process (start time differs),
    # killing it would SIGKILL an innocent bystander — refuse instead.
    if not _pid_identity_ok(meta):
        meta["status"] = "pid_reused"
        _write_meta(process_id, meta)
        return ToolResult(
            f"process {process_id} pid {pid} was reused by another process — "
            f"refusing to kill a bystander (mark status=pid_reused)",
            is_error=True,
        )
    # Kill the whole process group (the spawn used start_new_session).
    _terminate_group(pid)
    meta["status"] = "exited"
    meta["exit_code"] = -9
    _write_meta(process_id, meta)
    return ToolResult(
        f"Killed process {process_id} (pid {pid})",
        metadata={"operation": "kill_process", "process_id": process_id, "pid": pid},
    )


def _terminate_group(pid: int | None) -> None:
    """SIGKILL a spawned process group (or taskkill on Windows).  Best-effort."""
    if not pid or pid <= 0:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
        elif hasattr(os, "killpg"):
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass


# ── supervisor state machine (T5) ──


async def _spawn_child(meta: dict[str, Any]) -> "asyncio.subprocess.Process":
    """Launch one child for a registry entry (fresh spawn or a retry)."""
    from modus.tools.builtins import _process_group_kwargs, _safe_shell_env

    return await asyncio.create_subprocess_shell(
        str(meta.get("command") or ""),
        cwd=str(meta.get("cwd") or os.getcwd()),
        stdout=open(str(meta.get("stdout_log")), "a", encoding="utf-8"),
        stderr=open(str(meta.get("stderr_log")), "a", encoding="utf-8"),
        env=_safe_shell_env(),
        **_process_group_kwargs(),
    )


def _supervise_process(process_id: str, proc: "asyncio.subprocess.Process") -> None:
    """Start the background supervisor for a process.  Fire-and-forget.

    Implements supervisor's too_quickly + backoff + startretries semantics: the
    child must survive ``startsecs`` to be ``running``; a faster non-zero exit
    is a startup failure → ``backoff`` (exponential, capped), retried up to
    ``startretries``, then given up on (``failed``).  A faster exit-0 is a
    completed one-shot job, not a crash.

    The loop is generation-guarded and re-reads the registry at every decision
    point, so a manual kill / cleanup / explicit restart (which writes a
    terminal status or bumps ``gen``) supersedes it: it never resurrects a
    process the user closed.  Best-effort — a supervisor failure just leaves the
    child as an ordinary orphan rather than raising into a tool call.
    """
    asyncio.create_task(_supervise_loop(process_id, proc))


async def _supervise_loop(
    process_id: str, proc: "asyncio.subprocess.Process",
) -> None:
    try:
        meta = _read_meta(process_id)
        if meta is None:
            return
        gen = int(meta.get("gen") or 0)
        startsecs = _parse_float(meta.get("startsecs"), _DEFAULT_STARTSECS, "startsecs")
        startretries = _parse_int(
            meta.get("startretries"), _DEFAULT_STARTRETRIES, "startretries",
        )
        retries_left = _parse_int(meta.get("retries_left"), startretries, "retries_left")
        while True:
            if not _still_supervised(process_id, gen, "starting"):
                return
            try:
                code = await asyncio.wait_for(proc.wait(), timeout=startsecs)
            except asyncio.TimeoutError:
                # Survived startsecs → the launch succeeded.
                if not _still_supervised(process_id, gen, "starting"):
                    return
                meta = _read_meta(process_id) or meta
                meta["status"] = "running"
                meta["exit_code"] = None
                meta["ended_at"] = None
                _write_meta(process_id, meta)
                # G3: keep reaping a long-running background job.  ``running``
                # is the only expected state now; the eventual exit is final
                # (no backoff for a process that already survived startsecs).
                # The loop stays generation- and terminal-state-guarded, so a
                # manual kill / cleanup / explicit restart still supersedes it.
                await _reap_running(process_id, meta, proc, gen)
                return
            # The child exited within startsecs (too_quickly).  exit 0 is a
            # completed job; anything else is a startup failure to retry.
            if not _still_supervised(process_id, gen, "starting"):
                return
            meta = _read_meta(process_id) or meta
            meta["exit_code"] = code
            meta["ended_at"] = time.time()
            if code == 0:
                meta["status"] = "completed"
                _write_meta(process_id, meta)
                _notify_process_completed(process_id, meta, exit_code=code)
                return
            if retries_left <= 0:
                meta["status"] = "failed"
                _write_meta(process_id, meta)
                _notify_process_completed(process_id, meta, exit_code=code)
                return
            retries_left -= 1
            meta["retries_left"] = retries_left
            meta["status"] = "backoff"
            delay = min(_BACKOFF_BASE * (2 ** (startretries - retries_left - 1)),
                        _BACKOFF_MAX)
            meta["backoff_until"] = time.time() + delay
            _write_meta(process_id, meta)
            await asyncio.sleep(delay)
            if not _still_supervised(process_id, gen, "backoff"):
                return
            meta = _read_meta(process_id) or meta
            meta["status"] = "starting"
            meta["retry_count"] = int(meta.get("retry_count") or 0) + 1
            _write_meta(process_id, meta)
            proc = await _spawn_child(meta)
            # A kill/restart that landed while the retry child was being created
            # must win: never register a process the user closed.
            if not _still_supervised(process_id, gen, "starting"):
                _terminate_group(proc.pid)
                return
            meta["pid"] = proc.pid
            meta["born_at"] = _read_born_at(proc.pid)
            _write_meta(process_id, meta)
    except Exception:
        pass


async def _reap_running(
    process_id: str,
    meta: dict[str, Any],
    proc: "asyncio.subprocess.Process",
    gen: int,
) -> None:
    """Reap a long-running background job to a terminal state (G3 reaper).

    The supervisor's startup window is done (the child survived ``startsecs``
    and the entry is ``running``).  This coroutine blocks on the child's exit
    and, when it arrives, records ``completed`` (exit 0) or ``failed``
    (non-zero) and fires the ``process_completed`` event — the wake-up signal
    that lets a finished build/test/download continue the owning run.

    The state machine is preserved: ``running`` is the only expected state
    here, and any manual terminal write (kill / cleanup / explicit restart
    bumping ``gen``) supersedes this reaper exactly as it does the startup
    supervisor — it never resurrects or overwrites a closed process.
    """
    try:
        code = await proc.wait()
    except asyncio.CancelledError:
        return
    if not _still_supervised(process_id, gen, "running"):
        return
    meta = _read_meta(process_id) or meta
    meta["exit_code"] = code
    meta["ended_at"] = time.time()
    meta["status"] = "completed" if code == 0 else "failed"
    _write_meta(process_id, meta)
    _notify_process_completed(process_id, meta, exit_code=code)


def _still_supervised(process_id: str, gen: int, expected: str) -> bool:
    """True when the registry entry still belongs to this supervisor.

    A manual kill/cleanup (terminal status) or an explicit restart (``gen``
    bumped) supersedes the running supervisor; it must step back without
    writing.
    """
    meta = _read_meta(process_id)
    if meta is None:
        return False
    if int(meta.get("gen") or 0) != gen:
        return False
    if str(meta.get("status")) in _MANUAL_TERMINAL:
        return False
    return str(meta.get("status")) == expected


async def restart_process(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Explicitly restart a spawned background process (supervisor semantics).

    Allowed for every non-``fatal``, non-``pid_reused`` state: a stopped job, a
    ``failed`` one, or a currently ``running`` one (stop + fresh start).  The
    current child is terminated, a fresh child launched, and the state machine
    restarted — an in-flight auto-retry (backoff) is superseded via ``gen``.
    Fatal processes are deliberately not restartable (the launch budget was
    exhausted); PID-reused entries are not restarted because the recorded pid is
    a bystander and killing it would hit the wrong process.
    """
    process_id = str(payload.get("process_id") or "").strip()
    if not process_id:
        return ToolResult("restart_process requires a process_id", is_error=True)
    meta = _read_meta(process_id)
    if meta is None:
        return ToolResult(f"process not found: {process_id}", is_error=True)
    status = str(meta.get("status") or "running")
    if status == "fatal":
        return ToolResult(
            f"process {process_id} is FATAL (startretries exhausted) and is not "
            f"restartable — spawn it fresh with spawn_process instead",
            is_error=True,
        )
    if status == "pid_reused":
        return ToolResult(
            f"process {process_id} pid {meta.get('pid')} was reused by another "
            f"process — refusing to kill a bystander; spawn fresh instead",
            is_error=True,
        )
    command = str(meta.get("command") or "")
    if not command:
        return ToolResult(f"process {process_id} has no command to restart", is_error=True)

    # Supersede any in-flight supervisor, then stop the current child.
    meta["gen"] = int(meta.get("gen") or 0) + 1
    _write_meta(process_id, meta)
    pid = meta.get("pid")
    if pid and _pid_alive(pid) and _pid_identity_ok(meta):
        _terminate_group(pid)

    # Launch a fresh child and hand it to a new supervisor.
    proc = await _spawn_child(meta)
    meta["pid"] = proc.pid
    meta["born_at"] = _read_born_at(proc.pid)
    meta["status"] = "starting"
    meta["exit_code"] = None
    meta["ended_at"] = None
    meta["retries_left"] = int(meta.get("startretries") or _DEFAULT_STARTRETRIES)
    meta["retry_count"] = int(meta.get("retry_count") or 0) + 1
    meta["backoff_until"] = None
    meta["started_at"] = time.time()
    _write_meta(process_id, meta)
    _supervise_process(process_id, proc)
    return ToolResult(
        f"Restarted {process_id}: new pid {proc.pid} · {command[:80]}",
        metadata={
            "operation": "restart_process",
            "process_id": process_id,
            "pid": proc.pid,
            "command": command[:200],
            "status": "starting",
        },
    )


# Tool declaration.  Registered with the agent via get_process_tools() so the
# coordinator can wire it into builtins without C3 touching this file.
RESTART_PROCESS_TOOL = Tool(
    name="restart_process",
    description=(
        "Restart a background process spawned with spawn_process.  Stops the "
        "current child (if any), launches a fresh one, and restarts the "
        "supervisor (startsecs / backoff / startretries).  Allowed for every "
        "non-fatal state; refused for FATAL or PID-reused entries."
    ),
    parameters=object_schema(
        {"process_id": {"type": "string"}},
        ["process_id"],
    ),
    required_keys=["process_id"],
    handler=restart_process,
    is_read_only=False,
    is_concurrency_safe=False,
    danger_level="medium",
    requires_approval=True,
    capabilities=("exec",),
)


def get_process_tools() -> list[Tool]:
    """All process tool declarations, for wiring into the tool registry.

    The coordinator merges these into ``builtins.get_builtin_tools()`` (which
    C3 owns); keeping the declarations here lets this file stay self-contained
    for T5 without editing builtins.py.
    """
    return [RESTART_PROCESS_TOOL]
