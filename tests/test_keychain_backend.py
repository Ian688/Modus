"""Keychain credential backend: fake security CLI round-trips + migration.

The real macOS Keychain is never touched.  ``_run_security`` is monkeypatched
to an in-memory fake so the backend's add/find/delete framing is verified.
"""

import sys

import pytest

from modus.desktop import credential_backend as cb
from modus.desktop.credential_backend import (
    JsonCredentialBackend,
    KeychainCredentialBackend,
    backend_for,
    migrate_credentials_to_keychain,
    migration_report,
)
from modus.desktop.model_repository import ModelRepository


class FakeSecurityStore:
    """In-memory stand-in for `security add/find/delete-generic-password`."""

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
    return type("Result", (), {
        "returncode": code, "stdout": stdout, "stderr": stderr,
    })()


@pytest.fixture
def fake_security(monkeypatch):
    store = FakeSecurityStore()
    monkeypatch.setattr(cb, "_run_security", store.run)
    monkeypatch.setattr(sys, "platform", "darwin")
    return store


def test_keychain_backend_round_trips_through_add_find_delete(fake_security):
    backend = KeychainCredentialBackend()
    assert backend._available() is True
    backend.set_credential("model-a", "sk-secret-1234")
    assert backend.has_credential("model-a", {}) is True
    assert backend.get_credential("model-a", {}) == "sk-secret-1234"
    backend.remove_credential("model-a", {})
    assert backend.has_credential("model-a", {}) is False


def test_keychain_backend_degraded_to_json_on_non_darwin(fake_security):
    backend = KeychainCredentialBackend()
    fake_security.run = lambda *a, **k: _done(0)  # ensure CLI is reachable
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sys, "platform", "linux")
    try:
        assert backend._available() is False
        record = {"api_key": "plaintext-fallback"}
        assert backend.get_credential("m", record) == "plaintext-fallback"
        backend.set_credential("m", "key")  # no-op, no crash
    finally:
        monkeypatch.undo()


def test_json_backend_keeps_key_on_record():
    backend = JsonCredentialBackend()
    record = {"api_key": "sk-json"}
    assert backend.has_credential("m", record) is True
    assert backend.get_credential("m", record) == "sk-json"


def test_backend_for_marker_routes_to_keychain_or_json():
    assert isinstance(backend_for("keychain"), KeychainCredentialBackend)
    assert isinstance(backend_for("json"), JsonCredentialBackend)
    assert isinstance(backend_for("unknown"), JsonCredentialBackend)


def test_repository_with_keychain_backend_never_writes_plaintext_to_json(tmp_path, fake_security):
    repo = ModelRepository(tmp_path / "models.json", backend=KeychainCredentialBackend())
    created = repo.create(name="DeepSeek", provider="deepseek", model="deepseek-v4", api_key="sk-migrated-9999")

    # The public DTO reveals only hint, never the key.
    assert created.has_credential is True
    assert created.credential_hint == "…9999"
    assert "sk-migrated" not in created.to_wire().values()

    # JSON holds no plaintext; the runtime resolves the key from the store.
    raw = (tmp_path / "models.json").read_text(encoding="utf-8")
    assert "sk-migrated" not in raw
    assert "credential_storage" in raw

    runtime = repo.runtime_model(created.id)
    assert runtime["api_key"] == "sk-migrated-9999"


def test_migration_report_is_redacted_and_lists_impact(tmp_path, fake_security):
    repo = ModelRepository(tmp_path / "models.json")
    repo.create(name="A", provider="openai", model="gpt-x", api_key="sk-secret-abcd")
    repo.create(name="B", provider="deepseek", model="ds-v4", api_key="sk-another-1234")

    report = migration_report(repo)
    assert report["target"] == "macos_keychain"
    assert report["total_models"] == 2
    assert "sk-secret" not in str(report)
    assert "sk-another" not in str(report)
    hints = {r["credential_hint"] for r in report["records"]}
    assert hints == {"…abcd", "…1234"}


def test_migrate_credentials_to_keychain_moves_keys_and_backs_up(tmp_path, fake_security):
    repo = ModelRepository(tmp_path / "models.json")
    repo.create(name="A", provider="openai", model="gpt-x", api_key="sk-move-a")
    repo.create(name="B", provider="deepseek", model="ds-v4", api_key="sk-move-b")

    result = migrate_credentials_to_keychain(repo)
    assert result["moved"] == 2

    # Plaintext gone from JSON; keys resolvable from the fake store.
    raw = (tmp_path / "models.json").read_text(encoding="utf-8")
    assert "sk-move" not in raw
    assert "credential_storage" in raw
    assert repo.runtime_model(repo.list_public()[0].id)["api_key"] == "sk-move-a"
    assert repo.runtime_model(repo.list_public()[1].id)["api_key"] == "sk-move-b"

    # A timestamped backup exists for reversibility.
    backups = list(tmp_path.glob("models.json.pre-keychain-*.bak"))
    assert len(backups) == 1
    assert "sk-move" in backups[0].read_text(encoding="utf-8")


def test_migrate_credentials_is_macos_only(tmp_path, fake_security):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sys, "platform", "linux")
    try:
        repo = ModelRepository(tmp_path / "models.json")
        repo.create(name="A", provider="openai", model="gpt-x", api_key="sk-x")
        with pytest.raises(RuntimeError):
            migrate_credentials_to_keychain(repo)
    finally:
        monkeypatch.undo()


def test_repository_json_backend_behavior_is_unchanged(tmp_path):
    repo = ModelRepository(tmp_path / "models.json")
    created = repo.create(name="Legacy", provider="custom", model="m", api_key="sk-plain")
    # JSON backend keeps plaintext on the record by design.
    assert (tmp_path / "models.json").read_text(encoding="utf-8") == repo.path.read_text(encoding="utf-8")
    assert "sk-plain" in (tmp_path / "models.json").read_text(encoding="utf-8")
    assert repo.runtime_model(created.id)["api_key"] == "sk-plain"
