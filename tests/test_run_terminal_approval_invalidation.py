import asyncio

import pytest

from modus.runtime.controller import RunController
from modus.runtime.state import RunState


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED])
async def test_terminal_run_rejects_new_approval_requests(terminal):
    controller = RunController(run_id="run-terminal", mode="default")
    controller.transition(RunState.RUNNING)
    if terminal is RunState.CANCELLED:
        controller.cancel()
        controller.cancel_complete()
    else:
        controller.transition(terminal)

    future = controller.register_approval("after-terminal")

    assert future.done()
    assert await future == "deny"
    assert controller.pending_approval_ids == ()


@pytest.mark.asyncio
async def test_cancel_denies_all_registered_approvals_and_clears_registry():
    controller = RunController(run_id="run-cancel", mode="default")
    controller.transition(RunState.RUNNING)
    first = controller.register_approval("first")
    second = controller.register_approval("second")

    assert controller.cancel() is RunState.CANCELLING

    assert await first == "deny"
    assert await second == "deny"
    assert controller.pending_approval_ids == ()
    assert controller.cancel_complete() is RunState.CANCELLED


@pytest.mark.asyncio
async def test_controller_resolves_only_its_own_pending_approval():
    first = RunController(run_id="run-a", mode="default")
    second = RunController(run_id="run-b", mode="default")
    first.transition(RunState.RUNNING)
    second.transition(RunState.RUNNING)
    future = first.register_approval("same-token")

    assert second.resolve_approval("same-token", "approve") is False
    assert not future.done()
    assert first.resolve_approval("same-token", "approve") is True
    assert await future == "approve"


@pytest.mark.asyncio
async def test_resolved_approval_cannot_be_replayed():
    controller = RunController(run_id="run-replay", mode="default")
    controller.transition(RunState.RUNNING)
    future = controller.register_approval("one-shot")

    assert controller.resolve_approval("one-shot", "deny") is True
    assert await future == "deny"
    assert controller.resolve_approval("one-shot", "approve") is False
