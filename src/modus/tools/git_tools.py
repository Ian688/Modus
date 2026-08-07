"""Git worktree tools for Peri worker isolation.

Each tool wraps a git operation as a bash command, returning structured
output (stdout, stderr, exit_code) for the host to review.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from typing import Any

from modus.tools.base import Tool, ToolContext, ToolResult

_SIGKILL = getattr(signal, "SIGKILL", 9)


# ── Helpers ──


async def _git(cmd: list[str], cwd: str | None = None) -> tuple[str, str, int]:
    """Run a git command and return (stdout, stderr, exit_code).

    ``git`` may hang on a network op (fetch/pull/push).  The ``Tool`` wrapper
    bounds the whole handler with a timeout, but a bare ``communicate`` would
    leave the child git process orphaned on timeout.  Run in a dedicated process
    group and terminate the whole tree on timeout, mirroring ``bash``.
    """
    try:
        return await _run_git(
            cmd, cwd=cwd,
            timeout=float(os.environ.get("MODUS_GIT_TOOL_TIMEOUT", "45") or 45),
        )
    except FileNotFoundError:
        return "", "git not found in PATH", 127
    except Exception as exc:
        return "", str(exc), 1


async def _run_git(
    cmd: list[str], cwd: str | None = None, *, env: dict[str, str] | None = None,
    timeout: float = 45.0,
) -> tuple[str, str, int]:
    """Run one git command in a process group with a hard timeout and cleanup.

    Mirrors ``bash``: the communicate task is shielded from the wait timeout so
    the process tree can always be terminated and reaped instead of orphaned.
    """
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    kwargs: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=merged_env,
        **kwargs,
    )
    communicate_task = asyncio.create_task(proc.communicate())
    try:
        done, _pending = await asyncio.wait(
            {communicate_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )
        if communicate_task in done:
            stdout, stderr = communicate_task.result()
            return (
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                proc.returncode or 0,
            )
        await _terminate_git_tree(proc, communicate_task)
        return "", f"git command timed out after {timeout:.0f}s", 124
    except asyncio.CancelledError:
        # ``Tool.execute`` may cancel this handler on its own I/O deadline; the
        # process tree must still be terminated and reaped.
        await _terminate_git_tree(proc, communicate_task)
        raise


async def _terminate_git_tree(
    proc: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    """Terminate a git process group and reap its pipe reader."""
    if proc.returncode is None:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                os.killpg(os.getpgid(proc.pid), _SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        await asyncio.wait_for(asyncio.shield(communicate_task), timeout=1.0)
    except TimeoutError:
        if not communicate_task.done():
            communicate_task.cancel()
        await asyncio.gather(communicate_task, return_exceptions=True)
        try:
            await proc.wait()
        except ProcessLookupError:
            pass


def _fmt_result(stdout: str, stderr: str, exit_code: int) -> str:
    flags = "⚠" if exit_code else "✓"
    text = stdout.strip() if stdout else stderr.strip()
    return f"{flags} git {text[:600]}" if text else f"{flags} git (ok)"


# ── Tool implementations ──


async def git_init_check(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Check whether the workspace is a Git repo without mutating it."""
    cwd = context.cwd or os.getcwd()
    _, _, code = await _git(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd)
    if code == 0:
        return ToolResult("✓ workspace is already a git repository")
    return ToolResult(
        "✗ workspace is not a Git repository; automatic git init is disabled",
        is_error=True,
    )


