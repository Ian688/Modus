from pathlib import Path


def test_websocket_allows_settings_access_before_a_primary_api_key_is_configured():
    server = (Path(__file__).parents[1] / "src/modus/desktop/server.py").read_text()

    assert 'logger.warning("WebSocket started without a primary API key; settings remain available")' in server
    assert 'await websocket.close()' not in server[server.index('async def websocket_endpoint'):server.index('async def websocket_endpoint') + 1500]
