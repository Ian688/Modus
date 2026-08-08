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
            scope="per-resource" if request.get("resource_key") else "per-tool",
            resource_key=str(request.get("resource_key") or ""),
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
    _db_startup(db)
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


@app.command("goal")
def goal_cmd(
    action: Annotated[str, typer.Argument(help="set, get, status, pause, resume, continue, clear")],
    objective: Annotated[str | None, typer.Option("--objective", "-o", help="Goal objective (set)")] = None,
    cwd: Annotated[Path | None, typer.Option("--cwd")] = None,
) -> None:
    """Manage the Wave-4 cross-turn goal: set / inspect / pause / resume / clear.

    A goal lets the agent keep attacking an objective across turns and runs
    until it completes, hits its budget, or is blocked — instead of stopping
    when one run's budget is spent.  ``continue`` re-arms a soft-stopped goal.
    """
    from modus.agent.goal import GoalStore

    store = GoalStore()
    key = None  # CLI shares the "default" session goal for now
    # Rehydrate the persisted goal from disk so a fresh CLI process sees a goal
    # set by an earlier process (cross-run continuation).
    try:
        store.load(key)
    except Exception:
        pass
    action = action.lower()
    if action == "set":
        if not objective:
            typer.echo("goal set requires --objective", err=True)
            raise typer.Exit(1)
        state = store.set(key, objective)
        typer.echo(f"Goal set: {objective} (status={state.status})")
    elif action in {"get", "status"}:
        state = store.active(key) or store.get(key)
        if state is None:
            typer.echo("（无活动目标）")
            return
        typer.echo(
            f"objective: {state.objective}\n"
            f"status: {state.status}\n"
            f"tokens: {state.tokens_used} · turns: {state.turns_executed}\n"
            f"blocked: {state.blocked_count}x {state.blocked_reason or ''}\n"
            f"created: {state.created_at}"
        )
    elif action == "pause":
        state = store.pause(key)
        typer.echo(f"Goal paused (status={state.status})")
    elif action == "resume":
        state = store.resume(key)
        typer.echo(f"Goal resumed (status={state.status})")
    elif action == "continue":
        # A soft-stopped goal (budget_limited / max_turns) resets to active so
        # the next run re-injects steering.
        state = store.get(key)
        if state is None:
            typer.echo("（无目标可继续）")
            return
        store.resume(key)
        typer.echo(f"Goal continued (status={state.status})")
    elif action == "clear":
        store.clear(key)
        typer.echo("Goal cleared.")
    else:
        typer.echo(
            f"unknown action: {action} (set/get/status/pause/resume/continue/clear)",
            err=True,
        )
        raise typer.Exit(2)


@app.command("evaluate")
def evaluate_cmd(
    run: Annotated[str | None, typer.Option("--run", help="Re-score an already-completed run by id")] = None,
    suite: Annotated[Path | None, typer.Option("--suite", help="Batch-score a directory of scenario JSON files")] = None,
    scorer: Annotated[str, typer.Option("--scorer", help="Scorer name from the registry")] = "static_json",
    cwd: Annotated[Path | None, typer.Option("--cwd")] = None,
) -> None:
    """离线重评分：join 场景×已完成的 run 轨迹，输出 EvalReport。

    重评分不重跑 agent：轨迹来自 run_events 已落盘的 event 流（或
    ``~/.modus/trajectories/{run_id}.json``）。``--run`` 对一个已有 run 做结构
    评分（terminal state/stop_reason/token），需要内容级判定（expected 对照）
    时用 ``--suite`` 提供场景文件；``--suite`` 批跑一个目录下的 JSON 场景文件
    （每个文件含 ``expected`` 与可选 ``run_id``/``match``），单个场景失败不中断
    其余场景。
    """
    from modus.desktop import db as db_module

    data_dir = _data_dir()
    db_module.DB_DIR = data_dir
    db_module.DB_PATH = data_dir / "desktop.db"
    _db_startup(db_module)

    if run:
        _evaluate_run(db_module, run, scorer=scorer)
        return
    suite_path = Path(suite) if suite is not None else None
    if suite_path is not None:
        if not suite_path.is_dir():
            typer.echo(f"suite 目录不存在：{suite_path}", err=True)
            raise typer.Exit(2)
        _evaluate_suite(db_module, suite_path.resolve(), scorer=scorer)
        return
    typer.echo("evaluate 需要 --run <id> 或 --suite <dir>", err=True)
    raise typer.Exit(2)


