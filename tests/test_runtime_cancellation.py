import asyncio

import pytest

from modus.runtime.cancellation import RunCancelled, await_or_cancel


@pytest.mark.asyncio
async def test_await_or_cancel_reaps_stalled_work():
    token = asyncio.Event()
    reaped = asyncio.Event()

    async def stalled():
        try:
            await asyncio.Event().wait()
        finally:
            reaped.set()

    waiting = asyncio.create_task(await_or_cancel(stalled(), token))
    await asyncio.sleep(0)
    token.set()

    with pytest.raises(RunCancelled):
        await waiting
    assert reaped.is_set()


@pytest.mark.asyncio
async def test_await_or_cancel_returns_completed_value():
    assert await await_or_cancel(asyncio.sleep(0, result="done"), asyncio.Event()) == "done"


@pytest.mark.asyncio
async def test_parent_task_cancellation_reaps_the_wrapped_work():
    """A sibling failure cancelling the wrapper must not detach its provider."""
    work_started = asyncio.Event()
    work_reaped = asyncio.Event()

    async def stalled_work():
        work_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            work_reaped.set()

    parent = asyncio.create_task(await_or_cancel(stalled_work(), asyncio.Event()))
    await work_started.wait()
    parent.cancel()

    with pytest.raises(asyncio.CancelledError):
        await parent
    assert work_reaped.is_set()
