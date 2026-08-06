import asyncio

import pytest
from fastapi.testclient import TestClient


def test_health_and_busy_routes_have_distinct_contracts():
    from modus.desktop import server

    with TestClient(server.app) as client:
        assert client.get("/api/health").json() == {"status": "ok", "version": "0.1.0"}
        assert client.get("/api/busy").json() == {"busy": False}


@pytest.mark.asyncio
async def test_busy_observes_the_real_background_run_task():
    from modus.desktop import server

    session = server.DaoSession(id="busy-session", db_id="db")
    server.manager._sessions[session.id] = session
    release = asyncio.Event()

    async def run():
        await release.wait()

    try:
        assert server.start_session_run(session, run()) is True
        await asyncio.sleep(0)
        assert await server.api_busy() == {"busy": True}
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert await server.api_busy() == {"busy": False}
    finally:
        server.manager._sessions.pop(session.id, None)


@pytest.mark.asyncio
async def test_repository_broadcast_has_per_window_identity_and_one_revision():
    from modus.desktop import server

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    first = server.manager.create(engine=object())
    second = server.manager.create(engine=object())
    first.db_id, first.model_id = "first-db", "model-a"
    second.db_id, second.model_id = "second-db", "model-b"
    first_socket, second_socket = Socket(), Socket()
    server.manager.attach_websocket(first, first_socket)
    server.manager.attach_websocket(second, second_socket)
    try:
        await server._broadcast_model_repository(
            {"models": [], "selection": {}}, origin=first,
            extra={"model": {"id": "new"}},
        )
    finally:
        server.manager.discard(first)
        server.manager.discard(second)

    first_packet = first_socket.sent[-1]
    second_packet = second_socket.sent[-1]
    assert first_packet["repository_revision"] == second_packet["repository_revision"]
    assert first_packet["db_id"] == "first-db"
    assert first_packet["model_id"] == "model-a"
    assert first_packet["model"] == {"id": "new"}
    assert second_packet["db_id"] == "second-db"
    assert second_packet["model_id"] == "model-b"
    assert "model" not in second_packet


@pytest.mark.asyncio
async def test_shared_capability_broadcasts_reach_every_window_with_revisions(monkeypatch):
    from modus.desktop import server

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    class Mcp:
        def list_configs(self):
            return [{"name": "search", "status": "connected"}]

    class Extensions:
        def list_public(self):
            return [{"id": "mcp.search", "status": "connected"}]

    monkeypatch.setattr(server, "mcp_manager", Mcp())
    monkeypatch.setattr(server, "extension_registry", Extensions())
    origin = server.manager.create(engine=object())
    observer = server.manager.create(engine=object())
    origin_socket, observer_socket = Socket(), Socket()
    server.manager.attach_websocket(origin, origin_socket)
    server.manager.attach_websocket(observer, observer_socket)
    try:
        await server._broadcast_skills(
            [{"name": "review", "description": "", "prompt": "Review"}],
            origin=origin,
        )
        await server._broadcast_extensions(origin=origin)
    finally:
        server.manager.discard(origin)
        server.manager.discard(observer)

    for socket in (origin_socket, observer_socket):
        assert socket.sent[0]["type"] == "skills_updated"
        assert socket.sent[0]["skills_revision"] > 0
        assert socket.sent[1]["type"] == "extensions_updated"
        assert socket.sent[1]["extensions_revision"] > 0
        assert socket.sent[1]["servers"][0]["status"] == "connected"
    assert origin_socket.sent[0]["skills_revision"] == observer_socket.sent[0]["skills_revision"]
    assert origin_socket.sent[1]["extensions_revision"] == observer_socket.sent[1]["extensions_revision"]


@pytest.mark.asyncio
async def test_mcp_tool_change_rebuilds_idle_hosts_and_advances_revision(monkeypatch):
    from modus.desktop import server

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    session = server.manager.create(engine=object())
    socket = Socket()
    server.manager.attach_websocket(session, socket)
    rebuilt = []

    async def rebuild(target):
        rebuilt.append(target.id)

    monkeypatch.setattr(server, "_rebuild_session_engine", rebuild)
    try:
        await server._handle_mcp_tools_changed("search")
    finally:
        server.manager.discard(session)

    assert rebuilt == [session.id]
    assert socket.sent[-1]["type"] == "extensions_updated"
    assert socket.sent[-1]["changed_server"] == "search"
    assert session.extensions_revision == socket.sent[-1]["extensions_revision"]


