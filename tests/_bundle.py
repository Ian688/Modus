"""Frontend bundle helper for contract tests.

Contract tests assert on JS behavior strings.  The JS lives partly inline in
``index.html`` and partly in external ``.js`` files (protocol.js, workbench.js,
plus the split core/markdown/timeline/... files).  ``js_bundle()`` concatenates
them in the same order the page loads them, so assertions stay valid whether a
function is inline or in an external file.  ``page_html()`` returns the raw
HTML/CSS for DOM assertions.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src/modus/desktop/static"

# Order must match the <script src> sequence in index.html so concatenation
# mirrors real load order.  Inline JS is appended by js_bundle().
EXTERNAL_SCRIPTS = [
    "protocol.js",
    "workbench.js",
    "core.js",
    "markdown.js",
    "userCard.js",
    "timeline.js",
    "workspace.js",
    "websocket.js",
    "theme.js",
    "settings.js",
    "contextbar.js",
    "agentstatus.js",
    "workbenchwindows.js",
    "windowrouter.js",
    "moduswindows.js",
    "kanban.js",
    "cloud_accounts.js",
    "auth.js",
    "account.js",
    "bindings.js",
]


def _inline_js(page: str) -> str:
    # Only the bare <script> block (no src attribute) holds inline JS.
    match = re.search(r"<script>(.*?)</script>", page, re.S)
    return match.group(1) if match else ""


def js_bundle() -> str:
    """Return the concatenated page JS (external + inline), load order."""
    page = STATIC.joinpath("index.html").read_text(encoding="utf-8")
    parts = []
    for name in EXTERNAL_SCRIPTS:
        path = STATIC.joinpath(name)
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    parts.append(_inline_js(page))
    return "\n".join(parts)


def page_html() -> str:
    """Return the raw index.html for HTML/DOM assertions.

    CSS no longer lives inline in index.html — it was externalized to
    workbench.css so the page stays small.  Use ``css_bundle()`` for style
    assertions instead.
    """
    return STATIC.joinpath("index.html").read_text(encoding="utf-8")


def css_bundle() -> str:
    """Return the full merged stylesheet (workbench.css) for CSS assertions."""
    return STATIC.joinpath("workbench.css").read_text(encoding="utf-8")
