from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from modus.tools.base import Tool, ToolContext, ToolDecision, ToolResult
from modus.tools.registry import ToolRegistry

# execute_all 的并发调度逻辑。读操作并发，写操作顺序。
class ToolExecutor:
    def __init__(self, registry: ToolRegistry, *, max_concurrent_read: int = 4):
        self.registry = registry
        self.max_concurrent_read = max(1, int(max_concurrent_read or 1))

    async def execute_all(
        self,
        calls: list[dict[str, Any]],
        context: ToolContext,
    ) -> list[ToolResult]:
        read_calls: list[tuple[dict[str, Any], Tool]] = []
        sequential_calls: list[tuple[dict[str, Any], Tool | None]] = []

        for call in calls:
            name = _tool_call_name(call)
            tool = self.registry.get(name)
            if tool and tool.is_read_only and tool.is_concurrency_safe:
                read_calls.append((call, tool))
            else:
                sequential_calls.append((call, tool))

        results: list[ToolResult] = []
        if read_calls:
            semaphore = asyncio.Semaphore(self.max_concurrent_read)

            async def run_read(call: dict[str, Any], tool: Tool) -> ToolResult:
                async with semaphore:
                    return await self._execute_single(call, tool, context)

            results.extend(
                await asyncio.gather(*(run_read(call, tool) for call, tool in read_calls))
            )

        for call, tool in sequential_calls:
            results.append(await self._execute_single(call, tool, context))
        
        return results

    async def _execute_single(
        self,
        call: dict[str, Any],
        tool: Tool | None,
        context: ToolContext,
    ) -> ToolResult:
        tool_call_id = str(call.get("id") or "")
        name = _tool_call_name(call)
        payload = _tool_call_arguments(call)

        # This guard intentionally precedes approval. Once a user cancels a
        # run, neither a new approval card nor any new tool side effect may
        # start. Running handlers may finish their current atomic operation;
        # the executor prevents the next boundary from being crossed.
        if context.cancel_event is not None and context.cancel_event.is_set():
            return ToolResult(
                tool_use_id=tool_call_id,
                content=f'Run cancelled: tool "{name}" will not start.',
                is_error=True,
            )

        if not tool:
            return ToolResult(
                tool_use_id=tool_call_id,
                content=f'Tool "{name}" not found. Available tools: {", ".join(self.registry.list_names())}',
                is_error=True,
            )

        try:
            if "_modus_argument_error" in payload:
                return ToolResult(
                    tool_use_id=tool_call_id,
                    content=f'Tool "{name}" argument error: {payload["_modus_argument_error"]}',
                    is_error=True,
                )
            data = tool.validate(payload)
            # Deny-first capability gate.  This runs BEFORE approval: a tool
            # class the run is not granted is refused outright, regardless of
            # what a human might be asked next.  An explicit grant set is
            # fail-closed — undeclared capabilities count as denied.
            from modus.tools.capabilities import capabilities_granted

            if not capabilities_granted(tool.capabilities, context.granted_capabilities):
                missing = ", ".join(tool.capabilities or ["(undeclared)"])
                return ToolResult(
                    tool_use_id=tool_call_id,
                    content=(
                        f'Tool "{name}" denied: run is not granted capability '
                        f"{missing}."
                    ),
                    is_error=True,
                    metadata={"operation": "capability-denied"},
                )
            decision = await self._approval_decision(tool, data, context, tool_call_id)
            if decision.is_denied:
                # A2 deny re-injection (cc-haha #1051 lesson): a denial is not
                # an abort.  When a human denied, the model receives a
                # structured, non-error tool result so it sees the denial and
                # can change course instead of blindly retrying the same call.
                # Infrastructure/policy denials remain genuine errors.
                if decision.human:
                    return ToolResult.denied(
                        tool.name,
                        reason=decision.reason,
                        tool_use_id=tool_call_id,
                        suggestions=[
                            "modify the tool arguments and retry",
                            "use a different approach",
                            "request the needed permission",
                        ],
                    )
                return ToolResult(
                    tool_use_id=tool_call_id,
                    content=decision.error,
                    is_error=True,
                )
            if decision.is_skipped:
                # A skip is a fail-closed non-error: the tool does not run, but
                # the run continues as if the tool result were a no-op.  It now
                # tells the model *why* instead of a bare bracket marker.
                return ToolResult.skipped(
                    tool.name,
                    reason=decision.reason,
                    tool_use_id=tool_call_id,
                )
            # ``decision.payload`` is the (possibly user-modified) payload the
            # human approved.  It is re-validated before execution below.
            effective_payload = decision.payload if decision.payload is not None else data
            try:
                effective_payload = tool.validate(effective_payload)
            except Exception as exc:
                return ToolResult(
                    tool_use_id=tool_call_id,
                    content=f'Tool "{name}" approval-modified input invalid: {exc}',
                    is_error=True,
                )
            # An approval may complete at the same time as a user interruption.
            # Approval only authorizes this exact input; it never authorizes a
            # side effect after the owning run has been cancelled. Re-check at
            # the final execution boundary so no handler starts in that gap.
            if context.cancel_event is not None and context.cancel_event.is_set():
                return ToolResult(
                    tool_use_id=tool_call_id,
                    content=f'Run cancelled: tool "{name}" will not start.',
                    is_error=True,
                )
            # Capture a side-git pre-turn snapshot before the first mutating
            # tool of a run executes, so revert_turn has something to restore.
            # Best-effort: a snapshot failure never blocks the tool itself.
            if not tool.is_read_only and not context._snapshot_taken:
                self._capture_pre_turn_snapshot(context)
                context._snapshot_taken = True
            result = await tool.execute(effective_payload, context)
            result.tool_use_id = tool_call_id
            # A1/A2 grant recording.  Only the executor converts an advisory
            # "remember this" hint from the approval surface into a grant in the
            # session store, and only after the human approved the exact payload
            # that will be executed (for ``modify``, the re-validated edited
            # payload).  A per-resource remember never records an invocation
            # with no resource scope.
            if decision.remember or decision.remember_pattern:
                self._record_remember(
                    tool, effective_payload, decision, context, tool_call_id,
                )
            return result
        except Exception as exc:
            return ToolResult(
                tool_use_id=tool_call_id,
                content=f'Tool "{name}" execution error: {exc}',
                is_error=True,
            )

    def _record_remember(
        self,
        tool: Tool,
        effective_payload: dict[str, Any],
        decision: _ApprovalDecision,
        context: ToolContext,
        tool_call_id: str,
    ) -> None:
        """Persist a human "remember this" into the session grant store.

        ``modify`` records under the resource key of the *effective* payload
        the human approved, so a remember applies to the rewritten resource
        (A1 step 4: ``m`` records with the modified payload's resource_key).
        A ``remember_pattern`` writes an A2 command rule.  Fail-open on store
        absence (an embedder that never wired a store just skips the remember).
        """
        try:
            from modus.policy.approval import SessionGrantStore

            store = getattr(context, "grant_store", None)
            if not isinstance(store, SessionGrantStore):
                return
            pattern = decision.remember_pattern
            if pattern:
                store.add_rule(tool.name, pattern, "approve")
                return
            # ``remember_key`` is precomputed from the exact (possibly edited)
            # payload that was approved; fall back to recomputing for safety.
            key = decision.remember_key or _effective_resource_key(tool, effective_payload)
            if key:
                store.record_grant(tool.name, key, "approve")
        except Exception:
            # Remembering is advisory; never let it break the executed tool.
            return

    def _capture_pre_turn_snapshot(self, context: ToolContext) -> None:
        """Best-effort side-git snapshot before a run's first mutation.

        ``revert_turn`` restores from these.  A failure here is silent: the
        tool still runs, and the run simply has no earlier snapshot to restore.
        Captures only inside a real persisted run (a run_id is present); ad-hoc
        embedder/CLI/test calls without one skip the snapshot entirely.
        """
        if not context.run_id:
            return
        if not context.workspace_root and not context.cwd:
            return
        try:
            from modus.tools.snapshot import create_snapshot

            root = context.workspace_root or context.cwd
            create_snapshot(root, phase="pre-turn", summary="before run mutations")
        except Exception:
            return

    async def _approval_decision(
        self,
        tool: Tool,
        payload: dict[str, Any],
        context: ToolContext,
        tool_call_id: str,
    ) -> _ApprovalDecision:
        """Return the human-approval decision for one validated tool payload.

        Fail-closed: any unrecognized response, expiry, hash mismatch, or
        transport error denies execution.  ``modify`` returns the replacement
        payload to execute (re-validated by the caller); ``skip`` signals a
        no-op non-error.

        A1 scoping: ``resource_key`` scopes the approval to a concrete
        resource (rewritten command, URL origin, target path).  A remembered
        session grant for the same ``(tool, resource_key)`` is reused instead
        of re-asking.  A persisted rule (A2) may auto-allow or auto-deny a
        matching resource before the callback is ever consulted.
        """
        from modus.policy.approval import ApprovalDecision, ApprovalPolicy

        grant_store = getattr(context, "grant_store", None)
        resource_key = _effective_resource_key(tool, payload)

        policy = ApprovalPolicy(context.config.policy)
        decision = policy.scoped_decision(tool, resource_key, grant_store)
        if decision is ApprovalDecision.ALLOW:
            return _ApprovalDecision.allowed()
        if decision is ApprovalDecision.DENY:
            return _ApprovalDecision.denied(
                f'Tool "{tool.name}" denied by approval policy.'
            )
        if context.approval_callback is None:
            return _ApprovalDecision.denied(
                f'Tool "{tool.name}" requires approval, but no approval callback is available.'
            )
        input_hash = _canonical_hash(payload)
        expires_at = int(time.time()) + 600
        request = {
            "tool_name": tool.name,
            "tool_call_id": tool_call_id,
            "description": tool.description,
            # The approval UI receives an independent display copy; the executor
            # retains ``payload`` as the only object it will ever execute.
            "input": json.loads(json.dumps(payload, ensure_ascii=False)),
            "input_hash": input_hash,
            "approval_expires_at": expires_at,
            "danger_level": tool.danger_level,
            "requires_approval": tool.requires_approval,
            "data_disclosure": tool.data_disclosure,
            # Impact classification for the approval surface: a read-only tool is
            # a pure probe (no side effect), a mutating tool changes workspace
            # state, and a not_read_only-but-low-trust tool (e.g. a remote MCP
            # tool) is undetermined until its handler runs.  This is advisory UI
            # context for the human reviewer; enforcement stays in the guards.
            "impact_class": _impact_class(tool),
        }
        if resource_key:
            request["resource_key"] = resource_key
        try:
            response = context.approval_callback(request)
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:
            return _ApprovalDecision.denied(
                f'Tool "{tool.name}" approval failed closed: {exc}'
            )

        decision_str, modified_input, reason, remember, remember_pattern = _parse_approval_response(
            response, tool, payload,
        )
        if decision_str == "modify":
            if modified_input is None:
                return _ApprovalDecision.denied(
                    f'Tool "{tool.name}" approval modify missing replacement input.'
                )
            # A user-modified payload is a NEW execution contract.  It must be
            # re-hashed against the display copy it was returned with so a
            # callback that mutates between the display and the return is still
            # caught.  The executor re-validates the schema before running.
            if _canonical_hash(modified_input) != _canonical_hash(
                json.loads(json.dumps(modified_input, ensure_ascii=False))
            ):
                return _ApprovalDecision.denied(
                    f'Tool "{tool.name}" approval modify failed closed: input instability.'
                )
            remember_key = _effective_resource_key(tool, modified_input)
            return _ApprovalDecision.modified(
                modified_input,
                remember=remember,
                remember_key=remember_key,
                remember_pattern=remember_pattern,
            )
        if decision_str == "approve":
            if time.time() >= expires_at:
                return _ApprovalDecision.denied(f'Tool "{tool.name}" approval expired.')
            # Do not trust the transport/UI-facing object after it has crossed
            # the callback boundary.  A changed display copy is a mismatched
            # approval.
            if _canonical_hash(request["input"]) != input_hash:
                return _ApprovalDecision.denied(
                    f'Tool "{tool.name}" approval denied: input changed after approval request.'
                )
            return _ApprovalDecision.allowed(
                remember=remember,
                remember_key=resource_key,
                remember_pattern=remember_pattern,
            )
        if decision_str == "skip":
            return _ApprovalDecision.skipped(reason or "")
        # A plain deny (or unrecognized decision) from the callback is a human
        # decision: fail closed, but report it to the model as information.
        return _ApprovalDecision.human_denied(reason or "")


