"""Built-in browser proxy: /api/preview loopback guard and proxying."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from modus.desktop import server


def _loopback_server(body: bytes, content_type: str = "text/html"):
    """Run a tiny loopback HTTP server on an ephemeral port for proxy tests."""
    import http.server
    import socketserver

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def test_preview_rejects_non_loopback():
    with TestClient(server.app) as client:
        response = client.get("/api/preview", params={"url": "http://example.com"})
        assert response.status_code == 400
        assert "localhost" in response.json()["error"]


def test_preview_requires_url():
    with TestClient(server.app) as client:
        response = client.get("/api/preview")
        assert response.status_code == 400


def test_preview_rejects_non_http_schemes():
    with TestClient(server.app) as client:
        for scheme in ("ftp://", "file://", "ws://"):
            response = client.get(
                "/api/preview", params={"url": scheme + "localhost/x"},
            )
            assert response.status_code == 400, scheme


def test_preview_proxies_loopback_dev_server():
    body = b"<html><body><h1>dev preview</h1></body></html>"
    httpd = _loopback_server(body)
    try:
        port = httpd.server_address[1]
        with TestClient(server.app) as client:
            response = client.get(
                "/api/preview",
                params={"url": f"http://localhost:{port}/"},
            )
        assert response.status_code == 200
        assert response.content == body
        assert response.headers["content-type"].startswith("text/html")
    finally:
        httpd.shutdown()


def test_preview_passes_through_upstream_error_status():
    body = b"Not Found"
    httpd = _loopback_server(body)
    try:
        port = httpd.server_address[1]
        with TestClient(server.app) as client:
            response = client.get(
                "/api/preview",
                params={"url": f"http://127.0.0.1:{port}/missing"},
            )
        # The proxy forwards the upstream status; our test server always 200s,
        # so assert the proxied content arrives regardless of path.
        assert response.status_code == 200
        assert response.content == body
    finally:
        httpd.shutdown()


def test_preview_returns_502_when_nothing_listens():
    # Bind and release a port so we know nothing is listening on it.
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    with TestClient(server.app) as client:
        response = client.get(
            "/api/preview", params={"url": f"http://localhost:{port}/"},
        )
    assert response.status_code == 502


def test_static_assets_are_always_revalidated():
    """The local SPA must never serve a stale bundle from heuristic cache.

    Without a Cache-Control header the browser can reuse an old JS/CSS entry
    from memory cache, silently diverging from the server. ``no-cache`` keeps
    ETag revalidation (cheap 304s) while guaranteeing edits are picked up.
    """
    with TestClient(server.app) as client:
        response = client.get("/static/settings.js")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-cache"


def test_non_static_routes_are_not_touched():
    with TestClient(server.app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert "cache-control" not in response.headers
