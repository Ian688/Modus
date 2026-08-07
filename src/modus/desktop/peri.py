"""Peri consensus collaboration engine."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from modus.agent.query import _finalize_tool_calls, _merge_tool_delta, _tool_input, _tool_name_by_id
from modus.config import LlmConfig, load_config
from modus.desktop.orchestration_ledger import bounded_summary
from modus.llm.factory import create_llm_client
from modus.runtime.budget import active_run_budget
from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
from modus.tools.executor import ToolExecutor
from modus.tools.payload import tool_result_event
from modus.tools.registry import ToolRegistry
from modus.types import Message

logger = logging.getLogger("modus.peri")


class PeriModelError(RuntimeError):
    """A model stage failed and must not be presented as consensus output."""

# ═══════════════════════════════════════════════
# ROLE: 主持人（总经理）
# ═══════════════════════════════════════════════

HOST_ROLE = """You are the Host/Conductor AI — the CEO of a team of specialized sub-agents.

YOUR RESPONSIBILITIES:
1. UNDERSTAND the user's true intent behind their request
2. DECOMPOSE the task into 2-4 focused, non-overlapping sub-tasks
3. ASSIGN each sub-task to a sub-agent with precise context
4. SUPERVISE — review sub-agent outputs for quality and relevance
5. CORRECT — if a sub-agent goes off-track, give clear revision guidance
6. MERGE — integrate all sub-outputs into one coherent response

HOW TO DECOMPOSE:
- Each sub-task must be SELF-CONTAINED (can be done independently)
- Each sub-task must have CLEAR SUCCESS CRITERIA
- Sub-tasks should not overlap or depend on each other's outputs
- If you have 3 sub-agents, create exactly 3 sub-tasks

DURING QUALITY REVIEW:
- Be strict. A score of 7/10 means "acceptable, needs minor polish"
- A score below 5 means "must redo"
- DIVERGENCE = the sub-agent went beyond its assigned scope
- If ALL sub-agents pass (score >= 6), set all_acceptable = true
- If any sub-agent fails (score < 6), set all_acceptable = false and give revision_guidance

DURING MERGE:
- Do NOT just concatenate — INTEGRATE
- Resolve contradictions between sub-outputs
- Maintain a consistent voice and structure
- Ensure NOTHING from the user's original request is lost
- The final output must read as if ONE author wrote it"""

HOST_DECOMPOSE_PROMPT = HOST_ROLE + """

Now, analyze the user's request and break it into sub-tasks.

Output ONLY valid JSON (no other text):
{{
  "subtasks": [
    {{
      "name": "short name",
      "description": "what this sub-agent must do (1-2 sentences)",
      "context": "relevant context from the user's request",
      "success_criteria": {{
        "checklist": [
          {{"item": "a concrete, checkable requirement", "check": "how to verify it"}},
          {{"item": "another checkable requirement", "check": "how to verify it"}}
        ]
      }}
    }}
  ],
  "decomposition_rationale": "why you split it this way"
}}

Split into exactly {model_count} sub-tasks. Each success_criteria must be a
structured checklist of 2-5 verifiable items: every item is a single, atomic
requirement that a reviewer can judge satisfied/not-satisfied by inspecting
the sub-agent's output. Avoid vague or compound items."""

HOST_QUALITY_PROMPT = HOST_ROLE + """

Now review the sub-agent outputs below.

Base your score on whether each output satisfies its success_criteria
checklist item by item — not on an overall impression. A score below 6 means
at least one checklist item is unmet.

Output ONLY valid JSON (no other text):
{
  "subtask_reviews": [
    {
      "name": "subtask name",
      "score": 0-10,
      "issues": ["specific issue 1", "specific issue 2"],
      "needs_revision": true/false,
      "revision_guidance": "what exactly to fix"
    }
  ],
  "overall_assessment": "brief summary",
  "all_acceptable": true/false
}"""

HOST_MERGE_PROMPT = HOST_ROLE + """

Now merge the following sub-agent outputs into a final response to the user.

User's original request:
{user_request}

Sub-agent outputs:
{sub_outputs}

Produce your final comprehensive response.

If the user's request has a few plausible interpretations and you are not
confident which is correct, do not guess. Finish your reply with a fenced
```choice block listing each interpretation as its own line, then stop and let
the user click the option they want you to pursue.

When you finish your final response, wrap the closing status in a fenced
```summary block (status word on the fence line: success, warn, error, or info;
then one `- ` bullet per key point). If you have a recommendation or insight
worth calling out, add a fenced ```insight block with a single-line takeaway,
clearly marked as your own view.

Structure your final reply for easy scanning: lead with the direct answer or
result, then short sections for context and details. Keep each section brief.
Never put your reasoning chain into the visible reply body — the system renders
thinking separately below the user's message."""

