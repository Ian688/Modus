from __future__ import annotations

import asyncio
import random
import threading
import time
from typing import Any, AsyncIterator

from modus.runtime.budget import active_run_budget
from modus.runtime.cancellation import RunCancelled

_jitter_counter = 0
_jitter_lock = threading.Lock()

def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """带抖动的指数退避，防止多个会话同时重试造成 thundering herd"""
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        seed = _jitter_counter
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    jitter = random.Random(seed + attempt).uniform(0, jitter_ratio * delay)
    return delay + jitter


def retry_chat(
    chat,
    *,
    max_attempts: int = 2,
    retryable_status: tuple[int, ...] = (429, 500, 502, 503, 529),
) -> Any:
    """Wrap an ``async def chat(...) -> AsyncIterator`` with safe transient retry.

    Retries ONLY when the stream ends before producing any event that is not an
    ``error`` — i.e. a connect/read/timeout failure before a single text, tool
    or thinking delta.  A stream that already yielded content is never retried,
    because replaying it would duplicate visible output and could re-execute
    tool calls (approve-then-execute invariant).  The retry loop is budget-aware
    (aborts when remaining wall time < backoff, or on cancel) so a flaky
    provider can never inflate a run.

    ``chat`` receives the same ``(messages, tools, *, system_prompt)`` args each
    attempt and is expected to be an async generator that yields typed events.
    """
    if max_attempts < 1:
        max_attempts = 1

    async def retrying_chat(messages, tools, *, system_prompt) -> AsyncIterator[dict[str, Any]]:
        attempt = 0
        last_error: str | None = None
        while attempt < max_attempts:
            attempt += 1
            stream = chat(messages, tools, system_prompt=system_prompt)
            saw_delta = False
            saw_error = False
            async for event in stream:
                event_type = event.get("type")
                if event_type == "error":
                    saw_error = True
                    last_error = str(event.get("error") or "Unknown model error")
                    if not saw_delta:
                        # Zero-output failure: buffer it; retries may follow.
                        continue
                    # Mid-stream error after real content: not retryable,
                    # forward it and stop.
                    yield event
                    return
                if event_type in {"text_delta", "thinking_delta", "tool_call_delta", "message_end"}:
                    saw_delta = True
                yield event
            if saw_delta:
                # Real content was produced: never replay this request.
                return
            if not saw_error:
                # Clean stream end with no error and no delta.
                return
            if attempt >= max_attempts:
                yield {"type": "error", "error": last_error}
                return
            # Zero-output transient failure: retry with backoff, budget-aware.
            budget = active_run_budget()
            delay = jittered_backoff(attempt, base_delay=0.5, max_delay=4.0)
            if budget is not None and budget.remaining_wall_seconds < delay:
                yield {"type": "error", "error": last_error}
                return
            await asyncio.sleep(delay)
        return

    return retrying_chat