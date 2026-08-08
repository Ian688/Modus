from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from modus.config import ModusConfig

DangerLevel = Literal["safe", "medium", "high"]
DataDisclosure = Literal["none", "workspace_metadata", "workspace_content"]
ToolDecision = Literal["approve", "deny", "skip", "modify"]

# Literal mirror of tools.capabilities.Capability — kept here so Tool
# declarations read as plain strings without importing the enum.
CapabilityClass = Literal["filesystem", "exec", "network", "memory", "agent"]

# An approval callback may return a plain decision string, or a structured
# ApprovalResponse.  ``Any`` keeps the forward reference to ApprovalResponse
# from being evaluated at module load (it is defined below).
ApprovalCallback = Callable[[dict[str, Any]], Awaitable[Any] | Any]


@dataclass(slots=True)
class ApprovalResponse:
    """Structured human-approval decision returned by an approval callback.

    ``decision`` is one of approve/deny/skip/modify.  ``modified_input`` is
    required for modify: the exact replacement payload the executor will
    re-validate and re-hash before execution.  deny/skip may carry a ``reason``.

    Grant-scoped fields (``remember`` / ``pattern``) are only ever advisory
    hints from the human side of the approval surface.  The executor retains
    the sole authority to persist a grant into the ``SessionGrantStore`` after
    it has recomputed the resource key from the *effective* (possibly
    user-modified) payload, so a remember can never out-scope the exact payload
    that was approved.
    """

    decision: ToolDecision
    modified_input: dict[str, Any] | None = None
    reason: str = ""
    # True when the human asked to remember this resource (per-resource
    # session grant).  ``remember_pattern`` applies when the remember is a
    # command-pattern rule (per-tool "remember the rule").
    remember: bool = False
    remember_pattern: str | None = None

    @classmethod
    def approve(
        cls,
        *,
        remember: bool = False,
        remember_pattern: str | None = None,
    ) -> "ApprovalResponse":
        # A command-pattern rule is itself a remember intent: recording it IS
        # remembering.  ``remember=True`` guarantees the executor persists it.
        if remember_pattern:
            remember = True
        return cls(
            "approve",
            remember=remember,
            remember_pattern=remember_pattern,
        )

    @classmethod
    def deny(cls, reason: str = "") -> "ApprovalResponse":
        return cls("deny", reason=reason)

    @classmethod
    def skip(cls, reason: str = "") -> "ApprovalResponse":
        return cls("skip", reason=reason)

    @classmethod
    def modify(
        cls,
        modified_input: dict[str, Any],
        *,
        remember: bool = False,
        remember_pattern: str | None = None,
    ) -> "ApprovalResponse":
        return cls(
            "modify",
            modified_input=modified_input,
            remember=remember,
            remember_pattern=remember_pattern,
        )

    def __eq__(self, other: object) -> bool:
        """An ApprovalResponse compares equal to its plain decision string.

        Legacy callers compare callback results to ``"approve"``/``"deny"``/
        ``"skip"``; this keeps those comparisons working while still carrying
        the optional reason / remember payloads.
        """
        if isinstance(other, str):
            return self.decision == other
        if isinstance(other, ApprovalResponse):
            return (
                self.decision == other.decision
                and self.modified_input == other.modified_input
                and self.reason == other.reason
                and self.remember == other.remember
                and self.remember_pattern == other.remember_pattern
            )
        return NotImplemented

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

    @classmethod
    def denied(
        cls,
        tool_name: str,
        reason: str = "",
        *,
        tool_use_id: str | None = None,
        suggestions: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        """Structured denial result for A2 deny re-injection.

        ``is_error=False``: a denial is information for the agent to change
        course (cc-haha #1051 lesson), never a turn-aborting failure.  The
        content is a self-contained block the model reads as a tool result.
        """
        lines = [
            f"[Tool {tool_name} was NOT executed — approval denied by the user]",
        ]
        if reason:
            lines.append(f"reason: {reason}")
        if suggestions:
            lines.append(f"suggestions: {'; '.join(suggestions)}")
        meta = dict(metadata or {})
        meta["operation"] = "approval-denied"
        meta["approved"] = False
        return cls(
            content="\n".join(lines),
            is_error=False,
            display_summary=f"{tool_name} denied",
            tool_use_id=tool_use_id,
            metadata=meta,
        )

    @classmethod
    def skipped(
        cls,
        tool_name: str,
        reason: str = "",
        *,
        tool_use_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        """Fail-closed non-error skip result (A2).

        A skip keeps today's semantics — the tool does not run and the run
        continues as if the result were a no-op — but surfaces *why* to the
        model instead of a bare bracket marker, so the agent is not tempted to
        retry the identical call.
        """
        lines = [
            f"[Tool skipped by user approval] {tool_name} was NOT executed",
        ]
        if reason:
            lines.append(f"reason: {reason}")
        meta = dict(metadata or {})
        meta["operation"] = "skipped"
        meta["approved"] = False
        return cls(
            content="\n".join(lines),
            is_error=False,
            display_summary=f"{tool_name} skipped",
            tool_use_id=tool_use_id,
            metadata=meta,
        )

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
    # Set by the executor the first time a mutating tool is about to run, after
    # a side-git pre-turn snapshot has been captured for this run.  Guards
    # against capturing the same snapshot repeatedly across tool calls.
    _snapshot_taken: bool = False
    # Capability grant set for the active run.  ``None`` (the default) grants
    # every declared capability — today's unrestricted behavior.  An explicit
    # set (e.g. ``["filesystem"]`` for a read-only lens run) makes the executor
    # deny every tool that declares a capability outside the set, before
    # approval.  Empty list means "deny everything declared"; tools with no
    # declared capabilities are also denied under any explicit set.
    granted_capabilities: list[str] | None = None
    # Session-scoped approval grant store (A1 scoped cache + A2 rule memory).
    # ``None`` keeps today's behavior: every ASK goes to the callback and no
    # per-resource reuse happens.  The executor lazily creates a store when a
    # human explicitly asks to remember a resource.
    grant_store: Any = None

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
    # Capability classes this tool exercises.  The executor denies a tool under
    # an explicit capability grant unless every declared class is granted.  An
    # empty list means "no declared capability" — fine when no lockdown grant is
    # active (``granted_capabilities=None`` grants everything), denied under one.
    capabilities: tuple[CapabilityClass, ...] = ()
    timeout: float = 60.0
    required_keys: list[str] = field(default_factory=list)
    # Optional resource-key extractor for scoped approval (A1).  When set, the
    # executor scopes approvals to the returned key (e.g. a rewritten command,
    # a URL origin, a target path).  ``None`` means the tool has no finer
    # resource scope and keeps per-tool semantics.
    permission_hint: Callable[[dict[str, Any]], str | None] | None = None

    def resource_key(self, payload: dict[str, Any]) -> str | None:
        """Return this tool's approval resource key for a validated payload.

        ``None`` (no hint, or the hint itself returned None) means the approval
        is not resource-scoped: it stays per-tool.
        """
        if self.permission_hint is None:
            return None
        try:
            key = self.permission_hint(payload)
        except Exception:
            return None
        return key if isinstance(key, str) and key else None

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