async def git_ensure_branch(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Create a branch from main (or current HEAD) for isolation. Returns branch name."""
    return ToolResult("✗ branch creation is disabled until an approved worktree plan exists", is_error=True)


async def git_worktree_add(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Add a git worktree at a path for a branch. Returns the worktree path."""
    return ToolResult("✗ worktree creation is disabled until explicit user approval", is_error=True)


async def git_worktree_remove(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Remove a git worktree and optionally delete its branch."""
    return ToolResult("✗ automatic or forced worktree cleanup is disabled", is_error=True)


async def git_branch_diff(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Show diff of a branch vs main (or another base)."""
    branch = str(payload.get("branch", "")).strip()
    base = str(payload.get("base", "main")).strip()
    if not branch:
        return ToolResult("✗ git_branch_diff requires a 'branch' name", is_error=True)
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(["git", "diff", f"{base}...{branch}", "--stat"], cwd=cwd)
    if code != 0:
        return ToolResult(f"✗ diff failed: {stderr[:300]}", is_error=True)
    # Get the full diff (truncated)
    stdout_full, _, _ = await _git(["git", "diff", f"{base}...{branch}", "-U3"], cwd=cwd)
    stat = stdout.strip()
    diff = stdout_full.strip()[:4000]
    result = f"📊 {stat or '(no changes)'}"
    if diff:
        result += f"\n\n```diff\n{diff}\n```"
    return ToolResult(result)


async def git_merge_to_main(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Merge a branch into main (or another target)."""
    return ToolResult("✗ direct checkout/merge is disabled; an approved merge plan is required", is_error=True)


# ── Registration ──


GIT_WORKTREE_TOOLS: list[Tool] = [
    Tool(
        name="git_init_check",
        description="Ensure the workspace is a git repository, initialise if needed",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=git_init_check,
        is_read_only=True,
    ),
    Tool(
        name="git_branch_diff",
        description="Show diff of a branch vs main (for host review)",
        parameters={
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Branch to diff"},
                "base": {"type": "string", "description": "Base branch (default: main)"},
            },
            "required": ["branch"],
        },
        handler=git_branch_diff,
        is_read_only=True,
    ),
]

# ── Sub-agent git tools (for use within worktree) ──


# Only well-known public code hosts are clone targets.  A clone pulls remote
# code into the workspace, so it is an approval-gated, host-allowlisted action.
_ALLOWED_CLONE_HOSTS = ("github.com", "gitee.com")


def _parse_clone_target(url: str) -> str:
    """Normalize a repo URL and return it only if the host is allowlisted."""
    import re

    url = str(url or "").strip()
    if not url:
        raise ValueError("git_clone requires a 'url'")
    if not url.startswith(("https://", "git@")):
        raise ValueError("git_clone only accepts https:// or git@ URLs")
    host = ""
    if url.startswith("https://"):
        m = re.match(r"^https://([^/]+)/", url)
        if m:
            host = m.group(1).lower()
    elif url.startswith("git@"):
        m = re.match(r"^git@([^:]+):", url)
        if m:
            host = m.group(1).lower()
    if host not in _ALLOWED_CLONE_HOSTS:
        raise ValueError(f"git_clone host not allowed: {host or 'unknown'}")
    if not re.search(r"[:/][A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", url):
        raise ValueError("git_clone url must reference a repository path")
    return url


async def git_clone(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Clone a public GitHub/Gitee repository into the project area."""
    try:
        url = _parse_clone_target(str(payload.get("url") or ""))
    except ValueError as exc:
        return ToolResult(str(exc), is_error=True)
    cwd = context.cwd or os.getcwd()
    from pathlib import Path

    projects_root = Path(cwd).resolve() / ".." / ".modus-projects"
    target_path = str(payload.get("path") or "").strip()
    clone_into = target_path or str(projects_root)
    Path(clone_into).mkdir(parents=True, exist_ok=True)
    stdout, stderr, code = await _git(["git", "clone", "--quiet", url], cwd=clone_into)
    if code != 0:
        return ToolResult(f"✗ git clone failed: {stderr.strip()[:300]}", is_error=True)
    return ToolResult(f"✓ cloned {url} into {clone_into}")


# ── Sub-agent git tools (for use within worktree) ──


async def git_add(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Stage files in the worktree (git add)."""
    pathspec = str(payload.get("path", "."))
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(["git", "add", pathspec], cwd=cwd)
    return ToolResult(
        f"✓ staged {pathspec}" if code == 0 else f"✗ git add failed: {stderr[:300]}",
        is_error=code != 0,
    )


async def git_commit(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Commit staged changes in the worktree."""
    msg = str(payload.get("message", "update via Peri worker")).strip()
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(["git", "commit", "-m", msg], cwd=cwd)
    if code == 0:
        return ToolResult(f"✓ committed: {stdout.strip()[:200]}")
    if "nothing to commit" in stderr or "nothing to commit" in stdout:
        return ToolResult("✓ nothing to commit (no changes)")
    return ToolResult(f"✗ git commit failed: {stderr[:300]}", is_error=True)


async def git_status(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Show worktree status (git status --short)."""
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(["git", "status", "--short"], cwd=cwd)
    if code == 0:
        body = stdout.strip() or "(clean)"
        return ToolResult(f"📋 {body[:800]}")
    return ToolResult(f"✗ git status failed: {stderr[:300]}", is_error=True)


async def git_diff_work(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Show unstaged diff in the worktree."""
    cwd = context.cwd or os.getcwd()
    path = str(payload.get("path", "")).strip()
    cmd = ["git", "diff"]
    if path:
        cmd.append(path)
    stdout, stderr, code = await _git(cmd, cwd=cwd)
    if code == 0:
        body = stdout.strip()[:3000] or "(no unstaged changes)"
        return ToolResult(f"```diff\n{body}\n```")
    return ToolResult(f"✗ git diff failed: {stderr[:300]}", is_error=True)


SUBAGENT_GIT_TOOLS: list[Tool] = [
    Tool(
        name="git_status",
        description="Show worktree status (modified/staged/untracked files)",
        parameters={
            "type": "object",
            "properties": {},
        },
        handler=git_status,
        is_read_only=True,
    ),
    Tool(
        name="git_diff_work",
        description="Show unstaged diff in the worktree",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (optional)"},
            },
        },
        handler=git_diff_work,
        is_read_only=True,
    ),
    Tool(
        name="git_add",
        description="Stage files inside the worker's private worktree (git add)",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Pathspec to stage (default: .)"},
            },
        },
        handler=git_add,
        is_read_only=False,
        is_concurrency_safe=False,
        danger_level="medium",
        requires_approval=False,
    ),
    Tool(
        name="git_commit",
        description="Commit staged changes inside the worker's private worktree",
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message"},
            },
        },
        handler=git_commit,
        is_read_only=False,
        is_concurrency_safe=False,
        danger_level="medium",
        requires_approval=False,
    ),
]


