"""Cancellation primitives shared by model and orchestration boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from modus.runtime.budget import BudgetExceeded, StopReason, active_run_budget

T = TypeVar("T")


class RunCancelled(asyncio.CancelledError):
    """The active Modus run was explicitly cancelled by its owner."""


async def await_or_cancel(
    awaitable: Awaitable[T], cancel_event: asyncio.Event | None,
) -> T:
    """Await work while making a run token interrupt a stalled operation.

    Cancellation is cooperative at this boundary: when the token wins, the
    child task is cancelled and reaped before ``RunCancelled`` is raised. This
    prevents detached provider/worker tasks from emitting late events.
    """
    if cancel_event is None:
        return await awaitable
    if cancel_event.is_set():
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise RunCancelled("run cancelled")

    work = asyncio.ensure_future(awaitable)
    cancelled = asyncio.create_task(cancel_event.wait())
    budget = active_run_budget()
    deadline = asyncio.create_task(asyncio.sleep(budget.remaining_wall_seconds)) if budget else None
    try:
        waiters = {work, cancelled}
        if deadline is not None:
            waiters.add(deadline)
        done, _ = await asyncio.wait(
            waiters, return_when=asyncio.FIRST_COMPLETED,
        )
        if cancelled in done and cancel_event.is_set():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise RunCancelled("run cancelled")
        if deadline is not None and deadline in done:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            budget.finish(StopReason.WALL_TIME)
            raise BudgetExceeded(StopReason.WALL_TIME)
        return await work
    finally:
        # The parent orchestration task can itself be cancelled by a sibling's
        # failure.  In that path neither the run token nor the budget deadline
        # wins, so ``work`` would otherwise detach and keep streaming events or
        # performing tool side effects after the Run reached terminal state.
        if not work.done():
            work.cancel()
        cancelled.cancel()
        if deadline is not None:
            deadline.cancel()
        await asyncio.gather(
            work, cancelled, *([deadline] if deadline is not None else []),
            return_exceptions=True,
        )