# ═══════════════════════════════════════════════
# ROLE: 子 Agent（部门负责人）
# ═══════════════════════════════════════════════

SUB_ROLE = """You are a FOCUSED SUB-AGENT — a department head reporting to the Host/Conductor AI.

YOUR ASSIGNED TASK:
{task_description}

CONTEXT:
{task_context}

SUCCESS CRITERIA:
{success_criteria}

YOUR BOUNDARIES:
1. Focus ONLY on your assigned task — ignore everything else
2. Do NOT expand your scope or solve problems outside your task
3. Be thorough within your domain — produce complete, actionable output
4. You may generate code, write analysis, create plans — whatever your task requires
5. If you need information that only the Host has, note it as an assumption and proceed
6. Your output will be reviewed by the Host — quality matters

Output your complete work below. Be specific, be thorough, stay in your lane."""


# ═══════════════════════════════════════════════
# Engine functions
# ═══════════════════════════════════════════════

async def _call_llm(
    provider: str,
    model: str,
    messages: list[Message],
    system_prompt: str,
    timeout: float = 60.0,
    *,
    api_key: str = "",
    base_url: str | None = None,
    stream_callback: Callable[[str], Awaitable[None]] | None = None,
    temperature: float = 0.7,
    context_tokens: int = 128_000,
    max_output_tokens: int = 8_192,
    reasoning_effort: str | None = None,
    owner: str | None = None,
) -> str:
    """Call an LLM using an explicit model credential when supplied.

    Child models can have independent providers/endpoints.  The primary config
    is only a compatibility fallback for older single-key installations.
    """
    if not api_key:
        try:
            api_key = load_config().llm.api_key
        except Exception:
            api_key = ""
    if not api_key:
        raise PeriModelError(f"{provider}/{model} has no API key")
    llm_cfg = LlmConfig(
        provider=provider, model=model, api_key=api_key, base_url=base_url, timeout=timeout,
        temperature=temperature, max_context_window=context_tokens,
        max_tokens=min(max_output_tokens, max(1, context_tokens // 4)),
        reasoning_effort=reasoning_effort,
    )
    client = create_llm_client(llm_cfg)
    budget = active_run_budget()
    if budget is not None:
        budget.begin_turn()
    text = ""
    async for event in client.chat(messages, [], system_prompt=system_prompt):
        if event.get("type") == "text_delta":
            chunk = str(event.get("text") or "")
            text += chunk
            if stream_callback and chunk:
                await stream_callback(chunk)
        elif event.get("type") == "error":
            raise PeriModelError(str(event.get("error") or "unknown LLM error"))
        elif event.get("type") == "usage" and budget is not None:
            usage = event.get("usage") or {}
            budget.record_usage(
                usage.get("input_tokens", 0), usage.get("output_tokens", 0), owner=owner,
            )
            budget.check_limits()
    if not text.strip():
        raise PeriModelError(f"{provider}/{model} returned an empty response")
    return text


async def decompose_task(
    user_message: str,
    provider: str,
    model: str,
    model_count: int,
    *,
    api_key: str = "",
    base_url: str | None = None,
    temperature: float = 0.4,
    context_tokens: int = 128_000,
    reasoning_effort: str | None = None,
) -> list[dict]:
    """Host decomposes user message into focused sub-tasks."""
    msgs = [Message(role="user", content=user_message)]
    prompt = HOST_DECOMPOSE_PROMPT.format(model_count=model_count)
    text = await _call_llm(
        provider, model, msgs, prompt, timeout=60.0,
        api_key=api_key, base_url=base_url,
        temperature=temperature, context_tokens=context_tokens,
        reasoning_effort=reasoning_effort,
    )
    try:
        data = json.loads(text)
        subtasks = data.get("subtasks", [])
        if subtasks:
            return subtasks[:model_count]
    except (json.JSONDecodeError, KeyError):
        import re
        match = re.search(r'\{[\s\S]*"subtasks"[\s\S]*\}', text)
        if match:
            try:
                data = json.loads(match.group())
                return data.get("subtasks", [])[:model_count]
            except (json.JSONDecodeError, KeyError):
                pass
        return [{"name": "Main Task", "description": user_message, "context": user_message,
                 "success_criteria": "Complete and correct output"}]


def _subagent_system_prompt(subtask: dict, user_message: str, cwd: str, *, depth: int = 0) -> str:
    """Build a worker instruction that anchors local evidence to one workspace."""
    role = SUB_ROLE.format(
        task_description=subtask.get("description", user_message),
        task_context=subtask.get("context", user_message),
        success_criteria=subtask.get("success_criteria", "Complete and correct"),
    )
    depth_line = f"\n\nDEPTH: {depth}"
    return role + depth_line + (
        f"\n\nWORKING DIRECTORY: {cwd}\n"
        "Use relative paths only; never invent an absolute path. "
        "When the task depends on repository facts, you must obtain the relevant evidence "
        "with your available tools before concluding. State uncertainty instead of guessing."
    )


def build_revision_request(
    guidance: str,
    prior_output: str,
    tool_evidence: list[dict[str, Any]],
) -> str:
    """Make a revision self-contained without losing verified worker evidence."""
    evidence = _review_packet({"name": "已验证证据"}, "", tool_evidence)
    return (
        f"主持人修订要求：{guidance}\n\n"
        f"上一轮输出：\n{prior_output}\n\n"
        f"{evidence}\n\n"
        "Do not discard verified evidence. Correct only the stated issues and return a revised final answer."
    )


SUBAGENT_ALLOWED_TOOL_NAMES = frozenset({
    "glob", "grep", "list_dir", "read_file",
    "load_skill", "search_code", "web_search", "web_fetch",
    "git_status", "git_diff_work",
})

# Tools a worker may use to write within its *approved private worktree*.
# Bash, revert_turn, worktree mutation and merge remain Host-only so a worker
# can never escape its worktree root or mutate the base branch.
SUBAGENT_WRITABLE_TOOL_NAMES = frozenset({
    "write_file", "edit_file", "git_add", "git_commit",
})


def build_subagent_tool_registry(
    tools: list[Tool], *, writable: bool = False, recursive: bool = False,
    spawn_context: dict[str, Any] | None = None,
) -> ToolRegistry:
    """Return the child-agent's enforced evidence boundary.

    Read-only mode is the default and always safe.  ``writable=True`` adds a
    tightly scoped write set that is only meaningful inside an approved private
    worktree (the runner sets the worker's cwd to that worktree root and the
    PathGuard anchors there).  Shell and unapproved mutators never cross.

    ``recursive=True`` adds a ``spawn_subtask`` tool so a worker can decompose
    its own scope into children.  ``spawn_context`` supplies the closures that
    tool needs (ref_config, current depth, max depth, parent cwd, event and
    cancellation wiring); without it the tool cannot be built and recursion
    stays off.
    """
    allowed = set(SUBAGENT_ALLOWED_TOOL_NAMES)
    if writable:
        allowed |= SUBAGENT_WRITABLE_TOOL_NAMES
    registry = ToolRegistry()
    for tool in tools:
        if tool.name in allowed:
            registry.register(tool)
    # Read-only git inspection tools survive the allowlist; write tools are
    # included only in writable mode and only when the tool is genuinely
    # confined to the worktree (git add/commit run in the worker's cwd).
    from modus.tools.git_tools import SUBAGENT_GIT_TOOLS
    for tool in SUBAGENT_GIT_TOOLS:
        if tool.name in allowed:
            registry.register(tool)
    if recursive and spawn_context is not None:
        registry.register(_make_spawn_subtask_tool(spawn_context))
    return registry


def _make_spawn_subtask_tool(ctx: dict[str, Any]) -> Tool:
    """Build the ``spawn_subtask`` tool bound to one worker's recursion context.

    The handler recurses into ``execute_subtask`` with depth+1 inside a fresh
    ``_subtasks/<id>`` subdirectory under the parent's cwd, returning the child
    output as a normal tool result.  Depth is captured by closure: a worker at
    or past ``max_recursion_depth`` gets an explicit error instead of spawning.
    """
    import uuid

    execute = ctx["execute"]
    ref_config = ctx["ref_config"]
    depth = int(ctx.get("depth") or 0)
    max_depth = int(ctx.get("max_recursion_depth") or 0)
    parent_cwd = str(ctx.get("cwd") or os.getcwd())
    writable = bool(ctx.get("writable"))
    cancel_event = ctx.get("cancel_event")
    approval_callback = ctx.get("approval_callback")
    event_callback = ctx.get("event_callback")
    max_turns = int(ctx.get("max_turns") or 5)
    session_id = ctx.get("session_id")
    run_id = ctx.get("run_id")

    async def _spawn_handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
        if max_depth <= 0 or depth >= max_depth:
            return ToolResult(
                f"递归深度已达上限（{max_depth}），无法再 spawn 子任务。", is_error=True,
            )
        name = str(payload.get("name") or "子任务").strip()
        description = str(payload.get("description") or "").strip()
        child_context = str(payload.get("context") or description or "").strip()
        criteria = str(payload.get("success_criteria") or "").strip()
        if not description:
            return ToolResult("spawn_subtask requires a 'description'", is_error=True)
        child_dir = Path(parent_cwd) / "_subtasks" / uuid.uuid4().hex[:8]
        child_dir.mkdir(parents=True, exist_ok=True)
        child_task = {
            "name": name, "description": description,
            "context": child_context, "success_criteria": criteria,
        }
        try:
            result = await execute(
                child_task, ref_config, child_context,
                depth=depth + 1, max_recursion_depth=max_depth,
                cwd=str(child_dir), writable=writable,
                cancel_event=cancel_event, approval_callback=approval_callback,
                event_callback=event_callback, max_turns=max_turns,
                session_id=session_id, run_id=run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolResult(f"子任务执行失败: {exc}", is_error=True)
        return ToolResult(
            result,
            display_summary=f"子任务「{name}」完成",
            metadata={"operation": "spawn_subtask", "path": f"_subtasks/{child_dir.name}"},
        )

    return Tool(
        name="spawn_subtask",
        description=(
            "Decompose part of your assigned scope into a focused child sub-task, run it, "
            "and return its output. Use when a sub-part of your task is large enough to "
            "warrant its own isolated evidence-gathering pass."
        ),
        parameters=object_schema({
            "name": {"type": "string", "description": "Short name for the child sub-task"},
            "description": {"type": "string", "description": "What the child must do"},
            "context": {"type": "string", "description": "Relevant context for the child"},
            "success_criteria": {"type": "string", "description": "How to judge the child's output"},
        }, ["description"]),
        handler=_spawn_handler,
        is_read_only=False,
        is_concurrency_safe=False,
        danger_level="medium",
        requires_approval=False,
        capabilities=("agent",),
    )


def _safe_subagent_registry(
    registry: ToolRegistry, *, writable: bool = False,
    recursive: bool = False, spawn_context: dict[str, Any] | None = None,
) -> ToolRegistry:
    return build_subagent_tool_registry(
        [tool for name in registry.list_names() if (tool := registry.get(name)) is not None],
        writable=writable, recursive=recursive, spawn_context=spawn_context,
    )


async def _notify_subagent_event(
    callback: Callable[[dict[str, Any]], Any] | None,
    event: dict[str, Any],
) -> None:
    if callback is None:
        return
    result = callback(event)
    if inspect.isawaitable(result):
        await result


async def execute_subtask(
    subtask: dict,
    ref_config: dict,
    user_message: str,
    timeout: float = 120.0,
    *,
    stream_callback: Callable[[str], Awaitable[None]] | None = None,
    tool_registry: ToolRegistry | None = None,
    event_callback: Callable[[dict[str, Any]], Any] | None = None,
    max_turns: int = 5,
    cwd: str | None = None,
    temperature: float = 0.7,
    context_tokens: int = 128_000,
    reasoning_effort: str | None = None,
    owner: str | None = None,
    writable: bool = False,
    cancel_event: asyncio.Event | None = None,
    approval_callback: Callable[[dict[str, Any]], Any] | None = None,
    depth: int = 0,
    parent_task_id: str | None = None,
    max_recursion_depth: int = 0,
    session_id: str | None = None,
    run_id: str | None = None,
) -> str:
    """Execute one narrow worker task with bounded evidence tools.

    Read-only mode is the default.  ``writable=True`` keeps the scoped file/git
    write tools in the registry so a worker inside an approved private worktree
    can write files and commit; shell and unapproved mutators never cross.

    ``depth`` is the recursion level (top-level workers start at 0).  When
    ``max_recursion_depth > 0``, a worker at ``depth < max_recursion_depth``
    gets the ``spawn_subtask`` tool so it can decompose part of its own scope
    into children; the shared run budget still bounds the whole tree.
    """
    workspace = cwd or os.getcwd()
    system = _subagent_system_prompt(subtask, user_message, workspace, depth=depth)
    messages = [Message(role="user", content=f"Complete your assigned task.\n\nUser's original request: {user_message}")]

    # Preserve the previous lightweight path for callers that want a pure
    # reference response and no local workspace capability.
    if tool_registry is None:
        return await _call_llm(
            ref_config.get("provider", "deepseek"),
            ref_config.get("model", "deepseek-v4-flash"),
            messages, system, timeout=timeout,
            api_key=str(ref_config.get("api_key") or ""),
            base_url=ref_config.get("base_url") or None,
            stream_callback=stream_callback,
            temperature=temperature,
            context_tokens=context_tokens,
            max_output_tokens=int(ref_config.get("max_output_tokens") or 8_192),
            reasoning_effort=reasoning_effort or ref_config.get("reasoning_effort"),
            owner=owner,
        )

    api_key = str(ref_config.get("api_key") or "")
    if not api_key:
        try:
            api_key = load_config().llm.api_key
        except Exception:
            api_key = ""
    if not api_key:
        raise PeriModelError(
            f"{ref_config.get('provider', 'deepseek')}/{ref_config.get('model', 'unknown')} has no API key"
        )

    config = load_config()
    client = create_llm_client(LlmConfig(
        provider=ref_config.get("provider", "deepseek"),
        model=ref_config.get("model", "deepseek-v4-flash"),
        api_key=api_key,
        base_url=ref_config.get("base_url") or None,
        timeout=timeout,
        temperature=temperature,
        max_context_window=context_tokens,
        max_tokens=min(
            int(ref_config.get("max_output_tokens") or 8_192),
            max(1, context_tokens // 4),
        ),
        reasoning_effort=reasoning_effort or ref_config.get("reasoning_effort"),
    ))
    safe_registry = _safe_subagent_registry(tool_registry, writable=writable)
    if max_recursion_depth > 0 and depth < max_recursion_depth:
        spawn_context = {
            "execute": execute_subtask,
            "ref_config": ref_config,
            "depth": depth,
            "max_recursion_depth": max_recursion_depth,
            "cwd": cwd or os.getcwd(),
            "writable": writable,
            "cancel_event": cancel_event,
            "approval_callback": approval_callback,
            "event_callback": event_callback,
            "max_turns": max_turns,
            "session_id": session_id,
            "run_id": run_id,
        }
        safe_registry.register(_make_spawn_subtask_tool(spawn_context))
    executor = ToolExecutor(safe_registry)
    # Writable workers operate inside an approved private worktree; the two
    # approval gates (create_worktrees / merge_changes) already authorize the
    # writes, so the worker's own file/git tools are treated as pre-approved.
    # Recursive workers are likewise pre-authorized to spawn children (the
    # parent's own execution is already approved), so no per-spawn card.
    # An explicit caller-supplied callback still wins over this default.
    effective_approval = approval_callback
    if effective_approval is None and (writable or max_recursion_depth > 0):
        async def _auto_allow(_request: dict[str, Any]) -> str:
            return "allow"
        effective_approval = _auto_allow
    context = ToolContext(
        cwd=cwd or os.getcwd(), config=config, cancel_event=cancel_event,
        approval_callback=effective_approval, session_id=session_id, run_id=run_id,
        granted_capabilities=(
            getattr(getattr(config, "policy", None), "capability_grant", None)
        ),
    )
    definitions = safe_registry.definitions()

    for turn in range(max_turns):
        budget = active_run_budget()
        if budget is not None:
            budget.begin_turn()
        text = ""
        stop_reason = "end_turn"
        states: dict[int, dict[str, Any]] = {}
        async for event in client.chat(messages, definitions, system_prompt=system):
            event_type = event.get("type")
            if event_type == "text_delta":
                chunk = str(event.get("text") or "")
                text += chunk
                if stream_callback and chunk:
                    await stream_callback(chunk)
            elif event_type == "tool_call_delta":
                _merge_tool_delta(states, event["tool_call"])
            elif event_type == "message_end":
                stop_reason = str(event.get("stop_reason") or "end_turn")
            elif event_type == "error":
                raise PeriModelError(str(event.get("error") or "unknown LLM error"))
            elif event_type == "usage" and budget is not None:
                usage = event.get("usage") or {}
                budget.record_usage(
                    usage.get("input_tokens", 0), usage.get("output_tokens", 0), owner=owner,
                )
                budget.check_limits()

        calls = _finalize_tool_calls(states)
        messages.append(Message(role="assistant", content=text, tool_calls=calls))
        if stop_reason != "tool_use" and not calls:
            if not text.strip():
                raise PeriModelError("Worker returned an empty response")
            return text
        if not calls:
            if not text.strip():
                raise PeriModelError("Worker returned an empty response")
            return text

        for call in calls:
            name = str(call.get("function", {}).get("name") or "unknown")
            await _notify_subagent_event(event_callback, {
                "type": "subagent_tool_call", "tool_call_id": str(call.get("id") or ""), "name": name, "input": _tool_input(call),
            })
        results = await executor.execute_all(calls, context)
        for result in results:
            name = _tool_name_by_id(calls, result.tool_use_id or "")
            result_event = {
                "type": "subagent_tool_result", "tool_call_id": str(result.tool_use_id or ""), "name": name,
            }
            result_event.update(tool_result_event(result))
            await _notify_subagent_event(event_callback, result_event)
            messages.append(Message(role="tool", content=result.model_text(), tool_call_id=result.tool_use_id))

    raise PeriModelError(
        f"Worker exceeded {max_turns} tool turns without a final response"
    )


def _review_packet(subtask: dict, output: str, tool_evidence: list[dict[str, Any]]) -> str:
    """Give the host bounded, labelled evidence beside a worker conclusion.

    This packet is only for quality review.  The final merge still receives the
    clean worker outputs, so internal tool payloads do not leak into the user
    answer unless the host independently decides they are relevant.
    """
    title = str(subtask.get("name") or "子任务")
    if not tool_evidence:
        return f"## {title}\n{output}"
    lines = [f"## {title}", output, "", f"Evidence tools used ({len(tool_evidence)}):"]
    for event in tool_evidence:
        name = str(event.get("name") or "工具")
        marker = "ERROR: " if event.get("is_error") else ""
        result = bounded_summary(str(event.get("result") or ""), limit=300)
        lines.append(f"- {name}: {marker}{result}")
    return "\n".join(lines)


async def review_subtask_outputs(
    subtasks: list[dict],
    outputs: list[str],
    provider: str,
    model: str,
    user_message: str,
    *,
    api_key: str = "",
    base_url: str | None = None,
    temperature: float = 0.4,
    context_tokens: int = 128_000,
    reasoning_effort: str | None = None,
) -> tuple[bool, list[str], list[float]]:
    """Host reviews sub-agent outputs and determines if revision is needed.

    Returns ``(all_acceptable, guidance, scores)`` where ``scores`` are the
    per-subtask Host scores (0-10) that feed convergence detection; items
    without a valid score are skipped.
    """
    subtask_text = "\n\n".join(
        f"--- {s['name']} ---\nOutput: {o[:2000]}"
        for s, o in zip(subtasks, outputs)
    )
    msgs = [Message(role="user", content=subtask_text)]
    text = await _call_llm(
        provider, model, msgs, HOST_QUALITY_PROMPT, timeout=60.0,
        api_key=api_key, base_url=base_url,
        temperature=temperature, context_tokens=context_tokens,
        reasoning_effort=reasoning_effort,
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PeriModelError("Host review returned invalid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("all_acceptable"), bool):
        raise PeriModelError("Host review omitted a valid all_acceptable decision")
    reviews = data.get("subtask_reviews")
    if not isinstance(reviews, list):
        raise PeriModelError("Host review omitted subtask_reviews")
    guidance = [
        str(item.get("revision_guidance") or "").strip()
        for item in reviews
        if isinstance(item, dict) and item.get("needs_revision")
    ]
    guidance = [item for item in guidance if item]
    scores: list[float] = []
    for item in reviews:
        if not isinstance(item, dict):
            continue
        raw_score = item.get("score")
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        scores.append(score)
    return data["all_acceptable"], guidance, scores


HOST_CRITERIA_VERIFY_PROMPT = """You are verifying whether a sub-agent's output satisfies a structured checklist.

For EACH checklist item, judge satisfied/not-satisfied based ONLY on facts
verifiable in the sub-agent's output. A satisfied verdict must be supported by
specific content in the output; if the requirement is vague or cannot be
confirmed from the output, mark it not satisfied and say why.

Output ONLY valid JSON (no other text):
{
  "criteria_verdicts": [
    {"index": 0, "item": "...", "satisfied": true, "reason": "which part of the output satisfies it"},
    {"index": 1, "item": "...", "satisfied": false, "reason": "what is missing"}
  ]
}

One verdict per checklist item, in the same order."""


def normalize_criteria(task: dict) -> list[dict[str, str]]:
    """Extract a checklist of verifiable items from a task's success_criteria.

    New-style criteria are ``{"checklist": [{"item": ..., "check": ...}]}``.
    Legacy free-text criteria collapse to a single item so verification still
    runs, but at a coarser granularity.
    """
    criteria = task.get("success_criteria")
    if isinstance(criteria, dict):
        checklist = criteria.get("checklist")
        if isinstance(checklist, list):
            items: list[dict[str, str]] = []
            for entry in checklist:
                if isinstance(entry, dict):
                    item = str(entry.get("item") or entry.get("check") or "").strip()
                    if item:
                        items.append({"item": item, "check": str(entry.get("check") or "")})
                elif isinstance(entry, str) and entry.strip():
                    items.append({"item": entry.strip(), "check": ""})
            if items:
                return items
        if str(criteria.get("item") or "").strip():
            return [{"item": str(criteria["item"]), "check": ""}]
    text = str(criteria or "").strip()
    if text:
        return [{"item": text, "check": ""}]
    return [{"item": "Complete and correct output", "check": ""}]


async def verify_subtask_criteria(
    subtask: dict,
    output: str,
    provider: str,
    model: str,
    *,
    api_key: str = "",
    base_url: str | None = None,
    temperature: float = 0.4,
    context_tokens: int = 128_000,
    reasoning_effort: str | None = None,
) -> dict:
    """Have the Host judge each checklist item satisfied/not-satisfied.

    Returns ``{"verified": int, "total": int, "verdicts": [...]}`` where each
    verdict is ``{"index", "item", "satisfied", "reason"}``.  The count of
    satisfied items is the objective, auditable signal that replaces the
    subjective 0-10 score as the SPRT input.
    """
    checklist = normalize_criteria(subtask)
    body = "\n\n".join(
        f"[{i}] {entry['item']}" + (f"\n    check: {entry['check']}" if entry["check"] else "")
        for i, entry in enumerate(checklist)
    )
    msgs = [Message(role="user", content=f"## Sub-agent output\n\n{output[:4000]}\n\n## Checklist\n\n{body}")]
    text = await _call_llm(
        provider, model, msgs, HOST_CRITERIA_VERIFY_PROMPT, timeout=60.0,
        api_key=api_key, base_url=base_url,
        temperature=temperature, context_tokens=context_tokens,
        reasoning_effort=reasoning_effort,
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PeriModelError("Criteria verification returned invalid JSON") from exc
    verdicts = data.get("criteria_verdicts")
    if not isinstance(verdicts, list):
        raise PeriModelError("Criteria verification omitted criteria_verdicts")
    normalized: list[dict] = []
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            continue
        normalized.append({
            "index": int(verdict.get("index") or len(normalized)),
            "item": str(verdict.get("item") or ""),
            "satisfied": bool(verdict.get("satisfied")),
            "reason": str(verdict.get("reason") or ""),
        })
    verified = sum(1 for v in normalized if v["satisfied"])
    return {
        "verified": verified,
        "total": len(checklist),
        "verdicts": normalized,
    }


async def merge_outputs(
    subtasks: list[dict],
    outputs: list[str],
    provider: str,
    model: str,
    user_message: str,
    *,
    api_key: str = "",
    base_url: str | None = None,
    temperature: float = 0.4,
    context_tokens: int = 128_000,
    reasoning_effort: str | None = None,
) -> str:
    """Host merges all sub-agent outputs into the final response."""
    sub_text = "\n\n".join(
        f"--- {s['name']} ---\n{o}"
        for s, o in zip(subtasks, outputs)
    )
    sys_prompt = HOST_MERGE_PROMPT.format(
        user_request=user_message,
        sub_outputs=sub_text,
    )
    msgs = [Message(role="user", content=f"Produce the final merged response for: {user_message}")]
    final = await _call_llm(
        provider, model, msgs, sys_prompt, timeout=120.0,
        api_key=api_key, base_url=base_url,
        temperature=temperature, context_tokens=context_tokens,
        reasoning_effort=reasoning_effort,
    )
    return final
