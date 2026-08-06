"""Canonical serialization for tool results shared by every run loop.

``ToolResult`` carries distinct identities (raw local result, model payload,
artifacts, logs, disclosure facts).  This module is the single place that maps
one result to the event shape consumers see, so the ReAct loop, Peri workers,
and the desktop runners cannot drift apart on which fields reach the model,
the frontend, or audit.
"""
from __future__ import annotations

from typing import Any

from modus.tools.base import ToolResult


def tool_result_event(
    result: ToolResult,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the wire payload for a ``tool_result`` / ``subagent_tool_result``.

    ``result`` is always ``model_text()`` so the model and the frontend read
    the same bounded payload.  Optional fields are carried only when truthy:
    a result with no artifacts/disclosure stays a two-key dict, preserving the
    strict-equality contracts in the Peri subagent tests.  ``raw_result`` and
    ``logs`` never leave this boundary.
    """
    event: dict[str, Any] = {
        "result": result.model_text(),
        "is_error": result.is_error,
    }
    if result.display_summary:
        event["display_summary"] = result.display_summary
    if result.metadata:
        event["metadata"] = result.metadata
    if result.artifacts:
        event["artifacts"] = result.artifacts
    if result.disclosure:
        event["disclosure"] = result.disclosure
    if extra:
        event.update({key: value for key, value in extra.items() if value is not None})
    return event


def bounded_for_model(text: str, *, limit: int) -> tuple[str, dict[str, Any]]:
    """Bound oversized text for the model without inventing content.

    Returns the deterministic head/tail summary plus disclosure facts a caller
    can merge into ``ToolResult.disclosure``.
    """
    from modus.desktop.orchestration_ledger import bounded_summary

    if len(text) <= limit:
        return text, {"model_bytes_sent": len(text)}
    bounded = bounded_summary(text, limit=limit)
    return bounded, {
        "model_bytes_sent": len(bounded),
        "chars_omitted": len(text) - limit,
        "truncated": True,
    }


def artifact_ids_from_result(event: dict[str, Any]) -> tuple[str, ...]:
    """Collect artifact ids carried by an engine ``tool_result`` event.

    Runners pass these to ``emitter.emit(artifact_ids=...)`` so the frontend
    can link each tool row to its persisted full result without exposing the
    artifact array in the payload (which would break strict-equality contracts).
    """
    artifacts = event.get("artifacts") or []
    ids: list[str] = []
    for item in artifacts:
        if isinstance(item, dict):
            value = item.get("artifact_id")
            if value:
                ids.append(str(value))
    return tuple(ids)
