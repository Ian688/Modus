"""W0-1 concurrency worker layer (Wave 0).

Isolated worker processes with a memory cap, a concurrency-bounded queue, and
a reaper, so several heavy jobs (Excel analysis, system tuning, coding) can run
in parallel without starving the main process — and a runaway job's memory is
returned to the OS when it is killed.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from modus.config import WorkerConfig
from modus.runtime.workers import WorkerPool, _sample_rss


def _pool(**kw) -> WorkerPool:
    cfg = WorkerConfig(enabled=True, **kw)
    return WorkerPool(cfg, cwd=".")


@pytest.mark.asyncio
async def test_worker_spawn_and_reap():
    pool = _pool()
    await pool.start()
    try:
        wid = await pool.submit("test", [sys.executable, "-c", "print('hi')"])
        state = await pool.wait(wid, timeout=10)
        assert state["status"] == "completed"
        assert state["exit_code"] == 0
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_worker_failed_nonzero():
    pool = _pool()
    await pool.start()
    try:
        wid = await pool.submit("test", [sys.executable, "-c", "import sys; sys.exit(3)"])
        state = await pool.wait(wid, timeout=10)
        assert state["status"] == "failed"
        assert state["exit_code"] == 3
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_worker_oom_killed_by_watchdog():
    """The memory watchdog kills a worker whose sampled RSS exceeds the cap.

    Uses an injected RSS sampler so the enforcement is exercised
    deterministically without waiting for a real process to balloon.
    """
    limit = 64 * 1024 * 1024  # 64 MiB
    # Fake sampler reports the worker over its cap on the first tick.
    sampler = lambda pid: limit + 1  # noqa: E731
    pool = _pool(office_memory_limit=limit, tick_interval=1.0, memory_warn_ratio=0.5)
    pool._rss_sampler = sampler
    await pool.start()
    try:
        wid = await pool.submit(
            "office", [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        # Wait for the watchdog to kill it.
        state = await pool.wait(wid, timeout=15)
        assert state["status"] == "failed"
        # The watchdog sets exit -9 (killed), not a natural exit.
        assert state["exit_code"] == -9
        events = await pool.drain_events()
        assert "worker_oom" in [e["type"] for e in events]
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_worker_cancel_terminates_group():
    pool = _pool()
    await pool.start()
    try:
        wid = await pool.submit(
            "test", [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        # Wait for it to actually start.
        for _ in range(50):
            if (await pool.status(wid)).get("proc") or (await pool.status(wid))["status"] == "running":
                break
            await asyncio.sleep(0.05)
        ok = await pool.cancel(wid)
        assert ok is True
        state = await pool.wait(wid, timeout=10)
        assert state["status"] == "cancelled"
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_worker_pool_concurrency_queues():
    """Concurrency cap: the 3rd submission waits, it does not start at once."""
    pool = _pool(max_concurrency=2)
    await pool.start()
    try:
        script = "import time; time.sleep(0.5); print('done')"
        ids = []
        for _ in range(3):
            ids.append(await pool.submit("test", [sys.executable, "-c", script]))
        # Give the pool a moment to start the first two.
        await asyncio.sleep(0.3)
        statuses = [await pool.status(wid) for wid in ids]
        running = sum(1 for s in statuses if s["status"] == "running")
        assert running <= 2, f"concurrency cap violated: {running} running"
        # All three eventually complete.
        for wid in ids:
            state = await pool.wait(wid, timeout=15)
            assert state["status"] in ("completed", "failed")
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_worker_events_emitted():
    pool = _pool()
    await pool.start()
    try:
        wid = await pool.submit("test", [sys.executable, "-c", "print('hi')"])
        await pool.wait(wid, timeout=10)
        events = await pool.drain_events()
        kinds = [e["type"] for e in events]
        assert "worker_queued" in kinds
        assert "worker_started" in kinds
        assert "worker_completed" in kinds
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_queued_and_live():
    pool = _pool(max_concurrency=1)
    await pool.start()
    try:
        script = "import time; time.sleep(30)"
        wid1 = await pool.submit("test", [sys.executable, "-c", script])
        wid2 = await pool.submit("test", [sys.executable, "-c", script])  # queued
        await asyncio.sleep(0.2)
        await pool.shutdown()
        for wid in (wid1, wid2):
            state = await pool.status(wid)
            assert state["status"] in ("cancelled", "failed", "completed")
    finally:
        await pool.shutdown()


def test_sample_rss_returns_int():
    # Own process RSS must be a positive int on a supported platform.
    rss = _sample_rss(__import__("os").getpid())
    assert isinstance(rss, int)
