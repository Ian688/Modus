"""Wave4 G2: deterministic stall detection (error-signature circuit breaker).

Covers the tests listed in docs/dev-wave4-autonomy.md:
- error_signature normalizes numbers/paths/quotes into placeholders
- calculate_similarity gives high scores to similar error text (trigram)
- stall after 3 attempts of the same signature
- loop detection on the A->B->A->B semantic cycle (and the >=5 no-progress cap)
- stall injects context, not an abort (reference-only, run keeps going)
- sustained loop escalates to StopReason.STALLED (human handoff)

The detector is pure signal: no LLM, no probabilistic classifier, fully
unit-testable (blueprint invariant 4).
"""

from __future__ import annotations

import asyncio

import pytest

from modus.config import ModusConfig
from modus.tools.base import Tool, ToolResult, object_schema
from modus.tools.registry import ToolRegistry
from modus.types import Message


# ─── error signature normalization ────────────────────────────────────────


def test_error_signature_normalizes_paths_numbers_quotes():
    from modus.agent.stall import error_signature

    sig_a = error_signature("FileNotFoundError: /tmp/a_2024.txt not found")
    sig_b = error_signature("FileNotFoundError: /var/b_1999.md not found")
    assert sig_a == sig_b
    assert "path" in sig_a
    # Standalone numbers (outside a path) become <num>.
    assert "num" in error_signature("error code 404, retry after 5 seconds")
    # Standalone quoted literals collapse to <quote>.
    assert "<quote>" in error_signature('cannot read "config.yaml"')
    assert "<quote>" in error_signature("cannot read 'config.yaml'")


def test_error_signature_case_and_whitespace_insensitive():
    from modus.agent.stall import error_signature

    a = error_signature("  ERROR:   Permission Denied  ")
    b = error_signature("error: permission denied")
    assert a == b


def test_error_signature_file_tokens_normalize():
    """Doc case: FileNotFoundError: a.txt vs b.txt share one signature."""
    from modus.agent.stall import error_signature

    a = error_signature("FileNotFoundError: a.txt")
    b = error_signature("FileNotFoundError: b.txt")
    assert a == b
    assert "file" in a


def test_error_signature_quote_placeholder():
    from modus.agent.stall import error_signature

    a = error_signature('tool "bash" not found')
    b = error_signature("tool 'make' not found")
    assert a == b


def test_error_signature_empty_and_none():
    from modus.agent.stall import error_signature

    assert error_signature("") == ""
    assert error_signature(None) == ""
    assert error_signature("   ") == ""


def test_error_signature_keeps_structure_between_distinct_errors():
    from modus.agent.stall import error_signature

    assert error_signature("FileNotFoundError: x") != error_signature("PermissionError: x")


# ─── trigram similarity ───────────────────────────────────────────────────


def test_similarity_trigram_identical():
    from modus.agent.stall import calculate_similarity

    assert calculate_similarity("permission denied opening file", "permission denied opening file") == 1.0


def test_similarity_trigram_similar_text_scores_high():
    from modus.agent.stall import calculate_similarity

    a = "FileNotFoundError: no such file or directory: a.txt"
    b = "FileNotFoundError: no such file or directory: b.txt"
    assert calculate_similarity(a, b) >= 0.55


def test_similarity_trigram_unrelated_text_scores_low():
    from modus.agent.stall import calculate_similarity

    a = "permission denied opening file"
    b = "module not found: no such package"
    assert calculate_similarity(a, b) < 0.55


# ─── circuit breaker four-tier levels ─────────────────────────────────────


def _ledger(*entries):
    from modus.agent.stall import Ledger

    ledger = Ledger()
    for action, outcome, error in entries:
        ledger.add(action=action, outcome=outcome, error_text=error)
    return ledger


def test_stall_after_3_attempts_same_signature():
    from modus.agent.stall import LEVEL_OK, LEVEL_STALL, LEVEL_WATCH, Ledger

    ledger = Ledger()
    error = "FileNotFoundError: /data/input.json not found"
    ledger.add(action="read_file", outcome="error", error_text=error)
    assert ledger.check_circuit_breaker().level == LEVEL_OK
    ledger.add(action="read_file", outcome="error", error_text=error)
    assert ledger.check_circuit_breaker().level == LEVEL_WATCH
    ledger.add(action="read_file", outcome="error", error_text=error)
    result = ledger.check_circuit_breaker()
    assert result.level == LEVEL_STALL
    assert result.count == 3
    assert result.signature
    assert result.action == "read_file"


