from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from modus.modes import MOA_MODE, PERI_MODE, normalize_mode
from modus.desktop.credential_backend import (
    CredentialBackend, JsonCredentialBackend, KeychainCredentialBackend,
    KEYCHAIN_MARKER, STORAGE_KEY, backend_for,
)


@dataclass(frozen=True, slots=True)
class ModelPublic:
    """Browser-safe view of a configured model.

    A credential value never appears on this DTO. The UI may know that one is
    configured, but cannot read or re-submit it accidentally as part of an
    unrelated model edit.
    """

    id: str
    name: str
    provider: str
    model: str
    base_url: str | None
    has_credential: bool
    credential_hint: str | None
    context_window: int
    max_output_tokens: int
    supports_tools: bool
    supports_images: bool
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str | None
    capability_sources: dict[str, str]

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "has_credential": self.has_credential,
            "credential_hint": self.credential_hint,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "supports_tools": self.supports_tools,
            "supports_images": self.supports_images,
            "reasoning_efforts": list(self.reasoning_efforts),
            "default_reasoning_effort": self.default_reasoning_effort,
            "capability_sources": self.capability_sources,
        }


@dataclass(frozen=True, slots=True)
class ModelSelection:
    default_model_id: str | None
    moa_model_ids: tuple[str, ...]
    peri_model_ids: tuple[str, ...]
    moa_roles: dict[str, dict[str, Any]]
    peri_roles: dict[str, dict[str, Any]]

    def to_wire(self) -> dict[str, Any]:
        return {
            "default_model_id": self.default_model_id,
            "moa_model_ids": list(self.moa_model_ids),
            "peri_model_ids": list(self.peri_model_ids),
            "moa_roles": self.moa_roles,
            "peri_roles": self.peri_roles,
        }


