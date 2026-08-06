import asyncio

import pytest

from modus.config import ModusConfig
from modus.tools.base import ToolContext
from modus.tools.builtins import bash


class HangingProcess:
    pid = 4321
    returncode = None

    async def communicate(self):
        await asyncio.Event().wait()

    async def wait(self):
        return None

    def kill(self):
        self.returncode = -9


class CancelledProcess(HangingProcess):
    async def communicate(self):
        while self.returncode is None:
            await asyncio.sleep(0)
        return b"", b""


@pytest.mark.asyncio
async def test_bash_timeout_terminates_the_entire_unix_process_group(monkeypatch, tmp_path):
    created_with: dict = {}
    killed_groups: list[tuple[int, int]] = []

    async def fake_create_subprocess_shell(*args, **kwargs):
        created_with.update(kwargs)
        return HangingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    monkeypatch.setattr("modus.tools.builtins.os.killpg", lambda pid, signal: killed_groups.append((pid, signal)))
    context = ToolContext(cwd=str(tmp_path), config=ModusConfig())

    result = await bash({"command": "sleep 60", "timeout": 0.001}, context)

    assert result.is_error is True
    assert created_with["start_new_session"] is True
    assert killed_groups and killed_groups[0][0] == 4321


@pytest.mark.asyncio
async def test_bash_cancellation_terminates_the_entire_unix_process_group(monkeypatch, tmp_path):
    killed_groups: list[tuple[int, int]] = []
    process = CancelledProcess()

    async def fake_create_subprocess_shell(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    def killpg(pid, signal):
        killed_groups.append((pid, signal))
        process.returncode = -9

    monkeypatch.setattr("modus.tools.builtins.os.killpg", killpg)
    context = ToolContext(cwd=str(tmp_path), config=ModusConfig(), cancel_event=asyncio.Event())

    running = asyncio.create_task(bash({"command": "sleep 60", "timeout": 30}, context))
    await asyncio.sleep(0)
    context.cancel_event.set()
    result = await asyncio.wait_for(running, timeout=1)

    assert result.is_error is True
    assert "cancelled" in result.content.lower()
    assert killed_groups == [(4321, 9)]
