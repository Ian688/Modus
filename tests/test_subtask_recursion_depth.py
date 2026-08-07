"""Recursion-depth guard for default-mode ``spawn_subtask``.

The child ``ReActReasoner`` re-reads ``features.convergence.max_recursion_depth``
from its own config to decide whether to expose ``spawn_subtask`` again.  The
parent config must be copied and its depth decremented before building the child
reasoner, otherwise ``max_depth`` is a no-op and recursion is bounded only by
the soft turn/token budget.
"""
from __future__ import annotations

from modus.agent.subtask import _decrement_recursion_depth
from modus.config import ModusConfig


def test_decrements_depth():
    cfg = ModusConfig()
    cfg.features.convergence.max_recursion_depth = 3
    child = _decrement_recursion_depth(cfg)
    assert child.features.convergence.max_recursion_depth == 2
    # The parent config is untouched.
    assert cfg.features.convergence.max_recursion_depth == 3


def test_zero_stays_zero():
    cfg = ModusConfig()
    cfg.features.convergence.max_recursion_depth = 0
    child = _decrement_recursion_depth(cfg)
    assert child.features.convergence.max_recursion_depth == 0


def test_floor_at_zero():
    cfg = ModusConfig()
    cfg.features.convergence.max_recursion_depth = 1
    child = _decrement_recursion_depth(cfg)
    assert child.features.convergence.max_recursion_depth == 0


def test_copy_is_deep_not_shared():
    cfg = ModusConfig()
    cfg.features.convergence.max_recursion_depth = 2
    child = _decrement_recursion_depth(cfg)
    child.features.convergence.max_recursion_depth = 5
    assert cfg.features.convergence.max_recursion_depth == 2
