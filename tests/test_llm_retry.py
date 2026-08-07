"""Transient provider retry: zero-output failures retry, content never replays."""

from __future__ import annotations

import asyncio
import pytest

from modus.llm.retry import retry_chat
from modus.runtime.budget import RunBudget, RunLimits, bind_run_budget, reset_run_budget


async def _collect(coro):
    return [ev async for ev in coro]


def _chat_with_failures(fail_then_succeed: int):
    calls = {"count": 0}

    async def chat(messages, tools, *, system_prompt):
        calls["count"] += 1
        if calls["count"] <= fail_then_succeed:
            yield {"type": "error", "error": "connection reset"}
            return
        yield {"type": "text_delta", "text": "recovered"}

    return chat, calls


@pytest.mark.asyncio
async def test_retry_recovers_after_transient_zero_output_failure():
    chat, calls = _chat_with_failures(fail_then_succeed=2)
    wrapped = retry_chat(chat, max_attempts=3)
    events = await _collect(wrapped([], [], system_prompt="sys"))

    texts = "".join(ev["text"] for ev in events if ev["type"] == "text_delta")
    assert texts == "recovered"
    assert not any(ev["type"] == "error" for ev in events)
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_retry_exhaustion_yields_terminal_error():
    chat, calls = _chat_with_failures(fail_then_succeed=99)
    wrapped = retry_chat(chat, max_attempts=2)
    events = await _collect(wrapped([], [], system_prompt="sys"))

    errors = [ev for ev in events if ev["type"] == "error"]
    assert len(errors) == 1
    assert "connection reset" in errors[0]["error"]
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_never_retries_after_content_has_streamed():
    async def chat(messages, tools, *, system_prompt):
        yield {"type": "text_delta", "text": "partial"}
        yield {"type": "error", "error": "mid-stream break"}

    wrapped = retry_chat(chat, max_attempts=3)
    events = await _collect(wrapped([], [], system_prompt="sys"))

    # The mid-stream error is forwarded exactly once, never replayed.
    texts = [ev["text"] for ev in events if ev["type"] == "text_delta"]
    errors = [ev for ev in events if ev["type"] == "error"]
    assert texts == ["partial"]
    assert len(errors) == 1


@pytest.mark.asyncio
async def test_retry_respects_wall_time_budget():
    chat, calls = _chat_with_failures(fail_then_succeed=99)
    budget = RunBudget(RunLimits(max_wall_seconds=0.05))
    token = bind_run_budget(budget)
    try:
        wrapped = retry_chat(chat, max_attempts=5)
        events = await _collect(wrapped([], [], system_prompt="sys"))
    finally:
        reset_run_budget(token)

    # Backoff (0.5s+) exceeds the 0.05s budget, so only the first attempt runs.
    assert calls["count"] == 1
    assert any(ev["type"] == "error" for ev in events)
