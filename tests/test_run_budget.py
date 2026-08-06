import asyncio

import pytest

from modus.agent.query import query
from modus.config import ModusConfig
from modus.runtime.budget import BudgetExceeded, RunBudget, RunLimits, StopReason
from modus.tools.registry import ToolRegistry


def test_run_budget_enforces_turn_and_token_limits_with_snapshot():
    budget = RunBudget(RunLimits(max_turns=1, max_tokens=5, max_wall_seconds=60))
    assert budget.begin_turn() == 1
    with pytest.raises(BudgetExceeded) as turns:
        budget.begin_turn()
    assert turns.value.reason is StopReason.MAX_TURNS

    tokens = RunBudget(RunLimits(max_turns=2, max_tokens=5, max_wall_seconds=60))
    tokens.record_usage(2, 3)
    with pytest.raises(BudgetExceeded) as exceeded:
        tokens.check_limits()
    assert exceeded.value.reason is StopReason.TOKEN_LIMIT
    assert tokens.snapshot()["total_tokens"] == 5


def test_run_budget_exposes_bounded_verification_attempt_limit():
    budget = RunBudget(RunLimits(max_verification_attempts=2))

    assert budget.limits.max_verification_attempts == 2
    assert budget.verification.max_attempts == 2
    assert budget.snapshot()["max_verification_attempts"] == 2


def test_run_budget_ledger_attributes_usage_by_owner_while_totals_stay_authoritative():
    budget = RunBudget()
    budget.record_usage(100, 50, owner="host")
    budget.record_usage(30, 20, owner="moa:reference_1")
    budget.record_usage(10, 5, owner="peri:worker_2")
    # Anonymous usage still lands in totals but no ledger key.
    budget.record_usage(1, 1)

    snap = budget.snapshot()
    assert snap["total_tokens"] == 100 + 50 + 30 + 20 + 10 + 5 + 1 + 1
    assert snap["input_tokens"] == 100 + 30 + 10 + 1
    assert snap["output_tokens"] == 50 + 20 + 5 + 1
    assert snap["usage_ledger"] == {
        "host": {"input_tokens": 100, "output_tokens": 50},
        "moa:reference_1": {"input_tokens": 30, "output_tokens": 20},
        "peri:worker_2": {"input_tokens": 10, "output_tokens": 5},
    }


def test_run_budget_ledger_is_sorted_and_negative_usage_is_clamped():
    budget = RunBudget()
    budget.record_usage(5, 3, owner="peri:worker_2")
    budget.record_usage(2, 1, owner="moa:aggregator")
    budget.record_usage(-10, -5, owner="host")

    ledger = budget.snapshot()["usage_ledger"]
    assert list(ledger.keys()) == ["host", "moa:aggregator", "peri:worker_2"]
    assert ledger["host"]["input_tokens"] == 0
    assert ledger["host"]["output_tokens"] == 0


@pytest.mark.asyncio
async def test_query_reports_max_turns_as_a_structured_stop_reason():
    class ToolLoopClient:
        async def chat(self, messages, tools, *, system_prompt):
            yield {
                "type": "tool_call_delta", "tool_call": {
                    "index": 0, "id": "missing", "function": {
                        "name": "missing", "arguments": "{}",
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}

    budget = RunBudget(RunLimits(max_turns=1, max_tokens=100, max_wall_seconds=60))
    events = [event async for event in query(
        llm_client=ToolLoopClient(), tool_registry=ToolRegistry(), system_prompt="system",
        user_message="loop", history=None, cwd=".", config=ModusConfig(), budget=budget,
    )]

    assert events[-1]["type"] == "done"
    assert events[-1]["stop_reason"] == "max_turns"
    assert events[-1]["budget"]["turns"] == 1


@pytest.mark.asyncio
async def test_provider_error_is_not_reported_as_done():
    class ErrorClient:
        async def chat(self, messages, tools, *, system_prompt):
            yield {"type": "error", "error": "provider unavailable"}

    events = [event async for event in query(
        llm_client=ErrorClient(), tool_registry=ToolRegistry(), system_prompt="system",
        user_message="run", history=None, cwd=".", config=ModusConfig(),
    )]

    assert [event["type"] for event in events] == ["error"]
    assert events[0]["stop_reason"] == "engine_error"
