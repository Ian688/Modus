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
from pathlib import Path
from typing import Any

from modus.tools.base import ToolContext, ToolResult

# The registry lives under Modus's private data dir so it survives Desktop
# restarts and is never written into the user's workspace.
_PROCESSES_DIR = Path.home() / ".modus" / "processes"
_MAX_REGISTRY_ROWS = 50
_TAIL_BYTES = 64 * 1024


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

    completed/failed  — the reaper recorded a natural exit (exit code 0 / != 0)
    running           — registry says running AND the pid is alive AND its start
                        time matches the recorded born_at
    stopped           — registry says running but the pid is gone (no reaper hit)
    orphaned          — registry says running AND the pid is alive, but no Modus
                        process currently owns the registry (Desktop was killed and
                        restarted); the process is still running but unattended
    pid_reused        — registry says running AND the pid is alive, but its start
                        time differs from born_at: the OS handed our old pid to a
                        different process.  Killing it would hit a bystander.
    exited            — recorded a terminal exit code from kill
    """
    status = str(meta.get("status") or "running")
    if status in ("completed", "failed", "exited", "cancelled"):
        return status
    if status != "running":
        return status
    pid = meta.get("pid")
    alive = _pid_alive(pid)
    owned = _owned_by_this_process(meta)
    if alive:
        if not _pid_identity_ok(meta):
            return "pid_reused"
        return "running" if owned else "orphaned"
    return "stopped"


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
    proc_dir = _process_dir(process_id)
    proc_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = proc_dir / "stdout.log"
    stderr_path = proc_dir / "stderr.log"
    # Imported lazily to avoid a circular import: builtins imports process_tools
    # for the tool declarations, and process_tools needs builtins' helpers.
    from modus.tools.builtins import _process_group_kwargs, _safe_shell_env

    # The handler's own timeout must not bound a background process; spawn
    # synchronously (create_subprocess_exec is fast) and return immediately.
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(payload.get("cwd") or context.cwd or os.getcwd()),
        stdout=open(stdout_path, "a", encoding="utf-8"),
        stderr=open(stderr_path, "a", encoding="utf-8"),
        env=_safe_shell_env(),
        **_process_group_kwargs(),
    )
    meta = {
        "process_id": process_id,
        "pid": proc.pid,
        "command": command,
        "task_name": str(payload.get("task_name") or "").strip() or None,
        "description": str(payload.get("description") or "").strip() or None,
        "cwd": str(payload.get("cwd") or context.cwd or os.getcwd()),
        "status": "running",
        "started_at": time.time(),
        "born_at": _read_born_at(proc.pid),
        "spawned_by": os.getpid(),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }
    _write_meta(process_id, meta)
    # Background reaper: when the process exits naturally, record its exit code
    # and a terminal status so a query sees completed/failed instead of guessing
    # from a dead pid.  Best-effort; never raised.
    async def _reap():
        try:
            code = await proc.wait()
            meta["exit_code"] = code
            meta["status"] = "completed" if code == 0 else "failed"
            meta["ended_at"] = time.time()
            _write_meta(process_id, meta)
        except Exception:
            pass

    asyncio.create_task(_reap())
    return ToolResult(
        f"Spawned {process_id}: pid {proc.pid} · {command[:80]}",
        metadata={
            "operation": "spawn_process",
            "process_id": process_id,
            "pid": proc.pid,
            "command": command[:200],
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
    meta["status"] = "exited"
    meta["exit_code"] = -9
    _write_meta(process_id, meta)
    return ToolResult(
        f"Killed process {process_id} (pid {pid})",
        metadata={"operation": "kill_process", "process_id": process_id, "pid": pid},
    )
