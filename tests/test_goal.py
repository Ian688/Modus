"""Wave4 G1: goal cross-turn state machine, idle continuation, 3-strike blocked,
persistence, and the model-side goal tool.

Covers the tests listed in docs/dev-wave4-autonomy.md:
- state machine transitions (active -> budget_limited -> max_turns -> complete)
- 3-strike blocked (same reason x3; different reason resets)
- idle steering injection when a goal is active
- user input preemption (cancel / new message beats auto-continuation)
- JSONL persistence + goal-cleared tombstone
- GoalTool.complete
"""

from __future__ import annotations

import asyncio

import pytest

from modus.config import ModusConfig
from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
from modus.tools.executor import ToolExecutor
from modus.tools.registry import ToolRegistry
from modus.types import Message


def _store(tmp_path):
    from modus.agent.goal import GoalStore

    return GoalStore(root=tmp_path / "goals")


def _registry() -> ToolRegistry:
    async def echo(payload, _ctx):
        return ToolResult(str(payload.get("text") or ""))

    reg = ToolRegistry()
    reg.register(Tool(
        name="echo", description="echo",
        parameters=object_schema({"text": {"type": "string"}}, ["text"]),
        handler=echo,
    ))
    return reg


def _reasoner(*, store, session_id="sess", **kwargs):
    from modus.agent.strategies.react import ReActReasoner

    cfg = ModusConfig()
    return ReActReasoner(
        llm_client=kwargs.pop("llm_client", None),
        tool_registry=_registry(),
        system_prompt="sys",
        cwd="/tmp",
        config=cfg,
        goal_store=store,
        session_id=session_id,
        **kwargs,
    )


# ─── state machine ────────────────────────────────────────────────────────


def test_goal_state_machine_transitions(tmp_path):
    store = _store(tmp_path)
    state = store.set("s1", "把测试跑绿")
    assert state.status == "active"

    # active -> budget_limited (soft token limit)
    store.update_tokens("s1", {"input_tokens": 90, "output_tokens": 10}, total=100)
    assert store.get("s1").status == "budget_limited"
    assert store.get("s1").tokens_used == 100

    # resume keeps accounting but is soft-resumable
    store.resume("s1")
    assert store.get("s1").status == "active"
    assert store.get("s1").tokens_used == 100

    # active -> max_turns (per-goal turn budget)
    store.record_turn("s1", count=5, budget_total=5)
    assert store.get("s1").status == "max_turns"
    assert store.get("s1").turns_executed == 5

    # /goal continue resets the turn counter, keeps objective + tokens
    store.resume("s1")
    assert store.get("s1").status == "active"
    assert store.get("s1").turns_executed == 0
    assert store.get("s1").tokens_used == 100

    # active -> complete (GoalTool.complete)
    store.complete("s1")
    assert store.get("s1").status == "complete"


def test_goal_pause_resume(tmp_path):
    store = _store(tmp_path)
    store.set("s1", "跑绿")
    store.pause("s1")
    assert store.get("s1").status == "paused"
    # A paused goal is not active for the idle hook.
    assert store.active("s1") is None
    store.resume("s1")
    assert store.get("s1").status == "active"
    assert store.active("s1") is not None


def test_goal_terminal_states_are_not_runnable(tmp_path):
    store = _store(tmp_path)
    store.set("s1", "跑绿")
    store.complete("s1")
    assert store.active("s1") is None
    assert store.get("s1").is_terminal() is True

    store.set("s1", "跑绿")
    store.record_blocked_attempt("s1", "x")
    store.record_blocked_attempt("s1", "x")
    store.record_blocked_attempt("s1", "x")
    assert store.get("s1").is_terminal() is True
    assert store.active("s1") is None


def test_goal_session_isolation(tmp_path):
    store = _store(tmp_path)
    store.set("a", "goal A")
    store.set("b", "goal B")
    assert store.get("a").objective == "goal A"
    assert store.get("b").objective == "goal B"
    assert store.get("unseen") is None


# ─── 3-strike blocked ─────────────────────────────────────────────────────