class ModelRepository:
    """Single model/credential authority for the desktop process.

    Current persistence remains a local JSON file so introducing this boundary
    never silently migrates a user's secrets to a system service. JSON writes are
    atomic and owner-only where the platform supports chmod. A later explicitly
    approved Keychain migration can replace only the internal credential access.
    """

    def __init__(
        self, path: str | Path, *, backend: CredentialBackend | None = None,
        keychain_service: str = "modus",
    ) -> None:
        self.path = Path(path).expanduser()
        self._default_backend = backend or JsonCredentialBackend()
        self._keychain_service = str(keychain_service or "modus")

    @property
    def backend(self) -> CredentialBackend:
        return self._default_backend

    def _backend_for(self, record: dict[str, Any]) -> CredentialBackend:
        marker = str(record.get(STORAGE_KEY) or "json")
        if marker == self._default_backend.marker:
            return self._default_backend
        if marker == KEYCHAIN_MARKER:
            return KeychainCredentialBackend(service=self._keychain_service)
        return backend_for(marker)

    def _credential_value(self, record: dict[str, Any]) -> str:
        return self._backend_for(record).get_credential(str(record["id"]), record)

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty()
        return self._normalize(raw if isinstance(raw, dict) else {})

    def list_public(self) -> list[ModelPublic]:
        return [self._public(model) for model in self.read()["models"]]

    def public_snapshot(self) -> dict[str, Any]:
        data = self.read()
        return {
            "models": [self._public(model).to_wire() for model in data["models"]],
            "selection": self._selection(data).to_wire(),
        }

    def runtime_model(self, model_id: str | None = None) -> dict[str, Any] | None:
        data = self.read()
        chosen = model_id or data["selection"]["default_model_id"]
        record = next((dict(item) for item in data["models"] if item["id"] == chosen), None)
        if record is None:
            return None
        record["api_key"] = self._credential_value(record)
        return record

    def runtime_models(self, model_ids: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
        data = self.read()
        wanted = set(model_ids)
        records = [dict(item) for item in data["models"] if item["id"] in wanted]
        for record in records:
            record["api_key"] = self._credential_value(record)
        return records

    def runtime_mode_configuration(
        self, mode: str, role_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Resolve role metadata to credential-bearing runtime model configs.

        A persisted session snapshot contains model IDs and non-secret controls.
        Credentials are resolved just in time so keys are never copied into
        SQLite and credential rotation does not invalidate old sessions.
        """
        mode = normalize_mode(mode, strict=True)
        if mode not in {MOA_MODE, PERI_MODE}:
            raise ValueError("mode must be moa or peri")
        data = self.read()
        by_id = {item["id"]: item for item in data["models"]}
        configured = role_snapshot if isinstance(role_snapshot, dict) and role_snapshot else data["selection"].get(f"{mode}_roles", {})
        resolved: dict[str, dict[str, Any]] = {}
        for role, raw in configured.items():
            if not isinstance(raw, dict):
                continue
            model = by_id.get(str(raw.get("model_id") or ""))
            if model is None:
                continue
            role_config = self._role_config(raw, model, str(role))
            resolved[str(role)] = {
                **dict(model), **role_config,
                "api_key": self._credential_value(model),
            }
        return resolved

    def create(
        self,
        *,
        name: str,
        provider: str,
        model: str,
        api_key: str = "",
        base_url: str | None = None,
        context_window: int = 128_000,
        max_output_tokens: int = 8_192,
        supports_tools: bool = True,
        supports_images: bool = False,
        reasoning_efforts: list[str] | tuple[str, ...] = (),
        default_reasoning_effort: str | None = None,
        capability_sources: dict[str, str] | None = None,
    ) -> ModelPublic:
        data = self.read()
        record = self._record(
            identifier=uuid4().hex[:12], name=name, provider=provider, model=model,
            api_key=api_key, base_url=base_url,
            context_window=context_window, max_output_tokens=max_output_tokens,
            supports_tools=supports_tools, supports_images=supports_images,
            reasoning_efforts=reasoning_efforts,
            default_reasoning_effort=default_reasoning_effort,
            capability_sources=capability_sources,
        )
        data["models"].append(record)
        if data["selection"]["default_model_id"] is None:
            data["selection"]["default_model_id"] = record["id"]
        # Persist the credential via the backend before writing JSON so the
        # plaintext never lands in models.json when the backend stores it
        # out of band (e.g. Keychain).
        self._store_credential(record, api_key)
        self._write(data)
        return self._public(record)

    def update(self, model_id: str, **changes: Any) -> ModelPublic:
        data = self.read()
        record = self._require(data, model_id)
        for field in ("name", "provider", "model", "base_url"):
            if field in changes and changes[field] is not None:
                record[field] = self._clean_text(changes[field], field)
        # Empty/missing means "retain credential". An explicit replacement is
        # accepted only when the caller sends a non-empty key; it is persisted
        # through the backend (JSON keeps it on the record, Keychain stores it
        # out of band) so no key value is ever part of a public DTO.
        if changes.get("api_key"):
            self._store_credential(record, str(changes["api_key"]))
        if "context_window" in changes:
            record["context_window"] = self._bounded_int(
                changes["context_window"], "context_window", 1_024, 10_000_000,
            )
        if "max_output_tokens" in changes:
            record["max_output_tokens"] = self._bounded_int(
                changes["max_output_tokens"], "max_output_tokens", 1, 1_000_000,
            )
        for field in ("supports_tools", "supports_images"):
            if field in changes:
                record[field] = bool(changes[field])
        if "reasoning_efforts" in changes or "default_reasoning_effort" in changes:
            efforts = self._reasoning_efforts(changes.get("reasoning_efforts", record.get("reasoning_efforts", [])))
            default_effort = self._reasoning_default(
                changes.get("default_reasoning_effort", record.get("default_reasoning_effort")), efforts,
            )
            record["reasoning_efforts"] = efforts
            record["default_reasoning_effort"] = default_effort
        sources = dict(record.get("capability_sources") or {})
        for field in (
            "context_window", "max_output_tokens", "supports_tools",
            "supports_images", "reasoning_efforts",
        ):
            if field in changes:
                sources[field] = "user_configuration"
        record["capability_sources"] = self._capability_sources(sources)
        self._write(data)
        return self._public(record)

    def delete(self, model_id: str) -> dict[str, Any]:
        data = self.read()
        self._require(data, model_id)
        data["models"] = [item for item in data["models"] if item["id"] != model_id]
        selection = data["selection"]
        selection["moa_model_ids"] = [item for item in selection["moa_model_ids"] if item != model_id]
        selection["peri_model_ids"] = [item for item in selection["peri_model_ids"] if item != model_id]
        if selection["default_model_id"] == model_id:
            selection["default_model_id"] = data["models"][0]["id"] if data["models"] else None
        for mode in (MOA_MODE, PERI_MODE):
            roles_key = f"{mode}_roles"
            role_names = ("host", "reference_1", "reference_2") if mode == "moa" else ("host", "worker_1", "worker_2")
            previous = selection.get(roles_key, {})
            host = previous.get("host") if isinstance(previous.get("host"), dict) else None
            if host and host.get("model_id") == model_id:
                host = None
            if host is None and selection["default_model_id"]:
                host = {"model_id": selection["default_model_id"]}
            # One configured model should not silently consume multiple paid
            # role slots after a deletion/re-home operation. Compact surviving
            # participants so deleting slot 1 never leaves only slot 2 behind.
            seen_models = {str((host or {}).get("model_id") or "")}
            participants: list[dict[str, Any]] = []
            for role in role_names[1:]:
                raw = previous.get(role)
                participant_id = str((raw or {}).get("model_id") or "") if isinstance(raw, dict) else ""
                if not participant_id or participant_id == model_id or participant_id in seen_models:
                    continue
                seen_models.add(participant_id)
                participants.append(dict(raw))
            compacted = {"host": dict(host)} if host else {}
            compacted.update({role: raw for role, raw in zip(role_names[1:], participants)})
            selection[roles_key] = compacted
            selection[f"{mode}_model_ids"] = [
                item["model_id"] for item in selection[roles_key].values()
            ]
        self._write(data)
        return self.public_snapshot()

    def set_default(self, model_id: str) -> dict[str, Any]:
        data = self.read()
        self._require(data, model_id)
        data["selection"]["default_model_id"] = model_id
        self._write(data)
        return self.public_snapshot()

    def set_mode_configuration(self, mode: str, roles: dict[str, Any]) -> dict[str, Any]:
        """Persist a complete backend-validated role configuration.

        Role records are the authoritative source for temperature, context and
        reasoning controls used by the runners; ordered model IDs are derived.
        """
        mode = normalize_mode(mode, strict=True)
        if mode not in {MOA_MODE, PERI_MODE}:
            raise ValueError("mode must be moa or peri")
        allowed = ("host", "reference_1", "reference_2") if mode == "moa" else ("host", "worker_1", "worker_2")
        if not isinstance(roles, dict):
            raise ValueError("roles must be an object")
        data = self.read()
        normalized: dict[str, dict[str, Any]] = {}
        for role in allowed:
            raw = roles.get(role)
            if not isinstance(raw, dict) or not str(raw.get("model_id") or ""):
                continue
            model_id = str(raw["model_id"])
            normalized[role] = self._role_config(raw, self._require(data, model_id), role, strict=True)
        if "host" not in normalized:
            raise ValueError(f"{mode} requires a host model")
        data["selection"][f"{mode}_roles"] = normalized
        data["selection"][f"{mode}_model_ids"] = [normalized[role]["model_id"] for role in allowed if role in normalized]
        self._write(data)
        return self.public_snapshot()

    @staticmethod
    def _hint(api_key: str) -> str | None:
        key = api_key.strip()
        return "…" + key[-4:] if len(key) >= 4 else ("已配置" if key else None)

    def _public(self, record: dict[str, Any]) -> ModelPublic:
        key = self._credential_value(record)
        return ModelPublic(
            id=str(record["id"]), name=str(record["name"]), provider=str(record["provider"]),
            model=str(record["model"]), base_url=record.get("base_url") or None,
            has_credential=bool(key), credential_hint=self._hint(key),
            context_window=int(record.get("context_window") or 128_000),
            max_output_tokens=int(record.get("max_output_tokens") or 8_192),
            supports_tools=bool(record.get("supports_tools", True)),
            supports_images=bool(record.get("supports_images", False)),
            reasoning_efforts=tuple(record.get("reasoning_efforts") or ()),
            default_reasoning_effort=record.get("default_reasoning_effort") or None,
            capability_sources=self._capability_sources(record.get("capability_sources")),
        )

    def _selection(self, data: dict[str, Any]) -> ModelSelection:
        item = data["selection"]
        return ModelSelection(
            default_model_id=item["default_model_id"],
            moa_model_ids=tuple(item["moa_model_ids"]),
            peri_model_ids=tuple(item["peri_model_ids"]),
            moa_roles={key: dict(value) for key, value in item.get("moa_roles", {}).items()},
            peri_roles={key: dict(value) for key, value in item.get("peri_roles", {}).items()},
        )

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(self._normalize(data), indent=2, ensure_ascii=False) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".models-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            schema = int(raw.get("schema_version") or 0)
        except (TypeError, ValueError):
            schema = 0
        if schema != 5:
            raise ValueError("models.json must use Modus schema version 5")
        raw_models = raw.get("models")
        if not isinstance(raw_models, list):
            raise ValueError("models must be a list")
        selection_source = raw.get("selection")
        if not isinstance(selection_source, dict):
            raise ValueError("selection must be an object")

        models: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, candidate in enumerate(raw_models):
            if not isinstance(candidate, dict):
                continue
            record = self._record(
                identifier=str(candidate.get("id") or uuid4().hex[:12]),
                name=str(candidate.get("name") or candidate.get("model") or f"模型 {index + 1}"),
                provider=str(candidate.get("provider") or "custom"),
                model=str(candidate.get("model") or ""),
                api_key=str(candidate.get("api_key") or ""),
                base_url=candidate.get("base_url"),
                context_window=candidate.get("context_window", 128_000),
                max_output_tokens=candidate.get("max_output_tokens", 8_192),
                supports_tools=candidate.get("supports_tools", True),
                supports_images=candidate.get("supports_images", False),
                reasoning_efforts=candidate.get("reasoning_efforts", ()),
                default_reasoning_effort=candidate.get("default_reasoning_effort"),
                capability_sources=candidate.get("capability_sources"),
                storage=str(candidate.get(STORAGE_KEY) or "json"),
            )
            if not record["model"] or record["id"] in seen:
                continue
            seen.add(record["id"])
            models.append(record)

        default_id = selection_source.get("default_model_id")
        ids = {item["id"] for item in models}
        if default_id not in ids:
            default_id = models[0]["id"] if models else None
        moa_ids = self._valid_ids(selection_source.get("moa_model_ids", []), ids)
        peri_ids = self._valid_ids(selection_source.get("peri_model_ids", []), ids)
        by_id = {item["id"]: item for item in models}
        moa_roles = self._normalize_roles(
            MOA_MODE, selection_source.get("moa_roles"), moa_ids, by_id,
        )
        peri_roles = self._normalize_roles(
            PERI_MODE, selection_source.get("peri_roles"), peri_ids, by_id,
        )
        return {
            "schema_version": 5,
            "models": models,
            "selection": {
                "default_model_id": default_id,
                "moa_model_ids": [item["model_id"] for item in moa_roles.values()],
                "peri_model_ids": [item["model_id"] for item in peri_roles.values()],
                "moa_roles": moa_roles,
                "peri_roles": peri_roles,
            },
        }

    @staticmethod
    def _valid_ids(value: Any, known: set[str]) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(item) for item in value if str(item) in known))

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": 5,
            "models": [],
            "selection": {
                "default_model_id": None, "moa_model_ids": [], "peri_model_ids": [],
                "moa_roles": {}, "peri_roles": {},
            },
        }

    def _store_credential(self, record: dict[str, Any], api_key: str) -> None:
        """Persist a key through the active backend and mark the record.

        For the JSON backend the key stays on the record itself.  For out-of-band
        backends (Keychain) the key is written to the store, the record is marked
        ``credential_storage: keychain``, and the plaintext is removed from JSON.
        """
        if self._default_backend.marker == "json":
            record["api_key"] = str(api_key or "").strip()
            return
        if not api_key:
            return
        self._default_backend.set_credential(str(record["id"]), api_key)
        record[STORAGE_KEY] = self._default_backend.marker
        record["api_key"] = ""

    def _require(self, data: dict[str, Any], model_id: str) -> dict[str, Any]:
        record = next((item for item in data["models"] if item["id"] == model_id), None)
        if record is None:
            raise ValueError("unknown model id")
        return record

    def _record(
        self, *, identifier: str, name: str, provider: str, model: str, api_key: str, base_url: Any,
        context_window: Any = 128_000, max_output_tokens: Any = 8_192,
        supports_tools: Any = True, supports_images: Any = False,
        reasoning_efforts: Any = (), default_reasoning_effort: Any = None,
        capability_sources: Any = None, storage: str | None = None,
    ) -> dict[str, Any]:
        if storage == KEYCHAIN_MARKER:
            stored_api_key = ""
        else:
            stored_api_key = str(api_key or "").strip()
        return {
            "id": self._clean_text(identifier, "id"),
            "name": self._clean_text(name, "name"),
            "provider": self._clean_text(provider, "provider"),
            "model": self._clean_text(model, "model"),
            "base_url": self._clean_text(base_url, "base_url") if base_url else None,
            "api_key": stored_api_key,
            STORAGE_KEY: "json" if storage is None else storage,
            "context_window": self._bounded_int(context_window, "context_window", 1_024, 10_000_000),
            "max_output_tokens": self._bounded_int(max_output_tokens, "max_output_tokens", 1, 1_000_000),
            "supports_tools": bool(supports_tools),
            "supports_images": bool(supports_images),
            "reasoning_efforts": self._reasoning_efforts(reasoning_efforts),
            "default_reasoning_effort": self._reasoning_default(
                default_reasoning_effort, self._reasoning_efforts(reasoning_efforts),
            ),
            "capability_sources": self._capability_sources(capability_sources),
        }

    @staticmethod
    def _capability_sources(value: Any) -> dict[str, str]:
        allowed_sources = {
            "provider_api", "modus_catalog", "user_configuration", "unknown",
        }
        raw = value if isinstance(value, dict) else {}
        result: dict[str, str] = {}
        for field in (
            "context_window", "max_output_tokens", "supports_tools",
            "supports_images", "reasoning_efforts",
        ):
            source = str(raw.get(field))
            result[field] = source if source in allowed_sources else "user_configuration"
        return result

    def _normalize_roles(
        self, mode: str, raw_roles: Any, model_ids: list[str], by_id: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        names = ("host", "reference_1", "reference_2") if mode == "moa" else ("host", "worker_1", "worker_2")
        source = raw_roles if isinstance(raw_roles, dict) else {}
        result: dict[str, dict[str, Any]] = {}
        for index, role in enumerate(names):
            raw = source.get(role) if isinstance(source.get(role), dict) else {}
            model_id = str(raw.get("model_id") or (model_ids[index] if index < len(model_ids) else ""))
            if model_id not in by_id:
                continue
            result[role] = self._role_config(raw | {"model_id": model_id}, by_id[model_id], role)
        return result

    def _role_config(
        self, raw: dict[str, Any], model: dict[str, Any], role: str, *, strict: bool = False,
    ) -> dict[str, Any]:
        default_temperature = 0.4 if role == "host" else 0.7
        try:
            temperature = float(raw.get("temperature", default_temperature))
        except (TypeError, ValueError):
            temperature = default_temperature
        if strict and not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        temperature = min(2.0, max(0.0, temperature))
        if strict:
            context_tokens = self._bounded_int(
                raw.get("context_tokens", model["context_window"]), "context_tokens", 1_024,
                int(model["context_window"]),
            )
        else:
            try:
                context_tokens = int(raw.get("context_tokens", model["context_window"]))
            except (TypeError, ValueError):
                context_tokens = int(model["context_window"])
            context_tokens = min(int(model["context_window"]), max(1_024, context_tokens))
        effort = raw.get("reasoning_effort") or model.get("default_reasoning_effort")
        allowed_efforts = model.get("reasoning_efforts") or []
        if strict and effort and effort not in allowed_efforts:
            raise ValueError(f"reasoning_effort is not supported by model {model['id']}")
        if effort not in allowed_efforts:
            effort = model.get("default_reasoning_effort")
        return {
            "model_id": model["id"], "temperature": temperature,
            "context_tokens": context_tokens, "reasoning_effort": effort or None,
        }

    @staticmethod
    def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an integer") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"{field} must be between {minimum} and {maximum}")
        return number

    @staticmethod
    def _reasoning_efforts(value: Any) -> list[str]:
        if value in (None, ""):
            return []
        items = value.split(",") if isinstance(value, str) else value
        if not isinstance(items, (list, tuple)):
            raise ValueError("reasoning_efforts must be a list")
        allowed = {"minimal", "low", "medium", "high", "xhigh"}
        result = list(dict.fromkeys(str(item).strip().lower() for item in items if str(item).strip()))
        unknown = sorted(set(result) - allowed)
        if unknown:
            raise ValueError("unsupported reasoning efforts: " + ", ".join(unknown))
        return result

    @staticmethod
    def _reasoning_default(value: Any, efforts: list[str]) -> str | None:
        effort = str(value or "").strip().lower()
        if not effort:
            return efforts[0] if efforts else None
        if effort not in efforts:
            raise ValueError("default_reasoning_effort must be one of reasoning_efforts")
        return effort

    @staticmethod
    def _clean_text(value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field} is required")
        return text
