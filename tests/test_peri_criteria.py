"""Structured success_criteria verification for the Peri review loop.

Covers the checklist normalization, the Host per-item boolean verdict, and the
runner-level behavior: all criteria met -> run completes; criteria unmet but
the Host keeps rejecting -> revision loop still bounded and fails at the cap.
"""

from __future__ import annotations

import pytest

from modus.desktop.peri import normalize_criteria, verify_subtask_criteria


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


class _Engine:
    def __init__(self, config) -> None:
        self.config = config


# ── normalize_criteria ──


def test_normalize_checklist_criteria():
    task = {"success_criteria": {"checklist": [
        {"item": "列出三条证据", "check": "每条注明来源"},
        {"item": "无虚构路径", "check": "路径来自工具结果"},
    ]}}
    items = normalize_criteria(task)
    assert [entry["item"] for entry in items] == ["列出三条证据", "无虚构路径"]
    assert items[0]["check"] == "每条注明来源"


def test_normalize_legacy_free_text_criteria():
    task = {"success_criteria": "Complete and correct output"}
    items = normalize_criteria(task)
    assert len(items) == 1
    assert items[0]["item"] == "Complete and correct output"


def test_normalize_missing_criteria_falls_back():
    items = normalize_criteria({"name": "x"})
    assert len(items) == 1
    assert items[0]["item"]


# ── verify_subtask_criteria ──


@pytest.mark.asyncio
async def test_verify_subtask_criteria_parses_boolean_verdicts(monkeypatch):
    from modus.desktop import peri

    class FakeClient:
        async def chat(self, _messages, _tools, *, system_prompt):
            assert "Checklist" in _messages[0].content
            yield {"type": "text_delta", "text": (
                '{"criteria_verdicts": ['
                '{"index": 0, "item": "a", "satisfied": true, "reason": "present"},'
                '{"index": 1, "item": "b", "satisfied": false, "reason": "missing"}'
                "]}"
            )}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    monkeypatch.setattr(peri, "create_llm_client", lambda _cfg: FakeClient())
    monkeypatch.setattr(peri, "load_config", lambda: None)

    task = {"success_criteria": {"checklist": [
        {"item": "a", "check": ""},
        {"item": "b", "check": ""},
    ]}}
    result = await verify_subtask_criteria(task, "worker output", "test", "host", api_key="key")
    assert result["verified"] == 1
    assert result["total"] == 2
    assert result["verdicts"][0]["satisfied"] is True
    assert result["verdicts"][1]["satisfied"] is False


@pytest.mark.asyncio
async def test_verify_subtask_criteria_invalid_json_raises(monkeypatch):
    from modus.desktop import peri

    class BadClient:
        async def chat(self, _messages, _tools, *, system_prompt):
            yield {"type": "text_delta", "text": "not json"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    monkeypatch.setattr(peri, "create_llm_client", lambda _cfg: BadClient())
    monkeypatch.setattr(peri, "load_config", lambda: None)

    task = {"success_criteria": {"checklist": [{"item": "a"}]}}
    with pytest.raises(peri.PeriModelError, match="invalid JSON"):
        await verify_subtask_criteria(task, "out", "test", "host", api_key="key")


# ── runner level ──


@pytest.mark.asyncio
async def test_peri_completes_when_all_criteria_met_even_if_host_rejects(monkeypatch):
    """Criteria are authoritative: all met -> run completes despite a rejecting Host."""
    from modus.desktop import server

    websocket = FakeWebSocket()
    monkeypatch.setattr(server, "_load_models_for_session", lambda _s, _m: _one_worker_models())

    async def decompose(*_a, **_k):
        return [{"name": "Inspect", "description": "inspect", "success_criteria": {"checklist": [
            {"item": "produce evidence", "check": ""},
        ]}}]

    async def execute(*_a, **_k):
        return "worker output with evidence"

    async def review(*_a, **_k):
        # Host keeps rejecting, but criteria verification says all satisfied.
        return False, ["please elaborate"], [4.0]

    async def verify(*_a, **_k):
        return {"verified": 1, "total": 1, "verdicts": [
            {"index": 0, "item": "produce evidence", "satisfied": True, "reason": "present"},
        ]}

    async def merge(*_a, **_k):
        return "Host final consensus"

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", review)
    monkeypatch.setattr("modus.desktop.peri.verify_subtask_criteria", verify)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", merge)

    await server._run_peri_stream(websocket, server.DaoSession(id="s", db_id="db"), "task")

    assert any(item.get("type") == "done" and item.get("stop_reason") == "completed"
               for item in websocket.sent)


@pytest.mark.asyncio
async def test_peri_fails_when_criteria_unmet_and_host_rejects(monkeypatch):
    """Unmet criteria + rejecting Host -> revision loop fails at the cap."""
    from modus.desktop import server

    websocket = FakeWebSocket()
    monkeypatch.setattr(server, "_load_models_for_session", lambda _s, _m: _one_worker_models())

    async def decompose(*_a, **_k):
        return [{"name": "Inspect", "description": "inspect", "success_criteria": {"checklist": [
            {"item": "produce evidence", "check": ""},
        ]}}]

    async def execute(*_a, **_k):
        return "worker output"

    async def review(*_a, **_k):
        return False, ["please elaborate"], [4.0]

    async def verify(*_a, **_k):
        return {"verified": 0, "total": 1, "verdicts": [
            {"index": 0, "item": "produce evidence", "satisfied": False, "reason": "missing"},
        ]}

    async def merge(*_a, **_k):
        raise AssertionError("merge must not run when criteria are unmet")

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", review)
    monkeypatch.setattr("modus.desktop.peri.verify_subtask_criteria", verify)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", merge)

    await server._run_peri_stream(websocket, server.DaoSession(id="s", db_id="db"), "task")

    assert any(item.get("type") == "done" and item.get("stop_reason") == "failed"
               for item in websocket.sent)
    assert not any(item.get("type") == "done" and item.get("stop_reason") == "completed"
                   for item in websocket.sent)
