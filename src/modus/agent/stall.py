"""Deterministic stall detection (Wave4 G2): error-signature circuit breaker.

``run budget`` guards against runaway *duration*; this guards against running
in circles — the agent retrying the same failing operation or cycling between
two semantically-equivalent states (A -> B -> A -> B).  The whole detector is
pure signal: no LLM, no probability classifier (blueprint invariant 4 —
deterministic guards, never probabilistic classifiers), fully unit-testable.

Pipeline (ported from the loop-context reference, deterministically):

    error_text  ──error_signature()──>  normalized signature
    signatures  ──calculate_similarity()──>  trigram overlap
    attempts    ──check_circuit_breaker()──>  ok / watch / stall / loop
    loop        ──escalate──>  StopReason.STALLED (human handoff)

Triggering a level NEVER hard-breaks the loop by itself: it injects a
reference-only ``[STALL DETECTED]`` context block so the model can change
course.  Only a sustained ``loop`` escalates to a human handoff (see
``ReActReasoner`` in ``strategies/react.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Circuit-breaker levels (four-tier, from "all quiet" to "hand off to human").
LEVEL_OK = "ok"
LEVEL_WATCH = "watch"     # same error signature repeated >= 2 times
LEVEL_STALL = "stall"     # same error signature repeated >= 3 times, no progress
LEVEL_LOOP = "loop"       # semantic A->B->A->B cycle, or >= 5 no-progress attempts

# Thresholds (deterministic; tests pin them).
WATCH_THRESHOLD = 2
STALL_THRESHOLD = 3
LOOP_PATTERN_MIN_ATTEMPTS = 4    # A->B->A->B needs at least 4 attempts
LOOP_NO_PROGRESS_THRESHOLD = 5   # no-progress attempt cap before escalation
SIMILARITY_THRESHOLD = 0.55      # signatures treated as "the same error"
MAX_LEDGER_ATTEMPTS = 30         # bounded so a long run cannot grow it unbounded

_PATH_RE = re.compile(r"(?:\/[^\s\"'<>|?*]*)+")          # absolute paths
_QUOTED_RE = re.compile(r"\"[^\"]*\"|'[^']*'")           # quoted literals
_FILE_RE = re.compile(r"\b\w+(?:\.\w+)+\b")              # a.txt / v1.2.3
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")                 # numbers
# Punctuation/space collapse that PRESERVES the "<...>" placeholder brackets.
_NON_ALNUM_RE = re.compile(r"[^0-9a-z<>]+")
_WS_RE = re.compile(r"\s+")


def error_signature(error_text: str) -> str:
    """Normalize an error message into a comparable signature.

    Digits become ``<num>``, absolute path segments become ``<path>``, dotted
    file tokens become ``<file>``, and quoted literals collapse to ``<quote>``
    — so "FileNotFoundError: a.txt" and "FileNotFoundError: b.txt" (or
    "/tmp/a.txt" and "/var/b.txt") share one signature while structure such as
    the error class is kept.  The result is lowercase, whitespace-collapsed,
    punctuation-free.
    """
    if not error_text:
        return ""
    text = str(error_text).lower()
    # Order matters: paths before files (a path contains a file), files before
    # numbers (a file name may contain digits).
    text = _PATH_RE.sub("<path>", text)
    text = _QUOTED_RE.sub("<quote>", text)
    text = _FILE_RE.sub("<file>", text)
    text = _NUM_RE.sub("<num>", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _trigrams(text: str) -> set[str]:
    """3-char n-grams (language-agnostic; no tokenization needed)."""
    if len(text) < 3:
        padded = text.ljust(3)
        return {padded} if padded else set()
    return {text[i:i + 3] for i in range(len(text) - 2)}


def calculate_similarity(a: str, b: str) -> float:
    """Trigram character-overlap Dice coefficient between two signatures.

    ``0.0`` for completely different, ``1.0`` for identical.  Works on any
    language (no tokenizer): only raw trigram sets are compared.
    """
    sa = _trigrams(a)
    sb = _trigrams(b)
    if not sa or not sb:
        return 1.0 if a == b else 0.0
    overlap = len(sa & sb)
    return round((2.0 * overlap) / (len(sa) + len(sb)), 4)


@dataclass(slots=True)
class Attempt:
    """One recorded tool outcome in the stall ledger."""

    action: str                       # tool name, e.g. "run_tests"
    outcome: str                      # "success" / "error" / "skipped"
    error_signature: str = ""         # normalized signature ("" when no error)
    tokens: int = 0                   # tokens spent on this attempt
    similar_to: list[int] = field(default_factory=list)  # prior similar idxs

    @property
    def ok(self) -> bool:
        return self.outcome == "success"


@dataclass(slots=True)
class LevelResult:
    """Outcome of one circuit-breaker check."""

    level: str = LEVEL_OK
    count: int = 0                 # consecutive count of the repeated signature
    signature: str = ""            # repeated signature (if any)
    action: str = ""               # repeated action (if any)
    consecutive_loops: int = 0     # loop check points seen so far (no reset)
    pattern: list[str] = field(default_factory=list)  # detected ABAB (if any)


class Ledger:
    """Records tool outcomes and decides the stall level, deterministically.

    Thread-free by design: one loop owns one ledger.  The ledger keeps the
    bounded ``attempts`` list and the escalating ``consecutive_loops`` counter
    the reasoner uses to escalate a sustained loop to a human handoff.

    Semantics:
    - ``watch``/``stall`` count the *contiguous* suffix of error attempts that
      share one signature.  A success — or a genuinely different failure —
      breaks the streak (the agent changed course), so the level decays.
    - ``loop`` fires on the A->B->A->B action pattern (within the contiguous
      error run) or when the last N attempts are all failures.
    """

    def __init__(self, *, max_attempts: int = MAX_LEDGER_ATTEMPTS) -> None:
        self.max_attempts = max_attempts
        self.attempts: list[Attempt] = []
        self.consecutive_loops = 0
        # Idempotency guard: ``consecutive_loops`` advances at most once per new
        # ledger state, so calling ``check_circuit_breaker`` twice on the same
        # attempts list never double-counts a loop check point.
        self._last_loop_len = -1
        self._last_level = LEVEL_OK

    @property
    def level(self) -> str:
        """The last reported circuit-breaker level (``ok`` before any check)."""
        return self._last_level

    def add(
        self,
        *,
        action: str,
        outcome: str,
        error_text: str = "",
        tokens: int = 0,
    ) -> Attempt:
        """Record one tool outcome; returns the appended Attempt.

        The error signature is computed here (callers pass raw error text), and
        cross-similarity against prior error attempts is precomputed so the
        hot loop only pays for it once per attempt.
        """
        signature = error_signature(error_text)
        similar: list[int] = []
        if signature:
            for index, prior in enumerate(self.attempts):
                if prior.error_signature and (
                    prior.error_signature == signature
                    or calculate_similarity(prior.error_signature, signature)
                    >= SIMILARITY_THRESHOLD
                ):
                    similar.append(index)
        attempt = Attempt(
            action=action or "unknown",
            outcome=outcome,
            error_signature=signature,
            tokens=max(0, int(tokens)),
            similar_to=similar,
        )
        self.attempts.append(attempt)
        if len(self.attempts) > self.max_attempts:
            self.attempts = self.attempts[-self.max_attempts:]
        return attempt

    def check_circuit_breaker(self) -> LevelResult:
        """Return the current four-tier stall level.

        Priority: ``loop`` > ``stall`` > ``watch`` > ``ok``.  Idempotent per
        ledger state: repeated calls on the same attempts list return the same
        level and advance ``consecutive_loops`` at most once.  Any non-loop
        check point resets the escalation counter (the run changed course).
        """
        attempts = self.attempts
        loop_detected = False
        pattern: list[str] = []
        error_run = self._error_run()
        if len(error_run) >= LOOP_PATTERN_MIN_ATTEMPTS:
            tail = error_run[-LOOP_PATTERN_MIN_ATTEMPTS:]
            a, b = tail[0], tail[1]
            if a != b and (tail == [a, b, a, b] or tail == [b, a, b, a]):
                loop_detected = True
                pattern = list(tail)
        if not loop_detected:
            no_progress = attempts[-LOOP_NO_PROGRESS_THRESHOLD:]
            if (
                len(attempts) >= LOOP_NO_PROGRESS_THRESHOLD
                and len(no_progress) == LOOP_NO_PROGRESS_THRESHOLD
                and all(not a.ok for a in no_progress)
            ):
                loop_detected = True
        if loop_detected:
            if len(attempts) != self._last_loop_len:
                self.consecutive_loops += 1
                self._last_loop_len = len(attempts)
            self._last_level = LEVEL_LOOP
            return LevelResult(
                level=LEVEL_LOOP, consecutive_loops=self.consecutive_loops,
                pattern=pattern,
            )
        # Not looping: the agent changed course; reset the escalation counter.
        self.consecutive_loops = 0
        self._last_loop_len = -1
        streak = self._consecutive_same_error()
        if streak is not None:
            signature, action, count = streak
            if count >= STALL_THRESHOLD:
                self._last_level = LEVEL_STALL
                return LevelResult(
                    level=LEVEL_STALL, count=count,
                    signature=signature, action=action,
                )
            if count >= WATCH_THRESHOLD:
                self._last_level = LEVEL_WATCH
                return LevelResult(
                    level=LEVEL_WATCH, count=count,
                    signature=signature, action=action,
                )
        self._last_level = LEVEL_OK
        return LevelResult(level=LEVEL_OK)

    def recent_error_signatures(self, count: int = 3) -> list[str]:
        """The most recent ``count`` non-empty error signatures, newest first."""
        sigs = [a.error_signature for a in self.attempts if a.error_signature]
        return sigs[-max(1, count):][::-1]

    def diagnostic(self) -> dict[str, object]:
        """A human-readable summary for the written handoff to a human."""
        attempts = self.attempts[-10:]
        return {
            "level": self._last_level,
            "consecutive_loops": self.consecutive_loops,
            "attempts": [
                {
                    "action": a.action, "outcome": a.outcome,
                    "error_signature": a.error_signature, "tokens": a.tokens,
                }
                for a in attempts
            ],
        }

    # ── internals ─────────────────────────────────────────────────────────

    def _error_run(self) -> list[str]:
        """Contiguous suffix of non-success attempts (their actions).

        A success breaks the run: it is progress, so patterns that merely look
        alternating across a success are not a loop.
        """
        run: list[str] = []
        for attempt in reversed(self.attempts):
            if attempt.ok:
                break
            run.append(attempt.action)
        run.reverse()
        return run

    def _consecutive_same_error(self) -> tuple[str, str, int] | None:
        """(signature, action, count) of the trailing same-signature error run.

        Walks backwards from the newest attempt, stopping at the first success
        or the first error whose signature is not similar to the run's head.
        """
        count = 0
        ref: str | None = None
        action = ""
        for attempt in reversed(self.attempts):
            if attempt.ok or not attempt.error_signature:
                break
            sig = attempt.error_signature
            if ref is None:
                ref = sig
                action = attempt.action
            elif ref != sig and calculate_similarity(ref, sig) < SIMILARITY_THRESHOLD:
                break
            count += 1
        if count == 0:
            return None
        return ref, action, count


def build_stall_context_block(
    level: str,
    *,
    action: str = "",
    count: int = 0,
    signature: str = "",
    pattern: list[str] | None = None,
) -> str:
    """Render the reference-only ``[STALL DETECTED]`` context block.

    NOT an instruction channel: it informs the model of the failing pattern and
    suggests a course change, exactly like the goal-steering / self-adapt hints.
    The reasoner injects this as a plain user message; the model stays in charge.
    """
    if level == LEVEL_STALL:
        lines = [
            "[STALL DETECTED — REFERENCE ONLY]",
            (
                f"'{action or 'an operation'}' failed {count} times with the "
                f"same error signature. The failing pattern appears stuck. "
                f"Stop retrying this operation and switch to a different path "
                f"or approach before your next call."
            ),
        ]
        if signature:
            lines.append(f"Failed pattern (x{count}): {signature}")
        return "\n".join(lines)
    if level == LEVEL_LOOP:
        if pattern:
            shown = " -> ".join(pattern)
            lines = [
                "[LOOP DETECTED — REFERENCE ONLY]",
                (
                    f"The run is cycling between operations: {shown}. This "
                    f"loop is not converging. Break the cycle: choose one of "
                    f"these actions and go deeper, or change the approach "
                    f"entirely."
                ),
            ]
        else:
            lines = [
                "[LOOP DETECTED — REFERENCE ONLY]",
                (
                    f"The last {LOOP_NO_PROGRESS_THRESHOLD} attempts all "
                    f"failed with no success. Stop retrying and switch to a "
                    f"different strategy or report what is blocking you."
                ),
            ]
        return "\n".join(lines)
    return ""


__all__ = [
    "Attempt", "Ledger", "LevelResult",
    "LEVEL_OK", "LEVEL_WATCH", "LEVEL_STALL", "LEVEL_LOOP",
    "WATCH_THRESHOLD", "STALL_THRESHOLD", "LOOP_PATTERN_MIN_ATTEMPTS",
    "LOOP_NO_PROGRESS_THRESHOLD", "SIMILARITY_THRESHOLD", "MAX_LEDGER_ATTEMPTS",
    "build_stall_context_block", "calculate_similarity", "error_signature",
]
