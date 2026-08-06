"""Open the operating system's folder chooser without enumerating its files."""

from __future__ import annotations

import asyncio
import shutil
import sys


class DirectoryPickerUnavailable(RuntimeError):
    """Raised when the host has no supported native folder chooser."""


class DirectoryPickerError(RuntimeError):
    """Raised when a native folder chooser fails unexpectedly."""


def _picker_command() -> tuple[str, ...]:
    if sys.platform == "darwin":
        executable = shutil.which("osascript")
        if executable:
            return (
                executable,
                "-e",
                'POSIX path of (choose folder with prompt "选择 Agent 工作区（绑定目录，不会自动上传）")',
            )
    elif sys.platform == "win32":
        executable = shutil.which("powershell") or shutil.which("pwsh")
        if executable:
            script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$dialog=New-Object System.Windows.Forms.FolderBrowserDialog;"
                "$dialog.Description='选择 Agent 工作区（绑定目录，不会自动上传）';"
                "if($dialog.ShowDialog() -eq 'OK'){[Console]::Out.Write($dialog.SelectedPath)}"
            )
            return executable, "-NoProfile", "-STA", "-Command", script
    else:
        executable = shutil.which("zenity")
        if executable:
            return executable, "--file-selection", "--directory", "--title=选择 Agent 工作区（绑定目录，不会自动上传）"
        executable = shutil.which("kdialog")
        if executable:
            return executable, "--getexistingdirectory", ".", "--title", "选择 Agent 工作区（绑定目录，不会自动上传）"
    raise DirectoryPickerUnavailable("当前系统没有可用的文件夹选择器，请粘贴文件夹绝对路径。")


async def pick_directory() -> str | None:
    """Return one selected local directory, or ``None`` when the user cancels.

    The picker returns only the directory path. It does not inspect, enumerate,
    copy, or upload any file inside the selected directory.
    """

    command = _picker_command()
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    selected = stdout.decode("utf-8", errors="replace").strip()
    if process.returncode == 0:
        return selected or None
    # Native pickers use exit code 1 when the user closes or cancels them.
    if process.returncode == 1:
        return None
    detail = stderr.decode("utf-8", errors="replace").strip()
    raise DirectoryPickerError(detail or "无法打开文件夹选择器。")