# Worker push: registered separately from SUBAGENT_GIT_TOOLS so the fail-closed
# subagent list stays exact.  A future setting can inject this tool explicitly;
# it is never granted by writable mode alone.
SUBAGENT_PUSH_TOOL: Tool = Tool(
    name="git_push_worktree",
    description="Push the worker's branch to its remote (disabled by default)",
    parameters={
        "type": "object",
        "properties": {
            "remote": {"type": "string", "description": "Remote (default origin)"},
            "branch": {"type": "string", "description": "Branch to push"},
        },
    },
    handler=lambda payload, context: git_push(payload, context),
    is_read_only=False,
    is_concurrency_safe=False,
    danger_level="high",
    requires_approval=True,
)


# ═══ Host-level git tools: remote / branch / credential management ═══
# These extend the Peri-focused worktree tools with everyday operations the
# host can run inside the workspace.  Mutating operations (push / merge /
# credential write) require explicit approval via the standard approval
# channel.

async def _git_with_env(
    cmd: list[str], cwd: str | None = None, *, env: dict[str, str] | None = None,
) -> tuple[str, str, int]:
    """Like ``_git`` but accepts extra environment variables (e.g. GIT_ASKPASS)."""
    try:
        return await _run_git(
            cmd, cwd=cwd, env=env,
            timeout=float(os.environ.get("MODUS_GIT_TOOL_TIMEOUT", "45") or 45),
        )
    except FileNotFoundError:
        return "", "git not found in PATH", 127
    except Exception as exc:
        return "", str(exc), 1


def _validate_remote_url(url: str) -> str:
    """Reject file:// and local-path remotes; allow only http(s) and ssh git@."""
    import re

    url = str(url or "").strip()
    if not url:
        raise ValueError("remote URL is required")
    if url.startswith("file://") or url.startswith("/") or url.startswith("~"):
        raise ValueError("local file remotes are not allowed")
    if not (url.startswith("https://") or url.startswith("git@")):
        raise ValueError("remote URL must be https:// or git@")
    if url.startswith("https://"):
        m = re.match(r"^https://([^/]+)/", url)
        if not m or "." not in m.group(1):
            raise ValueError("remote host looks invalid")
    return url