@pytest.mark.asyncio
async def test_session_catalog_broadcast_reaches_other_windows_with_revision():
    from modus.desktop import server
    from modus.desktop.db import create_session

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    create_session(title="Shared catalog")
    origin = server.manager.create(engine=object())
    observer = server.manager.create(engine=object())
    origin_socket, observer_socket = Socket(), Socket()
    server.manager.attach_websocket(origin, origin_socket)
    server.manager.attach_websocket(observer, observer_socket)
    try:
        await server._broadcast_sessions_list(origin=origin)
    finally:
        server.manager.discard(origin)
        server.manager.discard(observer)

    assert origin_socket.sent == []
    assert observer_socket.sent[-1]["type"] == "sessions_changed"
    assert observer_socket.sent[-1]["catalog_revision"] > 0
    assert "sessions" not in observer_socket.sent[-1]


@pytest.mark.asyncio
async def test_revision_broadcasts_share_one_process_epoch():
    from modus.desktop import server

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    origin = server.manager.create(engine=object())
    observer = server.manager.create(engine=object())
    origin_socket, observer_socket = Socket(), Socket()
    server.manager.attach_websocket(origin, origin_socket)
    server.manager.attach_websocket(observer, observer_socket)
    try:
        await server._broadcast_model_repository({"models": [], "selection": {}}, origin=origin)
        await server._broadcast_skills([], origin=origin)
        await server._broadcast_extensions(origin=origin)
        await server._broadcast_sessions_list(origin=origin)
    finally:
        server.manager.discard(origin)
        server.manager.discard(observer)

    authoritative_types = {
        "model_repository_updated", "skills_updated", "extensions_updated",
        "sessions_changed",
    }
    packets = [
        packet for packet in observer_socket.sent
        if packet["type"] in authoritative_types
    ]
    assert {packet["type"] for packet in packets} == authoritative_types
    assert {packet["server_epoch"] for packet in packets} == {
        server.manager.server_epoch,
    }
    assert server._session_identity(origin)["server_epoch"] == server.manager.server_epoch


@pytest.mark.asyncio
async def test_session_catalog_broadcast_defers_busy_observers():
    import asyncio

    from modus.desktop import server

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    origin = server.manager.create(engine=object())
    busy = server.manager.create(engine=object())
    origin_socket, busy_socket = Socket(), Socket()
    server.manager.attach_websocket(origin, origin_socket)
    server.manager.attach_websocket(busy, busy_socket)
    release = asyncio.Event()
    try:
        assert server.start_session_run(busy, release.wait()) is True
        await asyncio.sleep(0)
        await server._broadcast_sessions_list(origin=origin)
        assert busy_socket.sent == []
    finally:
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        server.manager.discard(origin)
        server.manager.discard(busy)


@pytest.mark.asyncio
async def test_completed_run_owner_receives_its_fresh_catalog_snapshot():
    import asyncio

    from modus.desktop import server
    from modus.desktop.db import create_session

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    record = create_session(title="Run owner")
    owner = server.manager.create(engine=object())
    owner.db_id = record["id"]
    socket = Socket()
    server.manager.attach_websocket(owner, socket)
    release = asyncio.Event()
    try:
        assert server.start_session_run(owner, release.wait()) is True
        await asyncio.sleep(0)
        await server._broadcast_sessions_list(completed_runtime=owner)
    finally:
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        server.manager.discard(owner)

    assert socket.sent[-1]["type"] == "sessions_changed"
    assert socket.sent[-1]["catalog_revision"] > 0


@pytest.mark.asyncio
async def test_serialized_websocket_never_overlaps_outbound_sends():
    import asyncio

    from modus.desktop import server

    class RawSocket:
        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def send_json(self, _packet):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0)
            self.active -= 1

    raw = RawSocket()
    socket = server._SerializedWebSocket(raw)
    await asyncio.gather(
        socket.send_json({"sequence": 1}),
        socket.send_json({"sequence": 2}),
    )

    assert raw.max_active == 1


@pytest.mark.asyncio
async def test_serialized_websocket_normalizes_only_closed_socket_runtime_error():
    from fastapi import WebSocketDisconnect
    from starlette.websockets import WebSocketState

    from modus.desktop import server

    class RawSocket:
        def __init__(self, state):
            self.application_state = state

        async def send_json(self, _packet):
            raise RuntimeError("Cannot call send once a close message has been sent")

    closed = server._SerializedWebSocket(RawSocket(WebSocketState.DISCONNECTED))
    with pytest.raises(WebSocketDisconnect):
        await closed.send_json({"type": "closed"})

    connected = server._SerializedWebSocket(RawSocket(WebSocketState.CONNECTED))
    with pytest.raises(RuntimeError, match="Cannot call send"):
        await connected.send_json({"type": "application-error"})
