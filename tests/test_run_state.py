import pytest

from modus.runtime.state import InvalidRunTransition, RunState, RunStateMachine


def test_run_state_machine_allows_active_lifecycle_and_approval_resume():
    machine = RunStateMachine()

    assert machine.state is RunState.CREATED
    assert machine.transition(RunState.RUNNING) is RunState.RUNNING
    assert machine.transition(RunState.WAITING_APPROVAL) is RunState.WAITING_APPROVAL
    assert machine.transition(RunState.RUNNING) is RunState.RUNNING
    assert machine.transition(RunState.CANCELLING) is RunState.CANCELLING
    assert machine.transition(RunState.CANCELLED) is RunState.CANCELLED
    assert machine.is_terminal is True


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (RunState.CREATED, RunState.COMPLETED),
        (RunState.RUNNING, RunState.CREATED),
        (RunState.WAITING_APPROVAL, RunState.COMPLETED),
        (RunState.CANCELLING, RunState.RUNNING),
        (RunState.CANCELLED, RunState.RUNNING),
        (RunState.COMPLETED, RunState.FAILED),
        (RunState.FAILED, RunState.CANCELLING),
    ],
)
def test_run_state_machine_rejects_illegal_or_terminal_transitions(start, target):
    machine = RunStateMachine(state=start)

    with pytest.raises(InvalidRunTransition, match=f"{start.value}.*{target.value}"):
        machine.transition(target)


def test_waiting_approval_can_cancel_without_executing_the_pending_action():
    machine = RunStateMachine(state=RunState.WAITING_APPROVAL)

    assert machine.transition(RunState.CANCELLING) is RunState.CANCELLING
    assert machine.transition(RunState.CANCELLED) is RunState.CANCELLED
