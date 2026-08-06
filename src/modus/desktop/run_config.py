"""Browser-safe, immutable configuration records for Desktop Agent runs."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from modus.modes import normalize_mode


RUN_CONFIG_SCHEMA = "modus.run-config.v1"

_ROLE_FIELDS = (
    "model_id", "temperature", "context_tokens", "reasoning_effort",
)
_MODEL_FIELDS = ("name", "provider", "model")


def _positive_number(value: Any, default: int | float) -> int | float:
    if isinstance(default, int):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default
    try:
        parsed_float = float(value)
    except (TypeError, ValueError):
        return default
    return parsed_float if parsed_float > 0 else default


def build_run_config_snapshot(
    *,
    mode: str,
    host_model_id: str = "",
    reasoning_effort: str | None = None,
    roles: Mapping[str, Any] | None = None,
    models: Iterable[Mapping[str, Any]] = (),
    budget: Mapping[str, Any] | None = None,
    verification_required: bool = False,
    has_custom_system_prompt: bool = False,
) -> dict[str, Any]:
    """Build a deliberately small snapshot without credentials or prompt text.

    Callers pass only public model metadata. Role records are copied through a
    strict allowlist so a credential-bearing runtime model can never be stored
    accidentally if it reaches this boundary in the future.
    """
    model_by_id = {
        str(item.get("id") or ""): item
        for item in models
        if isinstance(item, Mapping) and str(item.get("id") or "")
    }
    normalized_roles: dict[str, dict[str, Any]] = {}
    for role, raw in (roles or {}).items():
        if not isinstance(raw, Mapping):
            continue
        record = {
            field: raw[field]
            for field in _ROLE_FIELDS
            if field in raw and raw[field] not in (None, "")
        }
        model_id = str(record.get("model_id") or "")
        public_model = model_by_id.get(model_id)
        if public_model is not None:
            record.update({
                field: public_model[field]
                for field in _MODEL_FIELDS
                if public_model.get(field) not in (None, "")
            })
        if record:
            normalized_roles[str(role)] = record

    host_id = str(host_model_id or "")
    if "host" not in normalized_roles and host_id:
        normalized_roles["host"] = {"model_id": host_id}
        public_host = model_by_id.get(host_id)
        if public_host is not None:
            normalized_roles["host"].update({
                field: public_host[field]
                for field in _MODEL_FIELDS
                if public_host.get(field) not in (None, "")
            })
    if not host_id:
        host_id = str(normalized_roles.get("host", {}).get("model_id") or "")

    budget_data = budget or {}
    max_attempts = int(_positive_number(
        budget_data.get("max_verification_attempts"), 3,
    ))
    return {
        "schema": RUN_CONFIG_SCHEMA,
        "mode": normalize_mode(mode),
        "host_model_id": host_id,
        "reasoning_effort": str(reasoning_effort or "") or None,
        "roles": normalized_roles,
        "budget": {
            "max_turns": int(_positive_number(budget_data.get("max_turns"), 20)),
            "max_tokens": int(_positive_number(budget_data.get("max_tokens"), 200_000)),
            "max_wall_seconds": float(_positive_number(
                budget_data.get("max_wall_seconds"), 600.0,
            )),
        },
        "verification": {
            "required": bool(verification_required),
            "max_attempts": max_attempts,
        },
        "prompt": {"custom": bool(has_custom_system_prompt)},
    }
