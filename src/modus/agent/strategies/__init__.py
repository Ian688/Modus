"""Reasoning strategies for the agent turn loop.

Strategies implement the ``Reasoner`` protocol (``modus.agent.reasoner``) and
are swappable through ``QueryEngine.ask(reasoner_factory=...)``.  The default
strategy is ReAct.
"""
from modus.agent.strategies.react import ReActReasoner

__all__ = ["ReActReasoner"]