async def git_remote_list(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(["git", "remote", "-v"], cwd=cwd)
    if code != 0:
        return ToolResult(f"✗ {stderr.strip()[:300]}", is_error=True)
    return ToolResult(stdout.strip() or "（无远程仓库）")


async def git_remote_add(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        url = _validate_remote_url(str(payload.get("url") or ""))
    except ValueError as exc:
        return ToolResult(str(exc), is_error=True)
    name = str(payload.get("name") or "origin").strip()
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(["git", "remote", "add", name, url], cwd=cwd)
    if code != 0:
        return ToolResult(f"✗ git remote add failed: {stderr.strip()[:300]}", is_error=True)
    return ToolResult(f"✓ 已添加远程 {name} → {url}")


async def git_remote_remove(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    name = str(payload.get("name") or "origin").strip()
    if not name or name in {"origin", "upstream"}:
        return ToolResult("✗ 拒绝删除 origin/upstream 远程", is_error=True)
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(["git", "remote", "remove", name], cwd=cwd)
    if code != 0:
        return ToolResult(f"✗ git remote remove failed: {stderr.strip()[:300]}", is_error=True)
    return ToolResult(f"✓ 已移除远程 {name}")


async def git_fetch(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    remote = str(payload.get("remote") or "").strip() or None
    cmd = ["git", "fetch"] + ([remote] if remote else [])
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(cmd, cwd=cwd)
    if code != 0:
        return ToolResult(f"✗ git fetch failed: {stderr.strip()[:300]}", is_error=True)
    return ToolResult("✓ 已从远程拉取引用")


async def git_pull(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    remote = str(payload.get("remote") or "origin").strip()
    branch = str(payload.get("branch") or "").strip()
    cmd = ["git", "pull", remote] + ([branch] if branch else [])
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(cmd, cwd=cwd)
    if code != 0:
        return ToolResult(f"✗ git pull failed: {stderr.strip()[:300]}", is_error=True)
    return ToolResult("✓ 已拉取最新改动")


async def git_push(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    remote = str(payload.get("remote") or "origin").strip()
    branch = str(payload.get("branch") or "").strip()
    cmd = ["git", "push", remote] + ([branch] if branch else [])
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(cmd, cwd=cwd)
    if code != 0:
        return ToolResult(f"✗ git push failed: {stderr.strip()[:300]}", is_error=True)
    return ToolResult("✓ 已推送到远程")


async def git_branch_list(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(["git", "branch", "-a"], cwd=cwd)
    if code != 0:
        return ToolResult(f"✗ {stderr.strip()[:300]}", is_error=True)
    return ToolResult(stdout.strip() or "（无分支）")


async def git_branch_create(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    branch = str(payload.get("branch") or "").strip()
    base = str(payload.get("base") or "").strip()
    if not branch:
        return ToolResult("✗ git_branch_create requires a 'branch'", is_error=True)
    cwd = context.cwd or os.getcwd()
    cmd = ["git", "checkout", "-b", branch] + ([base] if base else [])
    stdout, stderr, code = await _git(cmd, cwd=cwd)
    if code != 0:
        return ToolResult(f"✗ git branch create failed: {stderr.strip()[:300]}", is_error=True)
    return ToolResult(f"✓ 已创建并切换到分支 {branch}")


async def git_branch_checkout(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    branch = str(payload.get("branch") or "").strip()
    if not branch:
        return ToolResult("✗ git_branch_checkout requires a 'branch'", is_error=True)
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(["git", "checkout", branch], cwd=cwd)
    if code != 0:
        return ToolResult(f"✗ git checkout failed: {stderr.strip()[:300]}", is_error=True)
    return ToolResult(f"✓ 已切换到分支 {branch}")


async def git_branch_merge(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Merge a branch into the current HEAD with a reviewable no-ff commit.

    Never pushes; the merge stays local for the host to review.
    """
    branch = str(payload.get("branch") or "").strip()
    if not branch:
        return ToolResult("✗ git_branch_merge requires a 'branch'", is_error=True)
    message = str(payload.get("message") or f"modus: merge {branch}").strip()[:200]
    cwd = context.cwd or os.getcwd()
    stdout, stderr, code = await _git(
        ["git", "merge", "--no-ff", "-m", message, branch], cwd=cwd,
    )
    if code != 0:
        return ToolResult(f"✗ git merge failed: {stderr.strip()[:300]}", is_error=True)
    return ToolResult(f"✓ 已合并 {branch}")


async def git_credential_set(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    from modus.tools.git_credentials import set_git_credential

    remote = str(payload.get("remote") or "").strip()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not remote or not username or not password:
        return ToolResult("✗ git_credential_set requires remote/username/password", is_error=True)
    try:
        set_git_credential(remote, username, password)
    except Exception as exc:
        return ToolResult(f"✗ 凭据保存失败: {str(exc)[:200]}", is_error=True)
    return ToolResult(f"✓ 已保存 {remote} 的凭据（尾号 …{password[-4:]}）")


async def git_credential_clear(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    from modus.tools.git_credentials import clear_git_credential

    remote = str(payload.get("remote") or "").strip()
    if not remote:
        return ToolResult("✗ git_credential_clear requires a 'remote'", is_error=True)
    clear_git_credential(remote)
    return ToolResult(f"✓ 已清除 {remote} 的凭据")
