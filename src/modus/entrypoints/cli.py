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


def _format_approval_input(data: dict) -> str:
    """Render a tool-call payload readably without dumping raw secrets.

    One line per top-level key; bash/run_tests show the command prominently,
    file tools show the path and a bounded content excerpt.  Values are never
    expanded for keys that look like credentials (token/api_key/secret/...).
    """
    secret_keys = {"token", "api_key", "apikey", "secret", "password", "key"}
    lines: list[str] = []
    for key, value in (data or {}).items():
        if any(s in str(key).lower() for s in secret_keys):
            lines.append(f"  {key}: [red]•••• 已隐藏[/red]")
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if key in {"command", "description"} or len(text) <= 200:
            lines.append(f"  {key}: {text}")
        else:
            lines.append(f"  {key}: {text[:200]}…（共 {len(text)} 字符）")
    return "\n".join(lines) if lines else "  （无参数）"


async def _cli_approval_callback(request: dict) -> str:
    """Terminal HITL approval: rich card + y/n/s/m/r decision.

    The executor has already decided this tool needs approval (ApprovalPolicy)
    and validated it (CommandGuard / PathGuard / input_hash).  This callback is
    the human confirmation only; returning anything but an approve fails closed.
    Decisions:
      y  approve as-is
      n  deny (structured result, with an optional reason the agent can see)
      s  skip (tool does not run; the run continues)
      m  modify the tool arguments, then approve the edited payload
      r  approve AND remember this resource for the session (A1 per-resource
         grant: the same resource is not re-asked within this session)
    """
    from rich import box
    from rich.panel import Panel
    from rich.prompt import Prompt
    from modus.tools.base import ApprovalResponse

    danger = str(request.get("danger_level") or "medium")
    style = {"high": "red", "medium": "yellow", "safe": "green"}.get(danger, "yellow")
    tool_name = str(request.get("tool_name") or "工具")
    lines = [
        f"[bold]{tool_name}[/bold] 请求执行",
        f"危险级别: [{style}]{danger}[/{style}]",
        f"数据披露: {request.get('data_disclosure') or 'none'}",
        f"影响: {request.get('impact_class') or 'undetermined'}",
    ]
    if request.get("resource_key"):
        lines.append(f"资源: [cyan]{request['resource_key']}[/cyan]")
    if request.get("description"):
        lines.append(f"描述: {request['description']}")
    lines.append(f"参数:\n{_format_approval_input(request.get('input') or {})}")
    console.print(
        Panel(
            "\n".join(lines),
            title="⚠️ 审批请求", box=box.ROUNDED, border_style=style,
        )
    )
    answer = Prompt.ask(
        "批准执行? [y]批准 [n]拒绝 [s]跳过 [m]改参 [r]批准并记住本资源",
        choices=["y", "n", "s", "m", "r"], default="n", show_choices=False,
    )
    decision = answer.strip().lower()
    if decision == "y":
        _record_cli_audit("approved", request, "y")
        return "approve"
    if decision == "r":
        # A1 per-resource session grant: approve this exact resource and ask
        # the executor to remember it for the session.
        _record_cli_audit("approved", request, "r")
        return ApprovalResponse.approve(remember=True)
    if decision == "s":
        _record_cli_audit("skipped", request, "s")
        return "skip"
    if decision == "m":
        modified = _prompt_modified_args(request.get("input") or {})
        if modified is not None:
            remember = _prompt_remember_modified()
            _record_cli_audit("modified", request, f"m{'+r' if remember else ''}")
            return ApprovalResponse.modify(modified, remember=remember)
        _record_cli_audit("denied", request, "m-invalid")
        return ApprovalResponse.deny("modified input was invalid")
    reason = _prompt_deny_reason()
    _record_cli_audit("denied", request, "n")
    return ApprovalResponse.deny(reason=reason)


def _prompt_remember_modified() -> bool:
    """Ask whether to remember the *modified* resource for the session (A1).

    The executor records the grant under the resource key of the modified
    payload the human approved, so a later identical (edited) resource is not
    re-asked within the session.
    """
    from rich.prompt import Prompt

    try:
        raw = Prompt.ask(
            "记住改后的这个资源本会话? [y]是 [n]否",
            choices=["y", "n"], default="n", show_choices=False,
        )
    except (EOFError, KeyboardInterrupt):
        return False
    return str(raw).strip().lower() in {"y", "r"}


def _prompt_deny_reason() -> str:
    """Collect an optional denial reason for A2 deny re-injection.

    Returns an empty string when the user skips the prompt; the deny result is
    then still a structured non-error tool result, just without a reason.
    """
    from rich.prompt import Prompt

    try:
        raw = Prompt.ask("拒绝原因（可选，直接回车跳过）", default="")
    except (EOFError, KeyboardInterrupt):
        return ""
    return str(raw or "").strip()


def _record_cli_audit(outcome: str, request: dict, detail: str) -> None:
    """Best-effort audit of a CLI approval decision into the policy audit log.

    Never raises: audit is a diagnostic trail, not a gate.
    """
    try:
        from modus.policy.audit_log import AuditLog

        config = load_config()
        log = AuditLog(config.policy.audit_log_path)
        log.record(
            tool_name=str(request.get("tool_name") or "?"),
            input_data=dict(request.get("input") or {}),
            outcome=f"{outcome}:{detail}",
            approver="cli-human",
            cwd=str(request.get("cwd") or ""),
            phase=str(request.get("impact_class") or "execution"),
        )
    except Exception:
        return


