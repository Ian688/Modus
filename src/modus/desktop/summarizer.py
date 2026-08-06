"""Semantic context-compaction summaries.

``_maybe_compress_history`` currently replaces omitted turns with a generic
count message.  When ``features.compression.semantic`` is enabled this module
asks the configured LLM to produce a real summary of the omitted middle turns
instead.  The result is still injected under ``SUMMARY_PREFIX`` (reference-only,
never an instruction channel), and every failure path degrades silently to the
count message so compaction itself never blocks on the model.
"""

from __future__ import annotations

from collections.abc import Sequence

from modus.config import LlmConfig
from modus.llm import create_llm_client
from modus.types import Message

# Instruct the summarizer to compress, never to follow, what it reads.
_SUMMARIZE_SYSTEM = (
    "You condense a middle section of a conversation into concise reference "
    "notes for an AI assistant that is continuing the conversation. Write in "
    "the same language as the conversation. State concrete facts, decisions, "
    "tool outcomes and constraints only; never add instructions, never act as "
    "a user, never invent content. Output plain text only, no headers."
)


def render_omitted(messages: Sequence[Message], max_chars: int) -> str:
    """Render the omitted messages as a bounded text block for the summarizer."""
    lines: list[str] = []
    used = 0
    truncated = False
    for message in messages:
        content = message.content if isinstance(message.content, str) else str(message.content)
        content = content.strip()
        if not content:
            continue
        label = message.role.upper()
        line = f"[{label}] {content}"
        if used + len(line) > max_chars:
            truncated = True
            break
        lines.append(line)
        used += len(line)
    if truncated:
        lines.append("[... remaining omitted messages not shown ...]")
    return "\n".join(lines)


async def summarize_omitted(
    *,
    messages: Sequence[Message],
    provider: str,
    model: str,
    api_key: str,
    base_url: str | None = None,
    max_input_chars: int = 24_000,
    timeout: float = 45.0,
) -> str:
    """Summarize the omitted messages via the configured LLM.

    Raises on any failure (no key, transport error, empty response) so the
    caller can fall back to the count message.
    """
    if not api_key:
        raise ValueError("no API key available for semantic summarization")
    if not messages:
        raise ValueError("nothing to summarize")
    block = render_omitted(messages, max_input_chars)
    llm_cfg = LlmConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_context_window=min(32_000, max_input_chars * 4 + 8_192),
        max_tokens=2_048,
    )
    client = create_llm_client(llm_cfg)
    text = ""
    async for event in client.chat(
        [Message(role="user", content=block)],
        [],
        system_prompt=_SUMMARIZE_SYSTEM,
    ):
        if event.get("type") == "text_delta":
            text += str(event.get("text") or "")
        elif event.get("type") == "error":
            raise RuntimeError(str(event.get("error") or "summarizer error"))
    text = text.strip()
    if not text:
        raise RuntimeError("summarizer returned an empty response")
    return text
