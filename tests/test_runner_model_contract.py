import inspect


LEGACY_RUNTIME_KEYS = {"all", "primary_id", "moa_ref_ids", "peri_ids"}


def _configured_repository(tmp_path):
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    host = repository.create(
        name="Host", provider="test", model="host-v1", api_key="host-secret",
    )
    worker = repository.create(
        name="Worker", provider="test", model="worker-v1", api_key="worker-secret",
        reasoning_efforts=["low", "high"], default_reasoning_effort="high",
    )
    repository.set_mode_configuration("moa", {
        "host": {"model_id": host.id},
        "reference_1": {"model_id": worker.id},
    })
    repository.set_mode_configuration("peri", {
        "host": {"model_id": host.id},
        "worker_1": {"model_id": worker.id},
    })
    return repository, host.id, worker.id


def test_server_runtime_dto_contains_only_canonical_role_maps(monkeypatch, tmp_path):
    from modus.desktop import server

    repository, _host_id, _worker_id = _configured_repository(tmp_path)
    monkeypatch.setattr(server, "model_repository", repository)

    runtime = server._load_models()

    assert set(runtime) == {"moa_roles", "peri_roles"}
    assert LEGACY_RUNTIME_KEYS.isdisjoint(runtime)
    assert set(runtime["moa_roles"]) == {"host", "reference_1"}
    assert set(runtime["peri_roles"]) == {"host", "worker_1"}
    assert runtime["moa_roles"]["host"]["api_key"] == "host-secret"
    assert runtime["peri_roles"]["worker_1"]["api_key"] == "worker-secret"


def test_session_runtime_dto_resolves_snapshot_without_legacy_fields(monkeypatch, tmp_path):
    from modus.desktop import server

    repository, host_id, worker_id = _configured_repository(tmp_path)
    monkeypatch.setattr(server, "model_repository", repository)
    session = server.DaoSession(
        id="runtime", mode="peri",
        mode_config={
            "host": {"model_id": host_id, "temperature": 0.2},
            "worker_1": {"model_id": worker_id, "reasoning_effort": "low"},
        },
    )

    runtime = server._load_models_for_session(session, "peri")

    assert set(runtime) == {"peri_roles"}
    assert LEGACY_RUNTIME_KEYS.isdisjoint(runtime)
    assert runtime["peri_roles"]["host"]["temperature"] == 0.2
    assert runtime["peri_roles"]["worker_1"]["reasoning_effort"] == "low"
    assert runtime["peri_roles"]["worker_1"]["api_key"] == "worker-secret"


def test_mode_runners_have_no_legacy_model_resolution_fallback():
    from modus.desktop import moa_runner, peri_runner

    source = inspect.getsource(moa_runner) + inspect.getsource(peri_runner)

    for key in LEGACY_RUNTIME_KEYS:
        assert f'"{key}"' not in source
        assert f"'{key}'" not in source
