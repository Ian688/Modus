"""Prompt-cache breakpoint engineering (Wave2 C1).

The assembled Modus system prompt mixes a stable part (role, capability and
tool declarations, guidelines) with a part that changes every turn (current
time, working directory, env/memory snapshots).  Mixing them makes the whole
prefix re-encode on every dynamic change.

This module splits the system prompt into a static block and a dynamic block
(``PromptAssembler`` emits the boundary markers) and, when prompt caching is
enabled, applies Anthropic-style ``cache_control`` breakpoints:

- the static system block carries ``cache_control: {"type": "ephemeral"}`` so
  the provider can reuse the cached prefix even when the dynamic block changed;
- the first user message and the last ``breakpoints - 1`` user messages get a
  cache breakpoint (the provider-recommended multi-break pattern), letting the
  full context prefix be reused for the current turn.

Providers that do not understand the marker never receive it (the marker is
emitted only when ``enable_prompt_cache`` is on).
"""

from __future__ import annotations

from typing import Any

# Boundary markers emitted by ``PromptAssembler.build``.  ``STATIC_BOUNDARY``
# ends the static block; ``DYNAMIC_BOUNDARY`` starts the dynamic block.
# ``split_system_blocks`` understands both and treats a prompt with no marker
# as fully static (the safe default for callers that never ran the assembler).
STATIC_BOUNDARY = "__MODUS_STATIC__"
DYNAMIC_BOUNDARY = "__MODUS_DYNAMIC__"

# Default number of user-message cache breakpoints: first user + last two.
DEFAULT_BREAKPOINTS = 3

# Prompt-cache modes (factory maps a provider -> one of these).
PROMPT_CACHE_OFF = "off"      # no cache markers at all
PROMPT_CACHE_BASIC = "basic"  # static system block only, no user breakpoints
PROMPT_CACHE_FULL = "full"    # static system block + first/last user breakpoints


def split_system_blocks(system_prompt: str) -> tuple[str, str]:
    """Split ``system_prompt`` into ``(static, dynamic)`` blocks.

    ``static`` is everything up to the first dynamic boundary (an explicit
    ``__MODUS_STATIC__`` label before it is stripped); ``dynamic`` is
    everything after the dynamic boundary.  A prompt with no boundary marker is
    returned whole as ``(prompt, "")`` so a caller that never runs the
    assembler still caches the full prompt.
    """
    prompt = system_prompt or ""
    text = prompt.strip()
    if DYNAMIC_BOUNDARY in text:
        head, _, tail = text.partition(DYNAMIC_BOUNDARY)
        static = head.split(STATIC_BOUNDARY)[0].rstrip()
        return static, tail.strip()
    if STATIC_BOUNDARY in text:
        head, _, tail = text.partition(STATIC_BOUNDARY)
        return head.strip(), tail.strip()
    return text, ""


def strip_system_markers(system_prompt: str) -> str:
    """Remove the static/dynamic boundary marker lines.

    Used when prompt caching is disabled so the boundary labels the assembler
    emitted never leak into a prompt sent to a provider.
    """
    lines = (system_prompt or "").splitlines()
    cleaned = [line for line in lines if line.strip() not in {STATIC_BOUNDARY, DYNAMIC_BOUNDARY}]
    return "\n".join(cleaned).strip()


def apply_cache_to_messages(
    messages: list[dict[str, Any]],
    breakpoints: int | None = None,
) -> list[dict[str, Any]]:
    """Return a copy of ``messages`` with cache breakpoints applied.

    The first system message (the assembled static prompt) receives
    ``cache_control: {"type": "ephemeral"}``.  Later system messages — the
    dynamic block, context contracts, compaction summaries — are left alone, so
    a dynamic change never invalidates the cached static prefix.

    ``breakpoints`` user messages get a cache breakpoint: the first user
    message plus the last ``breakpoints - 1``.  ``breakpoints=0`` disables user
    breakpoints (basic mode); ``None`` uses ``DEFAULT_BREAKPOINTS``.
    """
    count = DEFAULT_BREAKPOINTS if breakpoints is None else max(0, int(breakpoints))
    result = [dict(message) for message in messages]
    # Mark the static system block (the first system message).
    for item in result:
        if item.get("role") == "system":
            item["cache_control"] = {"type": "ephemeral"}
            break
    if count > 0:
        user_indices = [
            index for index, item in enumerate(result) if item.get("role") == "user"
        ]
        if user_indices:
            marked = {user_indices[0]}
            last_count = max(1, count - 1)
            marked.update(user_indices[max(0, len(user_indices) - last_count):])
            for index in marked:
                result[index].setdefault("cache_control", {"type": "ephemeral"})
    return result
