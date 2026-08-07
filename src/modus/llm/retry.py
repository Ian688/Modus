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
    classify: Any = None,
) -> Any:
    """Wrap an ``async def chat(...) -> AsyncIterator`` with safe transient retry.

    Retries ONLY when the stream ends before producing any event that is not an
    ``error`` — i.e. a connect/read/timeout failure before a single text, tool
    or thinking delta.  A stream that already yielded content is never retried,
    because replaying it would duplicate visible output and could re-execute
    tool calls (approve-then-execute invariant).  The retry loop is budget-aware
    (aborts when remaining wall time < backoff, or on cancel) so a flaky
    provider can never inflate a run.

    Whether a zero-output error is retried is decided by ``classify``: a callable
    ``(message: str) -> bool`` that returns True for retryable reasons.  The
    default uses ``classify_api_error`` + ``recovery_policy`` so 429/5xx/timeout
    retry with backoff while auth/billing/context_overflow/400 fail fast.

    ``chat`` receives the same ``(messages, tools, *, system_prompt)`` args each
    attempt and is expected to be an async generator that yields typed events.
    """
    if max_attempts < 1:
        max_attempts = 1

    if classify is None:
        from modus.llm.errors import (
            FailoverReason, RecoveryAction, classify_api_error, recovery_policy,
        )

        def _default_classify(message: str) -> bool:
            # Extract an HTTP status from "API <code>: ..." style messages.
            status = 0
            prefix = "api "
            if message.lower().startswith(prefix):
                try:
                    status = int(message[len(prefix): message.index(":")])
                except (ValueError, IndexError):
                    status = 0
            lower = message.lower()
            if status == 0 and any(
                token in lower for token in ("connection", "timed out", "timeout",
                                             "read error", "reset", "stream interrupted")
            ):
                # Transport-level failures without an HTTP status are transient.
                return True
            classified = classify_api_error(RuntimeError(message), status)
            return recovery_policy(classified.reason) is RecoveryAction.RETRY_WITH_BACKOFF

        classify = _default_classify

    def _classify_failover(message: str) -> str:
        """Return the classified FailoverReason value for a provider error."""
        from modus.llm.errors import classify_api_error

        status = 0
        prefix = "api "
        if message.lower().startswith(prefix):
            try:
                status = int(message[len(prefix): message.index(":")])
            except (ValueError, IndexError):
                status = 0
        return classify_api_error(RuntimeError(message), status).reason.value

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
            if not classify(last_error):
                # Classified as non-retryable (auth/billing/context_overflow/
                # 400): surface it, do not burn attempts.
                yield {"type": "error", "error": last_error,
                       "failover": _classify_failover(last_error)}
                return
            if attempt >= max_attempts:
                yield {"type": "error", "error": last_error,
                       "failover": _classify_failover(last_error)}
                return
            # Zero-output transient failure: retry with backoff, budget-aware.
            budget = active_run_budget()
            delay = jittered_backoff(attempt, base_delay=0.5, max_delay=4.0)
            if budget is not None and budget.remaining_wall_seconds < delay:
                yield {"type": "error", "error": last_error,
                       "failover": _classify_failover(last_error)}
                return
            await asyncio.sleep(delay)
        return

    return retrying_chat