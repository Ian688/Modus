from fastapi.testclient import TestClient


def _patch_engine(monkeypatch, server) -> None:
    async def fake_registry(**_kwargs):
        return object()

    class FakeEngine:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", FakeEngine)


def test_skill_crud_over_websocket(monkeypatch, tmp_path):
    from modus.desktop import server
    from modus.skills import SkillRepository

    repository = SkillRepository(tmp_path / "skills")
    monkeypatch.setattr(server, "skill_repository", repository)
    _patch_engine(monkeypatch, server)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({"type":"skill_create", "name":"review-code", "description":"Review", "prompt":"Review this:"})
            created = socket.receive_json()
            assert created["type"] == "skills_updated"
            assert created["skills"] == [{"name":"review-code", "description":"Review", "prompt":"Review this:"}]

            socket.send_json({"type":"skills_list"})
            listed = socket.receive_json()
            assert listed["skills"] == created["skills"]

            socket.send_json({"type":"skill_delete", "name":"review-code"})
            deleted = socket.receive_json()
            assert deleted["skills"] == []
