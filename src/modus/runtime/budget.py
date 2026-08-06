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
