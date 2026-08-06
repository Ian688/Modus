from fastapi.testclient import TestClient


def _patch_engine(monkeypatch, server) -> None:
    async def fake_registry(**_kwargs):
        return object()

    class FakeEngine:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", FakeEngine)


def _patch_observable_engine(monkeypatch, server, observed: list[dict]) -> None:
    async def fake_registry(**_kwargs):
        return object()

    class FakeEngine:
        def __init__(self, **kwargs):
            observed.append({
                "provider": kwargs["config"].llm.provider,
                "model": kwargs["config"].llm.model,
                "api_key": kwargs["config"].llm.api_key,
                "reasoning_effort": kwargs["config"].llm.reasoning_effort,
            })

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", FakeEngine)


def test_models_list_returns_only_public_model_dtos(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    repository.create(name="Private", provider="test", model="test-1", api_key="secret-browser-must-not-see")
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({"type": "models_list"})
            reply = socket.receive_json()

    assert reply["type"] == "models_list"
    assert "secret-browser-must-not-see" not in str(reply)
    assert "api_key" not in str(reply)
    assert reply["data"]["models"][0]["has_credential"] is True


def test_model_websocket_create_select_assign_and_delete(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({"type": "model_create", "name": "Default", "provider": "test", "model": "one", "api_key": "not-for-browser"})
            created = socket.receive_json()
            assert created["type"] == "model_repository_updated"
            model_id = created["model"]["id"]
            assert "api_key" not in str(created)
            assert created["data"]["selection"]["default_model_id"] == model_id

            socket.send_json({
                "type": "mode_models_set", "mode": "moa",
                "roles": {
                    "host": {"model_id": model_id},
                    "reference_1": {"model_id": model_id},
                },
            })
            assigned = socket.receive_json()
            assert assigned["data"]["selection"]["moa_roles"] == {
                "host": {
                    "model_id": model_id, "temperature": 0.4,
                    "context_tokens": 128_000, "reasoning_effort": None,
                },
                "reference_1": {
                    "model_id": model_id, "temperature": 0.7,
                    "context_tokens": 128_000, "reasoning_effort": None,
                },
            }

            socket.send_json({"type": "model_delete", "id": model_id})
            deleted = socket.receive_json()

    assert deleted["data"]["models"] == []
    assert deleted["data"]["selection"] == {
        "default_model_id": None,
        "moa_model_ids": [],
        "peri_model_ids": [],
        "moa_roles": {},
        "peri_roles": {},
    }


def test_websocket_rejects_legacy_model_configuration_messages(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    model = repository.create(
        name="Default", provider="test", model="one", api_key="secret",
    )
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            for packet in (
                {"type": "models_save", "models": []},
                {"type": "toggle_moa", "enabled": True},
                {"type": "moa_configure", "references": [model.id]},
                {"type": "mode_models_set", "mode": "moa", "model_ids": [model.id]},
            ):
                socket.send_json(packet)
                reply = socket.receive_json()
                assert reply["type"] == "error"

    assert repository.public_snapshot()["selection"]["moa_roles"] == {}


def test_first_model_create_rebinds_the_transient_session_engine(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    observed = []
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_observable_engine(monkeypatch, server, observed)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            ready = socket.receive_json()
            assert ready["model_id"] == ""
            socket.send_json({
                "type": "model_create", "name": "First", "provider": "test",
                "model": "first-v1", "api_key": "first-secret",
            })
            updated = socket.receive_json()

    assert updated["model_id"] == updated["model"]["id"]
    assert observed[-1] == {
        "provider": "test", "model": "first-v1", "api_key": "first-secret",
        "reasoning_effort": None,
    }


def test_session_model_selection_does_not_change_repository_default(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.db import create_session, get_session
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    first = repository.create(name="Default", provider="test", model="first", api_key="one")
    second = repository.create(name="Session", provider="test", model="second", api_key="two")
    persisted = create_session(model_id=first.id)
    observed = []
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_observable_engine(monkeypatch, server, observed)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": persisted["id"]})
            while socket.receive_json()["type"] != "session_restored":
                pass
            socket.send_json({"type": "session_set_model", "model_id": second.id})
            updated = socket.receive_json()

    assert updated["type"] == "session_model_updated"
    assert updated["model_id"] == second.id
    assert repository.public_snapshot()["selection"]["default_model_id"] == first.id
    assert get_session(persisted["id"])["model_id"] == second.id
    assert observed[-1]["model"] == "second"


def test_repository_default_selection_does_not_mutate_current_session(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.db import create_session, get_session
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    first = repository.create(name="First", provider="test", model="first", api_key="one")
    second = repository.create(name="Second", provider="test", model="second", api_key="two")
    persisted = create_session(model_id=first.id)
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": persisted["id"]})
            while socket.receive_json()["type"] != "session_restored":
                pass
            socket.send_json({"type": "model_select_default", "model_id": second.id})
            updated = socket.receive_json()

    assert updated["type"] == "model_repository_updated"
    assert updated["model_id"] == first.id
    assert updated["data"]["selection"]["default_model_id"] == second.id
    assert get_session(persisted["id"])["model_id"] == first.id


def test_model_create_is_rejected_while_another_window_runs(monkeypatch, tmp_path):
    from concurrent.futures import Future

    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)
    owner = server.DaoSession(id="repository-owner", db_id="owner-db")
    from modus.desktop import accounts

    owner.owner_id = str(accounts.ensure_default_user()["user_id"])
    server.manager._sessions[owner.id] = owner
    active = Future()
    owner.active_run_task = active

    try:
        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as socket:
                socket.receive_json()
                socket.send_json({
                    "type": "model_create", "name": "Blocked", "provider": "test",
                    "model": "blocked", "api_key": "must-not-save",
                })
                reply = socket.receive_json()
                assert reply["type"] == "error"
                assert reply["code"] == "repository_busy"
    finally:
        active.set_result(None)
        owner.active_run_task = None
        server.manager._sessions.pop(owner.id, None)

    assert repository.public_snapshot()["models"] == []


def test_mode_model_configuration_is_frozen_while_another_window_runs(monkeypatch, tmp_path):
    from concurrent.futures import Future

    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    host = repository.create(name="Host", provider="test", model="host", api_key="host-key")
    peer = repository.create(name="Peer", provider="test", model="peer", api_key="peer-key")
    repository.set_mode_configuration("moa", {
        "host": {"model_id": host.id},
        "reference_1": {"model_id": peer.id},
    })
    repository.set_mode_configuration("peri", {
        "host": {"model_id": host.id},
        "worker_1": {"model_id": peer.id},
    })
    before = repository.public_snapshot()
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    active = Future()
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as owner_socket:
            owner_ready = owner_socket.receive_json()
            owner = server.manager.get(owner_ready["runtime_session_id"])
            assert owner is not None
            owner.active_run_task = active
            try:
                owner_socket.send_json({
                    "type": "mode_models_set", "mode": "moa",
                    "roles": {
                        "host": {"model_id": peer.id},
                        "reference_1": {"model_id": host.id},
                    },
                })
                owner_reply = owner_socket.receive_json()
                assert owner_reply["type"] == "error"
                assert owner_reply["code"] == "session_busy"
                assert owner_reply["run_owned_by_connection"] is True
                assert repository.public_snapshot() == before

                with client.websocket_connect("/ws") as editor_socket:
                    editor_ready = editor_socket.receive_json()
                    attempts = (
                        ("moa", "reference_1"),
                        ("peri", "worker_1"),
                    )
                    for mode, peer_role in attempts:
                        editor_socket.send_json({
                            "type": "mode_models_set", "mode": mode,
                            "roles": {
                                "host": {"model_id": peer.id, "temperature": 1.1},
                                peer_role: {"model_id": host.id, "temperature": 1.2},
                            },
                        })
                        reply = editor_socket.receive_json()
                        assert reply["type"] == "error"
                        assert reply["code"] == "repository_busy"
                        assert reply["run_owned_by_connection"] is False
                        assert reply["runtime_session_id"] == editor_ready["runtime_session_id"]
                        assert repository.public_snapshot() == before
            finally:
                active.set_result(None)
                owner.active_run_task = None


def test_missing_sessions_fail_without_partial_batch_delete(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import create_session, get_session

    _patch_engine(monkeypatch, server)
    existing = create_session(title="Keep")

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({
                "type": "session_delete_batch",
                "session_ids": [existing["id"], "missing-session"],
            })
            reply = socket.receive_json()
            assert reply["type"] == "error"
            assert reply["code"] == "session_not_found"
            assert reply["session_ids"] == ["missing-session"]

    assert get_session(existing["id"]) is not None


def test_session_get_and_rename_return_structured_missing_errors(monkeypatch):
    from modus.desktop import server

    _patch_engine(monkeypatch, server)
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            for packet in (
                {"type": "session_get", "session_id": "missing"},
                {"type": "session_rename", "session_id": "missing", "title": "Name"},
            ):
                socket.send_json(packet)
                reply = socket.receive_json()
                assert reply["type"] == "error"
                assert reply["code"] == "session_not_found"


def test_active_model_update_rebuilds_engine_with_rotated_credential(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    model = repository.create(
        name="Active", provider="test", model="before", api_key="before-secret",
    )
    observed = []
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_observable_engine(monkeypatch, server, observed)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({
                "type": "model_update", "id": model.id,
                "model": "after", "api_key": "after-secret",
            })
            updated = socket.receive_json()

    assert updated["model_id"] == model.id
    assert observed[-1]["model"] == "after"
    assert observed[-1]["api_key"] == "after-secret"


def test_deleting_active_model_rehomes_session_and_persisted_record(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.db import create_session, get_session
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    first = repository.create(
        name="First", provider="test", model="first", api_key="first-secret",
    )
    second = repository.create(
        name="Second", provider="test", model="second", api_key="second-secret",
    )
    persisted = create_session(model_id=first.id)
    observed = []
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_observable_engine(monkeypatch, server, observed)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": persisted["id"]})
            while socket.receive_json()["type"] != "session_restored":
                pass
            socket.send_json({"type": "model_delete", "id": first.id})
            updated = socket.receive_json()

    assert updated["model_id"] == second.id
    assert updated["mode"] == "default"
    assert observed[-1]["model"] == "second"
    assert observed[-1]["api_key"] == "second-secret"
    assert get_session(persisted["id"])["model_id"] == second.id


def test_deleting_current_worker_repairs_session_snapshot_without_global_drift(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.db import create_session, get_session
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    host = repository.create(name="Host", provider="test", model="host", api_key="h")
    first = repository.create(name="First", provider="test", model="first", api_key="a")
    second = repository.create(name="Second", provider="test", model="second", api_key="b")
    session_roles = {
        "host": {"model_id": host.id, "temperature": 0.1},
        "worker_1": {"model_id": first.id, "temperature": 0.2},
        "worker_2": {"model_id": second.id, "temperature": 0.3},
    }
    repository.set_mode_configuration("peri", {
        "host": {"model_id": host.id, "temperature": 1.1},
        "worker_1": {"model_id": first.id, "temperature": 1.2},
        "worker_2": {"model_id": second.id, "temperature": 1.3},
    })
    persisted = create_session(mode="peri", model_id=host.id, mode_config=session_roles)
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": persisted["id"]})
            while socket.receive_json()["type"] != "session_restored":
                pass
            socket.send_json({"type": "model_delete", "id": first.id})
            updated = socket.receive_json()

    repaired = get_session(persisted["id"])["mode_config"]
    assert updated["mode"] == "peri"
    assert repaired["host"]["temperature"] == 0.1
    assert repaired["worker_1"]["model_id"] == second.id
    assert repaired["worker_1"]["temperature"] == 0.3
    assert "worker_2" not in repaired


def test_unconfigured_collaboration_mode_is_rejected_before_session_mutation(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    model = repository.create(name="Default", provider="test", model="one", api_key="secret")
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            ready = socket.receive_json()
            assert ready["mode"] == "default"
            socket.send_json({"type": "session_set_mode", "mode": "peri"})
            error = socket.receive_json()

    assert error["type"] == "error"
    assert error["code"] == "mode_not_configured"
    assert server.manager.get(ready["runtime_session_id"]) is None


def test_model_delete_repairs_inactive_persisted_sessions(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.db import create_session, get_session
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    host = repository.create(name="Host", provider="test", model="host", api_key="h")
    removed = repository.create(name="Removed", provider="test", model="removed", api_key="r")
    survivor = repository.create(name="Survivor", provider="test", model="survivor", api_key="s")
    affected = create_session(
        title="Affected", mode="peri", model_id=host.id,
        mode_config={
            "host": {"model_id": host.id},
            "worker_1": {"model_id": removed.id},
            "worker_2": {"model_id": survivor.id, "temperature": 0.9},
        },
    )
    unaffected = create_session(title="Unaffected", model_id=host.id)
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "model_delete", "id": removed.id})
            updated = socket.receive_json()

    repaired = get_session(affected["id"])
    assert updated["repaired_session_ids"] == [affected["id"]]
    assert repaired["mode"] == "peri"
    assert repaired["mode_config"]["worker_1"]["model_id"] == survivor.id
    assert repaired["mode_config"]["worker_1"]["temperature"] == 0.9
    assert get_session(unaffected["id"])["mode"] == "default"


def test_model_delete_repairs_sessions_beyond_catalog_page_cap(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.db import create_session, get_session
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    fallback = repository.create(name="Fallback", provider="test", model="fallback", api_key="f")
    removed = repository.create(name="Removed", provider="test", model="removed", api_key="r")
    affected = create_session(title="Old affected", model_id=removed.id)
    for index in range(125):
        create_session(title=f"Newer {index:03d}", model_id=fallback.id)
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "model_delete", "id": removed.id})
            updated = socket.receive_json()

    assert affected["id"] in updated["repaired_session_ids"]
    assert get_session(affected["id"])["model_id"] == fallback.id


def test_resume_repairs_a_historical_missing_model_reference(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.db import create_session, get_session
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    fallback = repository.create(
        name="Fallback", provider="test", model="fallback", api_key="secret",
    )
    stale = create_session(
        mode="peri", model_id="missing-host",
        mode_config={
            "host": {"model_id": "missing-host"},
            "worker_1": {"model_id": "missing-worker"},
        },
        reasoning_effort="high",
    )
    observed = []
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_observable_engine(monkeypatch, server, observed)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": stale["id"]})
            packets = []
            while True:
                packet = socket.receive_json()
                packets.append(packet)
                if packet["type"] == "session_restored":
                    restored = packet
                    break

    assert restored["mode"] == "default"
    assert restored["model_id"] == fallback.id
    assert restored["reasoning_effort"] is None
    repaired = get_session(stale["id"])
    assert repaired["mode"] == "default"
    assert repaired["mode_config"] == {}
    assert repaired["model_id"] == fallback.id
    assert observed[-1]["model"] == "fallback"


def test_server_has_no_legacy_model_mutation_handlers():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "src/modus/desktop/server.py").read_text()
    for legacy_type in ("models_save", "toggle_moa", "moa_configure"):
        assert f'msg_type == "{legacy_type}"' not in source
    mode_handler = source[
        source.index('elif msg_type == "mode_models_set":'):
        source.index('elif msg_type == "interrupt":')
    ]
    assert "set_mode_configuration" in mode_handler
    assert "set_mode_models" not in mode_handler


def test_model_test_connection_reuses_saved_credential_without_saving(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    model = repository.create(name="Test", provider="test", model="one", api_key="saved-secret", base_url="https://api.example.test/v1")
    observed = {}

    class FakeClient:
        async def chat(self, messages, tools, *, system_prompt):
            observed["messages"] = messages
            yield {"type": "text_delta", "text": "OK"}

    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)
    monkeypatch.setattr(server, "create_llm_client", lambda cfg: (observed.update({"key": cfg.api_key, "url": cfg.base_url}) or FakeClient()))

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({
                "type": "model_test_connection", "request_id": "reuse-ok",
                "model_id": model.id,
            })
            reply = socket.receive_json()

    assert reply["type"] == "model_test_result"
    assert reply["request_id"] == "reuse-ok"
    assert reply["success"] is True
    assert observed["key"] == "saved-secret"
    assert observed["url"] == "https://api.example.test/v1"
    assert repository.runtime_model(model.id)["api_key"] == "saved-secret"


def test_model_test_connection_rejects_changed_identity_without_new_key(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    model = repository.create(name="Test", provider="test", model="one", api_key="saved-secret")
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({
                "type": "model_test_connection", "request_id": "reject-identity",
                "model_id": model.id, "base_url": "https://evil.invalid/v1",
            })
            reply = socket.receive_json()

    assert reply["type"] == "model_test_result"
    assert reply["request_id"] == "reject-identity"
    assert reply["success"] is False
    assert "重新输入 API Key" in reply["error"]


def test_websocket_model_test_result_echoes_request_id(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    async def fake_test(_message):
        return {"success": True, "response": "OK"}

    monkeypatch.setattr(server, "_test_model_connection", fake_test)
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({
                "type": "model_test_connection", "request_id": "test-b",
                "provider": "test", "model": "two", "api_key": "secret",
            })
            result = socket.receive_json()

    assert result == {
        "type": "model_test_result", "request_id": "test-b",
        "success": True, "response": "OK",
    }


def test_websocket_persists_validated_role_configuration(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    host = repository.create(
        name="Host", provider="test", model="host", api_key="secret-host",
        context_window=100_000, reasoning_efforts=["low", "high"],
    )
    worker = repository.create(
        name="Worker", provider="test", model="worker", api_key="secret-worker",
        context_window=50_000,
    )
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({
                "type": "mode_models_set", "mode": "peri",
                "roles": {
                    "host": {
                        "model_id": host.id, "temperature": 0.2,
                        "context_tokens": 80_000, "reasoning_effort": "high",
                    },
                    "worker_1": {
                        "model_id": worker.id, "temperature": 0.9,
                        "context_tokens": 40_000,
                    },
                },
            })
            reply = socket.receive_json()

    assert reply["type"] == "model_repository_updated"
    assert reply["data"]["selection"]["peri_roles"]["host"]["context_tokens"] == 80_000
    assert reply["data"]["selection"]["peri_roles"]["worker_1"]["temperature"] == 0.9
    assert "secret-host" not in str(reply)
    assert "secret-worker" not in str(reply)


def test_saving_mode_defaults_does_not_reconfigure_historical_session(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.db import create_session, get_session
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    old_host = repository.create(name="Old Host", provider="test", model="old-host", api_key="a")
    old_worker = repository.create(name="Old Worker", provider="test", model="old-worker", api_key="b")
    new_host = repository.create(name="New Host", provider="test", model="new-host", api_key="c")
    new_worker = repository.create(name="New Worker", provider="test", model="new-worker", api_key="d")
    historical_roles = {
        "host": {"model_id": old_host.id, "temperature": 0.1},
        "worker_1": {"model_id": old_worker.id, "temperature": 0.2},
    }
    persisted = create_session(
        mode="peri", model_id=old_host.id, mode_config=historical_roles,
    )
    observed = []
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_observable_engine(monkeypatch, server, observed)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": persisted["id"]})
            while socket.receive_json()["type"] != "session_restored":
                pass
            engine_count = len(observed)
            socket.send_json({
                "type": "mode_models_set", "mode": "peri",
                "roles": {
                    "host": {"model_id": new_host.id, "temperature": 0.8},
                    "worker_1": {"model_id": new_worker.id, "temperature": 0.9},
                },
            })
            updated = socket.receive_json()

    historical = get_session(persisted["id"])
    assert updated["data"]["selection"]["peri_roles"]["host"]["model_id"] == new_host.id
    assert updated["model_id"] == old_host.id
    assert updated["mode_config"]["host"]["model_id"] == old_host.id
    assert historical["model_id"] == old_host.id
    assert historical["mode_config"] == historical_roles
    assert len(observed) == engine_count


def test_new_enhanced_session_snapshots_latest_mode_defaults(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    host = repository.create(name="Host", provider="test", model="host", api_key="a")
    worker = repository.create(name="Worker", provider="test", model="worker", api_key="b")
    roles = {
        "host": {"model_id": host.id, "temperature": 0.3},
        "reference_1": {"model_id": worker.id, "temperature": 0.6},
    }
    repository.set_mode_configuration("moa", roles)
    expected = repository.public_snapshot()["selection"]["moa_roles"]
    monkeypatch.setattr(server, "model_repository", repository)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({
                "type": "session_create", "request_key": "new-moa-defaults",
                "title": "MOA", "mode": "moa",
            })
            created = socket.receive_json()

    assert created["type"] == "session_created"
    assert created["model_id"] == host.id
    assert created["mode_config"] == expected


def test_discovery_websocket_reuses_server_credential_without_exposing_it(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    source = repository.create(
        name="Source", provider="openai", model="existing",
        api_key="source-secret-never-for-browser",
    )
    observed = {}

    async def fake_discover(runtime_model):
        observed["key"] = runtime_model["api_key"]
        return {
            "source_model_id": runtime_model["id"], "provider": "openai",
            "catalog_version": "test", "warning": "capabilities need confirmation",
            "models": [{
                "id": "new-model", "name": "New Model", "provider": "openai",
                "availability_source": "provider_api",
                "capabilities": {
                    "context_window": None, "max_output_tokens": None,
                    "supports_tools": None, "supports_images": None,
                    "reasoning_efforts": [],
                },
                "capability_sources": {
                    "context_window": "unknown", "max_output_tokens": "unknown",
                    "supports_tools": "unknown", "supports_images": "unknown",
                    "reasoning_efforts": "unknown",
                },
            }],
        }

    monkeypatch.setattr(server, "model_repository", repository)
    monkeypatch.setattr(server, "discover_models", fake_discover)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({
                "type": "model_discover", "model_id": source.id,
                "request_id": "discover-source",
            })
            discovered = socket.receive_json()
            socket.send_json({
                "type": "model_create_discovered",
                "source_model_id": source.id, "discovered_model_id": "new-model",
                "name": "Confirmed New Model", "context_window": 96_000,
                "max_output_tokens": 12_000, "supports_tools": True,
                "supports_images": False, "reasoning_efforts": ["low", "high"],
                "default_reasoning_effort": "low",
                # Browser attempts to change credential/endpoint are ignored.
                "api_key": "browser-injected", "base_url": "https://evil.invalid/v1",
            })
            created = socket.receive_json()

    assert observed["key"] == "source-secret-never-for-browser"
    assert discovered["type"] == "model_discovery_result"
    assert discovered["request_id"] == "discover-source"
    assert "source-secret-never-for-browser" not in str(discovered)
    assert created["type"] == "model_repository_updated"
    assert "source-secret-never-for-browser" not in str(created)
    assert "browser-injected" not in str(created)
    runtime = repository.runtime_model(created["model"]["id"])
    assert runtime["api_key"] == "source-secret-never-for-browser"
    assert runtime["base_url"] is None
    assert runtime["model"] == "new-model"
    assert runtime["context_window"] == 96_000
    assert set(runtime["capability_sources"].values()) == {"user_configuration"}