@dataclass(slots=True)
class _ApprovalDecision:
    """Internal approval outcome: allowed / denied / skipped / modified.

    ``remember`` / ``remember_key`` / ``remember_pattern`` carry the A1/A2
    persistence intent from the human approval surface to the executor, which
    is the only component allowed to write into the ``SessionGrantStore``.

    ``human`` distinguishes a denial the *human* made (A2: re-inject as a
    non-error tool result so the agent sees it and changes course) from an
    infrastructure/policy/integrity denial (no callback, policy block, expiry,
    hash mismatch — a genuine failure, reported as an error).
    """

    allow: bool = False
    deny: bool = False
    skip: bool = False
    human: bool = False
    payload: dict[str, Any] | None = None
    error: str = ""
    reason: str = ""
    remember: bool = False
    remember_key: str | None = None
    remember_pattern: str | None = None

    @classmethod
    def allowed(
        cls,
        *,
        remember: bool = False,
        remember_key: str | None = None,
        remember_pattern: str | None = None,
    ) -> "_ApprovalDecision":
        return cls(
            allow=True,
            remember=remember,
            remember_key=remember_key,
            remember_pattern=remember_pattern,
        )

    @classmethod
    def denied(cls, error: str, reason: str = "") -> "_ApprovalDecision":
        """Infrastructure / policy / integrity denial (reported as an error)."""
        return cls(deny=True, error=error, reason=reason)

    @classmethod
    def human_denied(cls, reason: str = "") -> "_ApprovalDecision":
        """Human denial — re-injected to the model as a non-error (A2)."""
        return cls(deny=True, human=True, error=f"denied by user approval", reason=reason)

    @classmethod
    def skipped(cls, reason: str = "") -> "_ApprovalDecision":
        return cls(skip=True, error=reason, reason=reason)

    @classmethod
    def modified(
        cls,
        payload: dict[str, Any],
        *,
        remember: bool = False,
        remember_key: str | None = None,
        remember_pattern: str | None = None,
    ) -> "_ApprovalDecision":
        return cls(
            allow=True,
            payload=payload,
            remember=remember,
            remember_key=remember_key,
            remember_pattern=remember_pattern,
        )

    @property
    def is_allowed(self) -> bool:
        return self.allow

    @property
    def is_denied(self) -> bool:
        return self.deny

    @property
    def is_skipped(self) -> bool:
        return self.skip


