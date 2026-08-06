"""MOA — Hermes-aligned prompts and message building.

Key design patterns from Hermes' moa_loop.py:
- Reference system prompt explicitly says "you do NOT execute anything"
- Message context drops system prompts, folds tool results into assistant turns
- Ends with a synthetic advisory request (not just raw conversation)
- Aggregator produces guidance for the host, not a direct answer
- Original user prompt is preserved through the entire chain
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

from modus.config import ModusConfig, LlmConfig
from modus.llm.factory import create_llm_client
from modus.runtime.budget import active_run_budget
from modus.types import Message


# ── Hermes-aligned prompts ──

REFERENCE_ROLE = (
    "You are a reference advisor in a Mixture of Agents (MoA) process. You are "
    "NOT the acting agent and you do NOT execute anything: you cannot call "
    "tools, run commands, browse, or access files, repositories, or URLs, and "
    "you should not try to or apologize for being unable to. A separate "
    "aggregator/orchestrator model holds those capabilities and will take the "
    "actual actions.\n\n"
    "The conversation below is the current state of a task handled by that "
    "acting agent. Your job is to give your most intelligent analysis of that "
    "state: understand the goal, reason about the problem, and advise on what "
    "to do next. Surface the best approach, concrete next steps and tool-use "
    "strategy, likely pitfalls and risks, and anything the acting agent may "
    "have missed or gotten wrong. Assume any referenced files, URLs, or "
    "systems exist and reason about them from the context given rather than "
    "asking for access.\n\n"
    "Respond with your advice directly — no preamble, no disclaimers about "
    "tools or access. Your response is private guidance handed to the "
    "aggregator, not an answer shown to the user."
)

AGGREGATOR_PROMPT = (
    "You are the aggregator in a Mixture of Agents process. Synthesize the "
    "reference responses into concise, actionable guidance for the acting "
    "host model. Focus on next steps, tool-use strategy, risks, and any "
    "disagreements. Do not answer the user directly — produce context the "
    "host should use to shape its final response.\n\n"
    "Original user request:\n{user_prompt}\n\n"
    "Reference responses:\n{joined}"
)

HOST_GUIDANCE_PROMPT = (
    "Below is guidance synthesized by the aggregator from reference model "
    "advice. Use this guidance to produce the best possible final response "
    "to the user's request.\n\n"
    "## Aggregator Guidance\n{guidance}\n\n"
    "## Your Response\n"
    "Produce your final answer. The reference insights above are advisory — "
    "integrate them where helpful, but own the response as your own. "
    "Respond in the same language as the user's request."
)


# ── Utility functions (aligned with Hermes) ──

_REFERENCE_TOOL_RESULT_BUDGET = 4000
_AGGREGATOR_REFERENCE_BUDGET = 6000


def truncate_tool_result(text: str, budget: int = _REFERENCE_TOOL_RESULT_BUDGET) -> str:
    """Head+tail preview of a tool result for the advisory reference view."""
    if not text or len(text) <= budget:
        return text
    half = budget // 2
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n[... {omitted} chars omitted ...]\n{text[-half:]}"


def trim_for_aggregator(text: str, budget: int = _AGGREGATOR_REFERENCE_BUDGET) -> str:
    """Trim one reference output for the aggregator (head+tail)."""
    if budget <= 0 or len(text) <= budget:
        return text
    marker = "\n\n[... {} characters omitted ...]\n\n"
    marker_fmt = marker.format(0)
    available = budget - len(marker_fmt)
    if available <= 10:
        return text[:budget]
    head = available * 2 // 3
    tail = available - head
    omitted = len(text) - head - tail
    return text[:head] + marker.format(omitted) + text[-tail:]


def _render_tool_calls(tool_calls: Any) -> str:
    """Render tool_calls as readable text for the reference advisory view."""
    lines: list[str] = []
    for tc in tool_calls or []:
        fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
        name = fn.get("name") or (tc.get("name") if isinstance(tc, dict) else "") or "tool"
        args = fn.get("arguments")
        if isinstance(args, str):
            args_text = args
        elif args is not None:
            try:
                import json
                args_text = json.dumps(args, ensure_ascii=False)
            except Exception:
                args_text = str(args)
        else:
            args_text = ""
        lines.append(f"[called tool: {name}({args_text})]" if args_text else f"[called tool: {name}]")
    return "\n".join(lines)


def build_reference_messages(
    messages: list[Message],
    history_window: int = 0,
) -> list[dict[str, str]]:
    """Build an advisory view of the conversation for reference models.

    Matches Hermes' _reference_messages():
    - System prompts are dropped
    - Assistant turns: tool_calls rendered inline as [called tool: ...]
    - Tool results: folded into preceding assistant turn as [tool result: ...]
    - Ends with a synthetic advisory request on a user turn
    """
    advisory_instruction = (
        "[The conversation above is the current state of the task. Give your "
        "most intelligent judgement: what is going on, what should happen next, "
        "what risks or mistakes you see, and how the acting agent should proceed.]"
    )
    rendered: list[dict[str, str]] = []
    last_user_content: str | None = None

    for msg in messages:
        role = msg.role
        content = msg.content if isinstance(msg.content, str) else str(msg.content) if msg.content else ""

        if role == "system":
            continue
        if role == "user":
            if content.strip():
                last_user_content = content
            rendered.append({"role": "user", "content": content})
        elif role == "assistant":
            parts: list[str] = []
            if content.strip():
                parts.append(content.strip())
            calls_text = _render_tool_calls(getattr(msg, "tool_calls", None))
            if calls_text:
                parts.append(calls_text)
            if parts:
                rendered.append({"role": "assistant", "content": "\n".join(parts)})
        elif role == "tool":
            result_text = truncate_tool_result(content)
            block = f"[tool result: {result_text}]"
            if rendered and rendered[-1].get("role") == "assistant":
                rendered[-1]["content"] = rendered[-1]["content"] + "\n" + block
            else:
                rendered.append({"role": "assistant", "content": block})

    # History window
    if history_window > 0 and last_user_content:
        user_indices = [i for i, m in enumerate(rendered) if m.get("role") == "user"]
        if len(user_indices) > history_window:
            first_user = rendered[user_indices[0]]
            rendered = rendered[user_indices[-history_window]:]
            # Fold first user content into the earliest retained user turn
            if rendered and rendered[0].get("role") == "user" and first_user["content"].strip():
                rendered[0]["content"] = (
                    f"[Original task anchor]\n{first_user['content']}\n\n"
                    f"[Current turn]\n{rendered[0]['content']}"
                )

    # End on a user turn (required by strict providers)
    if rendered and rendered[-1].get("role") == "assistant":
        rendered.append({"role": "user", "content": advisory_instruction})
    elif rendered and rendered[-1].get("role") == "user":
        pass  # Already ends on user

    if not rendered:
        if last_user_content is not None:
            return [{"role": "user", "content": last_user_content}]
        return [{"role": "user", "content": "(no conversation context)"}]
    return rendered


def get_user_prompt(messages: list[Message]) -> str:
    """Extract the latest user prompt from message history."""
    for msg in reversed(messages):
        if msg.role == "user" and msg.content:
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


# ── Core MOA functions ──


async def call_reference(
    ref_config: dict[str, str],
    messages: list[Message],
    system_prompt: str,
    temperature: float = 0.7,
    timeout: float = 30.0,
    *,
    context_tokens: int | None = None,
    reasoning_effort: str | None = None,
    stream_callback: Callable[[str], None | Coroutine] | None = None,
    owner: str | None = None,
) -> str:
    """Call one reference model with a trimmed advisory view of the conversation."""
    api_key = str(ref_config.get("api_key") or "")
    base_url = ref_config.get("base_url") or None
    if not api_key:
        try:
            from modus.config import load_config
            api_key = load_config().llm.api_key
        except Exception:
            api_key = ""
    if not api_key:
        return "[参考模型调用失败：未配置 API key]"

    llm_cfg = LlmConfig(
        provider=ref_config.get("provider", "deepseek"),
        model=ref_config.get("model", "deepseek-v4-flash"),
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
        max_context_window=int(context_tokens or ref_config.get("context_window") or 128_000),
        max_tokens=min(
            int(ref_config.get("max_output_tokens") or 8_192),
            max(1, int(context_tokens or ref_config.get("context_window") or 128_000) // 4),
        ),
        reasoning_effort=reasoning_effort or ref_config.get("reasoning_effort"),
    )
    client = create_llm_client(llm_cfg)

    from modus.types import Message
    # Build advisory view — matches Hermes' _reference_messages() pattern
    # Roughly reserve half of this role's allowance for system/response/tool
    # overhead. The history window remains deterministic and instruction-safe.
    allowance = max(1_024, int(context_tokens or ref_config.get("context_window") or 128_000) // 2)
    ref_messages = build_reference_messages(messages)
    while len(ref_messages) > 1 and sum(len(item["content"]) for item in ref_messages) // 4 > allowance:
        ref_messages.pop(0)

    # Prepend the advisor prompt as the system prompt
    full_system = REFERENCE_ROLE

    budget = active_run_budget()
    if budget is not None:
        budget.begin_turn()
    text = ""
    async for event in client.chat(
        [Message(role=m["role"], content=m["content"]) for m in ref_messages],
        [],
        system_prompt=full_system,
    ):
        if event.get("type") == "text_delta":
            chunk = str(event.get("text") or "")
            text += chunk
            if stream_callback and chunk:
                result = stream_callback(chunk)
                if asyncio.iscoroutine(result):
                    await result
        elif event.get("type") == "error":
            return f"[参考模型错误: {event['error']}]"
        elif event.get("type") == "usage" and budget is not None:
            usage = event.get("usage") or {}
            budget.record_usage(
                usage.get("input_tokens", 0), usage.get("output_tokens", 0), owner=owner,
            )
            budget.check_limits()
    return text


async def call_aggregator(
    host_config: dict[str, str],
    messages: list[Message],
    system_prompt: str,
    reference_outputs: list[tuple[str, str]],
    temperature: float = 0.4,
    timeout: float = 60.0,
    *,
    context_tokens: int | None = None,
    reasoning_effort: str | None = None,
    stream_callback: Callable[[str], None | Coroutine] | None = None,
    owner: str | None = None,
) -> str:
    """Aggregator synthesizes reference advice into guidance for the host."""
    api_key = str(host_config.get("api_key") or "")
    base_url = host_config.get("base_url") or None
    if not api_key:
        try:
            from modus.config import load_config
            api_key = load_config().llm.api_key
        except Exception:
            api_key = ""
    if not api_key:
        return "[聚合器调用失败：未配置 API key]"

    llm_cfg = LlmConfig(
        provider=host_config.get("provider", "deepseek"),
        model=host_config.get("model", "deepseek-v4-flash"),
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
        max_context_window=int(context_tokens or host_config.get("context_window") or 128_000),
        max_tokens=min(
            int(host_config.get("max_output_tokens") or 8_192),
            max(1, int(context_tokens or host_config.get("context_window") or 128_000) // 4),
        ),
        reasoning_effort=reasoning_effort or host_config.get("reasoning_effort"),
    )
    client = create_llm_client(llm_cfg)

    # Build trimmed reference block (Hermes-style head+tail)
    user_prompt = get_user_prompt(messages)
    joined = "\n\n".join(
        f"Reference {idx} — {label}:\n{trim_for_aggregator(text)}"
        for idx, (label, text) in enumerate(reference_outputs, start=1)
    )
    agg_prompt = AGGREGATOR_PROMPT.format(user_prompt=user_prompt, joined=joined)

    from modus.types import Message
    budget = active_run_budget()
    if budget is not None:
        budget.begin_turn()
    text = ""
    async for event in client.chat(
        [Message(role="user", content=agg_prompt)],
        [],
        system_prompt="",
    ):
        if event.get("type") == "text_delta":
            chunk = str(event.get("text") or "")
            text += chunk
            if stream_callback and chunk:
                result = stream_callback(chunk)
                if asyncio.iscoroutine(result):
                    await result
        elif event.get("type") == "error":
            return f"[聚合器错误: {event['error']}]"
        elif event.get("type") == "usage" and budget is not None:
            usage = event.get("usage") or {}
            budget.record_usage(
                usage.get("input_tokens", 0), usage.get("output_tokens", 0), owner=owner,
            )
            budget.check_limits()
    return text


async def call_host(
    host_config: dict[str, str],
    messages: list[Message],
    system_prompt: str,
    guidance: str,
    temperature: float = 0.7,
    timeout: float = 60.0,
    *,
    context_tokens: int | None = None,
    reasoning_effort: str | None = None,
    stream_callback: Callable[[str], None | Coroutine] | None = None,
    owner: str | None = None,
) -> str:
    """Host model produces the final response using aggregator guidance."""
    api_key = str(host_config.get("api_key") or "")
    base_url = host_config.get("base_url") or None
    if not api_key:
        try:
            from modus.config import load_config
            api_key = load_config().llm.api_key
        except Exception:
            api_key = ""
    if not api_key:
        return "[主持人调用失败：未配置 API key]"

    llm_cfg = LlmConfig(
        provider=host_config.get("provider", "deepseek"),
        model=host_config.get("model", "deepseek-v4-flash"),
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
        max_context_window=int(context_tokens or host_config.get("context_window") or 128_000),
        max_tokens=min(
            int(host_config.get("max_output_tokens") or 8_192),
            max(1, int(context_tokens or host_config.get("context_window") or 128_000) // 4),
        ),
        reasoning_effort=reasoning_effort or host_config.get("reasoning_effort"),
    )
    client = create_llm_client(llm_cfg)

    host_prompt = HOST_GUIDANCE_PROMPT.format(guidance=guidance)
    full_system = system_prompt + "\n\n" + host_prompt

    # Pass the conversation messages + host guidance as context
    # The host sees the original conversation plus the aggregator guidance
    budget = active_run_budget()
    if budget is not None:
        budget.begin_turn()
    text = ""
    async for event in client.chat(
        [m for m in messages if m.role != "system"],
        [],
        system_prompt=full_system,
    ):
        if event.get("type") == "text_delta":
            chunk = str(event.get("text") or "")
            text += chunk
            if stream_callback and chunk:
                result = stream_callback(chunk)
                if asyncio.iscoroutine(result):
                    await result
        elif event.get("type") == "error":
            return f"[主持人错误: {event['error']}]"
        elif event.get("type") == "usage" and budget is not None:
            usage = event.get("usage") or {}
            budget.record_usage(
                usage.get("input_tokens", 0), usage.get("output_tokens", 0), owner=owner,
            )
            budget.check_limits()
    return text


def build_guidance_block(reference_outputs: list[tuple[str, str]]) -> str:
    """Package reference outputs for the aggregator (legacy interface)."""
    if not reference_outputs:
        return ""
    parts: list[str] = []
    for idx, (label, text) in enumerate(reference_outputs, start=1):
        parts.append(f"--- 参考模型 {idx} ({label}) ---\n{trim_for_aggregator(text)}")
    return "\n\n".join(parts)
