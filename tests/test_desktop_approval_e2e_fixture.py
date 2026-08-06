from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def guard_home(tmp_path, monkeypatch):
    """Anchor Path.home() at tmp_path so the fixture's workspace is inside home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _until(socket, terminal_types: set[str], limit: int = 30) -> list[dict]:
    packets: list[dict] = []
    for _ in range(limit):
        packet = socket.receive_json()
        packets.append(packet)
        if packet["type"] in terminal_types:
            return packets
        if packet["type"] == "error":
            raise AssertionError(f"WebSocket failed before {terminal_types}: {packet!r}")
    raise AssertionError(f"did not receive {terminal_types}; got {packets!r}")


def _approval_event(packets: list[dict]) -> dict:
    return next(
        packet["event"]
        for packet in packets
        if packet["type"] == "agent_event" and packet["event"]["type"] == "approval_request"
    )


def test_opt_in_approval_fixture_allow_writes_only_controlled_workspace(monkeypatch, guard_home):
    """Exercise ToolExecutor → WebSocket approval → PathGuard write end-to-end.

    The fixture is explicitly selected by process environment, so this test
    proves the user-visible browser path without a live provider or exposing a
    test-mode switch to WebSocket clients.
    """
    from modus.desktop import server

    monkeypatch.setenv("MODUS_DESKTOP_TEST_MODE", "approval_write")
    monkeypatch.setenv("MODUS_DESKTOP_TEST_WORKSPACE", str(guard_home))

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({"type": "run_message", "content": "allow proof"})
            received = _until(socket, {"agent_event"})
            while not any(
                packet.get("type") == "agent_event"
                and packet["event"].get("type") == "approval_request"
                for packet in received
            ):
                received.extend(_until(socket, {"agent_event"}))
            approval = _approval_event(received)
            payload = approval["payload"]
            assert payload["tool_name"] == "write_file"
            assert payload["tool_call_id"] == "approval_e2e_write_1"
            assert payload["input"]["path"] == "approval-proof.txt"
            assert payload["input_hash"]
            assert payload["approval_expires_at"] > 0

            socket.send_json({
                "type": "approval_response", "run_id": approval["run_id"],
                "approval_id": payload["approval_id"], "decision": "approve",
            })
            terminal = _until(socket, {"done", "error"})

    proof = guard_home / "approval-proof.txt"
    assert proof.read_text() == "Approved browser E2E write: allow proof"
    assert any(packet["type"] == "done" for packet in terminal)


def test_opt_in_approval_fixture_deny_writes_nothing(monkeypatch, guard_home):
    from modus.desktop import server

    monkeypatch.setenv("MODUS_DESKTOP_TEST_MODE", "approval_write")
    monkeypatch.setenv("MODUS_DESKTOP_TEST_WORKSPACE", str(guard_home))

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({"type": "run_message", "content": "deny proof"})
            received = []
            while not any(
                packet.get("type") == "agent_event"
                and packet["event"].get("type") == "approval_request"
                for packet in received
            ):
                received.extend(_until(socket, {"agent_event"}))
            approval = _approval_event(received)
            socket.send_json({
                "type": "approval_response", "run_id": approval["run_id"],
                "approval_id": approval["payload"]["approval_id"], "decision": "deny",
            })
            terminal = _until(socket, {"done"})

    assert not (guard_home / "approval-proof.txt").exists()
    assert any(
        packet.get("type") == "agent_event"
        and packet["event"].get("type") == "run_error"
        and "approval denied" in packet["event"]["payload"]["message"].lower()
        for packet in terminal
    )


def test_second_browser_action_cannot_replay_resolved_approval(monkeypatch, guard_home):
    from modus.desktop import server

    monkeypatch.setenv("MODUS_DESKTOP_TEST_MODE", "approval_write")
    monkeypatch.setenv("MODUS_DESKTOP_TEST_WORKSPACE", str(guard_home))

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({"type": "run_message", "content": "replay proof"})
            received = []
            while not any(
                packet.get("type") == "agent_event"
                and packet["event"].get("type") == "approval_request"
                for packet in received
            ):
                received.extend(_until(socket, {"agent_event"}))
            approval = _approval_event(received)
            response = {
                "type": "approval_response", "run_id": approval["run_id"],
                "approval_id": approval["payload"]["approval_id"], "decision": "approve",
            }
            socket.send_json(response)
            assert any(packet["type"] == "done" for packet in _until(socket, {"done", "error"}))
            # The same browser can emit an old card response again; broker must
            # ignore it, and no new tool execution is possible.
            socket.send_json(response)

    proof = guard_home / "approval-proof.txt"
    assert proof.read_text() == "Approved browser E2E write: replay proof"


def test_approval_fixture_is_not_enabled_without_explicit_process_mode(monkeypatch, tmp_path):
    from modus.desktop import server

    monkeypatch.delenv("MODUS_DESKTOP_TEST_MODE", raising=False)
    monkeypatch.setenv("MODUS_DESKTOP_TEST_WORKSPACE", str(tmp_path))
    assert server._approval_e2e_workspace() is None
