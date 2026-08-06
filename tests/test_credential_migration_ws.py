"""WebSocket surface for credential migration: redacted report + confirmed run.

The Keychain CLI is faked; the report path is exercised end-to-end over the
WebSocket and must never leak a key value.
"""

import sys

from fastapi.testclient import TestClient

from modus.desktop import credential_backend as cb


class FakeSecurityStore:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def run(self, args, *, input_text=None):
        args = list(args)
        if args[0] == "add-generic-password":
            service = args[args.index("-s") + 1]
            account = args[args.index("-a") + 1]
            password = args[args.index("-w") + 1]
            self.entries[(service, account)] = password
            return _done(0)
        if args[0] == "find-generic-password":
            service = args[args.index("-s") + 1]
            account = args[args.index("-a") + 1]
            key = (service, account)
            if key not in self.entries:
                return _done(44, stdout="", stderr="not found")
            if "-w" in args:
                return _done(0, stdout=self.entries[key] + "\n")
            return _done(0)
        if args[0] == "delete-generic-password":
            service = args[args.index("-s") + 1]
            account = args[args.index("-a") + 1]
            self.entries.pop((service, account), None)
            return _done(0)
        return _done(2, stderr="unknown")


def _done(code, stdout="", stderr=""):
    return type("Result", (), {"returncode": code, "stdout": stdout, "stderr": stderr})()


def _patch_engine(monkeypatch, server):
    async def fake_registry(**_kwargs):
        return object()

    class FakeEngine:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", FakeEngine)


def _receive_until(socket, target: str, limit: int = 20):
    for _ in range(limit):
        packet = socket.receive_json()
        if packet.get("type") == target:
            return packet
    raise AssertionError(f"missing {target}")


def test_credential_migration_report_over_websocket_is_redacted(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repo = ModelRepository(tmp_path / "models.json")
    repo.create(name="DeepSeek", provider="deepseek", model="deepseek-v4", api_key="sk-top-secret-abcd")
    monkeypatch.setattr(server, "model_repository", repo)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({
                "type": "credential_migration_report", "request_id": "report-1",
            })
            report = _receive_until(socket, "credential_migration_report")

    assert report["request_id"] == "report-1"
    assert report["report"]["total_models"] == 1
    assert "sk-top-secret" not in str(report)
    record = report["report"]["records"][0]
    assert record["has_credential"] is True
    assert record["credential_hint"] == "…abcd"


def test_credential_migration_run_moves_keys_through_fake_keychain(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repo = ModelRepository(tmp_path / "models.json")
    repo.create(name="DeepSeek", provider="deepseek", model="deepseek-v4", api_key="sk-move-secret")
    monkeypatch.setattr(server, "model_repository", repo)
    monkeypatch.setattr(cb, "_run_security", FakeSecurityStore().run)
    monkeypatch.setattr(sys, "platform", "darwin")
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({
                "type": "credential_migration_run", "request_id": "run-1",
            })
            done = _receive_until(socket, "credential_migration_done")
            socket.send_json({"type": "model_repository_get"})
            _receive_until(socket, "model_repository_updated")

    assert done["request_id"] == "run-1"
    assert done["result"]["moved"] == 1
    raw = (tmp_path / "models.json").read_text(encoding="utf-8")
    assert "sk-move-secret" not in raw
    assert repo.runtime_model(repo.list_public()[0].id)["api_key"] == "sk-move-secret"


def test_credential_migration_run_rejected_on_non_macos(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.desktop.model_repository import ModelRepository

    repo = ModelRepository(tmp_path / "models.json")
    repo.create(name="DeepSeek", provider="deepseek", model="deepseek-v4", api_key="sk-x")
    monkeypatch.setattr(server, "model_repository", repo)
    monkeypatch.setattr(sys, "platform", "linux")
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({
                "type": "credential_migration_run", "request_id": "run-failed",
            })
            packet = _receive_until(socket, "error")
    assert packet.get("code") == "credential_migration_failed"
    assert packet.get("operation") == "credential_migration_run"
    assert packet.get("request_id") == "run-failed"
