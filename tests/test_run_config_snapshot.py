import pytest

from modus.config import ModusConfig
from modus.runtime.controller import RunController


class PublicRepository:
    def public_snapshot(self):
        return {
            "models": [
                {
                    "id": "host", "name": "Host Model", "provider": "test",
                    "model": "host-v1", "has_credential": True,
                },
                {
                    "id": "worker-a", "name": "Worker A", "provider": "test",
                    "model": "worker-v1", "has_credential": True,
                },
            ],
            "selection": {"default_model_id": "host"},
        }


@pytest.mark.parametrize(
    ("mode", "roles", "expected_roles"),
    [
        ("default", {}, {"host"}),
        (
            "moa",
            {
                "host": {"model_id": "host", "temperature": 0.3},
                "reference_1": {"model_id": "worker-a", "reasoning_effort": "low"},
            },
            {"host", "reference_1"},
        ),
        (
            "peri",
            {
                "host": {"model_id": "host", "temperature": 0.4},
                "worker_1": {"model_id": "worker-a", "context_tokens": 32_000},
            },
            {"host", "worker_1"},
        ),
    ],
)
def test_run_start_freezes_effective_configuration(
    monkeypatch, mode, roles, expected_roles,
):
    from modus.desktop import server
    from modus.desktop.db import create_session, get_run
    from modus.desktop.events import RunEventEmitter

    monkeypatch.setattr(server, "model_repository", PublicRepository())
    persisted = create_session(
        mode=mode, model_id="host", mode_config=roles,
        reasoning_effort="high", system_prompt="private prompt body",
    )
    session = server.DaoSession(
        id="runtime", db_id=persisted["id"], mode=mode, model_id="host",
        mode_config=dict(roles), reasoning_effort="high",
        system_prompt="private prompt body",
    )
    controller = RunController.from_config(
        run_id=f"run-{mode}", mode=mode, config=ModusConfig(),
    )
    async def ignore_event(_message):
        return None

    emitter = RunEventEmitter(
        run_id=controller.run_id, mode=mode, send_json=ignore_event,
    )

    server._persist_run_start(
        session, emitter, controller, mode,
        verification_required=(mode == "default"),
    )
    session.model_id = "changed-later"
    session.reasoning_effort = "low"
    session.mode_config = {}
    server._persist_run_start(session, emitter, controller, mode)

    snapshot = get_run(controller.run_id)["config_snapshot"]
    assert snapshot["schema"] == "modus.run-config.v1"
    assert snapshot["mode"] == mode
    assert snapshot["host_model_id"] == "host"
    assert snapshot["reasoning_effort"] == "high"
    assert set(snapshot["roles"]) == expected_roles
    assert snapshot["roles"]["host"]["name"] == "Host Model"
    assert snapshot["verification"]["required"] is (mode == "default")
    assert snapshot["prompt"] == {"custom": True}
    assert "private prompt body" not in str(snapshot)
    assert "credential" not in str(snapshot).lower()


def test_default_snapshot_uses_effective_engine_reasoning(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import create_session

    monkeypatch.setattr(server, "model_repository", PublicRepository())
    persisted = create_session(model_id="host")
    config = ModusConfig()
    config.llm.reasoning_effort = "medium"
    session = server.DaoSession(
        id="runtime", db_id=persisted["id"], model_id="host",
        engine=type("Engine", (), {"config": config})(),
    )
    controller = RunController.from_config(
        run_id="run-engine-default", mode="default", config=config,
    )

    snapshot = server._run_config_snapshot(session, controller, "default")

    assert snapshot["reasoning_effort"] == "medium"