def _prompt_modified_args(original: dict) -> dict | None:
    """Let the user edit a tool-call payload as JSON before approving.

    Returns the replacement payload, or None when the user abandons the edit
    (so the caller fails closed to deny).
    """
    from rich.prompt import Prompt

    console.print("[dim]当前参数：[/dim]")
    console.print(json.dumps(original, ensure_ascii=False, indent=2))
    console.print("[dim]输入修改后的参数（JSON），或输入 . 放弃：[/dim]")
    try:
        raw = Prompt.ask("")
    except (EOFError, KeyboardInterrupt):
        return None
    if raw is None or raw.strip() in {"", "."}:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        console.print(f"[red]JSON 解析失败：{exc}，已拒绝。[/red]")
        return None
    if not isinstance(parsed, dict):
        console.print("[red]参数必须是 JSON 对象，已拒绝。[/red]")
        return None
    return parsed

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


@app.command("audit")
def audit_cmd(
    tail: Annotated[int, typer.Option("--tail", help="Number of recent entries")] = 20,
    tool: Annotated[str | None, typer.Option("--tool", help="Filter by tool name")] = None,
    cwd: Annotated[Path | None, typer.Option("--cwd")] = None,
) -> None:
    """Show recent redacted audit entries (approvals, tool executions)."""
    from modus.policy.audit_log import AuditLog

    root = (cwd or Path.cwd()).resolve()
    config = load_config(project_root=root)
    log = AuditLog(config.policy.audit_log_path)
    events = log.tail(max(1, min(int(tail), 500)))
    if tool:
        events = [event for event in events if str(event.get("tool_name") or "") == tool]
    if not events:
        typer.echo("（无审计记录）")
        return
    for event in events:
        timestamp = str(event.get("timestamp") or "")[:19]
        name = str(event.get("tool_name") or "?")
        outcome = str(event.get("outcome") or "?")
        approver = str(event.get("approver") or "?")
        input_preview = str(event.get("input") or "{}")
        if len(input_preview) > 120:
            input_preview = input_preview[:117] + "..."
        typer.echo(f"{timestamp}  {name}  {outcome}  (approver={approver})  {input_preview}")


@app.command("mcp")
def mcp_cmd(
    serve: Annotated[bool, typer.Option("--serve", help="Run the MCP server")] = True,
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
    allow_dangerous: Annotated[
        bool, typer.Option("--allow-dangerous",
                           help="Expose write/exec tools (bash, spawn, git writes) — default denies them")] = False,
    capabilities: Annotated[
        str | None, typer.Option("--capabilities",
                                 help="Comma-separated capability whitelist (filesystem,network,...)")] = None,
    transport: Annotated[
        str, typer.Option("--transport", help="stdio (default) or http")] = "stdio",
    host: Annotated[str, typer.Option("--host", help="HTTP bind host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="HTTP port")] = 4000,
) -> None:
    """Expose Modus's built-in tools to other AI agents as an MCP server.

    Defaults to a read-only lens (safe + read-only tools only).  Add
    --allow-dangerous to expose write/exec tools; even then a headless call to
    a write tool is denied (approval never auto-grants).
    """
    root = (cwd or Path.cwd()).resolve()
    from modus.mcp_server import mcp_serve, mcp_serve_http

    cap_list = [c.strip() for c in (capabilities or "").split(",") if c.strip()] or None
    if transport == "http":
        mcp_serve_http(
            cwd=str(root), allow_dangerous=allow_dangerous,
            capabilities=cap_list, host=host, port=port,
        )
    else:
        mcp_serve(
            cwd=str(root), allow_dangerous=allow_dangerous, capabilities=cap_list,
        )


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


def _repl_session() -> tuple[Any, str]:
    """Initialize the CLI's own persistent database and return (db, session_id).

    Uses a dedicated ``~/.modus`` data dir shared with the Desktop, but the CLI
    session is tagged ``cli`` so it never collides with Desktop sessions.  The
    DB is only touched when memory/recall is actually wanted, so a plain REPL
    run without persisted state costs nothing.
    """
    from modus.desktop import db

    data_dir = _data_dir()
    db.DB_DIR = data_dir
    db.DB_PATH = data_dir / "desktop.db"
    db.init_db()
    sid = _active_session(db)
    return db, sid


def _memory_message(db_module: Any, session_id: str, query: str) -> Message | None:
    """Build a bounded memory/recall system message for one CLI turn."""
    try:
        from modus.desktop.memory import get_memory_context

        context = get_memory_context(session_id, query=query)
        if context:
            return Message(role="system", content=context)
    except Exception:
        return None
    return None


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
    # A normal close (Ctrl-C / exit) reaps background processes this CLI spawned.
    from modus.process_cleanup import install_process_cleanup

    install_process_cleanup()
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
    # Persistent CLI session: injects prior memories/recall and persists turns.
    db_module, session_id = _repl_session()
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
        # Query-scoped memory + episodic recall as a bounded system message.
        memory = _memory_message(db_module, session_id, text)
        turn_history = list(history)
        if memory is not None:
            turn_history = [*turn_history, memory]
        try:
            result = await engine.ask_complete_async(
                text, history=turn_history, approval_callback=_cli_approval_callback,
            )
        except Exception as exc:
            console.print(f"[red]Error: {exc}[/red]")
            continue
        console.print(result.text)
        if result.turns or result.total_tokens:
            console.print(f"[dim]· {result.turns} 轮 · {result.total_tokens} tokens[/dim]")
        history.append(Message(role="user", content=text))
        history.append(Message(role="assistant", content=result.text))
        try:
            db_module.add_message(session_id, "user", text, token_count=0)
            db_module.add_message(session_id, "assistant", result.text, token_count=0)
        except Exception:
            pass


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
    result = await engine.ask_complete_async(prompt, approval_callback=_cli_approval_callback)
    typer.echo(result.text)
