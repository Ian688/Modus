"""Deterministic scorers for offline trajectory evaluation (Wave5 E1).

Scorers are pure functions ``fn(scenario, trajectory, **opts) -> dict`` that
produce a score dict with at least ``pass`` / ``partial`` / ``reason``.  Per
the blueprint's principle 4 (deterministic pure functions as the scoring
boundary), none of these scorers invoke an LLM; the optional ``llm_judge`` is
deliberately *not* provided here and lives behind the self-review guard in the
evaluator so a boundary never depends on a model judging its own output.
"""
