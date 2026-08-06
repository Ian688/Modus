from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from modus import __version__
from modus.agent import QueryEngine
from modus.bootstrap import build_tool_registry
from modus.config import get_config_paths, load_config
from modus.llm import create_llm_client
from modus.types import Message

app = typer.Typer(
    name="modus", help="Modus - multi-mode AI Agent",
    invoke_without_command=True, no_args_is_help=False,
)
console = Console()

def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"modus {__version__}")
        raise typer.Exit()

@app.command("serve")
def desktop_serve(
    port: Annotated[int, typer.Option("--port", "-p", help="WebSocket server port")] = 3000,
    host: Annotated[str, typer.Option("--host", help="Bind address")] = "127.0.0.1",
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
) -> None:
    """启动 Modus Desktop WebSocket 服务"""
    import os
    # PORT 环境变量优先（preview 工具用它分配空闲端口）；显式 --port 覆盖它。
    port = int(os.environ.get("PORT") or port)
    from modus.desktop import start_server
    workspace_root = None
    if cwd is not None:
        workspace_root = cwd.expanduser().resolve()
        if not workspace_root.is_dir():
            typer.echo(f"工作目录不存在或不是文件夹：{workspace_root}", err=True)
            raise typer.Exit(2)
    start_server(host=host, port=port, workspace_root=workspace_root)
    
@app.command("doctor")
def doctor(
    cwd: Annotated[Path | None, typer.Option("--cwd")] = None,
) -> None:
    root = (cwd or Path.cwd()).resolve()
    config = load_config(project_root=root)
    checks = {
        "python": sys.version.split()[0],
        "api_key": "configured" if config.llm.api_key else "missing",
        "provider": config.llm.provider,
        "model": config.llm.model,
        "cwd": str(root),
        "auto_memorize": config.memory.auto_memorize,
        "sandbox_enabled": config.sandbox.enabled,
        "park_on_disconnect": config.features.park_on_disconnect,
        "max_recursion_depth": config.features.convergence.max_recursion_depth,
        "writable_workers": config.features.writable_workers,
    }
    console.print_json(json.dumps(checks, ensure_ascii=False))


@app.command("memory")
def memory_cmd(
    action: Annotated[str, typer.Argument(help="set, get, list or clear")],
    content: Annotated[str | None, typer.Option("--content", help="Memory content (set)")] = None,
    category: Annotated[str, typer.Option("--category", help="fact, preference, constraint or general")] = "fact",
    cwd: Annotated[Path | None, typer.Option("--cwd")] = None,
) -> None:
    """Manage persistent memories (session or project scope)."""
    from modus.desktop import db
    from modus.desktop.memory import add_memory, clear_memories, get_memories, search_memories

    root = (cwd or Path.cwd()).resolve()
    data_dir = _data_dir()
    db.DB_DIR = data_dir
    db.DB_PATH = data_dir / "desktop.db"
    db.init_db()
    sid = _active_session(db)
    action = action.lower()
    if action == "set":
        if not content:
            typer.echo("memory set requires --content", err=True)
            raise typer.Exit(1)
        add_memory(sid, content, category)
        typer.echo(f"Saved ({category}): {content[:120]}")
    elif action == "list":
        for mem in get_memories(sid):
            typer.echo(f"[{mem['category']}] {mem['content'][:160]}")
    elif action == "clear":
        clear_memories(sid)
        typer.echo("Cleared session memories.")
    elif action == "get":
        if not content:
            typer.echo("memory get requires --content (query)", err=True)
            raise typer.Exit(1)
        for mem in search_memories(sid, content):
            typer.echo(f"[{mem['category']}] {mem['content'][:160]}")
    else:
        typer.echo(f"unknown action: {action} (set/get/list/clear)", err=True)
        raise typer.Exit(2)


def _data_dir() -> Path:
    env = __import__("os").environ.get("MODUS_DATA_DIR")
    if env:
        return Path(env).resolve()
    from modus.paths import data_path
    return Path(data_path(".")).resolve()


def _active_session(db_module: Any) -> str:
    """Return the most recently touched session, creating one if needed."""
    sessions = db_module.list_sessions(limit=5)
    if sessions:
        return str(sessions[0]["id"])
    return str(db_module.create_session("cli")["id"])


@app.callback()
def main(
    ctx: typer.Context,
    prompt: Annotated[str | None, typer.Option("-p", "--prompt", help="Single prompt mode")] = None,
    model: Annotated[str | None, typer.Option("-m", "--model", help="Override model")] = None,
    provider: Annotated[str | None, typer.Option("--provider", help="Override provider")] = None,
    plain: Annotated[bool, typer.Option("--plain", help="Plain text mode")] = False,
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
    version: Annotated[bool, typer.Option("--version", callback=_version_callback, is_eager=True)] = False,
) -> None:
    _ = version
    if ctx.invoked_subcommand is not None:
        return
    root = (cwd or Path.cwd()).resolve()
    overrides: dict = {}
    if provider or model or plain:
        overrides = {"llm": {"provider": provider, "model": model}, "render_mode": "plain" if plain else None}
    config = load_config(project_root=root, overrides=overrides)
    if prompt is not None:
        asyncio.run(_run_prompt(prompt, str(root), config))
    else:
        asyncio.run(_run_repl(str(root), config))

async def _run_repl(cwd: str, config) -> None:
    if not config.llm.api_key:
        typer.echo("Error: API key not configured.", err=True)
        raise typer.Exit(1)
    registry = await build_tool_registry(config=config, cwd=cwd)
    engine = QueryEngine(
        llm_client=create_llm_client(config.llm),
        tool_registry=registry,
        config=config,
        cwd=cwd,
    )
    history: list[Message] = []
    console.print(f"[dim]Modus REPL · {cwd} · 输入 exit/quit 退出[/dim]")
    while True:
        try:
            prompt = typer.prompt("you", prompt_suffix="› ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        text = prompt.strip()
        if not text:
            continue
        if text.lower() in {"exit", "quit", "exit()", "quit()"}:
            break
        try:
            result = await engine.ask_complete_async(text, history=history)
        except Exception as exc:
            console.print(f"[red]Error: {exc}[/red]")
            continue
        console.print(result.text)
        if result.turns or result.total_tokens:
            console.print(f"[dim]· {result.turns} 轮 · {result.total_tokens} tokens[/dim]")
        history.append(Message(role="user", content=text))
        history.append(Message(role="assistant", content=result.text))


async def _run_prompt(prompt: str, cwd: str, config) -> None:
    if not config.llm.api_key:
        typer.echo("Error: API key not configured.", err=True)
        raise typer.Exit(1)
    registry = await build_tool_registry(config=config, cwd=cwd)
    engine = QueryEngine(
        llm_client=create_llm_client(config.llm),
        tool_registry=registry,
        config=config,
        cwd=cwd,
    )
    result = await engine.ask_complete_async(prompt)
    typer.echo(result.text)
