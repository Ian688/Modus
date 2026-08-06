import json
import os
import stat

import pytest

from modus.desktop.model_repository import ModelRepository


def _model(identifier: str, *, api_key: str) -> dict:
    return {
        "id": identifier,
        "name": f"Model {identifier}",
        "provider": "test",
        "model": f"model-{identifier}",
        "base_url": "https://example.test/v1",
        "api_key": api_key,
        "context_window": 128_000,
        "max_output_tokens": 8_192,
        "supports_tools": True,
        "supports_images": False,
        "reasoning_efforts": ["low", "high"],
        "default_reasoning_effort": "high",
        "capability_sources": {
            "context_window": "user_configuration",
            "max_output_tokens": "user_configuration",
            "supports_tools": "user_configuration",
            "supports_images": "user_configuration",
            "reasoning_efforts": "user_configuration",
        },
    }


def _canonical_payload() -> dict:
    return {
        "schema_version": 5,
        "models": [
            _model("default", api_key="secret-default-1234"),
            _model("ref", api_key="secret-ref-5678"),
        ],
        "selection": {
            "default_model_id": "default",
            "moa_model_ids": ["default", "ref"],
            "peri_model_ids": ["default", "ref"],
            "moa_roles": {
                "host": {"model_id": "default"},
                "reference_1": {"model_id": "ref"},
            },
            "peri_roles": {
                "host": {"model_id": "default"},
                "worker_1": {"model_id": "ref"},
            },
        },
    }


def test_public_snapshot_never_contains_api_key_values(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_canonical_payload()))
    repository = ModelRepository(path)

    snapshot = repository.public_snapshot()
    wire = json.dumps(snapshot)

    assert "api_key" not in wire
    assert "secret-default-1234" not in wire
    selection = snapshot["selection"]
    assert selection["default_model_id"] == "default"
    assert selection["moa_model_ids"] == ["default", "ref"]
    assert selection["peri_model_ids"] == ["default", "ref"]
    assert selection["moa_roles"]["reference_1"]["model_id"] == "ref"
    assert selection["peri_roles"]["worker_1"]["model_id"] == "ref"
    assert snapshot["models"][0]["has_credential"] is True
    assert snapshot["models"][0]["credential_hint"] == "…1234"


def test_delete_repairs_all_assignments_and_rehomes_default(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_canonical_payload()))
    repository = ModelRepository(path)

    snapshot = repository.delete("default")

    assert snapshot["selection"]["default_model_id"] == "ref"
    assert snapshot["selection"]["moa_model_ids"] == ["ref"]
    assert snapshot["selection"]["peri_model_ids"] == ["ref"]
    assert [model["id"] for model in snapshot["models"]] == ["ref"]


def test_delete_compacts_surviving_participants_into_the_first_slot(tmp_path):
    repository = ModelRepository(tmp_path / "models.json")
    host = repository.create(name="Host", provider="test", model="host", api_key="h")
    first = repository.create(name="First", provider="test", model="first", api_key="a")
    second = repository.create(name="Second", provider="test", model="second", api_key="b")
    repository.set_mode_configuration("peri", {
        "host": {"model_id": host.id},
        "worker_1": {"model_id": first.id, "temperature": 0.5},
        "worker_2": {"model_id": second.id, "temperature": 0.8},
    })

    snapshot = repository.delete(first.id)

    assert snapshot["selection"]["peri_roles"]["worker_1"]["model_id"] == second.id
    assert snapshot["selection"]["peri_roles"]["worker_1"]["temperature"] == 0.8
    assert "worker_2" not in snapshot["selection"]["peri_roles"]


def test_update_without_key_retains_credential_but_public_view_stays_redacted(tmp_path):
    repository = ModelRepository(tmp_path / "models.json")
    created = repository.create(name="Model", provider="test", model="test-1", api_key="keep-me-9876")

    updated = repository.update(created.id, name="Renamed")

    assert updated.name == "Renamed"
    assert updated.has_credential is True
    assert repository.runtime_model(created.id)["api_key"] == "keep-me-9876"
    assert "keep-me-9876" not in json.dumps(repository.public_snapshot())


def test_new_write_is_normalized_atomic_and_owner_only(tmp_path):
    path = tmp_path / "models.json"
    repository = ModelRepository(path)
    repository.create(name="Model", provider="test", model="test-1", api_key="key")

    stored = json.loads(path.read_text())

    assert stored["schema_version"] == 5
    assert set(stored) == {"schema_version", "models", "selection"}
    assert set(stored["selection"]) == {
        "default_model_id", "moa_model_ids", "peri_model_ids",
        "moa_roles", "peri_roles",
    }
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert set(stored["models"][0]["capability_sources"].values()) == {"user_configuration"}


@pytest.mark.parametrize("payload", [
    {},
    {"schema_version": 4, "models": [], "selection": {}},
    {"schema_version": 5, "models": [], "selection": None},
    {"schema_version": 5, "models": None, "selection": {}},
])
def test_repository_rejects_noncanonical_storage(payload, tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError):
        ModelRepository(path).read()


def test_modus_catalog_source_is_preserved(tmp_path):
    path = tmp_path / "models.json"
    payload = _canonical_payload()
    payload["models"][0]["capability_sources"] = {
        field: "modus_catalog" for field in (
            "context_window", "max_output_tokens", "supports_tools",
            "supports_images", "reasoning_efforts",
        )
    }
    path.write_text(json.dumps(payload))

    model = ModelRepository(path).public_snapshot()["models"][0]

    assert set(model["capability_sources"].values()) == {"modus_catalog"}


def test_capabilities_and_mode_roles_are_validated_and_persisted(tmp_path):
    repository = ModelRepository(tmp_path / "models.json")
    host = repository.create(
        name="Host", provider="test", model="reasoner", api_key="host-key",
        context_window=200_000, max_output_tokens=32_000,
        supports_tools=True, reasoning_efforts=["low", "high"],
        default_reasoning_effort="high",
    )
    reference = repository.create(
        name="Reference", provider="test", model="advisor", api_key="ref-key",
        context_window=64_000, supports_tools=False,
    )

    snapshot = repository.set_mode_configuration("moa", {
        "host": {
            "model_id": host.id, "temperature": 0.3,
            "context_tokens": 160_000, "reasoning_effort": "high",
        },
        "reference_1": {
            "model_id": reference.id, "temperature": 0.8,
            "context_tokens": 48_000,
        },
    })

    assert snapshot["selection"]["moa_roles"] == {
        "host": {
            "model_id": host.id, "temperature": 0.3,
            "context_tokens": 160_000, "reasoning_effort": "high",
        },
        "reference_1": {
            "model_id": reference.id, "temperature": 0.8,
            "context_tokens": 48_000, "reasoning_effort": None,
        },
    }
    runtime = repository.runtime_mode_configuration("moa")
    assert runtime["host"]["api_key"] == "host-key"
    assert runtime["host"]["supports_tools"] is True
    assert runtime["reference_1"]["supports_tools"] is False
    assert "api_key" not in json.dumps(snapshot)


def test_unknown_assignment_rejected(tmp_path):
    repository = ModelRepository(tmp_path / "models.json")
    with pytest.raises(ValueError, match="unknown model id"):
        repository.set_mode_configuration("moa", {
            "host": {"model_id": "not-present"},
        })