def test_blocked_3_strike_same_reason(tmp_path):
    store = _store(tmp_path)
    store.set("s2", "fix tests")
    for _ in range(2):
        store.record_blocked_attempt("s2", "file locked")
        assert store.get("s2").status == "active"
    store.record_blocked_attempt("s2", "file locked")
    assert store.get("s2").status == "blocked"
    assert store.get("s2").blocked_count == 3
    # Terminal: further attempts are no-ops.
    store.record_blocked_attempt("s2", "file locked")
    assert store.get("s2").blocked_count == 3
    assert store.get("s2").status == "blocked"


def test_blocked_3_strike_different_reason_resets(tmp_path):
    store = _store(tmp_path)
    store.set("s3", "fix tests")
    store.record_blocked_attempt("s3", "network down")
    store.record_blocked_attempt("s3", "network down")
    assert store.get("s3").blocked_count == 2
    # A different reason resets the streak (never misreads hard/slow as blocked).
    store.record_blocked_attempt("s3", "missing dependency")
    assert store.get("s3").blocked_count == 1
    assert store.get("s3").blocked_reason == "missing dependency"
    assert store.get("s3").status == "active"


def test_blocked_count_threshold_constant():
    from modus.agent.goal import BLOCKED_CONSECUTIVE_THRESHOLD

    assert BLOCKED_CONSECUTIVE_THRESHOLD == 3


# ─── idle steering injection ──────────────────────────────────────────────


