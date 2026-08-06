"""Reasoning-strategy seam: a Reasoner drives the agent turn loop.

The default ReAct loop in ``query`` is one concrete strategy.  This module
defines the protocol a future reasoning strategy (plan-then-act, reflection,
tree search, or an autonomous AGI loop) implements so it can drive the same
runner, budget, approval, tool-execution and persistence layers without a
rewrite.  The contract is the canonical event stream the runners consume.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from modus.types import Message


class Reasoner(Protocol):
    """A reasoning strategy that yields the canonical run event stream.

    Implementations own the turn loop: they call the model, route tool calls
    through the shared executor, and emit typed events.  The event vocabulary
    is fixed so any runner can consume it unchanged.
    """

    async def run(
        self,
        messages: list[Message],
        *,
        approval_callback: Callable[[dict[str, Any]], Awaitable[str] | str] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield events: text_delta/thinking_delta/tool_call/tool_result/usage/
        turn_complete/done/error."""
        ...  # pragma: no cover - protocol
