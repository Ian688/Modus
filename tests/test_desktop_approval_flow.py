import asyncio

import pytest


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, packet: dict) -> None:
        self.sent.append(packet)


@pytest.mark.asyncio
async def test_desktop_approval_bridge_emits_typed_request_then_resolves_matching_future():
    from modus.desktop.events import RunEventEmitter
    from modus.desktop.server import DaoSession, resolve_pending_approval, wait_for_user_approval

    websocket = FakeWebSocket()
    session = DaoSession(id="session", db_id="db")
    emitter = RunEventEmitter(run_id="run_approval", mode="default", send_json=websocket.send_json)

    waiting = asyncio.create_task(wait_for_user_approval(
        websocket, session, emitter,
        {"tool_name": "bash", "input": {"command": "echo ok"}, "danger_level": "high"},
        timeout=1,
    ))
    for _ in range(10):
        if session.pending_approvals:
            break
        await asyncio.sleep(0)
    approval_event = next(packet["event"] for packet in websocket.sent if packet["type"] == "agent_event")
    approval_id = approval_event["payload"]["approval_id"]

    assert approval_event["type"] == "approval_request"
    assert approval_event["channel_id"] == "user_host"
    assert approval_event["payload"]["tool_name"] == "bash"
    assert approval_event["payload"]["run_id"] == emitter.run_id
    # These are empty only for this direct legacy bridge test; the executor
    # supplies all three bindings during a real tool approval.
    assert "tool_call_id" in approval_event["payload"]
    assert "input_hash" in approval_event["payload"]
    assert "approval_expires_at" in approval_event["payload"]
    assert resolve_pending_approval(session, emitter.run_id, approval_id, "approve") is True
    assert await waiting == "allow"
    assert session.pending_approvals == {}
    resolved = next(
        packet["event"] for packet in websocket.sent
        if packet["type"] == "agent_event" and packet["event"]["type"] == "approval_resolved"
    )
    assert resolved["parent_event_id"] == approval_event["event_id"]
    assert resolved["payload"]["approval_id"] == approval_id
    assert resolved["payload"]["decision"] == "allow"


@pytest.mark.asyncio
async def test_reconnected_session_can_resolve_run_bound_approval_through_shared_broker():
    from modus.desktop.events import RunEventEmitter
    from modus.desktop.server import DaoSession, resolve_pending_approval, wait_for_user_approval

    websocket = FakeWebSocket()
    owner_session = DaoSession(id="owner", db_id="db")
    reconnected_session = DaoSession(id="reconnected", db_id="db")
    emitter = RunEventEmitter(run_id="run_reconnect", mode="default", send_json=websocket.send_json)
    waiting = asyncio.create_task(wait_for_user_approval(
        websocket, owner_session, emitter, {"tool_name": "write_file"}, timeout=1,
    ))
    for _ in range(10):
        if owner_session.pending_approvals:
            break
        await asyncio.sleep(0)
    approval_event = next(packet["event"] for packet in websocket.sent if packet["type"] == "agent_event")
    approval_id = approval_event["payload"]["approval_id"]

    assert resolve_pending_approval(reconnected_session, emitter.run_id, approval_id, "approve") is True
    assert await waiting == "allow"
    assert owner_session.pending_approvals == {}


@pytest.mark.asyncio
async def test_external_waiter_cancellation_does_not_cancel_visible_approval_future():
    from modus.desktop.events import RunEventEmitter
    from modus.desktop.server import DaoSession, resolve_pending_approval, wait_for_user_approval

    websocket = FakeWebSocket()
    session = DaoSession(id="session", db_id="db")
    emitter = RunEventEmitter(run_id="run_shield", mode="default", send_json=websocket.send_json)
    waiting = asyncio.create_task(wait_for_user_approval(
        websocket, session, emitter, {"tool_name": "write_file"}, timeout=1,
    ))
    for _ in range(10):
        if session.pending_approvals:
            break
        await asyncio.sleep(0)
    approval_event = next(packet["event"] for packet in websocket.sent if packet["type"] == "agent_event")
    approval_id = approval_event["payload"]["approval_id"]

    # Simulate cancellation delivered to the waiter by an outer execution layer.
    # The visible approval must remain consumable instead of becoming stale.
    waiting.cancel()
    await asyncio.sleep(0)
    assert waiting.done() is False
    assert resolve_pending_approval(session, emitter.run_id, approval_id, "approve") is True
    assert await waiting == "allow"