class RecordingClient:
    """Fake LLM that records the user messages it saw each call."""

    model_name = "fake"
    provider_name = "test"
    max_context_window = 128_000

    def __init__(self, *, tool_turns: int = 0, final_text: str = "done"):
        self.tool_turns = tool_turns
        self.final_text = final_text
        self.seen: list[list[str]] = []

    async def chat(self, messages, tools, *, system_prompt):
        self.seen.append([str(m.content) for m in messages if m.role == "user"])
        tool_roles = sum(1 for m in messages if m.role == "tool")
        if tool_roles < self.tool_turns:
            yield {
                "type": "tool_call_delta", "tool_call": {
                    "index": 0, "id": f"t{tool_roles}",
                    "function": {"name": "echo", "arguments": '{"text":"x"}'},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
        else:
            yield {"type": "text_delta", "text": self.final_text}
            yield {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 1}}
            yield {"type": "message_end", "stop_reason": "end_turn"}


@pytest.mark.asyncio
async def test_goal_steering_injected_on_idle(tmp_path):
    from modus.agent.strategies.react import ReActReasoner

    store = _store(tmp_path)
    store.set("s4", "把测试跑绿")
    client = RecordingClient(tool_turns=1)
    reasoner = ReActReasoner(
        llm_client=client, tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
        goal_store=store, session_id="s4",
    )
    events = [ev async for ev in reasoner.run(
        [Message(role="user", content="把测试跑绿")],
    )]
    assert events[-1]["type"] == "done"
    # The first model turn saw the user request AND the injected steering.
    assert any("<goal-steering>" in m for m in client.seen[0])
    # Steering is injected at most once per run: exactly one steering message
    # exists in the final history, however many turns the loop ran.
    final = events[-1]["messages"]
    steering_msgs = [
        m for m in final if "<goal-steering>" in str(m.content)
    ]
    assert len(steering_msgs) == 1
    # The steering carries the objective + completion audit.
    assert "把测试跑绿" in str(steering_msgs[0].content)
    assert "completion_audit" in str(steering_msgs[0].content)


@pytest.mark.asyncio
async def test_goal_turn_and_token_accounting(tmp_path):
    from modus.agent.strategies.react import ReActReasoner

    store = _store(tmp_path)
    store.set("s4", "把测试跑绿")
    client = RecordingClient(tool_turns=1)
    reasoner = ReActReasoner(
        llm_client=client, tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
        goal_store=store, session_id="s4",
    )
    [ev async for ev in reasoner.run([Message(role="user", content="把测试跑绿")])]
    state = store.get("s4")
    assert state.turns_executed == 2  # tool round-trip + final answer
    assert state.tokens_used >= 3  # usage recorded
    assert state.status == "active"


# ─── user input preemption ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_message_preempts_goal_cancel(tmp_path):
    from modus.agent.strategies.react import ReActReasoner

    store = _store(tmp_path)
    store.set("s5", "把测试跑绿")
    client = RecordingClient(tool_turns=2)
    reasoner = ReActReasoner(
        llm_client=client, tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
        goal_store=store, session_id="s5",
    )
    cancel_event = asyncio.Event()
    cancel_event.set()
    # Direct hook: a set cancel suppresses steering entirely.
    pending: list[Message] = [Message(role="user", content="我改主意了")]
    reasoner._maybe_inject_goal_steering(pending, cancel_event)
    assert not any("<goal-steering>" in str(m.content) for m in pending)
    # And the run itself stops immediately without a model call.
    events = [ev async for ev in reasoner.run(
        [Message(role="user", content="我改主意了")], cancel_event=cancel_event,
    )]
    # The loop emits an error event (pre-existing cancel contract), then the
    # terminal ``done`` carries stop_reason=cancelled.  No steering ever ran.
    assert any(ev["type"] == "error" for ev in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["stop_reason"] == "cancelled"


def test_paused_goal_does_not_steer(tmp_path):
    from modus.agent.strategies.react import ReActReasoner

    store = _store(tmp_path)
    store.set("s5", "把测试跑绿")
    store.pause("s5")
    reasoner = _reasoner(store=store, session_id="s5")
    pending: list[Message] = [Message(role="user", content="hi")]
    reasoner._maybe_inject_goal_steering(pending, None)
    assert not any("<goal-steering>" in str(m.content) for m in pending)


def test_no_goal_does_not_steer(tmp_path):
    from modus.agent.strategies.react import ReActReasoner

    reasoner = _reasoner(store=_store(tmp_path), session_id="nosession")
    pending: list[Message] = [Message(role="user", content="hi")]
    reasoner._maybe_inject_goal_steering(pending, None)
    assert not any("<goal-steering>" in str(m.content) for m in pending)


# ─── soft budget-limited stop ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_budget_limited_soft_stop(tmp_path):
    from modus.agent.strategies.react import ReActReasoner

    store = _store(tmp_path)
    store.set("s6", "把测试跑绿")

    class BudgetClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            yield {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 1}}
            tool_roles = sum(1 for m in messages if m.role == "tool")
            if tool_roles < 4:
                yield {
                    "type": "tool_call_delta", "tool_call": {
                        "index": 0, "id": f"b{tool_roles}",
                        "function": {"name": "echo", "arguments": '{"text":"x"}'},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
            else:
                yield {"type": "text_delta", "text": "finished"}
                yield {"type": "message_end", "stop_reason": "end_turn"}

    reasoner = ReActReasoner(
        llm_client=BudgetClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
        goal_store=store, session_id="s6", goal_tokens_budget=6,
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="把测试跑绿")])]
    done = events[-1]
    # Soft stop: not a hard failure, a goal_budget_limited handoff.
    assert done["stop_reason"] == "goal_budget_limited"
    assert store.get("s6").status == "budget_limited"
    assert store.get("s6").tokens_used >= 6
    # The summarization prompt was injected so the run ends with a handoff.
    assert any(
        "<goal-summary-request>" in str(m.content)
        for m in done["messages"] if m.role == "user"
    )
    # Resume is soft: a fresh round may continue.
    store.resume("s6")
    assert store.get("s6").status == "active"


@pytest.mark.asyncio
async def test_goal_max_turns_soft_stop_and_continue(tmp_path):
    from modus.agent.strategies.react import ReActReasoner

    store = _store(tmp_path)
    store.set("s6", "把测试跑绿")

    class TurnClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            if any("<goal-summary-request>" in str(m.content) for m in messages):
                yield {"type": "text_delta", "text": "max_turns handoff"}
                yield {"type": "message_end", "stop_reason": "end_turn"}
                return
            yield {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 1}}
            tool_roles = sum(1 for m in messages if m.role == "tool")
            if tool_roles < 6:
                yield {
                    "type": "tool_call_delta", "tool_call": {
                        "index": 0, "id": f"m{tool_roles}",
                        "function": {"name": "echo", "arguments": '{"text":"x"}'},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
            else:
                yield {"type": "text_delta", "text": "finished"}
                yield {"type": "message_end", "stop_reason": "end_turn"}

    reasoner = ReActReasoner(
        llm_client=TurnClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
        goal_store=store, session_id="s6", goal_turns_budget=2,
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="把测试跑绿")])]
    done = events[-1]
    assert done["stop_reason"] == "goal_max_turns"
    assert store.get("s6").status == "max_turns"
    # /goal continue resets the counter so a fresh round keeps attacking.
    store.resume("s6")
    assert store.get("s6").status == "active"
    assert store.get("s6").turns_executed == 0