def _impact_class(tool: Tool) -> str:
    """Classify a tool's expected side effects for the approval surface.

    ``read-only`` is a pure probe with no workspace side effect; anything else
    is ``mutating`` (writes, execution, credential changes).  This is
    human-review context only — the capability gate, CommandGuard and PathGuard
    remain the actual enforcement.
    """
    return "read-only" if tool.is_read_only else "mutating"


def _canonical_hash(payload: dict[str, Any]) -> str:
    """Return a stable SHA-256 binding for an already-validated tool payload."""
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ── A1 resource-key helpers ────────────────────────────────────────────────
#
# The design doc wires ``permission_hint`` on the shared builtin Tool
# declarations (tools/builtins.py).  Until that lands, the executor drives the
# same scoping from tool metadata via ``_default_permission_hint``, so scoped
# approval works for the flagged tools today and the hints are simply migrated
# onto the Tool objects later.


def _effective_resource_key(tool: Tool, payload: dict[str, Any]) -> str | None:
    """Resource key for a user-*modified* payload (``m`` decision).

    Prefers an explicit ``permission_hint`` when the Tool has one; otherwise
    falls back to the metadata-driven default extractor so a modified command
    is remembered under its rewritten resource, not the original one.
    """
    return tool.resource_key(payload) or _default_resource_key(tool, payload)


