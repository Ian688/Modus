"""Deterministic reasoner selection for the agent loop.

Chooses ReAct vs PlanExecute (vs GoalReasoner under ``agent_mode="goal"``)
from the request shape and config, so the default base adapts without a router
model.  ``select_reasoner`` is a pure function: given the user request,
conversation history, and config, it returns which reasoner class to use.
Explicit ``reasoner_factory`` and ``agent_mode`` always win; this heuristic only
fills the gap when neither is set.

Heuristic (deliberately conservative):
- A request with multi-step cues and file-mutation intent -> PlanExecute.
- A conversational / single-step request -> ReAct.
- ``agent_mode="plan"`` -> PlanExecute.
- ``agent_mode="goal"`` -> GoalReasoner (a thin goal-aware wrapper around ReAct
  that keeps the cross-turn goal state in the loop).
- anything else -> ReAct.
"""

from __future__ import annotations

from typing import Any

_MULTI_STEP_CUES = ("然后", "并且", "同时", "先", "再", "最后", "接着", "以及", "先做")
_FILE_CUES = ("写", "改", "创建", "重构", "实现", "修复", "文件", "测试", "新增")


def _looks_multi_step(request: str) -> bool:
    return any(cue in request for cue in _MULTI_STEP_CUES)


def _looks_file_intent(request: str) -> bool:
    return any(cue in request for cue in _FILE_CUES)


def select_reasoner(
    request: str,
    history: list[Any] | None = None,
    config: Any = None,
    *,
    explicit_factory: Any = None,
) -> Any:
    """Return the reasoner class for this request, or None to let callers decide.

    ``explicit_factory`` (a reasoner_factory) wins outright.  Otherwise the
    heuristic applies; ``config.prompt.agent_mode`` remains the fallback when
    the request looks conversational.
    """
    if explicit_factory is not None:
        return explicit_factory

    from modus.agent.goal import GoalReasoner
    from modus.agent.strategies import PlanExecuteReasoner, ReActReasoner

    request_text = str(request or "")
    # A conversational / simple request stays on ReAct even under agent_mode
    # unless the config explicitly pins plan.
    mode = str(getattr(getattr(config, "prompt", None), "agent_mode", "react"))
    if mode == "plan":
        return PlanExecuteReasoner
    if mode == "goal":
        # Goal-driven mode: wrap ReAct so the same safety boundaries hold while
        # the cross-turn goal state is kept in the loop.
        return GoalReasoner
    if _looks_multi_step(request_text) and _looks_file_intent(request_text):
        return PlanExecuteReasoner
    return ReActReasoner
