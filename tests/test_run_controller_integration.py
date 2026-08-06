import pytest

from modus.runtime.controller import RunController
from modus.runtime.state import RunState


@pytest.mark.asyncio
async def test_session_cancellation_delegates_to_its_active_run_controller():
    from modus.desktop.server import DaoSession

    controller = RunController(run_id="run-1", mode="default")
    controller.transition(RunState.RUNNING)
    future = controller.register_approval("approval-1")
    session = DaoSession(id="session", db_id="db", active_controller=controller)

    session.cancel_stream()

    assert controller.cancel_event.is_set()
    assert await future == "deny"
    assert controller.state is RunState.CANCELLING


@pytest.mark.asyncio
async def test_session_cancel_keeps_legacy_event_in_sync_with_active_controller():
    from modus.desktop.server import DaoSession

    controller = RunController(run_id="run-1", mode="peri")
    controller.transition(RunState.RUNNING)
    session = DaoSession(id="session", db_id="db", active_controller=controller)
    legacy_cancel = session._ensure_cancel()

    session.cancel_stream()

    assert legacy_cancel.is_set()
    assert controller.cancel_event.is_set()