def _default_permission_hint(tool: Tool) -> str | None:
    """Best-effort default resource scope for tools without a permission_hint.

    Mirrors the metadata the builtins already declare: command-based exec tools
    scope by the rewritten command, fetch tools by URL origin, path tools by
    target path, git fetch/pull/push by the remote name.  Returns None for
    anything it cannot classify confidently — None means "no finer scope,
    stays per-tool".
    """
    name = tool.name
    if name in {"bash", "run_tests"}:
        return "command"
    if name in {"web_fetch", "browser_navigate"}:
        return "origin"
    if name in {"git_fetch", "git_pull", "git_push"}:
        return "remote"
    if name in {"write_file", "edit_file", "office_exec"}:
        return "path"
    return None


def _default_resource_key(tool: Tool, payload: dict[str, Any]) -> str | None:
    """Extract a resource key from a payload for the flagged builtins.

    ``command`` tools use the rewritten command (approving ``cat a`` never
    reuses for ``rm -rf``).  Fetch tools use the URL origin (approving one host
    never reuses for an intranet address).  Git write tools use the remote
    name.  Path tools use the resolved target path.
    """
    kind = _default_permission_hint(tool)
    if kind == "command":
        command = str(payload.get("command") or "").strip()
        return command or None
    if kind == "remote":
        remote = str(payload.get("remote") or "").strip()
        return remote or None
    if kind == "origin":
        url = str(payload.get("url") or "").strip()
        if not url:
            return None
        try:
            parsed = urlparse(url)
        except ValueError:
            return url
        netloc = parsed.netloc or parsed.path
        return netloc or url
    if kind == "path":
        path = str(payload.get("path") or payload.get("file_path") or "").strip()
        return path or None
    return None


