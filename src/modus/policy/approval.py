"""Pure approval decisions shared by CLI, desktop, and all run modes."""
from __future__ import annotations

from enum import StrEnum

from modus.config import PolicyConfig
from modus.tools.base import Tool


class ApprovalDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ApprovalPolicy:
    """Fail-closed mapping from configured policy and tool metadata to a decision."""

    def __init__(self, config: PolicyConfig) -> None:
        self._config = config

    def evaluate(self, tool: Tool, session_decision: str | None = None) -> ApprovalDecision:
        if session_decision is not None:
            try:
                return ApprovalDecision(session_decision)
            except ValueError:
                return ApprovalDecision.DENY

        hitl_mode = self._config.hitl_mode
        if hitl_mode == "always":
            return ApprovalDecision.ASK
        if hitl_mode != "auto":
            return ApprovalDecision.DENY

        if tool.danger_level not in {"safe", "medium", "high"}:
            return ApprovalDecision.DENY
        if tool.danger_level == "safe" and tool.is_read_only and not tool.requires_approval:
            return ApprovalDecision.ALLOW
        return ApprovalDecision.ASK
