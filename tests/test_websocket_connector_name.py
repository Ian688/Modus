from pathlib import Path

from _bundle import js_bundle


def test_websocket_connector_uses_modus_specific_name_to_avoid_extension_collision():
    page = js_bundle()

    assert "function modusConnectSocket()" in page
    assert "function modusWebSocketUrl()" in page
    assert 'if (location.protocol === "file:") return "ws://127.0.0.1:3000/ws";' in page
    assert 'ws.send(JSON.stringify({type:"model_repository_get"}));' in page
    assert "无法连接 Modus 服务，请先运行 ./start.sh" in page
    assert "setTimeout(modusConnectSocket, 3000)" in page
    assert "modusConnectSocket();" in page
    assert "function connect()" not in page
    assert "function connect()" not in page
    assert "setTimeout(connect," not in page