@pytest.mark.asyncio
async def test_cross_run_approval_response_fails_closed_without_resolving_original_request():
    from modus.desktop.events import RunEventEmitter
    from modus.desktop.server import DaoSession, resolve_pending_approval, wait_for_user_approval

    websocket = FakeWebSocket()
    session = DaoSession(id="session", db_id="db")
    owner = RunEventEmitter(run_id="run_owner", mode="default", send_json=websocket.send_json)
    stale = RunEventEmitter(run_id="run_stale", mode="default", send_json=websocket.send_json)
    waiting = asyncio.create_task(wait_for_user_approval(
        websocket, session, owner, {"tool_name": "write_file"}, timeout=1,
    ))
    for _ in range(10):
        if session.pending_approvals:
            break
        await asyncio.sleep(0)
    approval_event = next(packet["event"] for packet in websocket.sent if packet["type"] == "agent_event")
    approval_id = approval_event["payload"]["approval_id"]

    assert resolve_pending_approval(session, stale.run_id, approval_id, "approve") is False
    assert len(session.pending_approvals) == 1
    assert not waiting.done()
    assert resolve_pending_approval(session, owner.run_id, approval_id, "approve") is True
    assert await waiting == "allow"


@pytest.mark.asyncio
async def test_cancelling_a_session_denies_all_pending_approvals_immediately():
    from modus.desktop.events import RunEventEmitter
    from modus.desktop.server import DaoSession, wait_for_user_approval

    websocket = FakeWebSocket()
    session = DaoSession(id="session", db_id="db")
    emitter = RunEventEmitter(run_id="run_approval", mode="default", send_json=websocket.send_json)
    waiting = asyncio.create_task(wait_for_user_approval(
        websocket, session, emitter, {"tool_name": "bash"}, timeout=1,
    ))
    for _ in range(10):
        if session.pending_approvals:
            break
        await asyncio.sleep(0)

    session.cancel_stream()

    assert await waiting == "deny"
    assert session.pending_approvals == {}


@pytest.mark.asyncio
async def test_desktop_approval_bridge_fails_closed_on_timeout_or_unknown_id():
    from modus.desktop.events import RunEventEmitter
    from modus.desktop.server import DaoSession, resolve_pending_approval, wait_for_user_approval

    websocket = FakeWebSocket()
    session = DaoSession(id="session", db_id="db")
    emitter = RunEventEmitter(run_id="run_approval", mode="default", send_json=websocket.send_json)

    assert resolve_pending_approval(session, emitter.run_id, "missing", "approve") is False
    decision = await wait_for_user_approval(
        websocket, session, emitter,
        {"tool_name": "bash", "input": {}, "danger_level": "high"},
        timeout=0.001,
    )

    assert decision == "deny"
    assert session.pending_approvals == {}


@pytest.mark.asyncio
async def test_approval_timeout_records_explicit_resolution_reason(monkeypatch, tmp_path):
    from modus.desktop import db
    from modus.desktop.events import RunEventEmitter
    from modus.desktop.server import DaoSession, wait_for_user_approval

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    persisted = db.create_session("approval timeout")
    db.create_run("run-timeout", persisted["id"], "default")
    websocket = FakeWebSocket()
    session = DaoSession(id="session", db_id=persisted["id"])
    emitter = RunEventEmitter(run_id="run-timeout", mode="default", send_json=websocket.send_json)

    assert await wait_for_user_approval(
        websocket, session, emitter, {"tool_name": "bash"}, timeout=0.001,
    ) == "deny"

    with db._get_conn() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE run_id='run-timeout'").fetchone()
    assert row["decision"] == "deny"
    assert row["resolution_reason"] == "approval_timeout"


def test_frontend_declares_typed_approval_card_and_response_handler():
    from pathlib import Path

    from _bundle import js_bundle

    page = js_bundle()
    assert 'case "approval_request":' in page
    assert 'event.type === "approval_request" || event.type === "approval_resolved"' in page
    assert "approval_response" in page
    assert "approval_id" in page
    assert "approvalsByTool" in page
    assert "_renderApprovalState" in page
    assert 'class="execution-receipt"' in page
    assert 'stateName === "running"' in page
    assert "_finishRun(event.run_id)" in page
    assert "timelineRenderer.markApprovalDecision" in page
