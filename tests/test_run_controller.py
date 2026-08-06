import pytest

from modus.runtime.controller import RunController
from modus.runtime.state import InvalidRunTransition, RunState


def test_controller_starts_created_and_reaches_one_completed_terminal_state():
    controller = RunController(run_id="r1", mode="default")

    assert controller.state is RunState.CREATED
    assert controller.transition(RunState.RUNNING) is RunState.RUNNING
    assert controller.transition(RunState.COMPLETED) is RunState.COMPLETED
    assert controller.is_terminal is True

    with pytest.raises(InvalidRunTransition):
        controller.transition(RunState.FAILED)


@pytest.mark.asyncio
async def test_cancel_marks_token_and_drains_pending_approvals_fail_closed():
    controller = RunController(run_id="r1", mode="moa")
    controller.transition(RunState.RUNNING)
    future = controller.register_approval("approval-1")

    controller.cancel()

    assert controller.cancel_event.is_set()
    assert await future == "deny"
    assert controller.state is RunState.CANCELLING
    assert controller.pending_approval_ids == ()
    assert controller.transition(RunState.CANCELLED) is RunState.CANCELLED


@pytest.mark.parametrize("mode", ["default", "peri", "moa"])
def test_every_mode_uses_the_same_approval_lifecycle(mode):
    controller = RunController(run_id=f"r-{mode}", mode=mode)

    controller.transition(RunState.RUNNING)
    controller.transition(RunState.WAITING_APPROVAL)
    controller.transition(RunState.RUNNING)
    controller.transition(RunState.COMPLETED)

    assert controller.is_terminal is True


def test_cancellation_from_created_is_immediately_terminal():
    controller = RunController(run_id="r1", mode="default")

    controller.cancel()

    assert controller.state is RunState.CANCELLED
    assert controller.is_terminal is True