@pytest.mark.asyncio
async def test_goal_survives_run_budget_cap_and_steers_next_round(tmp_path):
    """A hard run-budget stop leaves the goal active; the next round re-steers."""
    from modus.agent.strategies.react import ReActReasoner
    from modus.runtime.budget import RunBudget, RunLimits

    store = _store(tmp_path)
    store.set("s6", "把测试跑绿")

    class RoundClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000
        calls = 0

        async def chat(self, messages, tools, *, system_prompt):
            RoundClient.calls += 1
            if RoundClient.calls == 1:
                yield {
                    "type": "tool_call_delta", "tool_call": {
                        "index": 0, "id": "r0",
                        "function": {"name": "echo", "arguments": '{"text":"x"}'},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
            else:
                yield {"type": "text_delta", "text": "round two"}
                yield {"type": "message_end", "stop_reason": "end_turn"}

    RoundClient.calls = 0
    b1 = RunBudget(RunLimits(max_turns=1, max_tokens=200_000, max_wall_seconds=60))
    r1 = ReActReasoner(
        llm_client=RoundClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
        goal_store=store, session_id="s6", budget=b1,
    )
    events1 = [ev async for ev in r1.run([Message(role="user", content="把测试跑绿")])]
    assert events1[-1]["stop_reason"] == "max_turns"
    # The goal stays active (neither blocked nor complete) across the stop.
    assert store.get("s6").status == "active"
    assert store.get("s6").turns_executed == 1

    # Round two, fresh budget: steering is re-injected because a new run starts.
    RoundClient.calls = 0
    client2 = RoundClient()
    b2 = RunBudget(RunLimits(max_turns=5, max_tokens=200_000, max_wall_seconds=60))
    r2 = ReActReasoner(
        llm_client=client2, tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
        goal_store=store, session_id="s6", budget=b2,
    )
    # Probe the steering hook directly for the fresh run.
    r2._goal_steered_this_run = False
    pending = [Message(role="user", content="继续")]
    r2._maybe_inject_goal_steering(pending, None)
    assert any("<goal-steering>" in str(m.content) for m in pending)


# ─── persistence ──────────────────────────────────────────────────────────


def test_goal_persist_resume(tmp_path):
    store = _store(tmp_path)
    store.set("s7", "把测试跑绿")
    store.update_tokens("s7", {"input_tokens": 40, "output_tokens": 10})
    store.record_turn("s7", count=3)

    # A brand-new store (fresh process) hydrates from the JSONL file.
    fresh = _store(tmp_path)
    loaded = fresh.load("s7")
    assert loaded is not None
    assert loaded.objective == "把测试跑绿"
    assert loaded.tokens_used == 50
    assert loaded.turns_executed == 3
    assert loaded.status == "active"


def test_goal_clear_tombstone_prevents_resurrection(tmp_path):
    store = _store(tmp_path)
    store.set("s7", "把测试跑绿")
    store.clear("s7")
    assert store.get("s7") is None

    # Even after replay (fresh store), the tombstone keeps the goal cleared.
    fresh = _store(tmp_path)
    assert fresh.load("s7") is None
    # But the file (audit trail) still exists.
    assert (tmp_path / "goals" / "s7.jsonl").exists()


def test_goal_persists_terminal_status(tmp_path):
    store = _store(tmp_path)
    store.set("s7", "把测试跑绿")
    store.complete("s7")
    fresh = _store(tmp_path)
    loaded = fresh.load("s7")
    assert loaded.status == "complete"


# ─── GoalTool ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_tool_complete(tmp_path):
    from modus.agent.goal import make_goal_tool

    store = _store(tmp_path)
    store.set("s8", "把测试跑绿")
    tool = make_goal_tool(store=store)
    result = await tool.execute(
        {"action": "complete", "usage": {"input_tokens": 5, "output_tokens": 3}},
        ToolContext(cwd=".", config=ModusConfig(), session_id="s8"),
    )
    assert not result.is_error
    assert "goal complete" in result.content
    assert "把测试跑绿" in result.content
    assert store.get("s8").status == "complete"


@pytest.mark.asyncio
async def test_goal_tool_get_and_blocked(tmp_path):
    from modus.agent.goal import make_goal_tool

    store = _store(tmp_path)
    store.set("s8", "把测试跑绿")
    tool = make_goal_tool(store=store)

    got = await tool.execute(
        {"action": "get"},
        ToolContext(cwd=".", config=ModusConfig(), session_id="s8"),
    )
    assert not got.is_error
    assert "把测试跑绿" in got.content

    blocked = await tool.execute(
        {"action": "blocked", "reason": "file locked"},
        ToolContext(cwd=".", config=ModusConfig(), session_id="s8"),
    )
    assert not blocked.is_error
    assert "blocked attempt 1/3" in blocked.content
    assert store.get("s8").blocked_count == 1


@pytest.mark.asyncio
async def test_goal_tool_3_strike_through_tool(tmp_path):
    from modus.agent.goal import make_goal_tool

    store = _store(tmp_path)
    store.set("s8", "把测试跑绿")
    tool = make_goal_tool(store=store)
    ctx = ToolContext(cwd=".", config=ModusConfig(), session_id="s8")
    for _ in range(2):
        await tool.execute({"action": "blocked", "reason": "file locked"}, ctx)
    result = await tool.execute({"action": "blocked", "reason": "file locked"}, ctx)
    assert "marked blocked" in result.content
    assert store.get("s8").status == "blocked"


@pytest.mark.asyncio
async def test_goal_tool_runs_through_executor_without_approval(tmp_path):
    from modus.agent.goal import make_goal_tool

    store = _store(tmp_path)
    store.set("s8", "把测试跑绿")
    registry = ToolRegistry()
    registry.register(make_goal_tool(store=store))
    executor = ToolExecutor(registry)
    ctx = ToolContext(
        cwd=".", config=ModusConfig(), session_id="s8",
        approval_callback=lambda _req: (_ for _ in ()).throw(AssertionError("must not ask")),
    )
    result = (await executor.execute_all(
        [{"id": "g1", "function": {"name": "goal", "arguments": '{"action":"get"}'}}],
        ctx,
    ))[0]
    assert not result.is_error
    assert "把测试跑绿" in result.content
    # The goal tool is declared memory-only; an explicit non-memory grant denies it.
    locked_ctx = ToolContext(
        cwd=".", config=ModusConfig(), session_id="s8",
        granted_capabilities=["filesystem"],
    )
    denied = (await executor.execute_all(
        [{"id": "g2", "function": {"name": "goal", "arguments": '{"action":"get"}'}}],
        locked_ctx,
    ))[0]
    assert denied.is_error


# ─── select_reasoner goal mode ────────────────────────────────────────────


def test_select_reasoner_agent_mode_goal():
    from modus.agent.goal import GoalReasoner
    from modus.agent.strategies.select import select_reasoner

    cfg = ModusConfig()
    cfg.prompt.agent_mode = "goal"
    assert select_reasoner("把测试跑绿", [], cfg) is GoalReasoner


@pytest.mark.asyncio
async def test_goal_mode_wraps_react_and_wires_goal_tool(tmp_path):
    from modus.agent.goal import GoalStore
    from modus.agent.strategies.react import ReActReasoner

    store = GoalStore(root=tmp_path / "goals")
    store.set("gm", "把测试跑绿")
    client = RecordingClient(tool_turns=0, final_text="ok")
    reasoner = ReActReasoner(
        llm_client=client, tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
        goal_store=store, session_id="gm",
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="把测试跑绿")])]
    assert events[-1]["stop_reason"] == "completed"
    assert "<goal-steering>" in client.seen[0][1]