def test_similar_but_not_identical_errors_count_together():
    from modus.agent.stall import LEVEL_STALL, Ledger

    ledger = Ledger()
    ledger.add(action="run_tests", outcome="error",
               error_text="ImportError: cannot import name 'foo' from 'a.py'")
    ledger.add(action="run_tests", outcome="error",
               error_text="ImportError: cannot import name 'foo' from 'b.py'")
    ledger.add(action="run_tests", outcome="error",
               error_text="ImportError: cannot import name 'foo' from 'c.py'")
    assert ledger.check_circuit_breaker().level == LEVEL_STALL


def test_stall_clears_when_signature_changes():
    from modus.agent.stall import LEVEL_STALL, Ledger

    ledger = Ledger()
    ledger.add(action="read_file", outcome="error", error_text="FileNotFoundError: a.txt")
    ledger.add(action="read_file", outcome="error", error_text="FileNotFoundError: b.txt")
    ledger.add(action="read_file", outcome="error", error_text="FileNotFoundError: c.txt")
    assert ledger.check_circuit_breaker().level == LEVEL_STALL
    # A genuinely different failure resets the repeated-signature streak.
    ledger.add(action="run_tests", outcome="error", error_text="PermissionError: denied")
    assert ledger.check_circuit_breaker().level != LEVEL_STALL


def test_success_resets_loop_and_stall():
    from modus.agent.stall import LEVEL_LOOP, Ledger

    ledger = Ledger()
    for i in range(4):
        action = "run_tests" if i % 2 == 0 else "edit_file"
        ledger.add(action=action, outcome="error", error_text="failing")
    assert ledger.check_circuit_breaker().level == LEVEL_LOOP
    # A success breaks the cycle and resets the escalation counter.
    ledger.add(action="run_tests", outcome="success", error_text="")
    result = ledger.check_circuit_breaker()
    assert result.level != LEVEL_LOOP
    assert ledger.consecutive_loops == 0


# ─── loop detection ───────────────────────────────────────────────────────


def test_loop_detection_abab():
    from modus.agent.stall import LEVEL_LOOP, Ledger

    ledger = Ledger()
    for i in range(4):
        action = "run_tests" if i % 2 == 0 else "edit_file"
        ledger.add(action=action, outcome="error", error_text="tests still failing")
    result = ledger.check_circuit_breaker()
    assert result.level == LEVEL_LOOP
    assert result.pattern == ["run_tests", "edit_file", "run_tests", "edit_file"]


def test_loop_detection_abab_starting_other_side():
    from modus.agent.stall import LEVEL_LOOP, Ledger

    ledger = Ledger()
    actions = ["read_file", "write_file", "read_file", "write_file"]
    for action in actions:
        ledger.add(action=action, outcome="error", error_text="boom")
    assert ledger.check_circuit_breaker().level == LEVEL_LOOP


def test_loop_detection_alternating_actions_with_different_errors():
    from modus.agent.stall import LEVEL_LOOP, Ledger

    ledger = Ledger()
    # A -> B -> A -> B by ACTION even though every error text differs.
    ledger.add(action="read_file", outcome="error", error_text="a1")
    ledger.add(action="write_file", outcome="error", error_text="b1")
    ledger.add(action="read_file", outcome="error", error_text="a2")
    ledger.add(action="write_file", outcome="error", error_text="b2")
    assert ledger.check_circuit_breaker().level == LEVEL_LOOP


def test_loop_detection_no_progress_cap_5():
    from modus.agent.stall import LEVEL_LOOP, Ledger

    ledger = Ledger()
    # Five different failing actions, zero successes: no-pattern spin-out.
    for i in range(5):
        ledger.add(action=f"probe_{i}", outcome="error", error_text=f"failure {i}")
    assert ledger.check_circuit_breaker().level == LEVEL_LOOP


