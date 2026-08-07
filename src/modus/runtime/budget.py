"""Deterministic per-run limits and machine-readable stop semantics."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum

from modus.runtime.verification import RunVerification


class StopReason(StrEnum):
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    TOKEN_LIMIT = "token_limit"
    WALL_TIME = "wall_time"
    CANCELLED = "cancelled"
    ENGINE_ERROR = "engine_error"
    FAILED = "failed"
    VERIFICATION_REQUIRED = "verification_required"
    VERIFICATION_RETRY_LIMIT = "verification_retry_limit"
    # The loop produced no text and no successful tool result for several
    # consecutive turns (self-aware stall detection).
    NO_PROGRESS = "no_progress"


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """One turn's observable outcome for self-inspection.

    The signals distinguish *model activity* (it is responding to the world)
    from *silence*.  A working model that probes with failing tools, thinks
    aloud, or edits is active; a degenerate model that emits nothing for N turns
    is stalled.  ``made_progress`` therefore credits text, thinking, and any
    attempted tool call — not tool success, because non-zero exits and failing
    test runs are normal investigative output.
    """

    turn: int
    text_chars: int = 0
    thinking_chars: int = 0
    tool_calls: int = 0
    tool_successes: int = 0
    tool_errors: int = 0
    result_chars: int = 0
    tokens: int = 0
    stop_reason: str = ""

    def made_progress(self) -> bool:
        """A turn shows model activity: text, thinking, or any tool attempt.

        Tool *errors* do not count against progress because a debugging model
        legitimately strings together failing probes (run_tests red, bash
        non-zero, read_file refusals) without narrating.
        """
        return (
            self.text_chars > 0
            or self.thinking_chars > 0
            or self.tool_calls > 0
        )


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: StopReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RunLimits:
    max_turns: int = 20
    max_tokens: int = 200_000
    max_wall_seconds: float = 600.0
    max_verification_attempts: int = 3

    def __post_init__(self) -> None:
        if (
            self.max_turns < 1
            or self.max_tokens < 1
            or self.max_wall_seconds <= 0
            or self.max_verification_attempts < 1
        ):
            raise ValueError("run limits must be positive")


class RunBudget:
    """One monotonic accounting source shared by every phase of a run."""

    def __init__(self, limits: RunLimits | None = None) -> None:
        self.limits = limits or RunLimits()
        self.started_at = time.monotonic()
        self.turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop_reason: StopReason | None = None
        self.verification = RunVerification(max_attempts=self.limits.max_verification_attempts)
        # Per-role attribution (e.g. "moa:reference_1", "peri:worker_2").
        # Totals stay authoritative; this ledger only explains where they went.
        self.usage_ledger: dict[str, dict[str, int]] = {}
        # Self-observation: one record per completed turn, oldest first.  The
        # loop reads this to detect stall (no progress) and to inform adaptive
        # behavior.  Bounded so a long run cannot grow it unbounded.
        self.turn_records: list[TurnRecord] = []
        # Optional billing hook (default None keeps CLI/embedder/runner free).
        self._quota_check: Callable[[int, int], bool] | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    @property
    def remaining_wall_seconds(self) -> float:
        return max(0.0, self.limits.max_wall_seconds - self.elapsed_seconds)

    def begin_turn(self) -> int:
        self.check_limits()
        if self.turns >= self.limits.max_turns:
            self.stop_reason = StopReason.MAX_TURNS
            raise BudgetExceeded(self.stop_reason)
        self.turns += 1
        return self.turns

    def record_usage(self, input_tokens: int = 0, output_tokens: int = 0, *, owner: str | None = None) -> None:
        input_tokens = max(0, int(input_tokens))
        output_tokens = max(0, int(output_tokens))
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if owner:
            entry = self.usage_ledger.setdefault(owner, {"input_tokens": 0, "output_tokens": 0})
            entry["input_tokens"] += input_tokens
            entry["output_tokens"] += output_tokens

    def attach_quota_check(self, check: Callable[[int, int], bool] | None) -> None:
        """Attach an optional balance callback ``check(input, output) -> ok``.

        When attached, ``check_limits`` stops the run once the callback reports
        the accumulated usage exceeds the remaining balance.  Default None keeps
        CLI / embedder / test runs unaffected.
        """
        self._quota_check = check

    def check_limits(self) -> None:
        if self.elapsed_seconds >= self.limits.max_wall_seconds:
            self.stop_reason = StopReason.WALL_TIME
            raise BudgetExceeded(self.stop_reason)
        if self.total_tokens >= self.limits.max_tokens:
            self.stop_reason = StopReason.TOKEN_LIMIT
            raise BudgetExceeded(self.stop_reason)
        if self._quota_check is not None:
            try:
                ok = self._quota_check(self.input_tokens, self.output_tokens)
            except Exception:
                ok = True  # a failed quota probe must not kill the run
            if not ok:
                self.stop_reason = StopReason.TOKEN_LIMIT
                raise BudgetExceeded(self.stop_reason)

    def finish(self, reason: StopReason) -> StopReason:
        if self.stop_reason is None:
            self.stop_reason = reason
        return self.stop_reason

    def record_turn(
        self,
        *,
        turn: int,
        text_chars: int = 0,
        thinking_chars: int = 0,
        tool_calls: int = 0,
        tool_successes: int = 0,
        tool_errors: int = 0,
        result_chars: int = 0,
        tokens: int = 0,
        stop_reason: str = "",
    ) -> None:
        """Append one turn's observable outcome to the self-observation ledger."""
        self.turn_records.append(TurnRecord(
            turn=max(1, int(turn)),
            text_chars=max(0, int(text_chars)),
            thinking_chars=max(0, int(thinking_chars)),
            tool_calls=max(0, int(tool_calls)),
            tool_successes=max(0, int(tool_successes)),
            tool_errors=max(0, int(tool_errors)),
            result_chars=max(0, int(result_chars)),
            tokens=max(0, int(tokens)),
            stop_reason=stop_reason,
        ))
        if len(self.turn_records) > 200:
            # Keep the ledger bounded; the newest turns are what stall
            # detection reads.
            self.turn_records = self.turn_records[-200:]

    def recent_turn_records(self, count: int = 5) -> list[TurnRecord]:
        """Return the newest ``count`` turn records, oldest-first within the window."""
        return self.turn_records[-max(1, count):]

    def stalled_for(
        self,
        threshold: int = 3,
        *,
        warmup_turns: int = 2,
        min_elapsed_seconds: float = 0.0,
    ) -> bool:
        """True when the last ``threshold`` turns all made no progress.

        Guards against false positives on fresh runs: requires at least
        ``warmup_turns`` on record and (optionally) a minimum elapsed wall-clock
        before the window can be considered stalled.
        """
        if threshold <= 0:
            return False
        if len(self.turn_records) < max(warmup_turns, threshold):
            return False
        if self.elapsed_seconds < min_elapsed_seconds:
            return False
        recent = self.recent_turn_records(threshold)
        if len(recent) < threshold:
            return False
        return all(not record.made_progress() for record in recent)

    def trends(self, window: int = 5) -> dict[str, Any]:
        """Summarize recent turns for self-adaptive behavior (pure, no side effects).

        Returns a compact dict a reasoner can read each turn to decide bounded
        adaptations:
        - ``tool_error_rate``: fraction of recent tool attempts that errored.
        - ``tool_error_hotspot``: the single tool with the most errors, or None.
        - ``consecutive_no_progress``: current no-progress streak.
        - ``error_classes``: distinct failover/stop reasons seen recently.
        - ``text_silence_ratio``: fraction of recent turns with no text output.
        """
        records = self.recent_turn_records(window)
        if not records:
            return {
                "tool_error_rate": 0.0, "tool_error_hotspot": None,
                "consecutive_no_progress": 0, "error_classes": [],
                "text_silence_ratio": 0.0,
            }
        attempts = sum(r.tool_calls for r in records)
        errors = sum(r.tool_errors for r in records)
        streak = 0
        error_classes: set[str] = set()
        silent = 0
        for record in records:
            if record.stop_reason and record.stop_reason != "completed":
                error_classes.add(record.stop_reason)
            if record.made_progress():
                streak = 0
            else:
                streak += 1
            if record.text_chars == 0:
                silent += 1
        return {
            "tool_error_rate": round(errors / attempts, 3) if attempts else 0.0,
            # True when the recent window had repeated tool failures; a reasoner
            # can use this to suggest a corrective hint (bounded, not a loop).
            "tool_error_hotspot": errors > 0 and errors >= (attempts or 1) * 0.5,
            "consecutive_no_progress": streak,
            "error_classes": sorted(error_classes),
            "text_silence_ratio": round(silent / len(records), 3),
        }

    def snapshot(self) -> dict[str, object]:
        return {
            "max_turns": self.limits.max_turns,
            "max_tokens": self.limits.max_tokens,
            "max_wall_seconds": self.limits.max_wall_seconds,
            "max_verification_attempts": self.limits.max_verification_attempts,
            "turns": self.turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "usage_ledger": {owner: dict(entry) for owner, entry in sorted(self.usage_ledger.items())},
            "turn_records": [
                {
                    "turn": record.turn, "text_chars": record.text_chars,
                    "thinking_chars": record.thinking_chars,
                    "tool_calls": record.tool_calls, "tool_successes": record.tool_successes,
                    "tool_errors": record.tool_errors, "result_chars": record.result_chars,
                    "tokens": record.tokens, "stop_reason": record.stop_reason,
                }
                for record in self.turn_records
            ],
        }


_ACTIVE_BUDGET: ContextVar[RunBudget | None] = ContextVar(
    "modus_active_run_budget", default=None,
)


def active_run_budget() -> RunBudget | None:
    return _ACTIVE_BUDGET.get()


def bind_run_budget(budget: RunBudget) -> Token[RunBudget | None]:
    return _ACTIVE_BUDGET.set(budget)


def reset_run_budget(token: Token[RunBudget | None]) -> None:
    _ACTIVE_BUDGET.reset(token)
