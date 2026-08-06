"""Peri convergence loop: accept on convergence, fail without it.

These runner-level tests drive ``_run_peri_stream`` with a fake review that
either converges (score rising to acceptable, or semantically identical
output) or keeps rejecting, asserting the loop stops revising and completes
with ``stop_reason == completed`` on convergence, and fails otherwise.
"""

from __future__ import annotations

import pytest

from modus.config import ModusConfig


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


def _one_worker_models() -> dict:
    return {
        "peri_roles": {
            "host": {
                "id": "host", "model_id": "host", "name": "Host",
                "provider": "test", "model": "host", "api_key": "key",
            },
            "worker_1": {
                "id": "worker", "model_id": "worker", "name": "Worker",
                "provider": "test", "model": "worker", "api_key": "key",
            },
        },
    }


def _completed(websocket: FakeWebSocket) -> bool:
    return any(item.get("type") == "done" and item.get("stop_reason") == "completed"
               for item in websocket.sent)


def _failed(websocket: FakeWebSocket) -> bool:
    return any(item.get("type") == "done" and item.get("stop_reason") == "failed"
               for item in websocket.sent)


class _Engine:
    def __init__(self, config: ModusConfig | None = None) -> None:
        self.config = config or ModusConfig()


@pytest.mark.asyncio
async def test_peri_revision_rounds_until_scores_pass(monkeypatch):
    """Scores rising across rounds eventually pass the Host review."""
    from modus.desktop import server

    websocket = FakeWebSocket()
    monkeypatch.setattr(server, "_load_models_for_session", lambda _s, _m: _one_worker_models())

    async def decompose(*_a, **_k):
        return [{"name": "Inspect", "description": "inspect", "success_criteria": "facts"}]

    round_no = 0

    async def execute(*_a, **_k):
        nonlocal round_no
        round_no += 1
        return f"revised output round {round_no}"

    async def review(*_a, **_k):
        return (round_no >= 2), [], [7.0]

    async def merge(*_a, **_k):
        return "Host final consensus"

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", merge)

    await server._run_peri_stream(websocket, server.DaoSession(id="s", db_id="db"), "task")

    assert _completed(websocket)
    assert not _failed(websocket)


@pytest.mark.asyncio
async def test_peri_accepts_consensus_on_semantic_collapse(monkeypatch):
    """Identical output across rounds is expression-only; run completes."""
    from modus.desktop import server

    websocket = FakeWebSocket()
    monkeypatch.setattr(server, "_load_models_for_session", lambda _s, _m: _one_worker_models())

    async def decompose(*_a, **_k):
        return [{"name": "Inspect", "description": "inspect", "success_criteria": "facts"}]

    async def execute(*_a, **_k):
        return "the same conclusion every round"

    async def review(*_a, **_k):
        return False, ["please elaborate"], [8.0]

    async def merge(*_a, **_k):
        return "Host final consensus"

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", merge)

    await server._run_peri_stream(websocket, server.DaoSession(id="s", db_id="db"), "task")

    assert _completed(websocket)
    assert not _failed(websocket)


@pytest.mark.asyncio
async def test_peri_convergence_disabled_falls_back_to_single_revision(monkeypatch):
    """With convergence off, a second rejection fails the run (old contract)."""
    from modus.desktop import server

    config = ModusConfig()
    config.features.convergence.enabled = False
    session = server.DaoSession(
        id="s", db_id="db", engine=_Engine(config),
    )
    websocket = FakeWebSocket()
    monkeypatch.setattr(server, "_load_models_for_session", lambda _s, _m: _one_worker_models())

    async def decompose(*_a, **_k):
        return [{"name": "Inspect", "description": "inspect", "success_criteria": "facts"}]

    calls = 0

    async def execute(*_a, **_k):
        nonlocal calls
        return f"revised round {calls}"

    async def review(*_a, **_k):
        nonlocal calls
        calls += 1
        return False, ["add stronger evidence"], [3.0]

    async def merge(*_a, **_k):
        raise AssertionError("merge must not run when convergence is disabled and review rejects")

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", merge)

    await server._run_peri_stream(websocket, session, "task")

    assert _failed(websocket)
    assert not _completed(websocket)