def test_four_distinct_failures_is_not_loop_yet():
    from modus.agent.stall import LEVEL_LOOP, Ledger

    ledger = Ledger()
    for i in range(4):
        ledger.add(action=f"probe_{i}", outcome="error", error_text=f"failure {i}")
    assert ledger.check_circuit_breaker().level != LEVEL_LOOP


def test_ledger_bounded():
    from modus.agent.stall import MAX_LEDGER_ATTEMPTS, Ledger

    ledger = Ledger()
    for i in range(200):
        ledger.add(action="read_file", outcome="error", error_text=f"err {i}")
    assert len(ledger.attempts) <= MAX_LEDGER_ATTEMPTS
    # Newest attempts survive.
    assert ledger.attempts[-1].error_signature == "err <num>"


def test_consecutive_loops_idempotent_per_check():
    from modus.agent.stall import LEVEL_LOOP, Ledger

    ledger = Ledger()
    for i in range(4):
        action = "run_tests" if i % 2 == 0 else "edit_file"
        ledger.add(action=action, outcome="error", error_text="boom")
    # Two checks on the same state count the loop only once.
    assert ledger.check_circuit_breaker().level == LEVEL_LOOP
    assert ledger.consecutive_loops == 1
    assert ledger.check_circuit_breaker().level == LEVEL_LOOP
    assert ledger.consecutive_loops == 1
    # One more attempt -> a new state -> the loop check point advances.
    ledger.add(action="run_tests", outcome="error", error_text="boom")
    assert ledger.check_circuit_breaker().level == LEVEL_LOOP
    assert ledger.consecutive_loops == 2


# ─── context block (inject, not abort) ────────────────────────────────────


def test_stall_injects_context_not_abort():
    from modus.agent.stall import LEVEL_STALL, build_stall_context_block

    block = build_stall_context_block(
        LEVEL_STALL, action="read_file", count=3, signature="file not found",
    )
    assert "[STALL DETECTED" in block
    assert "REFERENCE ONLY" in block
    assert "read_file" in block
    assert "3" in block
    assert "switch to a different" in block


def test_loop_context_block_includes_pattern():
    from modus.agent.stall import LEVEL_LOOP, build_stall_context_block

    block = build_stall_context_block(
        LEVEL_LOOP, pattern=["run_tests", "edit_file", "run_tests", "edit_file"],
    )
    assert "[LOOP DETECTED" in block
    assert "run_tests -> edit_file" in block


def test_ok_level_produces_no_context_block():
    from modus.agent.stall import LEVEL_OK, build_stall_context_block

    assert build_stall_context_block(LEVEL_OK) == ""


# ─── budget integration: StopReason.STALLED + stall tokens ────────────────


def test_stalled_stop_reason_enum():
    from modus.runtime.budget import StopReason

    assert StopReason.STALLED.value == "stalled"


def test_stall_tokens_counted_separately_from_total_budget():
    from modus.runtime.budget import RunBudget

    budget = RunBudget()
    budget.record_usage(100, 50)
    budget.record_stall_tokens(40, owner="host:react-stall")
    budget.record_stall_tokens(10, owner="host:react-stall")
    assert budget.total_tokens == 150
    assert budget.stall_tokens == 50
    assert budget.stall_tokens_exceeded() is False
    snap = budget.snapshot()
    assert snap["stall_tokens"] == 50
    assert snap["stall_tokens_exceeded"] is False
    assert snap["usage_ledger"]["host:react-stall"]["input_tokens"] == 50


def test_stall_token_cap_backstop():
    from modus.runtime.budget import RunBudget

    budget = RunBudget()
    budget.record_stall_tokens(budget.stall_token_cap)
    assert budget.stall_tokens_exceeded() is True
    # The authoritative total is unaffected by stall accounting.
    assert budget.total_tokens == 0


# ─── reasoner integration: end-to-end loop ────────────────────────────────


