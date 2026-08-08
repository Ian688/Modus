"""Graceful-shutdown cleanup for spawned background processes.

When Modus Desktop is closed normally (Ctrl-C, ``exit``, service stop, SIGTERM),
spawned background processes would otherwise keep running with no owner.  This
module installs a process-wide cleanup: on exit, every process group this Modus
process spawned (``spawned_by == os.getpid()``) and that is still alive is
terminated.

Design notes:

- ``atexit`` covers every normal interpreter exit (including Ctrl-C / EOF and
  ``typer``'s exit path).  Signal handlers cover SIGTERM/SIGINT so a ``kill`` /
  service manager that sends a signal still triggers cleanup before the process
  exits.  The signal handler re-raises after cleanup so the exit status is
  preserved.
- Cleanup is idempotent: already ``exited``/``stopped`` registry entries are
  skipped, and ``_pid_alive`` probes before signalling.
- Only processes owned by THIS Modus process are touched.  A process spawned by
  a previous Desktop run (whose registry ``spawned_by`` differs) is left
  untouched — it is the ``orphaned`` case that the user must decide about, and
  terminating it on an unrelated close would be destructive.
- A graceful SIGTERM with a short grace period precedes the SIGKILL so a child
  that handles SIGTERM can clean up its own state.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import time
from typing import Any

logger = logging.getLogger(__name__)

_GRACE_SECONDS = 2.0
_installed = False


def _iter_owned_meta() -> list[tuple[str, dict[str, Any]]]:
    """Registry entries spawned by the current Modus process."""
    from modus.tools.process_tools import _PROCESSES_DIR, _read_meta

    if not _PROCESSES_DIR.is_dir():
        return []
    rows: list[tuple[str, dict[str, Any]]] = []
    try:
        children = sorted(_PROCESSES_DIR.iterdir(), reverse=True)
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        meta = _read_meta(child.name)
        if meta is None:
            continue
        if meta.get("spawned_by") != os.getpid():
            continue
        rows.append((child.name, meta))
    return rows


def cleanup_spawned_processes(*, signal_triggered: bool = False) -> None:
    """Terminate every live process group this Modus process spawned.

    Idempotent and best-effort: a failure to reach one process never blocks the
    rest, and never raises (cleanup must not break interpreter exit).
    """
    from modus.tools.process_tools import _pid_alive, _pid_identity_ok, _write_meta

    owned = _iter_owned_meta()
    live: list[tuple[str, dict[str, Any]]] = []
    for process_id, meta in owned:
        status = str(meta.get("status") or "running")
        if status in ("exited", "stopped"):
            continue
        # Identity check: a pid alive but with a different start time means the
        # OS reused our old pid for a different process — do not kill a
        # bystander on an unrelated close.
        if _pid_alive(meta.get("pid")) and _pid_identity_ok(meta):
            live.append((process_id, meta))

    if not live:
        return

    if signal_triggered:
        logger.info("cleaning up %d spawned process group(s) on signal", len(live))
    else:
        logger.info("cleaning up %d spawned process group(s) on exit", len(live))

    for process_id, meta in live:
        try:
            _terminate_tree(meta.get("pid"))
            meta["status"] = "exited"
            meta["exit_code"] = -15
            _write_meta(process_id, meta)
        except Exception:
            logger.exception("cleanup failed for process %s", process_id)


def _terminate_tree(pid: int | None) -> None:
    """Send SIGTERM to the process group, then SIGKILL after a grace period.

    Mirrors the killpg semantics of ``_terminate_process_group`` but lets a
    SIGTERM-handling child exit cleanly first.
    """
    if not pid or pid <= 0:
        return
    try:
        if os.name == "nt":
            import subprocess

            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
            return
        if hasattr(os, "killpg"):
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            time.sleep(_GRACE_SECONDS)
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            time.sleep(_GRACE_SECONDS)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception:
        logger.exception("could not terminate pid %s", pid)


def _atexit_cleanup() -> None:
    cleanup_spawned_processes()


def _signal_cleanup(signum: int, frame: Any) -> None:
    cleanup_spawned_processes(signal_triggered=True)
    # Re-raise the original signal so the exit status is preserved.
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def install_process_cleanup() -> None:
    """Install the exit/signal cleanup hook.  Idempotent.

    Call once near the top of a Modus entrypoint (CLI ``main`` / Desktop server
    ``start_server``) so a normal close reaps this process's background jobs.
    """
    global _installed
    if _installed:
        return
    _installed = True
    atexit.register(_atexit_cleanup)
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _signal_cleanup)
        except (ValueError, OSError):
            # Not the main thread / unsupported signal — atexit still covers us.
            continue