def _evaluate_run(db_module: Any, run_id: str, *, scorer: str) -> None:
    """Score one completed run and print a report.

    A single run has no machine-scorable ``expected`` reference unless its
    scenario is provided, so ``--run`` reports the run's own structural outcome
    (terminal state, stop reason, tool usage, budget) and — when the run's
    ``final_result`` carries structured JSON that the static_json comparator
    can parse against the run's ``objective`` — a content score.  Content
    scoring against a real reference answer is the ``--suite`` path.
    """
    from modus.evaluation import Evaluator, EvalReport

    run = db_module.get_run(run_id)
    if run is None:
        typer.echo(f"未找到 run：{run_id}", err=True)
        raise typer.Exit(1)

    evaluator = Evaluator()
    if scorer != "static_json":
        # A registered custom scorer still sees the full trajectory.
        trajectory = db_module.load_trajectory(run_id) or {
            "run_id": run_id, "state": run.get("state"),
            "objective": run.get("objective"),
            "final_result": run.get("final_result"),
            "budget": run.get("budget") or {},
            "events": db_module.get_run_events(run_id),
        }
        score = evaluator.score(
            {"scenario_id": "run", "run_id": run_id,
             "expected": _infer_expected(str(run.get("objective") or ""))},
            trajectory, scorer=scorer,
        )
    else:
        score = _structural_score(run)

    state = str(run.get("state") or "")
    report = EvalReport({"schema": "modus.eval-report.v1", "scenarios": [{
        "scenario_id": "run", "runs": 1,
        "passed": int(score.get("pass")), "failed": int(not score.get("pass")),
        "partial": int(bool(score.get("partial"))),
        "pass": bool(score.get("pass")),
        "score": score.get("f1") or 0.0,
        "precision": score.get("precision") or 0.0,
        "recall": score.get("recall") or 0.0,
        "tokens": {"total_tokens": int((run.get("budget") or {}).get("total_tokens") or 0)},
        "cost_usd": score.get("cost_usd") or 0.0,
        "latency_ms": {"p50": score.get("latency_ms") or 0.0, "p95": 0.0},
        "reasons": [str(score.get("reason") or "")],
        "run_ids": [run_id],
    }], "summary": {
        "scenarios": 1, "runs": 1,
        "passed": int(score.get("pass")), "failed": int(not score.get("pass")),
        "pass": bool(score.get("pass")),
        "precision": score.get("precision") or 0.0,
        "recall": score.get("recall") or 0.0,
        "f1": score.get("f1") or 0.0,
        "total_tokens": int((run.get("budget") or {}).get("total_tokens") or 0),
        "cost_usd": score.get("cost_usd") or 0.0,
    }})
    _print_report(report)
    typer.echo(f"\n[dim]run {run_id} · state={state} · "
               f"objective={str(run.get('objective') or '')[:80]} · "
               f"stop_reason={run.get('stop_reason') or '-'}[/dim]")


def _structural_score(run: dict) -> dict:
    """Structural verdict for a single run when no reference answer exists.

    A completed run passes structurally (it reached a terminal success with a
    durable outcome); any other terminal state fails with its stop reason.
    ``partial`` reports that the run reached a terminal state even when it did
    not complete, so a failed/interrupted run still shows as "partially"
    resolved rather than silently skipped.
    """
    state = str(run.get("state") or "")
    passed = state == "completed"
    reason = (
        "run completed; a structured reference answer is only scored via --suite"
        if passed
        else f"run did not complete (state={state}, stop_reason={run.get('stop_reason') or '-'})"
    )
    return {
        "pass": passed,
        "partial": state in {"completed", "failed", "interrupted"},
        "strict": passed,
        "precision": 0.0, "recall": 0.0, "f1": 0.0,
        "matched": 0, "expected_count": 0, "predicted_count": 0,
        "missing_keys": [], "extra_keys": [], "diffs": [],
        "reason": reason,
        "answer": str(run.get("final_result") or "")[:2000],
        "parsed": None,
        "run_id": str(run.get("run_id") or ""),
        "scenario_id": "run",
    }


