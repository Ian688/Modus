"""Credential storage backends for model API keys.

The desktop process keeps model metadata in ``models.json``.  A credential
backend decides where each model's API key actually lives.  The default JSON
backend keeps keys inside ``models.json`` (atomic, owner-only, but plaintext).
On macOS an explicitly approved Keychain backend moves keys into the system
Keychain and leaves only a storage marker in JSON.

No backend ever returns a full key through a public DTO; ``has_credential``
and ``credential_hint`` stay the only browser-visible signals.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

KEYCHAIN_SERVICE = "modus"
KEYCHAIN_MARKER = "keychain"

# Marker keys stored on a model record to say where its key lives.
STORAGE_KEY = "credential_storage"


class CredentialBackend(ABC):
    """Store/retrieve a model's API key outside the public DTO surface."""

    marker: str

    @abstractmethod
    def has_credential(self, model_id: str, record: dict[str, Any]) -> bool:
        ...

    @abstractmethod
    def get_credential(self, model_id: str, record: dict[str, Any]) -> str:
        """Return the plaintext key for runtime use (never for the browser)."""
        ...

    @abstractmethod
    def set_credential(self, model_id: str, api_key: str) -> None:
        """Persist a new/replacement key, marking the record storage."""
        ...

    @abstractmethod
    def remove_credential(self, model_id: str, record: dict[str, Any]) -> None:
        """Delete the stored key (used during migration rollback/cleanup)."""
        ...


class JsonCredentialBackend(CredentialBackend):
    """Keys live in the model record inside ``models.json`` (legacy default)."""

    marker = "json"

    def has_credential(self, model_id: str, record: dict[str, Any]) -> bool:
        return bool(str(record.get("api_key") or ""))

    def get_credential(self, model_id: str, record: dict[str, Any]) -> str:
        return str(record.get("api_key") or "")

    def set_credential(self, model_id: str, api_key: str) -> None:
        # The repository persists api_key on the record itself; nothing to do
        # beyond the caller writing the record.
        return

    def remove_credential(self, model_id: str, record: dict[str, Any]) -> None:
        record["api_key"] = ""


def _run_security(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the macOS ``security`` CLI.  Testable via monkeypatching this symbol."""
    return subprocess.run(
        ["/usr/bin/security", *args],
        input=input_text, capture_output=True, text=True, check=False,
    )


class KeychainCredentialBackend(CredentialBackend):
    """Keys live in the macOS Keychain as generic passwords.

    JSON stores only ``credential_storage: "keychain"``; the key is fetched on
    demand.  Falls back to JSON semantics on non-Darwin platforms so callers
    degrade gracefully instead of failing the whole repository.
    """

    marker = KEYCHAIN_MARKER

    def __init__(self, service: str = KEYCHAIN_SERVICE) -> None:
        self.service = service

    @staticmethod
    def _available() -> bool:
        return sys.platform == "darwin"

    def _accounts(self, model_id: str) -> list[str]:
        return [model_id, f"model:{model_id}"]

    def has_credential(self, model_id: str, record: dict[str, Any]) -> bool:
        if not self._available():
            return bool(str(record.get("api_key") or ""))
        for account in self._accounts(model_id):
            result = _run_security([
                "find-generic-password", "-s", self.service, "-a", account,
            ])
            if result.returncode == 0:
                return True
        return False

    def get_credential(self, model_id: str, record: dict[str, Any]) -> str:
        if not self._available():
            return str(record.get("api_key") or "")
        for account in self._accounts(model_id):
            result = _run_security([
                "find-generic-password", "-s", self.service, "-a", account, "-w",
            ])
            if result.returncode == 0:
                return result.stdout.strip()
        return ""

    def set_credential(self, model_id: str, api_key: str) -> None:
        if not api_key:
            return
        if not self._available():
            return
        key = api_key.strip()
        if not key:
            return
        for account in self._accounts(model_id):
            result = _run_security([
                "delete-generic-password", "-s", self.service, "-a", account,
            ])
            # Missing entries are fine; a fresh add below wins.
            _ = result
        add = _run_security([
            "add-generic-password", "-s", self.service, "-a", model_id, "-w", key,
        ])
        if add.returncode != 0:
            raise RuntimeError(f"Keychain write failed: {add.stderr.strip()}")

    def remove_credential(self, model_id: str, record: dict[str, Any]) -> None:
        if not self._available():
            record[STORAGE_KEY] = self.marker
            record["api_key"] = ""
            return
        for account in self._accounts(model_id):
            _run_security([
                "delete-generic-password", "-s", self.service, "-a", account,
            ])
        record.pop(STORAGE_KEY, None)
        record["api_key"] = ""


def backend_for(marker: str, *, service: str = KEYCHAIN_SERVICE) -> CredentialBackend:
    if marker == KEYCHAIN_MARKER:
        return KeychainCredentialBackend(service=service)
    return JsonCredentialBackend()


def migration_report(repository: Any) -> dict[str, Any]:
    """Redacted view of every plaintext model key that a Keychain migration
    would move.  Never includes key values, only whether one exists."""
    data = repository.read()
    records: list[dict[str, Any]] = []
    for model in data["models"]:
        key = str(model.get("api_key") or "")
        records.append({
            "id": str(model["id"]),
            "name": str(model.get("name") or ""),
            "provider": str(model.get("provider") or ""),
            "model": str(model.get("model") or ""),
            "has_credential": bool(key),
            "credential_hint": "…" + key[-4:] if len(key) >= 4 else None,
            "storage": str(model.get(STORAGE_KEY) or "json"),
        })
    selection = data.get("selection") or {}
    return {
        "target": "macos_keychain",
        "total_models": len(data["models"]),
        "records": records,
        "default_model_id": selection.get("default_model_id"),
        "moa_model_ids": selection.get("moa_model_ids", []),
        "peri_model_ids": selection.get("peri_model_ids", []),
    }


def migrate_credentials_to_keychain(repository: Any, *, service: str = KEYCHAIN_SERVICE) -> dict[str, Any]:
    """Move plaintext model keys from JSON into the Keychain.

    Must only be called after the user has reviewed and confirmed a redacted
    report.  Backs up the current JSON before altering it, so the change is
    reversible by restoring the backup.
    """
    if sys.platform != "darwin":
        raise RuntimeError("Keychain migration is only supported on macOS")
    data = repository.read()
    backend = KeychainCredentialBackend(service=service)
    moved = 0
    for model in data["models"]:
        key = str(model.get("api_key") or "").strip()
        if not key:
            continue
        backend.set_credential(str(model["id"]), key)
        model[STORAGE_KEY] = KEYCHAIN_MARKER
        model["api_key"] = ""
        moved += 1
    if moved:
        _backup(repository)
        repository._write(data)
    return {"moved": moved, "models": len(data["models"])}


def _backup(repository: Any) -> Path:
    """Copy the current models.json before a migration mutates it."""
    source = repository.path
    if not source.exists():
        raise RuntimeError("no models.json to back up")
    stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
    backup = source.with_name(f"models.json.pre-keychain-{stamp}.bak")
    backup.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass
    return backup
