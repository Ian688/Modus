"""Concurrency worker layer (Wave 0).

The user-facing requirement: Modus must be able to do many heavy jobs at once
(read several large Excel files + system optimization + agent coding + the
built-in Chrome preview) without starving each other or the main process, and
memory management must be good enough that the user never complains "Modus ate
all my memory again".

Architecture decision (see docs/dev-wave0-concurrency.md):

- Heavy work runs in its own worker *process*, not in the main process, so a
  runaway job can be killed without taking the whole agent down and its memory
  is returned to the OS on exit (process death is the only memory reclamation
  the user can rely on — not the host GC).
- Each worker carries a memory cap.  On POSIX that is enforced in the child via
  ``resource.setrlimit`` before exec; a process that exceeds it is SIGKILLed by
  the OS instead of silently ballooning.
- The worker boundary is a process + a bounded JSON protocol, never FFI, so any
  worker implementation can later be swapped for a faster one (e.g. Rust)
  without touching callers.
- The pool is concurrency-bounded: excess submissions queue, they never block
  the main loop.

This layer only *schedules* workers.  It does not decide what a worker runs;
tools (``office_exec``) submit jobs into the pool.  The memory enforcement is
per-worker so "many heavy jobs at once" does not become "one giant heap".
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from modus.config import WorkerConfig

# Sentinel: a worker has no process handle after it finished and was reaped.
_REAPED = object()

_MEMORY_WARN_EVENT = "worker_memory_warn"

# Process-wide WorkerPool singleton, lazily created from the active config.
# Tools (office_exec) grab it via ``_worker_pool_for``; the pool is started by
# whoever first needs it (or by the Desktop server at startup).
_pool: "WorkerPool | None" = None


def _worker_pool_for() -> "WorkerPool | None":
    """Return the process-wide WorkerPool (creating + starting it lazily).

    Returns None when the worker layer is disabled by config so callers can
    fall back to the direct path.  Never raises: a failure to start the pool
    degrades to no-pool (callers fall back).
    """
    global _pool
    try:
        if _pool is None:
            from modus.config import load_config

            config = load_config()
            if not config.runtime.worker.enabled:
                return None
            _pool = WorkerPool(config.runtime.worker, cwd=str(Path.cwd()))
            try:
                asyncio.get_running_loop().create_task(_pool.start())
            except RuntimeError:
                # No running loop — leave the pool unstarted; submit will
                # still queue and the caller can wait().
                pass
        return _pool
    except Exception:
        return None


@dataclass
class Worker:
    """One isolated heavy-job process under pool management."""

    worker_id: str
    kind: str
    proc: asyncio.subprocess.Process | None
    memory_limit: int  # bytes; 0 = no enforced cap
    status: str  # starting / running / done / failed / cancelled
    started_at: float
    ended_at: float | None = None
    exit_code: int | None = None
    output_paths: dict[str, str] = field(default_factory=dict)
    peak_rss: int = 0  # last sampled RSS (bytes), 0 if never sampled

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "kind": self.kind,
            "status": self.status,
            "started_at": round(self.started_at, 1),
            "ended_at": round(self.ended_at, 1) if self.ended_at else None,
            "exit_code": self.exit_code,
            "memory_limit": self.memory_limit,
            "peak_rss": self.peak_rss,
            "output": self.output_paths,
        }


class WorkerPool:
    """Concurrency-bounded scheduler for isolated worker processes.

    ``submit`` enqueues a job; if concurrency is free it starts immediately,
    otherwise it waits on the internal queue.  Workers are never created as a
    side effect of a tool call — they are explicit pool-managed processes with
    a memory cap, a status machine, and a reaper.
    """

    def __init__(
        self, config: WorkerConfig, *, cwd: str | None = None,
        rss_sampler: Callable[[int], int] | None = None,
    ) -> None:
        self.config = config
        self.cwd = cwd or os.getcwd()
        self._workers: dict[str, Worker] = {}
        self._queue: asyncio.Queue[tuple[str, str, list[str], int]] = asyncio.Queue()
        self._slots = 0
        self._lock = asyncio.Lock()
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._started = False
        # Injectable RSS sampler: tests substitute a fake one so the memory
        # watchdog can be exercised deterministically instead of waiting for a
        # real process to balloon.
        self._rss_sampler = rss_sampler or _sample_rss

    # ── lifecycle ──

    async def start(self) -> None:
        """Start the background scheduler + memory watchdog.  Idempotent."""
        if self._started:
            return
        self._started = True
        asyncio.create_task(self._scheduler())
        asyncio.create_task(self._memory_watchdog())

    async def shutdown(self) -> None:
        """Cancel queued jobs and terminate every live worker.  Best-effort."""
        self._started = False
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        for worker in list(self._workers.values()):
            await self._terminate(worker, status="cancelled")

    # ── public API ──

    async def submit(
        self,
        kind: str,
        argv: list[str],
        *,
        memory_limit: int | None = None,
        task_name: str = "",
    ) -> str:
        """Enqueue a job; returns a worker_id immediately (never blocks)."""
        worker_id = uuid.uuid4().hex[:10]
        limit = memory_limit if memory_limit is not None else (
            self.config.office_memory_limit if kind == "office" else 0
        )
        await self._queue.put((worker_id, kind, list(argv), limit))
        self._workers[worker_id] = Worker(
            worker_id=worker_id,
            kind=kind,
            proc=None,
            memory_limit=limit,
            status="starting",
            started_at=time.time(),
        )
        self._emit("worker_queued", worker_id=worker_id, kind=kind)
        return worker_id

    async def status(self, worker_id: str) -> dict[str, Any] | None:
        worker = self._workers.get(worker_id)
        return worker.to_dict() if worker is not None else None

    async def list_workers(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = [w.to_dict() for w in self._workers.values()]
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        return rows[:limit]

    async def cancel(self, worker_id: str) -> bool:
        worker = self._workers.get(worker_id)
        if worker is None or worker.proc is None:
            return False
        if worker.status in ("done", "failed", "cancelled"):
            return False
        await self._terminate(worker, status="cancelled")
        return True

    async def wait(self, worker_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Block until the worker reaches a terminal state and return its dict."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            worker = self._workers.get(worker_id)
            if worker is None:
                return {"worker_id": worker_id, "status": "unknown"}
            if worker.status in ("done", "failed", "cancelled"):
                return worker.to_dict()
            if deadline is not None and loop.time() >= deadline:
                return worker.to_dict()
            await asyncio.sleep(0.1)

    # ── internals ──

    async def _scheduler(self) -> None:
        while True:
            if not self._started and self._queue.empty():
                return
            worker_id, kind, argv, limit = await self._queue.get()
            async with self._lock:
                if not self._started:
                    return
                while self._slots >= max(1, self.config.max_concurrency):
                    await asyncio.sleep(0.05)
                self._slots += 1
            try:
                await self._start_worker(worker_id, kind, argv, limit)
            finally:
                async with self._lock:
                    self._slots = max(0, self._slots - 1)

    async def _start_worker(
        self, worker_id: str, kind: str, argv: list[str], limit: int,
    ) -> None:
        import tempfile
        from modus.tools.builtins import _safe_shell_env

        worker = self._workers[worker_id]
        out_dir = os.path.join(
            os.path.expanduser("~"), ".modus", "workers", worker_id,
        )
        os.makedirs(out_dir, exist_ok=True)
        stdout_path = os.path.join(out_dir, "stdout.log")
        stderr_path = os.path.join(out_dir, "stderr.log")
        env = _safe_shell_env()
        preexec = None
        if limit > 0 and os.name != "nt":
            import resource

            def _limit_mem() -> None:
                # Raise the hard cap first, then lower the soft cap to the
                # desired limit.  macOS ships with soft == hard for RLIMIT_AS,
                # so setrlimit(soft=limit) alone raises "current limit exceeds
                # maximum limit" — setting hard first is required.
                try:
                    hard = resource.getrlimit(resource.RLIMIT_AS)[1]
                    if hard == resource.RLIM_INFINITY:
                        resource.setrlimit(
                            resource.RLIMIT_AS, (limit, limit),
                        )
                    else:
                        resource.setrlimit(
                            resource.RLIMIT_AS, (limit, max(limit, hard)),
                        )
                except (ValueError, OSError):
                    pass

            preexec = _limit_mem

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.cwd,
                stdout=open(stdout_path, "a", encoding="utf-8"),
                stderr=open(stderr_path, "a", encoding="utf-8"),
                env=env,
                start_new_session=True,
                preexec_fn=preexec,
            )
        except Exception as exc:
            worker.status = "failed"
            worker.ended_at = time.time()
            worker.exit_code = -1
            self._emit(
                "worker_failed", worker_id=worker_id, kind=kind,
                error=f"spawn error: {exc}",
            )
            return

        worker.proc = proc
        worker.status = "running"
        worker.output_paths = {"stdout": stdout_path, "stderr": stderr_path}
        self._emit("worker_started", worker_id=worker_id, kind=kind, pid=proc.pid)

        try:
            code = await proc.wait()
        except Exception:
            code = None
        worker.exit_code = code
        worker.ended_at = time.time()
        worker.status = "completed" if code == 0 else "failed"
        self._emit(
            "worker_completed" if code == 0 else "worker_failed",
            worker_id=worker_id, kind=kind, exit_code=code,
        )

    async def _terminate(self, worker: Worker, *, status: str) -> None:
        proc = worker.proc
        if proc is None or proc.returncode is not None:
            worker.status = status
            return
        try:
            if os.name == "nt":
                import subprocess

                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                )
            elif hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
        worker.status = status
        worker.ended_at = time.time()
        self._emit(
            "worker_cancelled" if status == "cancelled" else "worker_failed",
            worker_id=worker.worker_id, kind=worker.kind,
        )

    async def _memory_watchdog(self) -> None:
        while self._started:
            await asyncio.sleep(max(1.0, self.config.tick_interval))
            for worker in list(self._workers.values()):
                proc = worker.proc
                if proc is None or proc.returncode is not None:
                    continue
                rss = self._rss_sampler(proc.pid)
                if rss:
                    worker.peak_rss = max(worker.peak_rss, rss)
                    if worker.memory_limit and rss >= worker.memory_limit:
                        # Hard cap: the worker exceeded its memory budget — kill
                        # it and return its memory to the OS.  This is the
                        # primary cross-platform enforcement (RLIMIT_AS is an
                        # extra Linux-only hard boundary; macOS reserves huge
                        # virtual address space so AS limits are unusable).
                        await self._terminate(worker, status="failed")
                        worker.exit_code = -9
                        self._emit(
                            "worker_oom", worker_id=worker.worker_id,
                            kind=worker.kind, rss=rss,
                            memory_limit=worker.memory_limit,
                        )
                    elif rss >= worker.memory_limit * self.config.memory_warn_ratio:
                        self._emit(
                            _MEMORY_WARN_EVENT, worker_id=worker.worker_id,
                            kind=worker.kind, rss=rss,
                            memory_limit=worker.memory_limit,
                        )

    def _emit(self, event: str, **fields: Any) -> None:
        # Best-effort: never raises, never blocks the caller.
        try:
            self._events.put_nowait({"type": event, **fields})
        except asyncio.QueueFull:
            pass

    async def drain_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not self._events.empty() and len(out) < limit:
            try:
                out.append(self._events.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out


def _sample_rss(pid: int) -> int:
    """Sample a process's resident set size in bytes; 0 when unsupported."""
    try:
        if os.name == "nt":
            import psutil  # optional; falls back to 0

            return int(psutil.Process(pid).memory_info().rss)
        # POSIX /proc (Linux) or `ps` fallback (macOS).
        if os.path.exists(f"/proc/{pid}/statm"):
            with open(f"/proc/{pid}/statm", encoding="utf-8") as handle:
                parts = handle.read().split()
            if len(parts) >= 2:
                page = os.sysconf("SC_PAGE_SIZE")
                return int(parts[1]) * page
        import subprocess

        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return int(out.stdout.strip()) * 1024
    except Exception:
        pass
    return 0
