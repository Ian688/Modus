"""Skill attachment contract: a run_message carrying skill_id injects the
resolved skill prompt as deliberate system context, and never fails a run."""

from pathlib import Path

from fastapi.testclient import TestClient

from modus.skills import SkillRepository


class CapturingEngine:
    last_history: list | None = None

    def __init__(self, **_kwargs):
        self.system_prompt = "base prompt"

    async def ask(self, message, history=None, **_kwargs):
        CapturingEngine.last_history = list(history or [])
        yield {"type": "done", "messages": [], "total_tokens": 0, "total_turns": 1}


def _patch_engine(monkeypatch, server):
    async def fake_registry(**_kwargs):
        return object()

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", CapturingEngine)


def _receive_until(socket, target: str, limit: int = 20):
    for _ in range(limit):
        packet = socket.receive_json()
        if packet.get("type") == target:
            return packet
    raise AssertionError(f"missing {target}")


def _seed_skill(monkeypatch, server, tmp_path: Path) -> SkillRepository:
    repository = SkillRepository(tmp_path / "skills")
    repository.save(
        name="review-code", description="Review this", prompt="Only emit review findings.",
    )
    monkeypatch.setattr(server, "skill_repository", repository)
    return repository


def test_run_message_attaches_skill_context_for_default(monkeypatch, tmp_path):
    from modus.desktop import server

    _patch_engine(monkeypatch, server)
    _seed_skill(monkeypatch, server, tmp_path)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({"type": "run_message", "content": "review", "skill_id": "review-code"})
            _receive_until(socket, "done")

    assert CapturingEngine.last_history is not None
    skill_messages = [m for m in CapturingEngine.last_history if m.role == "system" and "review-code" in m.content]
    assert skill_messages, "attached skill prompt must reach the engine history as system context"
    assert "Only emit review findings." in skill_messages[0].content


def test_run_message_unknown_skill_does_not_fail_run(monkeypatch, tmp_path):
    from modus.desktop import server

    _patch_engine(monkeypatch, server)
    _seed_skill(monkeypatch, server, tmp_path)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({"type": "run_message", "content": "review", "skill_id": "does-not-exist"})
            done = _receive_until(socket, "done")
            assert done["stop_reason"] == "completed"
