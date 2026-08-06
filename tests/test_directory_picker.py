import pytest
from fastapi.testclient import TestClient


def test_macos_picker_selects_one_folder_without_file_enumeration(monkeypatch):
    from modus.desktop import directory_picker

    monkeypatch.setattr(directory_picker.sys, "platform", "darwin")
    monkeypatch.setattr(directory_picker.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None)

    command = directory_picker._picker_command()

    assert command[0] == "/usr/bin/osascript"
    assert "choose folder" in command[-1]
    assert "POSIX path" in command[-1]
    assert "不会自动上传" in command[-1]


@pytest.mark.asyncio
async def test_picker_returns_only_selected_path(monkeypatch):
    from modus.desktop import directory_picker

    class Process:
        returncode = 0

        async def communicate(self):
            return b"/Users/example/Large Folder/\n", b""

    async def create_process(*command, **kwargs):
        assert command[0] == "picker"
        assert kwargs["stdout"] is directory_picker.asyncio.subprocess.PIPE
        return Process()

    monkeypatch.setattr(directory_picker, "_picker_command", lambda: ("picker",))
    monkeypatch.setattr(directory_picker.asyncio, "create_subprocess_exec", create_process)

    assert await directory_picker.pick_directory() == "/Users/example/Large Folder/"


@pytest.mark.asyncio
async def test_picker_cancel_is_not_an_error(monkeypatch):
    from modus.desktop import directory_picker

    class Process:
        returncode = 1

        async def communicate(self):
            return b"", b"User canceled. (-128)"

    async def create_process(*_command, **_kwargs):
        return Process()

    monkeypatch.setattr(directory_picker, "_picker_command", lambda: ("picker",))
    monkeypatch.setattr(directory_picker.asyncio, "create_subprocess_exec", create_process)

    assert await directory_picker.pick_directory() is None


def test_workspace_pick_websocket_binds_selected_path(monkeypatch, tmp_path):
    from modus.desktop import server

    async def select_directory():
        return str(tmp_path)

    monkeypatch.setattr(server, "pick_directory", select_directory)
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session_ready"
            socket.send_json({"type": "workspace_pick", "request_id": "pick-1"})
            for _ in range(20):
                packet = socket.receive_json()
                if packet["type"] == "workspace_opened":
                    break
            else:
                raise AssertionError("workspace_opened was not returned")

    assert packet["operation"] == "workspace_pick"
    assert packet["request_id"] == "pick-1"
    assert packet["workspace"]["root"] == str(tmp_path.resolve())