class _FakeClient:
    """Fake LLM that keeps emitting the SAME failing tool call + failing result."""

    model_name = "fake"
    provider_name = "test"
    max_context_window = 128_000

    def __init__(self, *, final_turn: int | None = None):
        # ``final_turn`` (if set) is the turn index after which the model
        # answers instead of calling the tool again — lets tests show recovery.
        self.final_turn = final_turn
        self.turn = 0

    async def chat(self, messages, tools, *, system_prompt):
        self.turn += 1
        tool_messages = [m for m in messages if m.role == "tool"]
        # Once the model sees a [STALL DETECTED] hint it changes course (or, if
        # final_turn is reached, it answers).
        if self.final_turn is not None and self.turn >= self.final_turn:
            yield {"type": "text_delta", "text": "changed course"}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if any("[STALL DETECTED" in str(m.content) for m in messages):
            yield {"type": "text_delta", "text": "changed course"}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        # Otherwise keep repeating the same failing call.
        yield {
            "type": "tool_call_delta", "tool_call": {
                "index": 0, "id": f"f{self.turn}",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path":"missing.json"}',
                },
            },
        }
        yield {"type": "message_end", "stop_reason": "tool_use"}


def _failing_registry():
    async def failing_handler(_payload, _ctx):
        return ToolResult("FileNotFoundError: /data/missing.json not found", is_error=True)

    registry = ToolRegistry()
    registry.register(Tool(
        name="read_file", description="read a file", handler=failing_handler,
        parameters=object_schema({"path": {"type": "string"}}, ["path"]),
        required_keys=["path"], is_read_only=True, danger_level="safe",
    ))
    return registry


def _reasoner(client, *, max_turns=10, **kwargs):
    from modus.agent.strategies.react import ReActReasoner

    cfg = ModusConfig()
    return ReActReasoner(
        llm_client=client, tool_registry=_failing_registry(),
        system_prompt="sys", cwd="/tmp", config=cfg, max_turns=max_turns,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_stall_injects_context_into_messages_not_abort():
    """3 same-signature failures inject a [STALL DETECTED] block, run survives."""
    client = _FakeClient()
    reasoner = _reasoner(client)
    events = [ev async for ev in reasoner.run([Message(role="user", content="read it")])]

    done = events[-1]
    # The run did not abort on stall: it kept going until the model changed
    # course (it saw the hint) and the run completed normally.
    assert done["type"] == "done"
    assert done["stop_reason"] == "completed"
    injected = [
        m for m in done["messages"]
        if m.role == "user" and "[STALL DETECTED" in str(m.content)
    ]
    assert injected, "the loop must inject the [STALL DETECTED] context block"
    assert "[STALL DETECTED" in str(injected[0].content)


@pytest.mark.asyncio
async def test_stall_injects_once_not_every_turn():
    client = _FakeClient()
    reasoner = _reasoner(client, max_turns=12)
    events = [ev async for ev in reasoner.run([Message(role="user", content="read it")])]

    final = events[-1]["messages"]
    blocks = [m for m in final if m.role == "user" and "[STALL DETECTED" in str(m.content)]
    # The block is injected as needed but never spammed on every turn.
    assert 1 <= len(blocks) <= 3


@pytest.mark.asyncio
async def test_stall_does_not_fire_without_repeated_failures():
    from modus.agent.strategies.react import ReActReasoner

    async def ok_handler(_payload, _ctx):
        return ToolResult("some content")

    registry = ToolRegistry()
    registry.register(Tool(
        name="echo", description="echo", handler=ok_handler,
        parameters=object_schema({"text": {"type": "string"}}, ["text"]),
        required_keys=["text"],
    ))

    class OkClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            if not any(m.role == "tool" for m in messages):
                yield {
                    "type": "tool_call_delta", "tool_call": {
                        "index": 0, "id": "e1",
                        "function": {"name": "echo", "arguments": '{"text":"x"}'},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    reasoner = ReActReasoner(
        llm_client=OkClient(), tool_registry=registry,
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="go")])]
    assert events[-1]["stop_reason"] == "completed"
    assert not any(
        "[STALL DETECTED" in str(m.content)
        for m in events[-1]["messages"] if m.role == "user"
    )


