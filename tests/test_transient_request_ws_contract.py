"""Correlation contracts for independent Desktop WebSocket requests.

These controls can outlive a socket or server epoch.  Every response therefore
needs enough identity for the browser to ignore stale replies and settle only
the request that produced it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


def _patch_runtime(monkeypatch, server, repository) -> None:
    async def fake_registry(**_kwargs):
        return object()

    class FakeEngine:
        def __init__(self, **_kwargs):
            self.cwd = Path.cwd()

    monkeypatch.setattr(server, "model_repository", repository)
    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _config: object())
    monkeypatch.setattr(server, "QueryEngine", FakeEngine)


def _receive_until(
    socket,
    packet_type: str,
    *,
    request_id: str | None = None,
    operation: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    for _ in range(limit):
        packet = socket.receive_json()
        if packet.get("type") != packet_type:
            continue
        if request_id is not None and packet.get("request_id") != request_id:
            continue
        if operation is not None:
            assert packet.get("operation") == operation, packet
        return packet
    raise AssertionError(
        f"missing type={packet_type!r}, request_id={request_id!r}, "
        f"operation={operation!r}"
    )


def _resume(socket, session_id: str) -> None:
    socket.send_json({"type": "resume_session", "db_id": session_id})
    _receive_until(socket, "session_restored")


def test_credential_migration_echoes_request_identity_on_success_and_error(
    monkeypatch, tmp_path,
):
    from modus.desktop import credential_backend, server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    _patch_runtime(monkeypatch, server, repository)

    monkeypatch.setattr(
        credential_backend,
        "migration_report",
        lambda _repository: {"total_models": 0, "records": []},
    )
    monkeypatch.setattr(
        credential_backend,
        "migrate_credentials_to_keychain",
        lambda _repository: {"moved": 0},
    )

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"

            socket.send_json({
                "type": "credential_migration_report",
                "request_id": "credential-report-ok",
            })
            report = _receive_until(
                socket,
                "credential_migration_report",
                request_id="credential-report-ok",
                operation="credential_migration_report",
            )

            socket.send_json({
                "type": "credential_migration_run",
                "request_id": "credential-run-ok",
            })
            done = _receive_until(
                socket,
                "credential_migration_done",
                request_id="credential-run-ok",
                operation="credential_migration_run",
            )

            def report_failure(_repository):
                raise ValueError("report unavailable")

            def run_failure(_repository):
                raise RuntimeError("migration unavailable")

            monkeypatch.setattr(
                credential_backend, "migration_report", report_failure,
            )
            monkeypatch.setattr(
                credential_backend,
                "migrate_credentials_to_keychain",
                run_failure,
            )

            socket.send_json({
                "type": "credential_migration_report",
                "request_id": "credential-report-error",
            })
            report_error = _receive_until(
                socket,
                "error",
                request_id="credential-report-error",
                operation="credential_migration_report",
            )

            socket.send_json({
                "type": "credential_migration_run",
                "request_id": "credential-run-error",
            })
            run_error = _receive_until(
                socket,
                "error",
                request_id="credential-run-error",
                operation="credential_migration_run",
            )

    assert report["report"]["total_models"] == 0
    assert report["operation"] == "credential_migration_report"
    assert done["result"] == {"moved": 0}
    assert done["operation"] == "credential_migration_run"
    assert report_error["code"] == "credential_migration_report_failed"
    assert run_error["code"] == "credential_migration_failed"


def test_artifact_get_identifies_the_artifact_on_success_and_error(
    monkeypatch, tmp_path,
):
    from modus.desktop import server
    from modus.desktop.artifacts import write_artifact
    from modus.desktop.db import create_run, create_session
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    _patch_runtime(monkeypatch, server, repository)
    owner = create_session()
    create_run("run-artifact-contract", owner["id"], "default")
    artifact = write_artifact(
        session_id=owner["id"],
        run_id="run-artifact-contract",
        kind="worker-response",
        title="Worker result",
        content="correlated artifact content",
    )

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            _resume(socket, owner["id"])

            socket.send_json({
                "type": "artifact_get",
                "artifact_id": artifact["artifact_id"],
                "session_id": owner["id"],
                "request_id": "artifact-contract-loaded",
            })
            loaded = _receive_until(
                socket, "artifact_content", operation="artifact_get",
                request_id="artifact-contract-loaded",
            )

            socket.send_json({
                "type": "artifact_get",
                "artifact_id": "art_missing_contract",
                "session_id": owner["id"],
                "request_id": "artifact-contract-missing",
            })
            missing = _receive_until(
                socket, "error", operation="artifact_get",
                request_id="artifact-contract-missing",
            )

    assert loaded["artifact_id"] == artifact["artifact_id"]
    assert loaded["artifact"]["artifact_id"] == artifact["artifact_id"]
    assert loaded["artifact"]["content"] == "correlated artifact content\n"
    assert loaded["session_id"] == owner["id"]
    assert loaded["requested_session_id"] == owner["id"]
    assert missing["artifact_id"] == "art_missing_contract"
    assert missing["session_id"] == owner["id"]
    assert missing["requested_session_id"] == owner["id"]
    assert missing["message"] == "artifact not found"


def test_model_discovery_echoes_request_identity_on_success_and_error(
    monkeypatch, tmp_path,
):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    available = repository.create(
        name="Available", provider="openai", model="available",
        api_key="server-only-key",
    )
    unavailable = repository.create(
        name="Unavailable", provider="openai", model="unavailable",
        api_key="server-only-key",
    )
    _patch_runtime(monkeypatch, server, repository)

    async def fake_discover(runtime_model):
        if runtime_model["id"] == unavailable.id:
            raise ValueError("provider unavailable")
        return {
            "source_model_id": runtime_model["id"],
            "provider": runtime_model["provider"],
            "catalog_version": "test",
            "warning": "",
            "models": [{
                "id": "available-v2",
                "name": "Available v2",
                "provider": runtime_model["provider"],
                "availability_source": "provider_api",
                "capabilities": {},
                "capability_sources": {},
            }],
        }

    monkeypatch.setattr(server, "discover_models", fake_discover)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"

            socket.send_json({
                "type": "model_discover",
                "model_id": available.id,
                "request_id": "discover-ok",
            })
            discovered = _receive_until(
                socket,
                "model_discovery_result",
                request_id="discover-ok",
                operation="model_discover",
            )

            socket.send_json({
                "type": "model_discover",
                "model_id": unavailable.id,
                "request_id": "discover-error",
            })
            discovery_error = _receive_until(
                socket,
                "error",
                request_id="discover-error",
                operation="model_discover",
            )

    assert discovered["source_model_id"] == available.id
    assert discovered["operation"] == "model_discover"
    assert discovered["models"][0]["id"] == "available-v2"
    assert discovery_error["source_model_id"] == unavailable.id
    assert discovery_error["message"] == "provider unavailable"
    assert "server-only-key" not in str(discovered)


def test_peri_git_readiness_echoes_request_identity_on_success_and_error(
    monkeypatch, tmp_path,
):
    from modus.desktop import git_readiness, server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    _patch_runtime(monkeypatch, server, repository)

    async def fake_readiness(_workspace, *, worker_count, plan_id, data_root):
        assert plan_id.startswith("preview-")
        assert data_root
        if worker_count == 9:
            raise ValueError("worker_count must be between 1 and 8")
        return {
            "ready": True,
            "repository": {"name": "fixture"},
            "workers": [{"ordinal": index} for index in range(1, worker_count + 1)],
            "blockers": [],
        }

    monkeypatch.setattr(
        git_readiness, "inspect_git_readiness", fake_readiness,
    )

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"

            socket.send_json({
                "type": "peri_git_readiness",
                "worker_count": 3,
                "request_id": "peri-readiness-ok",
            })
            readiness = _receive_until(
                socket,
                "peri_git_readiness",
                request_id="peri-readiness-ok",
                operation="peri_git_readiness",
            )

            socket.send_json({
                "type": "peri_git_readiness",
                "worker_count": 9,
                "request_id": "peri-readiness-error",
            })
            readiness_error = _receive_until(
                socket,
                "error",
                request_id="peri-readiness-error",
                operation="peri_git_readiness",
            )

    assert readiness["readiness"]["ready"] is True
    assert readiness["operation"] == "peri_git_readiness"
    assert len(readiness["readiness"]["workers"]) == 3
    assert readiness_error["message"] == "worker_count must be between 1 and 8"


def test_skill_fetch_url_identifies_success_and_invalid_url_error(
    monkeypatch, tmp_path,
):
    import httpx

    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    _patch_runtime(monkeypatch, server, repository)

    class FakeResponse:
        text = "# Imported skill\n\nUse the fixture."

        def raise_for_status(self) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def get(self, url, *, follow_redirects):
            assert url == "https://skills.example/demo.md"
            assert follow_redirects is True
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"

            socket.send_json({
                "type": "skill_fetch_url",
                "url": "file:///tmp/skill.md",
                "request_id": "skill-fetch-invalid",
            })
            invalid = _receive_until(
                socket,
                "error",
                request_id="skill-fetch-invalid",
                operation="skill_fetch_url",
            )

            socket.send_json({
                "type": "skill_fetch_url",
                "url": "https://skills.example/demo.md",
                "request_id": "skill-fetch-ok",
            })
            fetched = _receive_until(
                socket,
                "skill_fetched",
                request_id="skill-fetch-ok",
                operation="skill_fetch_url",
            )

    assert invalid["message"] == "URL 必须以 http:// 或 https:// 开头"
    assert fetched["operation"] == "skill_fetch_url"
    assert fetched["source"] == "https://skills.example/demo.md"
    assert fetched["name"] == "demo-md"
    assert fetched["content"].startswith("# Imported skill")


def test_session_settings_echo_request_identity_on_success_and_error(
    monkeypatch, tmp_path,
):
    from modus.desktop import server
    from modus.desktop.db import create_session
    from modus.desktop.model_repository import ModelRepository

    repository = ModelRepository(tmp_path / "models.json")
    _patch_runtime(monkeypatch, server, repository)
    current = create_session(system_prompt="before")
    other = create_session(system_prompt="other")

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            _resume(socket, current["id"])

            socket.send_json({
                "type": "session_get",
                "session_id": current["id"],
                "request_id": "session-settings-read",
            })
            read = _receive_until(
                socket,
                "session_data",
                request_id="session-settings-read",
                operation="session_get",
            )

            socket.send_json({
                "type": "session_update",
                "session_id": current["id"],
                "system_prompt": "after",
                "request_id": "session-settings-write",
            })
            written = _receive_until(
                socket,
                "session_updated",
                request_id="session-settings-write",
                operation="session_update",
            )

            socket.send_json({
                "type": "session_get",
                "session_id": "missing-session",
                "request_id": "session-settings-read-error",
            })
            read_error = _receive_until(
                socket,
                "error",
                request_id="session-settings-read-error",
                operation="session_get",
            )

            socket.send_json({
                "type": "session_update",
                "session_id": other["id"],
                "system_prompt": "must-not-change",
                "request_id": "session-settings-write-error",
            })
            write_error = _receive_until(
                socket,
                "error",
                request_id="session-settings-write-error",
                operation="session_update",
            )

    assert read["session_id"] == current["id"]
    assert read["system_prompt"] == "before"
    assert written["session_id"] == current["id"]
    assert written["system_prompt"] == "after"
    assert read_error["code"] == "session_not_found"
    assert read_error["session_id"] == "missing-session"
    assert write_error["code"] == "session_mismatch"
    assert write_error["session_id"] == other["id"]


def test_frontend_correlates_session_settings_without_discarding_new_input():
    from _bundle import js_bundle

    page = js_bundle()
    settings = page[
        page.index("let pendingSessionSettingsReadRequestId"):
        page.index("function memoryCategoryLabel")
    ]
    assert 'registerTransientRequestReset("session-settings"' in settings
    assert 'type:"session_get", session_id:targetSessionId' in settings
    assert "request_id:pendingSessionSettingsReadRequestId" in settings
    assert 'type:"session_update", session_id:targetSessionId' in settings
    assert "request_id:pendingSessionSettingsWriteRequestId" in settings
    assert "promptEl.value === pendingSessionSettingsReadBaseline" in settings
    assert "promptEl.value === submittedPrompt" in settings
    assert "pendingSessionSettingsReadRequestId = null" in settings

    router = page[
        page.index('case "session_data":'):
        page.index('case "session_messages":')
    ]
    assert "settleSessionSettingsRead(msg);" in router
    assert "settleSessionSettingsWrite(msg);" in router
    assert "promptEl.value = msg.system_prompt" not in router

    errors = page[
        page.index("function settleTransientRequestError"):
        page.index("// Skills import: template")
    ]
    assert 'case "session_get":' in errors
    assert 'case "session_update":' in errors
    assert "pendingSessionSettingsReadRequestId !== requestId" in errors
    assert "pendingSessionSettingsWriteRequestId !== requestId" in errors
