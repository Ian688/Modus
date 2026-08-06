"""Model capability negotiation.

Capabilities are the handshake a future AGI can advertise and the runtime can
query.  Today they derive from the model repository's static metadata plus
client probing; a future AGI registers richer capabilities through the same
``can()`` surface.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any


class Capability(StrEnum):
    TOOLS = "tools"
    IMAGES = "images"
    REASONING = "reasoning"
    EMBEDDINGS = "embeddings"
    STRUCTURED_OUTPUT = "structured_output"


class ModelCapabilities:
    """Resolved capability set for one model/client."""

    def __init__(
        self,
        *,
        supports_tools: bool = False,
        supports_images: bool = False,
        reasoning_effort: str | None = None,
        supports_embeddings: bool = False,
        supports_structured_output: bool = False,
    ):
        self._flags = {
            Capability.TOOLS: supports_tools,
            Capability.IMAGES: supports_images,
            Capability.REASONING: bool(reasoning_effort),
            Capability.EMBEDDINGS: supports_embeddings,
            Capability.STRUCTURED_OUTPUT: supports_structured_output,
        }

    def can(self, capability: Capability) -> bool:
        return bool(self._flags.get(capability, False))

    def advertised(self) -> list[str]:
        return [cap.value for cap, supported in self._flags.items() if supported]


def resolve_capabilities(
    *,
    llm_config: Any = None,
    client: Any = None,
    model_record: dict[str, Any] | None = None,
) -> ModelCapabilities:
    """Resolve capabilities from a model record and/or a live client.

    The model record is authoritative for declared capability metadata; the
    client is probed for things like embeddings that are duck-typed methods.
    """
    record = model_record or {}
    supports_tools = bool(record.get("supports_tools", False))
    supports_images = bool(record.get("supports_images", False))
    reasoning_effort = record.get("reasoning_effort") or (
        getattr(llm_config, "reasoning_effort", None) if llm_config else None
    )
    supports_embeddings = client is not None and hasattr(client, "embed")
    supports_structured = bool(record.get("supports_structured_output", False))
    return ModelCapabilities(
        supports_tools=supports_tools,
        supports_images=supports_images,
        reasoning_effort=reasoning_effort,
        supports_embeddings=supports_embeddings,
        supports_structured_output=supports_structured,
    )
