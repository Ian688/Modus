import asyncio

import pytest


@pytest.mark.asyncio
async def test_session_background_run_keeps_receiver_free_and_rejects_second_run():
    from modus.desktop.server import DaoSession, start_session_run

    session = DaoSession(id="session", db_id="db")
    started = asyncio.Event()
    release = asyncio.Event()

    async def run():
        started.set()
        await release.wait()

    assert start_session_run(session, run()) is True
    await started.wait()
    assert session.active_run_task is not None

    async def second():
        raise AssertionError("second run must not start")

    assert start_session_run(session, second()) is False
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.active_run_task is None

    async def good_run():
        return None

    assert start_session_run(session, good_run()) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.active_run_task is None


@pytest.mark.asyncio
async def test_run_task_exception_is_consumed_and_session_can_start_again():
    from modus.desktop.server import DaoSession, start_session_run

    session = DaoSession(id="session", db_id="db")

    async def bad_run():
        raise RuntimeError("expected test failure")

    assert start_session_run(session, bad_run()) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.active_run_task is None


@pytest.mark.asyncio
async def test_run_settlement_waits_for_post_done_cleanup_and_releases_all_ownership():
    from modus.desktop.session_state import active_run_owner, start_session_run
    from modus.desktop import server

    class WebSocket:
        def __init__(self):
            self.sent = []
            self.ownership_at_settlement = None

        async def send_json(self, packet):
            if packet["type"] == "run_settled":
                self.ownership_at_settlement = (
                    session.active_run_task,
                    active_run_owner(session.db_id),
                )
            self.sent.append(packet)

    session = server.DaoSession(id="settlement", db_id="settlement-db")
    websocket = WebSocket()
    done_sent = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def run():
        session.active_run_id = "run-settlement"
        await websocket.send_json({"type": "done", "stop_reason": "completed"})
        done_sent.set()
        await release_cleanup.wait()

    assert start_session_run(
        session, run(),
        on_settled=server._run_settlement_callback(websocket, session),
    ) is True
    task = session.active_run_task
    assert task is not None
    await done_sent.wait()
    assert session.active_run_task is not None
    assert active_run_owner(session.db_id) is session
    assert [packet["type"] for packet in websocket.sent] == ["done"]

    release_cleanup.set()
    await task
    assert [packet["type"] for packet in websocket.sent] == ["done", "run_settled"]
    assert websocket.sent[-1]["run_id"] == "run-settlement"
    assert websocket.sent[-1]["run_owned_by_connection"] is True
    assert websocket.ownership_at_settlement == (None, None)
    assert session.active_run_task is None
    assert session.active_run_session_id is None
    assert session.active_run_id is None
    assert active_run_owner(session.db_id) is None


@pytest.mark.asyncio
async def test_run_settlement_reaches_owner_and_same_session_observer_only():
    from modus.desktop import server

    class WebSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    owner = server.DaoSession(id="owner-view", db_id="shared-view")
    observer = server.DaoSession(id="observer-view", db_id="shared-view")
    unrelated = server.DaoSession(id="other-view", db_id="other-db")
    owner_socket, observer_socket, other_socket = WebSocket(), WebSocket(), WebSocket()
    for runtime, socket in (
        (owner, owner_socket), (observer, observer_socket), (unrelated, other_socket),
    ):
        server.manager._sessions[runtime.id] = runtime
        server.manager.attach_websocket(runtime, socket)
    try:
        await server._run_settlement_callback(owner_socket, owner)("run-shared")
    finally:
        for runtime in (owner, observer, unrelated):
            server.manager.discard(runtime)

    assert len(owner_socket.sent) == len(observer_socket.sent) == 1
    assert other_socket.sent == []
    owner_packet, observer_packet = owner_socket.sent[0], observer_socket.sent[0]
    assert owner_packet["run_owned_by_connection"] is True
    assert observer_packet["run_owned_by_connection"] is False
    assert owner_packet["runtime_session_id"] == owner.id
    assert observer_packet["runtime_session_id"] == observer.id
    assert owner_packet["db_id"] == observer_packet["db_id"] == "shared-view"


@pytest.mark.asyncio
async def test_session_identity_is_bound_until_background_run_finishes():
    from modus.desktop import server

    class WebSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    session = server.DaoSession(id="session", db_id="session-a")
    release = asyncio.Event()

    async def run():
        await release.wait()

    assert server.start_session_run(session, run()) is True
    websocket = WebSocket()
    assert session.active_run_session_id == "session-a"
    assert await server._reject_session_mutation_while_running(websocket, session, "切换会话") is True
    assert websocket.sent[-1]["code"] == "session_busy"
    assert session.db_id == "session-a"

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.active_run_task is None
    assert session.active_run_session_id is None
    assert await server._reject_session_mutation_while_running(websocket, session, "切换会话") is False


