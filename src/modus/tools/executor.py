from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from typing import Any

from modus.tools.base import Tool, ToolContext, ToolDecision, ToolResult
from modus.tools.registry import ToolRegistry

# execute_all 的并发调度逻辑。读操作并发，写操作顺序。
class ToolExecutor:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

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
            semaphore = asyncio.Semaphore(4)

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
            approval_error = await self._approval_error(tool, data, context, tool_call_id)
            if approval_error:
                return ToolResult(tool_use_id=tool_call_id, content=approval_error, is_error=True)
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
            result = await tool.execute(data, context)
            result.tool_use_id = tool_call_id
            return result
        except Exception as exc:
            return ToolResult(
                tool_use_id=tool_call_id,
                content=f'Tool "{name}" execution error: {exc}',
                is_error=True,
            )

    async def _approval_error(
        self,
        tool: Tool,
        payload: dict[str, Any],
        context: ToolContext,
        tool_call_id: str,
    ) -> str | None:
        """Return a fail-closed error string, or None when execution is allowed."""
        from modus.policy.approval import ApprovalDecision, ApprovalPolicy

        decision = ApprovalPolicy(context.config.policy).evaluate(tool)
        if decision is ApprovalDecision.ALLOW:
            return None
        if decision is ApprovalDecision.DENY:
            return f'Tool "{tool.name}" denied by approval policy.'
        if context.approval_callback is None:
            return f'Tool "{tool.name}" requires approval, but no approval callback is available.'
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
        }
        try:
            response = context.approval_callback(request)
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:
            return f'Tool "{tool.name}" approval failed closed: {exc}'
        if str(response).lower() not in {"approve", "allow"}:
            return f'Tool "{tool.name}" approval denied.'
        if time.time() >= expires_at:
            return f'Tool "{tool.name}" approval expired.'
        # Do not trust the transport/UI-facing object after it has crossed the
        # callback boundary.  A changed display copy is a mismatched approval.
        if _canonical_hash(request["input"]) != input_hash:
            return f'Tool "{tool.name}" approval denied: input changed after approval request.'
        return None


def _canonical_hash(payload: dict[str, Any]) -> str:
    """Return a stable SHA-256 binding for an already-validated tool payload."""
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

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
