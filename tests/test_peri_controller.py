import pytest

from modus.runtime.controller import RunController
from modus.runtime.state import RunState


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


async def _patch_successful_workflow(monkeypatch) -> None:
    async def fake_decompose(message, provider, model, count, **kwargs):
        return [{"name": "Research", "description": "research", "context": message}]

    async def fake_execute(task, model, user_message, timeout=120.0, **kwargs):
        return "worker output"

    async def fake_review(tasks, outputs, provider, model, user_message, **kwargs):
        return True, [], [8.0]

    async def fake_merge(tasks, outputs, provider, model, user_message, **kwargs):
        return "host final answer"

    monkeypatch.setattr("modus.desktop.peri.decompose_task", fake_decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", fake_execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", fake_review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", fake_merge)


@pytest.mark.asyncio
async def test_peri_uses_supplied_controller_and_leaves_terminal_ownership_to_caller(monkeypatch):
    from modus.desktop import server

    await _patch_successful_workflow(monkeypatch)
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "primary", "model_id": "primary", "name": "Host", "provider": "test", "model": "host"},
            "worker_1": {"id": "worker", "model_id": "worker", "name": "Worker", "provider": "test", "model": "worker"},
        },
    })
    session = server.DaoSession(id="s", db_id="db")
    controller = RunController(run_id="run-peri", mode="peri")

    await server._run_peri_stream(
        FakeWebSocket(), session, "research this", controller=controller,
    )

    assert controller.state is RunState.RUNNING
    assert session.active_controller is controller


@pytest.mark.asyncio
async def test_peri_owned_controller_finishes_completed_and_is_cleared(monkeypatch):
    from modus.desktop import server

    await _patch_successful_workflow(monkeypatch)
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "primary", "model_id": "primary", "name": "Host", "provider": "test", "model": "host"},
            "worker_1": {"id": "worker", "model_id": "worker", "name": "Worker", "provider": "test", "model": "worker"},
        },
    })
    session = server.DaoSession(id="s", db_id="db")
    observed: list[RunState] = []
    original_emit = server.RunEventEmitter.emit

    async def capture_terminal_emit(self, event_type, *args, **kwargs):
        if event_type.value == "run_completed":
            assert session.active_controller is not None
            observed.append(session.active_controller.state)
        return await original_emit(self, event_type, *args, **kwargs)

    monkeypatch.setattr(server.RunEventEmitter, "emit", capture_terminal_emit)
    await server._run_peri_stream(FakeWebSocket(), session, "research this")

    assert observed == [RunState.RUNNING]
    assert session.active_controller is None
