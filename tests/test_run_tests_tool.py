import asyncio
import json
import sys
from pathlib import Path

import pytest

from modus.config import ModusConfig
from modus.tools.base import ToolContext
from modus.tools.builtins import run_tests


def context(workspace: Path) -> ToolContext:
    return ToolContext(cwd=str(workspace), workspace_root=str(workspace), config=ModusConfig())


@pytest.fixture
def guard_home(tmp_path, monkeypatch):
    """Point Path.home() at a temp dir so the home-anchored guard has scope."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.mark.asyncio
async def test_run_tests_returns_structured_passing_evidence(guard_home):
    command = f'"{sys.executable}" -c "print(\'3 passed, 1 warning in 0.12s\')"'

    result = await run_tests({"command": command, "timeout": 10}, context(guard_home))
    evidence = json.loads(result.content)

    assert result.is_error is False
    assert evidence["schema"] == "modus.verification.v1"
    assert result.metadata["verification"]["schema"] == "modus.verification.v1"
    assert evidence["status"] == "passed"
    assert evidence["exit_code"] == 0
    assert evidence["counts"] == {"passed": 3, "warnings": 1}
    assert evidence["duration_seconds"] >= 0


@pytest.mark.asyncio
async def test_run_tests_preserves_failed_exit_and_counts(guard_home):
    command = f'"{sys.executable}" -c "print(\'1 passed, 2 failed in 0.10s\'); raise SystemExit(2)"'

    result = await run_tests({"command": command}, context(guard_home))
    evidence = json.loads(result.content)

    assert result.is_error is True
    assert evidence["status"] == "failed"
    assert evidence["exit_code"] == 2
    assert evidence["counts"]["passed"] == 1
    assert evidence["counts"]["failed"] == 2


@pytest.mark.asyncio
async def test_run_tests_rejects_escape_out_of_home(tmp_path, guard_home):
    workspace = guard_home / "workspace"
    workspace.mkdir()
    outside_home = tmp_path / "outside-home"
    outside_home.mkdir()

    # ``../..`` from a workspace nested inside the fake home lands outside home.
    result = await run_tests({"command": "true", "path": "../.."}, context(workspace))

    assert result.is_error is True
    assert "escapes home" in result.content.lower()


@pytest.mark.asyncio
async def test_run_tests_does_not_start_after_cancellation(guard_home):
    ctx = context(guard_home)
    ctx.cancel_event = asyncio.Event()
    ctx.cancel_event.set()

    result = await run_tests({"command": "true"}, ctx)

    assert result.is_error is True
    assert "cancelled" in result.content.lower()


@pytest.mark.asyncio
async def test_run_tests_cancels_a_running_process(guard_home):
    ctx = context(guard_home)
    ctx.cancel_event = asyncio.Event()
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'

    running = asyncio.create_task(run_tests({"command": command, "timeout": 10}, ctx))
    await asyncio.sleep(0.05)
    ctx.cancel_event.set()
    result = await asyncio.wait_for(running, timeout=2)
    evidence = json.loads(result.content)

    assert result.is_error is True
    assert evidence["status"] == "cancelled"
    assert result.display_summary.startswith("验证已取消")


@pytest.mark.asyncio
async def test_run_tests_reports_timeout_and_reaps_process(guard_home):
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'

    result = await asyncio.wait_for(
        run_tests({"command": command, "timeout": 1}, context(guard_home)), timeout=3,
    )
    evidence = json.loads(result.content)

    assert result.is_error is True
    assert evidence["status"] == "timed_out"
    assert result.display_summary.startswith("验证超时")


@pytest.mark.asyncio
async def test_run_tests_rejects_non_numeric_timeout(guard_home):
    result = await run_tests({"command": "true", "timeout": "later"}, context(guard_home))

    assert result.is_error is True
    assert "must be a number" in result.content