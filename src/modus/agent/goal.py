"""Goal state machine for cross-turn, goal-driven continuation (Wave4 G1).

One ``modus -p "把测试跑绿"`` run exhausts its budget and stops.  This module
gives Modus the *offensive* continuation the design calls for: a persistent
per-session ``GoalState`` the agent loop can inspect each turn, an idle
continuation hook that re-injects the objective when the user is not
interacting, a 3-strike blocked gate so "hard / slow" never reads as
"impossible", and a model-side ``goal`` tool for self-reporting.

Design invariants (from docs/dev-wave4-autonomy.md):
- Session isolation: goals are keyed by ``session_id``; concurrent sub-sessions
  never share a goal.
- User input always beats auto-continuation: the reasoner's idle hook only
  injects steering when the user message queue is empty (the loop checks
  ``cancel_event`` and new user turns before it ever steers).
- ``budget_limited`` is a *soft* state: the run injects a summarization prompt
  instead of hard-stopping, so progress already made is preserved and the goal
  can resume next round.
- 3-strike blocked: the same reason must be seen on three *consecutive* goal
  attempts before the goal is marked blocked — a single hard/slow reason never
  trips it, and a different reason resets the count.
- JSONL persistence under ``~/.modus/goals/{session_id}.jsonl``; every state
  change appends one record, and a ``goal-cleared`` tombstone prevents a
  cleared goal from being resurrected by an older record.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from modus.paths import data_path

# A goal is only marked blocked after the same reason is seen this many
# consecutive times.  This is what stops "难、慢" from being misread as "放弃".
BLOCKED_CONSECUTIVE_THRESHOLD = 3

# The 7-state machine's status values.  A str (not a StrEnum) keeps JSONL
# records trivially serializable and the status easy to embed in a meta prompt.
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_BUDGET_LIMITED = "budget_limited"
STATUS_MAX_TURNS = "max_turns"
STATUS_BLOCKED = "blocked"
STATUS_COMPLETE = "complete"

_RUNNABLE_STATUSES = frozenset({
    STATUS_ACTIVE,
    STATUS_BUDGET_LIMITED,  # soft limit: a fresh round may resume and summarize
})
_TERMINAL_STATUSES = frozenset({STATUS_COMPLETE, STATUS_BLOCKED})

# Marker for the goal-cleared tombstone record (prevents resurrection).
_TOMBSTONE = "goal-cleared"

# How long a goal stays paused before the idle hook may offer a resume (ms).
# Mirrors the accumulated-active accounting used by the CCB reference.
IDLE_RESUME_COOLDOWN_MS = 30_000


def _goals_root() -> Path:
    """``~/.modus/goals`` — pure file persistence, no DB table, no db.py."""
    return Path(data_path("goals"))


class GoalError(RuntimeError):
    """Raised for invalid goal-state transitions / usage (never internal)."""


@dataclass(slots=True)
class GoalState:
    """One session's persistent goal + cross-turn accounting.

    The state machine transitions are centralized in ``GoalStore`` so a goal
    can only move along the documented edges:
      active -> paused / resume
      active -> budget_limited (soft, via ``update_tokens``)
      active -> max_turns (reset-able via ``/goal continue`` / ``resume``)
      active -> blocked (same reason 3 consecutive times)
      active -> complete (via the model's GoalTool.complete)
      any running state -> budget_limited / max_turns
    """

    objective: str
    status: str = STATUS_ACTIVE
    tokens_used: int = 0
    turns_executed: int = 0
    blocked_reason: str = ""
    blocked_count: int = 0
    accumulated_active_ms: int = 0
    created_at: float = field(default_factory=time.time)

    def is_runnable(self) -> bool:
        return self.status in _RUNNABLE_STATUSES

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


class GoalStore:
    """Per-session goal store with JSONL persistence under ~/.modus/goals/.

    ``Map[session_id, GoalState]`` in process (session-isolated) with an
    append-only JSONL file per session so the goal survives process restarts.
    Every mutation appends one line; ``clear`` appends a ``goal-cleared``
    tombstone so replay never revives a cleared goal.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else _goals_root()
        self._goals: dict[str, GoalState] = {}
        self._lock = threading.Lock()
        self._disabled = False

    # ── public API ─────────────────────────────────────────────────────────

    def set(self, session_id: str | None, objective: str) -> GoalState:
        """Create or replace the active goal for a session.

        A new goal resets the blocked/limit accounting so the fresh objective
        starts clean.  Returns the new state.
        """
        key = _session_key(session_id)
        if not objective or not objective.strip():
            raise GoalError("goal objective must be non-empty")
        state = GoalState(objective=str(objective).strip())
        with self._lock:
            self._goals[key] = state
        self._append(key, state)
        return state

    def get(self, session_id: str | None) -> GoalState | None:
        return self._goals.get(_session_key(session_id))

    def active(self, session_id: str | None) -> GoalState | None:
        """Return the runnable goal for a session, if any (skips pause/terminal)."""
        state = self.get(session_id)
        if state is None or not state.is_runnable():
            return None
        return state

    def pause(self, session_id: str | None) -> GoalState:
        state = self._require(session_id)
        if not state.is_terminal():
            state.status = STATUS_PAUSED
            self._append(_session_key(session_id), state)
        return state

    def resume(self, session_id: str | None) -> GoalState:
        """Resume a goal.

        A ``max_turns`` goal is reset here (``/goal continue`` semantics): the
        turn counter is cleared so a fresh round gets a fresh budget while the
        objective and token accounting carry over.  A blocked goal can only be
        resumed through ``clear``.
        """
        state = self._require(session_id)
        if state.status == STATUS_MAX_TURNS:
            state.turns_executed = 0
        if not state.is_terminal():
            state.status = STATUS_ACTIVE
            self._append(_session_key(session_id), state)
        return state

    def clear(self, session_id: str | None) -> None:
        """Clear the goal for a session, writing a tombstone.

        Clearing never deletes the JSONL file (the audit trail stays); the
        tombstone record guarantees a later ``get`` returns None even after
        replay.
        """
        key = _session_key(session_id)
        with self._lock:
            self._goals.pop(key, None)
        self._append(key, _TOMBSTONE)

    def complete(self, session_id: str | None) -> GoalState | None:
        """Mark the active goal complete (model's GoalTool.complete)."""
        state = self.get(session_id)
        if state is None or state.is_terminal():
            return None
        state.status = STATUS_COMPLETE
        self._append(_session_key(session_id), state)
        return state

    # ── 3-strike blocked ───────────────────────────────────────────────────

    def record_blocked_attempt(self, session_id: str | None, reason: str) -> GoalState | None:
        """Register one blocked attempt; only 3 consecutive same-reason trips block.

        A different reason (or a success in between) resets the counter, so a
        single hard/slow obstacle never misclassifies the goal as impossible.
        """
        state = self._require(session_id)
        reason = (reason or "unspecified").strip() or "unspecified"
        if state.is_terminal():
            return state
        if state.blocked_reason == reason:
            state.blocked_count += 1
        else:
            state.blocked_reason = reason
            state.blocked_count = 1
        if state.blocked_count >= BLOCKED_CONSECUTIVE_THRESHOLD:
            state.status = STATUS_BLOCKED
        self._append(_session_key(session_id), state)
        return state

    # ── budget / turn accounting ───────────────────────────────────────────

    def update_tokens(self, session_id: str | None, usage: dict[str, Any] | None = None, *, total: int | None = None) -> GoalState | None:
        """Accumulate input+output+cache tokens; cross the token budget -> soft limit.

        ``budget_limited`` is deliberately *soft*: the run's reasoner injects a
        summarization prompt and stops attacking, but the goal survives and can
        be resumed next round.  Only used when the caller supplies an explicit
        ``total`` token budget via ``update_tokens(total=...)``.
        """
        state = self.get(session_id)
        if state is None or state.is_terminal():
            return state
        if usage:
            state.tokens_used += int(usage.get("input_tokens") or 0)
            state.tokens_used += int(usage.get("output_tokens") or 0)
            state.tokens_used += int(usage.get("cache_read_input_tokens") or 0)
        if total is not None and state.tokens_used >= total and state.status == STATUS_ACTIVE:
            state.status = STATUS_BUDGET_LIMITED
        self._append(_session_key(session_id), state)
        return state

    def record_turn(self, session_id: str | None, *, count: int = 1, tokens: int = 0, budget_total: int | None = None) -> GoalState | None:
        """Add executed turns (and optional tokens); cross turn budget -> max_turns."""
        state = self.get(session_id)
        if state is None or state.is_terminal():
            return state
        state.turns_executed += max(0, int(count))
        if tokens:
            state.tokens_used += max(0, int(tokens))
        if (
            state.status == STATUS_ACTIVE
            and budget_total is not None
            and state.turns_executed >= budget_total
        ):
            state.status = STATUS_MAX_TURNS
        self._append(_session_key(session_id), state)
        return state

    # ── persistence ────────────────────────────────────────────────────────

    def load(self, session_id: str | None) -> GoalState | None:
        """Hydrate a session's latest goal from its JSONL file (restart resume).

        Replays records in order; the final record wins.  A trailing
        ``goal-cleared`` tombstone yields None (the goal stays cleared).
        """
        key = _session_key(session_id)
        path = self._path_for(key)
        state: GoalState | None = None
        if path.exists():
            for line in _read_lines(path):
                event = _parse_record(line)
                if event is None:
                    continue
                if event == _TOMBSTONE:
                    state = None
                elif isinstance(event, dict):
                    state = _state_from_record(event)
        with self._lock:
            self._goals[key] = state
        return state

    def mark_paused_for_idle(self, session_id: str | None) -> GoalState | None:
        """Pause a budget_limited / paused goal so the idle hook does not re-steer.

        Called by the loop after a soft limit; the goal can be resumed by the
        user (``/goal resume``) or a later round.
        """
        state = self.get(session_id)
        if state is None or state.is_terminal() or state.status == STATUS_PAUSED:
            return state
        state.status = STATUS_PAUSED
        self._append(_session_key(session_id), state)
        return state

    def mark_budget_limited(self, session_id: str | None) -> GoalState | None:
        """Soft-limit transition: active -> budget_limited (token budget spent).

        Never a hard stop: the loop injects a summarization prompt and the goal
        survives to be resumed next round.
        """
        state = self.get(session_id)
        if state is None or state.is_terminal():
            return state
        state.status = STATUS_BUDGET_LIMITED
        self._append(_session_key(session_id), state)
        return state

    def mark_max_turns(self, session_id: str | None) -> GoalState | None:
        """Soft-limit transition: active -> max_turns (turn budget spent).

        ``resume`` (``/goal continue``) resets ``turns_executed`` so a fresh
        round keeps the objective and token accounting.
        """
        state = self.get(session_id)
        if state is None or state.is_terminal():
            return state
        state.status = STATUS_MAX_TURNS
        self._append(_session_key(session_id), state)
        return state

    def add_active_ms(self, session_id: str | None, ms: int) -> None:
        state = self.get(session_id)
        if state is None or state.is_terminal():
            return
        state.accumulated_active_ms += max(0, int(ms))
        self._append(_session_key(session_id), state)

    # ── internals ──────────────────────────────────────────────────────────

    def _require(self, session_id: str | None) -> GoalState:
        state = self.get(session_id)
        if state is None:
            raise GoalError("no active goal for this session")
        return state

    def _path_for(self, key: str) -> Path:
        return self._root / f"{key}.jsonl"

    def _append(self, key: str, record: GoalState | str) -> None:
        if self._disabled:
            return
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            if isinstance(record, str):
                line = json.dumps({"event": record}, ensure_ascii=False)
            else:
                line = json.dumps(_record_from_state(record), ensure_ascii=False)
            with self._root.joinpath(f"{key}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # Persistence is best-effort: an unwritable ~/.modus must never
            # break the in-memory goal machine.
            return

    def snapshot(self, session_id: str | None) -> dict[str, Any] | None:
        """Public, serializable view of a session's goal for the ``goal get`` tool."""
        state = self.get(session_id)
        return _record_from_state(state) if state is not None else None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"GoalStore({len(self._goals)} goals)"


def _session_key(session_id: str | None) -> str:
    # ``None`` (plain CLI) shares the "default" goal for the process, matching
    # the grant-store convention.  The key is sanitized so it can never escape
    # the goals directory (a hostile session_id must not write outside it).
    raw = str(session_id or "default")
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in "-_.")
    return safe or "default"


def _record_from_state(state: GoalState) -> dict[str, Any]:
    return asdict(state)


def _state_from_record(record: dict[str, Any]) -> GoalState:
    try:
        return GoalState(**record)
    except TypeError:
        # A forward-compatible record (extra/missing fields) hydrates what it
        # can; the machine falls back to defaults for anything absent.
        allowed = set(GoalState.__dataclass_fields__)
        filtered = {k: v for k, v in record.items() if k in allowed}
        return GoalState(**filtered)


def _parse_record(line: str) -> str | dict[str, Any] | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    if event.get("event") == _TOMBSTONE:
        return _TOMBSTONE
    if "objective" in event:
        return event
    return None


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


# ── shared default store ──────────────────────────────────────────────────

_DEFAULT_STORE: GoalStore | None = None
_STORE_LOCK = threading.Lock()


def default_goal_store() -> GoalStore:
    """Process-wide default store (module-level, mirrors react's grant store)."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _STORE_LOCK:
            if _DEFAULT_STORE is None:
                _DEFAULT_STORE = GoalStore()
    return _DEFAULT_STORE


# ── idle continuation payload builders (consumed by react.py) ─────────────

COMPLETION_AUDIT_LINES = (
    "1. 断言/验证通过了才叫完成，绝不只凭'看起来对了'。",
    "2. 每步都留证据（测试输出、日志、diff），失败也要记录。",
    "3. 卡住时换路径，不原地重试；连换三次都不同因才可标记 blocked。",
    "4. 预算/轮次受限时先总结已达成与剩余，再停（可续）。",
    "5. 用户新消息/取消永远优先，听到就停。",
    "6. 不伪造成功；做不到就如实报告，让用户决策。",
)


def build_goal_steering(state: GoalState, *, turns_budget: int | None = None, tokens_budget: int | None = None) -> str:
    """Build the ``<goal-steering>`` meta message injected on idle turns.

    Objective + token/turn state + Completion Audit, reference-only (the loop
    appends it as a plain user message, exactly like the existing self-adapt /
    diagnostics hints — it is steering context, never a new instruction
    channel).
    """
    status = state.status
    if status == STATUS_BUDGET_LIMITED:
        status_note = (
            "状态：budget_limited（软限制）——请先总结已达成与剩余，本次续跑到此为止。"
        )
    elif status == STATUS_MAX_TURNS:
        status_note = "状态：max_turns（本回合计数已重置）——本轮从累计进度继续逼近目标。"
    else:
        status_note = "状态：active —— 自动续跑中，继续逼近目标。"
    token_line = f"tokens_used={state.tokens_used}"
    if tokens_budget is not None:
        token_line += f"/{tokens_budget}"
    turn_line = f"turns_executed={state.turns_executed}"
    if turns_budget is not None:
        turn_line += f"/{turns_budget}"
    return (
        "<goal-steering>\n"
        f"objective: {state.objective}\n"
        f"{status_note}\n"
        f"{token_line}\n"
        f"{turn_line}\n"
        "completion_audit:\n"
        + "\n".join(f"  {line}" for line in COMPLETION_AUDIT_LINES)
        + "\n</goal-steering>"
    )


def build_budget_limited_summary_prompt(state: GoalState) -> str:
    """Summarization prompt injected after a budget limit (soft stop, not hard)."""
    return (
        "<goal-summary-request>\n"
        "本次运行的 token 预算已用尽。请不要继续执行新操作。\n"
        f"objective: {state.objective}\n"
        f"tokens_used={state.tokens_used}\n"
        "请输出一段简短总结：已达成什么、还差什么、下一步建议。\n"
        "</goal-summary-request>"
    )


# ── model-side goal tool (deferred, safe + read_only + memory) ────────────

_GOAL_ACTIONS = ("get", "update", "complete", "blocked")


async def _goal_handler_impl(payload: dict[str, Any], context: Any, store: GoalStore) -> Any:
    """Model-side ``goal`` tool implementation (CCB GoalTool port).

    - ``get``      : return the session's goal snapshot (read-only).
    - ``update``   : add token/turn accounting (e.g. after a long build).
    - ``complete`` : mark the goal complete, carrying an optional usage report.
    - ``blocked``  : register a blocked reason — only the 3rd *consecutive*
                     same reason flips the goal to blocked.

    Declared safe + read-only + memory capability: it never touches the
    filesystem, never needs approval, and only reads/updates the session's own
    goal state.
    """
    from modus.tools.base import ToolResult

    action = str(payload.get("action") or "get").strip().lower()
    if action not in _GOAL_ACTIONS:
        return ToolResult(
            f"goal action must be one of {', '.join(_GOAL_ACTIONS)}; got {action!r}",
            is_error=True,
        )
    session_id = getattr(context, "session_id", None)

    if action == "get":
        snap = store.snapshot(session_id)
        if snap is None:
            return ToolResult("No active goal for this session. Use /goal <objective> to set one.")
        import json as _json

        return ToolResult(_json.dumps(snap, ensure_ascii=False))

    if action == "update":
        usage = payload.get("usage") or {}
        total = payload.get("total_tokens_budget")
        state = store.update_tokens(
            session_id, usage, total=int(total) if total is not None else None,
        )
        if state is None:
            return ToolResult("No active goal for this session.")
        return ToolResult(
            f"goal updated: tokens_used={state.tokens_used} turns={state.turns_executed}"
        )

    if action == "complete":
        state = store.complete(session_id)
        if state is None:
            return ToolResult("No active goal to complete.")
        usage = payload.get("usage") or {}
        report = [
            f"goal complete: {state.objective}",
            f"tokens_used={state.tokens_used}",
            f"turns_executed={state.turns_executed}",
        ]
        if usage:
            report.append(f"usage={dict(usage)}")
        return ToolResult("\n".join(report), metadata={"operation": "goal-complete"})

    # action == "blocked"
    reason = str(payload.get("reason") or "").strip()
    if not reason:
        return ToolResult("goal blocked requires a 'reason'", is_error=True)
    state = store.record_blocked_attempt(session_id, reason)
    if state is None:
        return ToolResult("No active goal for this session.")
    if state.status == STATUS_BLOCKED:
        return ToolResult(
            f"goal marked blocked after {BLOCKED_CONSECUTIVE_THRESHOLD} consecutive "
            f"same-reason attempts: {state.blocked_reason}"
        )
    return ToolResult(
        f"blocked attempt {state.blocked_count}/{BLOCKED_CONSECUTIVE_THRESHOLD} "
        f"recorded (reason={state.blocked_reason}); keep trying alternate paths."
    )


def make_goal_tool(store: GoalStore | None = None) -> Any:
    """Build the deferred ``goal`` Tool for the model-side receipt channel.

    ``store`` is bound into the handler so a reasoner can wire the SAME store
    it inspects for steering/limits.  ``None`` falls back to the process-wide
    default store.
    """
    from modus.tools.base import Tool, object_schema

    async def _handler(payload: dict[str, Any], context: Any) -> Any:
        actual = store
        if actual is None:
            actual = getattr(context, "goal_store", None)
        if actual is None:
            actual = default_goal_store()
        return await _goal_handler_impl(payload, context, actual)

    return Tool(
        name="goal",
        description=(
            "Manage the session's cross-turn goal. Actions: get (current "
            "objective/status/tokens), update (record token/turn usage), "
            "complete (mark the goal done, optionally with a usage report), "
            "blocked (record a blocked reason; only 3 consecutive identical "
            "reasons block the goal). Read-only — never touches files."
        ),
        parameters=object_schema({
            "action": {
                "type": "string",
                "description": "One of get, update, complete, blocked",
            },
            "reason": {
                "type": "string",
                "description": "Blocked reason (action=blocked only)",
            },
            "usage": {
                "type": "object",
                "description": "Usage counters {input_tokens, output_tokens, cache_read_input_tokens}",
            },
            "total_tokens_budget": {
                "type": "number",
                "description": "Optional token budget to test against (action=update)",
            },
        }, ["action"]),
        required_keys=["action"],
        handler=_handler,
        is_read_only=True,
        is_concurrency_safe=True,
        danger_level="safe",
        data_disclosure="none",
        capabilities=("memory",),
    )


class GoalReasoner:
    """Goal-aware reasoner: a thin ReAct wrapper that keeps the cross-turn goal
    in the loop (``agent_mode="goal"``).

    ``select.py`` returns this class under ``agent_mode="goal"``.  It constructs
    a normal ``ReActReasoner`` bound to the same store it uses for steering and
    soft limits, so the objective survives across runs without touching the
    strategy's event vocabulary, budget, or safety boundaries.

    The wrapper is intentionally minimal: the goal machinery lives in the
    underlying ReAct loop (idle steering + soft limits + the ``goal`` tool); this
    class only wires the store and per-goal budgets through.
    """

    def __init__(self, **kwargs: Any) -> None:
        from modus.agent.strategies import ReActReasoner

        self._kwargs = dict(kwargs)
        # When the caller did not hand us a store, resolve the process-wide one
        # once so the reasoner and its tool share the same GoalStore.
        if self._kwargs.get("goal_store") is None:
            self._kwargs["goal_store"] = default_goal_store()
        self._reasoner: Any = None

    def run(self, messages: list[Any], **run_kwargs: Any):
        from modus.agent.strategies import ReActReasoner

        if self._reasoner is None:
            self._reasoner = ReActReasoner(**self._kwargs)
        return self._reasoner.run(messages, **run_kwargs)
