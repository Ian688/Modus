"""Pure approval decisions shared by CLI, desktop, and all run modes.

A1 scope dimension (PentesterFlow cacheKey port):
    ``ApprovalPolicy.scoped_decision`` adds a resource scope to the three-state
    decision.  Human-approved resources are remembered per session in a
    ``SessionGrantStore`` (in-memory + optional persistence), so approving
    ``cat a`` never reuses as approval for ``rm -rf``.  Scope is a shrinking
    lens: grants never expand an effective permission, they only cache a
    permission the policy already granted to this exact resource.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from modus.config import PolicyConfig
from modus.tools.base import Tool


class ApprovalDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ApprovalScope(StrEnum):
    """Scope granularity of an approval decision.

    ``per-invocation`` is the default for ASK: the human approved this exact
    payload and nothing is silently reused.  ``per-resource`` means a session
    grant may be reused while it matches the same ``(tool, resource_key)``.
    ``per-tool`` is the legacy unscooped behaviour used when a tool has no
    permission hint.
    """

    PER_INVOCATION = "per-invocation"
    PER_RESOURCE = "per-resource"
    PER_TOOL = "per-tool"

    @property
    def cacheable(self) -> bool:
        """Whether this scope permits session-level reuse of a grant."""
        return self is ApprovalScope.PER_RESOURCE


@dataclass(slots=True)
class SessionGrant:
    """One remembered human approval for ``(tool, resource_key)``."""

    tool: str
    resource_key: str
    decision: str
    created_at: float
    pattern: str | None = None  # set when the grant came from a command rule

    def audit_scope(self) -> str:
        """The approval scope level this grant encodes (A1 audit dimension).

        A per-resource session grant is ``per-resource``; a rule-based grant
        (a remembered command pattern) is recorded under ``per-resource`` too,
        because it still only ever reuses for the exact matching resource —
        never a broader tool.  The audit log stores this so a replay can show
        the scope under which a remembered approval was reused.
        """
        return ApprovalScope.PER_RESOURCE.value


@dataclass
class SessionGrantStore:
    """Session-scoped approval memory (A1 + A2 rule memory).

    ``grants`` caches per-resource approvals so an identical resource is not
    re-asked within the session.  ``rule_grants`` is rule memory: an explicit
    command-pattern rule that lets matching future invocations skip the prompt.
    Both are in-memory; ``_persist`` optionally writes the rules to a JSONL
    file so they survive process restarts (``test_rule_grants_persist``).
    """

    grants: dict[tuple[str, str], SessionGrant] = field(default_factory=dict)
    rule_grants: dict[str, str] = field(default_factory=dict)
    # (tool, pattern) -> decision for explicit command rules (A2 rule memory).
    _rules: dict[tuple[str, str], str] = field(default_factory=dict)
    persist_path: str | None = None
    # High-risk classes that must never be silently reused from a session grant
    # (noSessionCache in the PentesterFlow port): SSRF-prone network fetches and
    # sensitive-path exec (office script) stay per-invocation.  ``bash`` is
    # deliberately NOT here: its scoping is per-command, and the A1 acceptance
    # explicitly allows remembering one approved command and reusing exactly
    # that command within the session.
    _no_session_cache: set[str] = field(
        default_factory=lambda: {"web_fetch", "office_exec", "spawn_process"}
    )

    # ── per-resource grants (A1) ──

    def record_grant(self, tool: str, resource_key: str, decision: str, *, pattern: str | None = None) -> None:
        """Remember an approval for one (tool, resource_key)."""
        if not resource_key:
            return
        self.grants[(tool, resource_key)] = SessionGrant(
            tool=tool,
            resource_key=resource_key,
            decision=decision,
            created_at=float(__import__("time").time()),
            pattern=pattern,
        )

    def lookup(self, tool: str, resource_key: str) -> SessionGrant | None:
        """Return a remembered grant for (tool, resource_key), if any."""
        if not resource_key:
            return None
        if tool in self._no_session_cache:
            return None
        return self.grants.get((tool, resource_key))

    def clear_grants(self) -> None:
        self.grants.clear()

    # ── rule memory (A2) ──

    def add_rule(self, tool: str, pattern: str, decision: str) -> None:
        """Persist an explicit (tool, pattern) approval rule."""
        if not tool or not pattern:
            return
        self._rules[(tool, pattern)] = decision
        self.rule_grants[f"{tool}:{pattern}"] = decision
        self._persist()

    def rule_for(self, tool: str, resource_key: str | None) -> str | None:
        """Return the decision of the first rule whose pattern matches.

        ``resource_key`` may be None for tools without a permission hint; only
        an explicit per-tool rule (pattern ``*``) then applies.  Rules are
        explicit human-created consent (A2 rule memory), so unlike the implicit
        session cache they may apply to any tool — including high-risk exec
        tools the human explicitly chose a rule for.
        """
        for (rule_tool, pattern), decision in self._rules.items():
            if rule_tool != tool:
                continue
            if pattern == "*" or (
                resource_key is not None and _rule_matches(pattern, resource_key)
            ):
                return decision
        return None

    def rule_patterns(self) -> list[tuple[str, str, str]]:
        return [(tool, pattern, decision) for (tool, pattern), decision in self._rules.items()]

    def load_rules(self) -> None:
        """Reload persisted rule memory (A2 restart persistence)."""
        if not self.persist_path:
            return
        try:
            path = Path(self.persist_path).expanduser()
            if not path.exists():
                return
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tool = str(event.get("tool") or "")
                pattern = str(event.get("pattern") or "")
                decision = str(event.get("decision") or "")
                if tool and pattern and decision:
                    self._rules[(tool, pattern)] = decision
                    self.rule_grants[f"{tool}:{pattern}"] = decision
        except OSError:
            return

    def _persist(self) -> None:
        if not self.persist_path:
            return
        try:
            path = Path(self.persist_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                for (tool, pattern), decision in self._rules.items():
                    handle.write(
                        json.dumps(
                            {"tool": tool, "pattern": pattern, "decision": decision},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        except OSError:
            return


def _rule_matches(pattern: str, resource_key: str) -> bool:
    """Match a stored command pattern against a resource key.

    A pattern is a full-tool pattern like ``"cat *"``.  It matches when a
    token-by-token shell split of the key aligns with the pattern tokens,
    ``*`` matching any single token.  Prefix/space splitting is deliberate:
    ``"cat a"`` and ``"cat  a"`` (extra spaces) are treated as the same
    resource, while ``"cat a; rm -rf /"`` is a distinct key.  An exact string
    match always wins (the common single-command case).
    """
    if pattern == resource_key:
        return True
    pat_tokens = pattern.split()
    key_tokens = resource_key.split()
    if len(pat_tokens) != len(key_tokens):
        return False
    return all(p == "*" or p == k for p, k in zip(pat_tokens, key_tokens))


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

    def scoped_decision(
        self,
        tool: Tool,
        resource_key: str | None,
        session_grants: SessionGrantStore | None = None,
    ) -> ApprovalDecision:
        """Evaluate a tool in its resource scope (A1).

        A session grant may elevate ASK to ALLOW *only* when the tool has a
        per-resource scope, the exact resource key is remembered, and no
        deny rule overrides it.  This never enlarges effective permissions:
        an APPROVED resource is reused, a DENY rule always wins, and
        high-risk no-session-cache tools never reuse.  The raw policy decision
        (``evaluate``) remains authoritative for everything else.
        """
        base = self.evaluate(tool)

        if session_grants is not None:
            rule_decision = session_grants.rule_for(tool.name, resource_key)
            if rule_decision == "deny":
                return ApprovalDecision.DENY
            if rule_decision == "allow":
                # A persistent rule may only ever turn an ASK into ALLOW; it
                # never grants something the policy would already approve of.
                if base is ApprovalDecision.ASK:
                    return ApprovalDecision.ALLOW

        if (
            base is ApprovalDecision.ASK
            and session_grants is not None
            and resource_key
        ):
            grant = session_grants.lookup(tool.name, resource_key)
            if grant is not None and grant.decision == "approve":
                return ApprovalDecision.ALLOW
        return base
