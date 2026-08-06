"""Real-browser E2E fixtures.

Spins up the Modus Desktop server as a subprocess with:

- ``MODUS_DESKTOP_TEST_MODE=approval_write`` so the ``ApprovalE2EFixtureEngine``
  is selected (no live LLM provider; the real ToolExecutor/approval/PathGuard
  boundary is still exercised).
- ``MODUS_DESKTOP_TEST_WORKSPACE=<tmp>`` pointing at an existing controlled dir.
- ``MODUS_DATA_DIR=<tmp>/data`` so the server never touches ``~/.modus``.

A Playwright Chromium page (reusing the installed system Chrome via
``channel="chrome"``) connects to the subprocess.  This exercises the real
browser surface, not a mocked WebSocket client.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).parents[2]
SERVER_PORT = 3199
BASE_URL = f"http://127.0.0.1:{SERVER_PORT}"


@pytest.fixture(scope="session")
def server_proc(tmp_path_factory):
    """Start one isolated Modus Desktop server for the whole E2E session."""
    data_dir = tmp_path_factory.mktemp("modus-e2e-data")
    workspace = tmp_path_factory.mktemp("modus-e2e-workspace")

    env = os.environ.copy()
    env["MODUS_DESKTOP_TEST_MODE"] = "approval_write"
    env["MODUS_DESKTOP_TEST_WORKSPACE"] = str(workspace)
    env["MODUS_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")

    proc = subprocess.Popen(
        [
            sys.executable, "-B", "-m", "modus", "serve",
            "--host", "127.0.0.1", "--port", str(SERVER_PORT),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_ready(proc)
        yield {"port": SERVER_PORT, "workspace": workspace, "data_dir": data_dir}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _wait_for_ready(proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            raise AssertionError(f"server exited early:\n{out}")
        try:
            request = urllib.request.Request(
                f"{BASE_URL}/api/health", headers={"User-Agent": "modus-e2e"},
            )
            with urllib.request.urlopen(request, timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.2)
    raise AssertionError("server did not become healthy in time")


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.errors = errors  # type: ignore[attr-defined]
    yield page
    context.close()


@pytest.fixture()
def app(page, server_proc):
    """Ensure a default model exists, then navigate to a usable composer."""
    _seed_model(page)
    page.reload(wait_until="load")
    page.wait_for_function(
        "() => document.getElementById('composerSelect') && !document.getElementById('composerSelect').disabled",
        timeout=15_000,
    )
    return page


def _seed_model(page) -> None:
    """Create a default model from inside the page so the composer unlocks.

    The approval fixture engine does not call a real provider, so the model
    only needs to exist and be selectable as the default.
    """
    page.goto(BASE_URL, wait_until="load")
    # The frontend exposes the live WebSocket as the top-level `ws` binding.
    page.wait_for_function(
        "() => typeof ws !== 'undefined' && ws && ws.readyState === WebSocket.OPEN",
        timeout=15_000,
    )
    page.evaluate(
        """() => {
            ws.send(JSON.stringify({
              type: "model_create", name: "E2E", provider: "deepseek",
              model: "e2e-test", api_key: "e2e-key",
            }));
        }"""
    )
    # Wait for the repository update to unlock the composer.
    page.wait_for_function(
        "() => document.getElementById('composerSelect') && !document.getElementById('composerSelect').disabled",
        timeout=15_000,
    )


def seed_skill(page, *, name: str = "review-code", prompt: str = "只输出可验证的审查结论。") -> None:
    """Create a skill over the live WebSocket so the @ affordance has a target."""
    import json

    page.evaluate(
        """(args) => {
            ws.send(JSON.stringify({
              type: "skill_create", name: args.name,
              description: "E2E skill", prompt: args.prompt,
            }));
        }""",
        {"name": name, "prompt": prompt},
    )
    expected = json.dumps(name)
    page.wait_for_function(
        "() => typeof modusSkills !== 'undefined' && modusSkills.some(s => s.name === "
        + expected
        + ")",
        timeout=10_000,
    )


@pytest.fixture()
def seed_skill_on_page(page):
    """Fixture exposing a bound skill-seeder for the current page."""
    def _seed(**kwargs):
        seed_skill(page, **kwargs)
    return _seed