def _evaluate_suite(db_module: Any, suite_dir: Path, *, scorer: str) -> None:
    """Batch-score every JSON scenario file in ``suite_dir`` (failure-isolated)."""
    from modus.evaluation import Evaluator, EvalReport
    from modus.evaluation.evaluator import EvaluationError

    evaluator = Evaluator()
    joins: list[tuple[dict, dict | str]] = []
    skipped: list[str] = []
    for path in sorted(suite_dir.glob("*.json")):
        try:
            scenario = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append(f"{path.name}（解析失败：{exc}）")
            continue
        if not isinstance(scenario, dict) or "expected" not in scenario:
            skipped.append(f"{path.name}（缺少 expected 字段）")
            continue
        scenario.setdefault("scenario_id", path.stem)
        target = scenario.get("run_id")
        if target:
            joins.append((scenario, str(target)))
        else:
            # No explicit run: score against every persisted trajectory of the
            # suite, so a scenario file can evaluate a whole batch of runs.
            for trajectory in db_module.list_trajectories():
                joins.append((scenario, str(trajectory.get("run_id") or "")))

    scores: list[dict] = []
    for scenario, target in joins:
        try:
            scores.append(evaluator.score(scenario, target, scorer=scorer))
        except EvaluationError as exc:
            # Failure isolation: one bad join must not abort the suite.
            skipped.append(f"{scenario.get('scenario_id')}@{target}（{exc}）")

    if not scores:
        typer.echo("suite 没有可评分的场景", err=True)
        for item in skipped:
            typer.echo(f"  - 跳过：{item}", err=True)
        raise typer.Exit(1)
    report = EvalReport(_build_suite_report(scores))
    _print_report(report)
    if skipped:
        typer.echo(f"\n[dim]跳过 {len(skipped)} 个场景：[/dim]")
        for item in skipped:
            typer.echo(f"  - {item}")


def _build_suite_report(scores: list[dict]) -> dict:
    from modus.evaluation import build_report

    return build_report(scores)


def _infer_expected(objective: str) -> dict:
    """Derive a minimal expected dict from a run objective.

    The offline evaluator needs an ``expected`` reference.  A bare objective is
    not machine-scorable, so we return an empty dict and let the static_json
    scorer report "no structured answer" rather than guessing.  Scenario files
    (``--suite``) always carry a real ``expected``.
    """
    return {}


def _print_report(report: Any) -> None:
    """Render an EvalReport as a compact rich table."""
    from rich.table import Table

    summary = report.summary
    console.print(
        f"[bold]EvalReport[/bold] · "
        f"scenarios={summary.get('scenarios')} runs={summary.get('runs')} "
        f"passed={summary.get('passed')} failed={summary.get('failed')} "
        f"tokens={summary.get('total_tokens')} cost=${summary.get('cost_usd')}"
    )
    table = Table(title="场景 × 轨迹")
    table.add_column("scenario")
    table.add_column("pass")
    table.add_column("f1")
    table.add_column("precision")
    table.add_column("recall")
    table.add_column("tokens")
    table.add_column("reason")
    for scenario in report.scenarios:
        table.add_row(
            str(scenario.get("scenario_id") or ""),
            "✅" if scenario.get("pass") else "❌",
            f"{scenario.get('score') or 0:.3f}",
            f"{scenario.get('precision') if 'precision' in scenario else ''}",
            f"{scenario.get('recall') if 'recall' in scenario else ''}",
            str((scenario.get("tokens") or {}).get("total_tokens") or ""),
            str((scenario.get("reasons") or [""])[0])[:60],
        )
    console.print(table)


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


def _db_startup(db_module: Any) -> None:
    """Converged startup: writer lease -> schema migration -> only then write.

    The shared ``desktop.db`` has exactly one writer at a time.  A second
    Modus instance (Desktop/CLI/MCP) holding the lease is reported with an
    explicit error instead of silently racing writes.  Read-only queries stay
    allowed without the lease (WAL already permits many readers), so a
    headless reader never needs to hold it.
    """
    from modus.desktop.db import acquire_writer_lease, WriterLeaseError

    try:
        if not acquire_writer_lease():
            raise WriterLeaseError(
                "另一个 Modus 实例正在运行并占用数据库（%s）。"
                "请先关闭它，或使用只读查询。" % db_module.DB_PATH
            )
        db_module.init_db()
    except WriterLeaseError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


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
    _db_startup(db)
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
