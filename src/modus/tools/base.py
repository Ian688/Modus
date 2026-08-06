from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from modus.config import ModusConfig

DangerLevel = Literal["safe", "medium", "high"]
DataDisclosure = Literal["none", "workspace_metadata", "workspace_content"]
ToolDecision = Literal["approve", "deny", "skip"]
ApprovalCallback = Callable[[dict[str, Any]], Awaitable[str] | str]

@dataclass(slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    display_summary: str | None = None
    tool_use_id: str | None = None
    # Small, non-secret facts for the Desktop timeline.  The model still
    # receives ``content`` as the canonical tool result; metadata only helps
    # the UI render an explicit operation/path/status without parsing prose.
    metadata: dict[str, Any] = field(default_factory=dict)
    # Full local result, never sent to the model, the event stream, or the
    # browser.  Handlers put oversized raw output here and expose only a
    # bounded ``model_payload`` (with the full text persisted as an artifact).
    raw_result: Any = None
    # What the current model sees for this tool result.  ``None`` keeps
    # backward compatibility: consumers fall back to ``content``.
    model_payload: str | None = None
    # Local artifacts that hold the full result (persisted under Modus's
    # private data directory).  Exposed to the UI by id only.
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    # Diagnostic details, not part of the model payload or the visible result.
    logs: list[str] = field(default_factory=list)
    # Data-flow facts for audit: local_bytes_read, model_bytes_sent,
    # raw_content_sent, redacted, etc.  Opaque to the model.
    disclosure: dict[str, Any] = field(default_factory=dict)

    def model_text(self) -> str:
        """Return the canonical text this tool result reveals to the model."""
        return self.model_payload if self.model_payload else self.content

@dataclass(slots=True)
class ToolContext:
    cwd: str
    config: ModusConfig
    approval_callback: ApprovalCallback | None = None
    # The runtime injects the active run token.  Handlers must fail before any
    # new side effect once it is set; an absent token keeps non-desktop callers
    # backward compatible.
    cancel_event: asyncio.Event | None = None
    # The owning conversation id, when the tool runs inside a persisted Desktop
    # session.  Absent for CLI/embedders; memory tools use it to target writes.
    session_id: str | None = None
    run_id: str | None = None
    # Explicit workspace root; used as the relative-path base.  ``None`` means
    # no workspace bound, so relative paths anchor to the home directory.
    workspace_root: str | None = None
    # Modus's own output directory (~/.modus/output).  Tools that produce
    # temporary files land here instead of the user's filesystem when no
    # workspace is bound.
    modus_output_dir: str | None = None

@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    danger_level: DangerLevel = "safe"
    requires_approval: bool = False
    # Describes what the tool result reveals to the current model. Workspace
    # binding alone never authorizes raw content disclosure.
    data_disclosure: DataDisclosure = "none"
    timeout: float = 60.0
    required_keys: list[str] = field(default_factory=list)

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(f'tool "{self.name}" input must be an object')
        for key in self.required_keys:
            if key not in payload:
                raise ValueError(f'tool "{self.name}" missing required input: {key}')
        return payload

    async def execute(self, payload: dict[str, Any], context: ToolContext) -> ToolResult:
        """Run a handler without putting human approval behind a 60-second timer.

        The executor asks for approval before it reaches this method.  Therefore
        tool timeouts must constrain actual I/O only; applying them around the
        whole approval path cancels a visible card while a person is reading it.
        """
        data = self.validate(payload)
        return await asyncio.wait_for(self.handler(data, context), timeout=self.timeout)

def object_schema(
    properties: dict[str, dict[str, Any]],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }
