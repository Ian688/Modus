"""Reasoning strategies for the agent turn loop.

Strategies implement the ``Reasoner`` protocol (``modus.agent.reasoner``) and
are swappable through ``QueryEngine.ask(reasoner_factory=...)``.  The default
strategy is ReAct; ``config.prompt.agent_mode="plan"`` selects plan-then-execute.
"""
from modus.agent.strategies.plan_execute import PlanExecuteReasoner
from modus.agent.strategies.react import ReActReasoner

__all__ = ["ReActReasoner", "PlanExecuteReasoner"]
