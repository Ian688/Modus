import os
import subprocess
import sys
from pathlib import Path
import tomllib

import pytest


def test_desktop_serve_forwards_only_an_explicit_workspace(tmp_path, monkeypatch):
    from modus.entrypoints import cli

    observed = {}
    monkeypatch.setattr(
        "modus.desktop.start_server",
        lambda **kwargs: observed.update(kwargs),
    )

    cli.desktop_serve(port=3210, host="127.0.0.1", cwd=tmp_path)

    assert observed == {
        "host": "127.0.0.1", "port": 3210,
        "workspace_root": tmp_path.resolve(),
    }


def test_desktop_serve_does_not_forward_the_process_directory(monkeypatch):
    from modus.entrypoints import cli

    observed = {}
    monkeypatch.setattr(
        "modus.desktop.start_server",
        lambda **kwargs: observed.update(kwargs),
    )

    cli.desktop_serve(port=3211, host="127.0.0.1", cwd=None)

    assert observed["workspace_root"] is None


def test_python_module_entrypoint_loads_cli_help_without_touching_desktop_db(tmp_path):
    root = Path(__file__).parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HOME"] = str(tmp_path)

    completed = subprocess.run(
        [sys.executable, "-B", "-m", "modus", "--help"],
        cwd=root, env=env, capture_output=True, text=True, timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Modus" in completed.stdout
    assert not (tmp_path / ".modus" / "desktop.db").exists()


def test_modus_is_the_only_installed_command():
    scripts = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["scripts"]

    assert scripts == {"modus": "modus.entrypoints.cli:app"}


def test_cli_repl_replaces_the_not_implemented_stub():
    cli = (Path(__file__).parents[1] / "src/modus/entrypoints/cli.py").read_text()

    # No-argument invocation enters the REPL instead of printing a stub.
    assert "Interactive REPL not yet implemented" not in cli
    assert "asyncio.run(_run_repl(str(root), config))" in cli
    assert "async def _run_repl(cwd: str, config)" in cli
    assert "exit/quit" in cli
    # REPL maintains per-conversation history for the engine.
    assert "history: list[Message] = []" in cli
    assert "history.append(Message(role=\"user\", content=text))" in cli


def test_cli_repl_requires_api_key():
    import asyncio

    import typer
    from modus.config import load_config
    from modus.entrypoints.cli import _run_repl

    config = load_config()
    if config.llm.api_key:
        # Guard: this test asserts the fail-fast path only when no key is set.
        pytest.skip("API key configured; fail-fast path not exercised")
    with pytest.raises(typer.Exit) as exc:
        asyncio.run(_run_repl(str(Path.cwd()), config))
    assert exc.value.exit_code == 1