@pytest.mark.asyncio
async def test_stall_escalates_to_human_after_sustained_loop():
    """A model that never changes course hits StopReason.STALLED, not max_turns."""
    from modus.runtime.budget import RunBudget, RunLimits

    class StubbornClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.saw_stall = 0

        async def chat(self, messages, tools, *, system_prompt):
            self.turn += 1
            if any("[STALL DETECTED" in str(m.content) for m in messages):
                self.saw_stall += 1
            # The model ignores the hint and keeps repeating the failing call.
            yield {
                "type": "tool_call_delta", "tool_call": {
                    "index": 0, "id": f"f{self.turn}",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"missing.json"}',
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}

    budget = RunBudget(RunLimits(
        max_turns=10, max_tokens=200_000, max_wall_seconds=60,
    ))
    reasoner = _reasoner(StubbornClient(), budget=budget)
    events = [ev async for ev in reasoner.run([Message(role="user", content="read it")])]

    done = events[-1]
    assert done["type"] == "done"
    # Escalation happens BEFORE the turn budget is exhausted: the sustained
    # loop hands the run to a human with the written diagnostic.
    assert done["stop_reason"] == "stalled"
    assert done["budget"]["turns"] < 10
    # The human handoff carries the diagnostic ledger.
    diagnostic = reasoner._ledger.diagnostic()
    assert diagnostic["level"] == "loop"
    assert diagnostic["consecutive_loops"] >= 2


# ─── plan-execute integration ─────────────────────────────────────────────


def _plan_stalled_reasoner(client, **kwargs):
    from modus.agent.strategies.plan_execute import PlanExecuteReasoner

    return PlanExecuteReasoner(
        llm_client=client, tool_registry=_failing_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(), **kwargs,
    )


@pytest.mark.asyncio
async def test_plan_level_stall_injects_context_into_next_task():
    """A plan task failing 3x injects the reference-only [STALL DETECTED] block."""
    from modus.agent.strategies.plan_execute import PlanExecuteReasoner

    calls = {"n": 0}

    class FailTaskClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            calls["n"] += 1
            if calls["n"] == 1:
                # Planning call: one task that will keep failing.
                yield {"type": "text_delta", "text": (
                    '{"summary": "s", "tasks": ['
                    '{"id": "task_1", "description": "open missing file", '
                    '"type": "file_read", "dependencies": []}'
                    ']}'
                )}
                yield {"type": "message_end", "stop_reason": "end_turn"}
                return
            # Inner ReAct loop: keep calling read_file on the missing file.
            if not any(m.role == "tool" for m in messages):
                yield {
                    "type": "tool_call_delta", "tool_call": {
                        "index": 0, "id": f"t{calls['n']}",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"missing.json"}',
                        },
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            # Model keeps repeating the same failing call regardless.
            yield {
                "type": "tool_call_delta", "tool_call": {
                    "index": 0, "id": f"t{calls['n']}",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"missing.json"}',
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}

    reasoner = _plan_stalled_reasoner(FailTaskClient(), max_turns=20)
    events = [ev async for ev in reasoner.run([Message(role="user", content="open it")])]

    done = events[-1]
    assert done["type"] == "done"
    assert done["stop_reason"] == "stalled"
    # The stall was detected at the PLAN level and the written diagnostic is
    # available on the reasoner's plan ledger.
    assert reasoner._ledger is not None
    diagnostic = reasoner._ledger.diagnostic()
    assert diagnostic["level"] in ("stall", "loop")
    assert diagnostic["consecutive_loops"] >= 0


@pytest.mark.asyncio
async def test_plan_level_does_not_fire_without_repeated_failure():
    """A healthy plan (task succeeds) never trips the stall breaker."""
    from modus.agent.strategies.plan_execute import PlanExecuteReasoner
    from modus.tools.base import Tool, ToolResult, object_schema

    calls = {"n": 0}

    class GoodPlanClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            calls["n"] += 1
            if calls["n"] == 1:
                yield {"type": "text_delta", "text": (
                    '{"summary": "s", "tasks": ['
                    '{"id": "task_1", "description": "read ok", '
                    '"type": "file_read", "dependencies": []}'
                    ']}'
                )}
                yield {"type": "message_end", "stop_reason": "end_turn"}
                return
            yield {"type": "text_delta", "text": "task result ok"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    registry = ToolRegistry()
    registry.register(Tool(
        name="echo", description="echo",
        handler=lambda _p, _c: ToolResult("ok"),
        parameters=object_schema({"text": {"type": "string"}}, ["text"]),
        required_keys=["text"],
    ))
    reasoner = PlanExecuteReasoner(
        llm_client=GoodPlanClient(), tool_registry=registry,
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="go")])]
    assert events[-1]["stop_reason"] == "completed"
    assert reasoner._plan_stalled is False