def _parse_approval_response(
    response: Any,
    tool: Any,
    original_payload: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, str, bool, str | None]:
    """Normalize a callback response into (decision, modified_input, reason,
    remember, remember_pattern).

    Accepts the legacy plain string (``approve``/``allow``/``deny``) and the
    structured ``ApprovalResponse`` (approve/deny/skip/modify).  Anything else
    is treated as deny (fail closed).  ``modify`` carries the replacement
    payload as-is; the executor re-validates and re-hashes it before running.
    ``remember`` / ``remember_pattern`` are advisory hints from the human
    surface; only the executor may convert them into grants.
    """
    from modus.tools.base import ApprovalResponse

    if isinstance(response, ApprovalResponse):
        decision = str(response.decision or "").lower()
        remember = bool(response.remember)
        remember_pattern = response.remember_pattern or None
        if decision == "approve":
            return "approve", None, "", remember, remember_pattern
        if decision == "modify":
            modified = response.modified_input
            if not isinstance(modified, dict) or not modified:
                return "deny", None, "", False, None
            return "modify", dict(modified), str(response.reason or ""), remember, remember_pattern
        if decision == "skip":
            return "skip", None, str(response.reason or ""), False, None
        # deny and everything else
        return "deny", None, str(response.reason or ""), False, None
    if isinstance(response, str):
        normalized = response.strip().lower()
        if normalized in {"approve", "allow"}:
            return "approve", None, "", False, None
        if normalized in {"skip", "skipped"}:
            return "skip", None, "", False, None
        return "deny", None, "", False, None
    return "deny", None, "", False, None

def _tool_call_name(call: dict[str, Any]) -> str:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(function.get("name") or call.get("name") or "")

def _tool_call_arguments(call: dict[str, Any]) -> dict[str, Any]:
    """Parse a completed tool call without turning malformed arguments into input.

    OpenAI-compatible providers stream ``arguments`` in fragments.  A provider
    can still terminate an otherwise valid tool call with truncated JSON; that
    must be surfaced as a call-format error, not passed to a handler as a
    plausible-looking dictionary.  Common file-path aliases are normalized at
    this single boundary so every tool consumer gets the same contract.
    """
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    arguments = function.get("arguments", call.get("arguments", {}))
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            return {
                "_modus_argument_error": "invalid_json",
                "_modus_raw_arguments": arguments,
                "_modus_parse_detail": exc.msg,
            }
    else:
        parsed = arguments

    if not isinstance(parsed, dict):
        return {"_modus_argument_error": "not_an_object"}

    # Some providers/models learned file_path or file from other tool schemas.
    # Preserve an explicit path if both were supplied.
    normalized = dict(parsed)
    if "path" not in normalized:
        for alias in ("file_path", "file"):
            if alias in normalized:
                normalized["path"] = normalized.pop(alias)
                break
    return normalized