@pytest.mark.asyncio
async def test_two_runtime_owners_cannot_run_the_same_persisted_session_concurrently():
    from modus.desktop.server import DaoSession, start_session_run

    first = DaoSession(id="window-a", db_id="shared-db")
    second = DaoSession(id="window-b", db_id="shared-db")
    started = asyncio.Event()
    release = asyncio.Event()

    async def first_run():
        started.set()
        await release.wait()

    async def second_run():
        raise AssertionError("second window must not enter the shared session run")

    assert start_session_run(first, first_run()) is True
    await started.wait()
    assert start_session_run(second, second_run()) is False
    assert second.active_run_task is None

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert start_session_run(second, asyncio.sleep(0)) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cross_window_session_mutation_detects_persisted_run_owner():
    from modus.desktop import server

    class WebSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    owner = server.DaoSession(id="owner", db_id="shared-db")
    observer = server.DaoSession(id="observer", db_id="shared-db")
    release = asyncio.Event()
    assert server.start_session_run(owner, release.wait()) is True
    await asyncio.sleep(0)

    socket = WebSocket()
    assert await server._reject_session_mutation_while_running(
        socket, observer, "删除会话", target_db_id="shared-db",
    ) is True
    assert socket.sent[-1]["code"] == "session_busy"

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_current_run_does_not_block_mutating_a_different_idle_session():
    from modus.desktop import server

    class WebSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    current = server.DaoSession(id="owner", db_id="running-db")
    release = asyncio.Event()
    assert server.start_session_run(current, release.wait()) is True
    await asyncio.sleep(0)

    socket = WebSocket()
    assert await server._reject_session_mutation_while_running(
        socket, current, "重命名会话", target_db_id="different-idle-db",
    ) is False
    assert socket.sent == []

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_shared_repository_mutation_waits_for_other_runtime_run():
    from modus.desktop import server

    class WebSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    owner = server.DaoSession(id="owner-global", db_id="owner-db")
    editor = server.DaoSession(id="editor-global", db_id="editor-db")
    release = asyncio.Event()
    server.manager._sessions[owner.id] = owner
    server.manager._sessions[editor.id] = editor
    try:
        assert server.start_session_run(owner, release.wait()) is True
        await asyncio.sleep(0)
        socket = WebSocket()

        assert await server._reject_global_model_mutation_while_running(
            socket, editor, "更新模型配置",
        ) is True
        assert socket.sent[-1]["code"] == "repository_busy"
        assert socket.sent[-1]["run_owned_by_connection"] is False
        assert socket.sent[-1]["runtime_session_id"] == editor.id
    finally:
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        server.manager._sessions.pop(owner.id, None)
        server.manager._sessions.pop(editor.id, None)


@pytest.mark.asyncio
async def test_shared_capability_mutation_waits_for_any_runtime_run():
    from modus.desktop import server

    class WebSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    owner = server.DaoSession(id="capability-owner", db_id="owner-db")
    editor = server.DaoSession(id="capability-editor", db_id="editor-db")
    release = asyncio.Event()
    server.manager._sessions[owner.id] = owner
    server.manager._sessions[editor.id] = editor
    try:
        assert server.start_session_run(owner, release.wait()) is True
        await asyncio.sleep(0)
        socket = WebSocket()
        assert await server._reject_shared_capability_mutation_while_running(
            socket, editor, "连接 MCP 服务器",
        ) is True
        assert socket.sent[-1]["code"] == "capabilities_busy"
        assert socket.sent[-1]["run_owned_by_connection"] is False
    finally:
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        server.manager._sessions.pop(owner.id, None)
        server.manager._sessions.pop(editor.id, None)


@pytest.mark.asyncio
async def test_disconnected_owner_still_freezes_shared_runtime_configuration():
    from modus.desktop import server

    class WebSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    owner = server.DaoSession(id="detached-owner", db_id="detached-owner-db")
    editor = server.DaoSession(id="detached-editor", db_id="detached-editor-db")
    release = asyncio.Event()
    server.manager._sessions[owner.id] = owner
    server.manager._sessions[editor.id] = editor
    try:
        assert server.start_session_run(owner, release.wait()) is True
        await asyncio.sleep(0)
        server.manager.discard(owner)
        assert owner.id not in server.manager._sessions

        socket = WebSocket()
        assert await server._reject_global_model_mutation_while_running(
            socket, editor, "更新模型配置",
        ) is True
        assert socket.sent[-1]["code"] == "repository_busy"
        assert socket.sent[-1]["run_owned_by_connection"] is False

        assert await server._reject_shared_capability_mutation_while_running(
            socket, editor, "连接 MCP 服务器",
        ) is True
        assert socket.sent[-1]["code"] == "capabilities_busy"
        assert socket.sent[-1]["run_owned_by_connection"] is False
        assert (await server.api_busy()) == {"busy": True}
    finally:
        release.set()
        task = owner.active_run_task
        if task is not None:
            await task
        server.manager._sessions.pop(editor.id, None)

    assert (await server.api_busy()) == {"busy": False}

@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["moa", "peri"])
async def test_enhanced_mode_run_keeps_websocket_receiver_free(monkeypatch, mode):
    from modus.desktop import server

    started = asyncio.Event()
    release = asyncio.Event()

    async def enhanced_run(_websocket, _session, _content):
        started.set()
        await release.wait()

    target = "_run_moa_session" if mode == "moa" else "_run_peri_session"
    monkeypatch.setattr(server, target, enhanced_run)
    session = server.DaoSession(id="session", db_id="db")
    coroutine = getattr(server, target)(object(), session, "task")

    assert server.start_session_run(session, coroutine) is True
    await started.wait()
    assert session.active_run_task is not None

    async def second():
        raise AssertionError("concurrent run must not start")

    assert server.start_session_run(session, second()) is False
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.active_run_task is None
