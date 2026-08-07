"""Real-browser preview contract for the Phase A1 browser toolset.

The desktop preview iframe (#kbPreviewFrame) is the human surface: browser tools
set metadata.preview_url, the frontend routes it through /api/preview, and the
iframe renders the localhost page.  This drives the actual UI with Playwright +
system Chrome to prove the preview pane is alive and the same-origin proxy
loads a local page inside it.
"""

import pytest
import urllib.parse


def test_preview_frame_exists_but_hidden_initially(app):
    """The A1 preview pane is present but hidden until loadPreview opens it."""
    app.wait_for_selector("#kbPreviewFrame", state="attached", timeout=10_000)
    assert app.locator("#kbPreviewSection").is_hidden()


def test_loadPreview_routes_through_same_origin_proxy(app):
    """loadPreview points the iframe at /api/preview?url=<localhost>."""
    app.wait_for_selector("#kbPreviewFrame", state="attached", timeout=10_000)
    app.evaluate("() => window.ModusWindows.loadPreview('http://localhost:3000/')")
    app.wait_for_timeout(800)
    src = app.locator("#kbPreviewFrame").get_attribute("src")
    assert src is not None and src.startswith("/api/preview?url=")
    assert "localhost" in src
    # The section and drawer are unhidden so the preview is visible.
    assert app.locator("#kbPreviewSection").is_visible()


def test_preview_proxy_serves_html(app, server_proc):
    """The /api/preview endpoint returns the server's own index.html (HTML)."""
    import urllib.request

    port = server_proc["port"]
    target = urllib.parse.quote(f"http://localhost:{port}/")
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/preview?url={target}",
        timeout=10,
    ) as resp:
        assert resp.status == 200
        body = resp.read(2000).decode("utf-8", errors="replace")
        assert "<html" in body or "Modus" in body
