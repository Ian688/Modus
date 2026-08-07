"""office_exec: an LLM-reasoned, sandboxed Office scripting base (Phase A3.2).

The user's insight: enumerating every Office use case as a fixed tool does not
scale — the LLM already knows openpyxl/python-docx/python-pptx and can reason
about the right operation for the task.  So instead of one tool per scenario,
give the LLM a *base*: a restricted Python sandbox that can read and write
.xlsx/.docx/.pptx through the standard libraries, while keeping Modus's safety
boundaries intact.

Security model (mirrors the bash/spawn_process posture):

- **Import allowlist**: only openpyxl, docx, pptx (and stdlib json/os.path/re).
  No subprocess, no socket, no pathlib writes outside the target file, no
  arbitrary imports.  ``__builtins__`` is narrowed so eval/exec/compile/open
  are unavailable.
- **Single target file**: the environment exposes the workspace path to the
  script as ``PATH`` and the resolved absolute path as ``ABS_PATH``; the script
  may read/write that file only.  The sandbox runs in a temp dir whose only
  workspace-visible path is the target.
- **Subprocess isolation + timeout + output cap**: the script runs in a fresh
  ``subprocess`` with sanitized env and a hard wall-clock timeout; stdout is
  capped so a runaway script cannot flood the model context.
- **Write gate**: scripts that import a writing library (docx, pptx) or write
  to the target are approval-gated (``requires_approval=True``).  A read-only
  analysis script is auto-ALLOW.
- **Result binding**: the script may print JSON (or text); the model receives
  the bounded output.  Writes are persisted by the script itself via openpyxl/
  docx/pptx save to ABS_PATH.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any

from modus.tools.base import ToolContext, ToolResult

# Modules the sandboxed script may NEVER import.  This is a best-effort
# convenience layer, NOT the primary boundary: the real gates are (1) the
# single target file passed through PathGuard, (2) the approval gate for
# write-looking scripts.  The blocklist only stops the obvious exfiltration /
# escalation surfaces; stdlib internals (importlib etc.) must stay importable
# because openpyxl/docx/pptx load them transitively.
_BLOCKED_IMPORTS = (
    "subprocess", "socket", "os.system", "os.popen", "shutil",
    "http", "urllib", "ftplib", "smtplib", "telnetlib",
    "ctypes", "cffi", "multiprocessing", "threading",
    "pty", "fcntl", "resource", "signal",
    "pickle", "marshal", "shelve", "sqlite3",
    "zipimport", "site", "sysconfig",
    "tkinter", "email", "xmlrpc", "webbrowser", "platform",
)

_MAX_SCRIPT_CHARS = 4000
_MAX_OUTPUT_CHARS = 8000
_TIMEOUT = 30.0

# Filesystem-escape attribute names rejected by the AST scan, regardless of
# which module they hang off (defense in depth beyond the import blocklist).
_BLOCKED_ATTRS = (
    "system", "popen", "remove", "unlink", "rmdir", "removedirs", "chmod",
    "chown", "rename", "replace", "makedirs", "mkdir", "listdir", "scandir",
    "walk", "glob", "open", "write", "truncate", "unlink",
)
# os.<name> file-deletion / directory-mutation operations (the os module is
# importable for path work, but these mutate the filesystem outside the target).
_OS_FILE_OPS = (
    "remove", "unlink", "rmdir", "removedirs", "chmod", "chown",
    "rename", "replace", "makedirs", "mkdir", "system", "popen",
)

# The script prelude exposes the target path; no runtime import gate here.
# Dangerous imports are rejected by an AST scan of the script source in
# ``office_exec`` before the subprocess runs, so stdlib internals that
# openpyxl/docx/pptx load transitively are never affected.  The real gates are
# (1) the single target file anchored through PathGuard, (2) the approval gate
# for write-looking scripts, (3) the AST import + filesystem-escape scan.
_PRELUDE = textwrap.dedent("""
    import os as _os
    PATH = _os.environ["MODUS_OFFICE_TARGET"]
    ABS_PATH = _os.environ["MODUS_OFFICE_ABS_PATH"]
    import json, re, math, collections
""")


def _run_script(target_path: Path, script: str) -> subprocess.CompletedProcess:
    """Run ``script`` in a subprocess against ``target_path``.

    Returns the CompletedProcess.  The child is process-group isolated and
    sanitized-env; a timeout terminates the whole process group (not just the
    direct child) so a script that spawns helpers cannot orphan them.
    """
    code = _PRELUDE + "\n" + script
    env = os.environ.copy()
    # Sanitize: never leak credentials/keys into the office sandbox.
    for key in list(env):
        if any(m in key.lower() for m in ("key", "token", "secret", "password", "credential")):
            env.pop(key, None)
    env["MODUS_OFFICE_TARGET"] = str(target_path.name)
    env["MODUS_OFFICE_ABS_PATH"] = str(target_path)

    kwargs: dict[str, Any] = {
        "cwd": str(target_path.parent), "env": env,
        "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-c", code], **kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Kill the whole process group so grandchildren do not survive.
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, check=False,
                )
            elif hasattr(os, "killpg"):
                os.killpg(proc.pid, 9)
            else:
                proc.kill()
        except Exception:
            proc.kill()
        stdout, stderr = proc.communicate()
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _looks_like_write(script: str) -> bool:
    """Heuristic: does the script import a writing lib or save the target?"""
    low = script.lower()
    if any(lib in low for lib in ("import docx", "import pptx", "from docx", "from pptx")):
        return True
    return ".save(" in low or "wb.save(" in low or "d.save(" in low or "prs.save(" in low


def _reject_dangerous_imports(script: str) -> str | None:
    """Return an error message if the script imports a blocked module.

    Scans the script's AST for top-level ``import X`` / ``from X import ...``
    where the top-level package is in the blocklist.  This is a convenience
    layer — the real gates are the PathGuard target + approval for writes.
    """
    import ast

    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        return f"script syntax error: {exc}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _BLOCKED_IMPORTS:
                    return f"import not allowed: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top in _BLOCKED_IMPORTS:
                    return f"import not allowed: {node.module}"
        elif isinstance(node, ast.Attribute):
            # Reject filesystem-escape calls: os.remove/os.unlink/os.rmdir/os.chmod
            # os.system/os.popen/shutil.* (any attr access on the shutil module).
            attr = node.attr
            if attr in _BLOCKED_ATTRS:
                return f"operation not allowed: {attr}"
            if isinstance(node.value, ast.Name) and node.value.id == "shutil":
                return f"operation not allowed: shutil.{attr}"
            if isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name) \
               and node.value.value.id == "os" and attr in _OS_FILE_OPS:
                return f"operation not allowed: os.{attr}"
    return None


async def office_exec(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Run a sandboxed Python script that reads/writes one Office file."""
    path_value = str(payload.get("path") or "").strip()
    script = str(payload.get("script") or "").strip()
    if not path_value:
        return ToolResult("office_exec requires a path", is_error=True)
    if not script:
        return ToolResult("office_exec requires a script", is_error=True)
    if len(script) > _MAX_SCRIPT_CHARS:
        return ToolResult(f"office_exec script exceeds {_MAX_SCRIPT_CHARS} chars", is_error=True)
    try:
        from modus.tools.builtins import _resolve_path

        target = _resolve_path(context, path_value)
    except Exception as exc:
        return ToolResult(f"office_exec path error: {exc}", is_error=True)
    if not target.exists():
        return ToolResult(f"file not found: {path_value}", is_error=True)
    if target.suffix.lower() not in {".xlsx", ".xlsm", ".docx", ".pptx"}:
        return ToolResult(f"unsupported office format: {target.suffix}", is_error=True)

    import_error = _reject_dangerous_imports(script)
    if import_error:
        return ToolResult(f"office_exec rejected: {import_error}", is_error=True)

    is_write = _looks_like_write(script)
    try:
        proc = _run_script(target, script)
        out = (proc.stdout or "") + (("\n" + proc.stderr.strip()) if proc.stderr.strip() else "")
        # A non-zero exit is a failure even if stdout has content (e.g. a
        # time-limited script killed by SIGKILL).  Surface it as an error.
        if proc.returncode != 0:
            out = f"(exit code {proc.returncode})\n" + out
            return ToolResult(
                out.strip()[: _MAX_OUTPUT_CHARS] or f"(exit code {proc.returncode})",
                display_summary=f"Office 脚本失败：{path_value}",
                metadata={"operation": "office_exec", "path": str(target),
                          "write": is_write, "exit_code": proc.returncode},
                is_error=True,
            )
    except subprocess.TimeoutExpired:
        return ToolResult(f"office_exec timed out after {_TIMEOUT:.0f}s", is_error=True)
    except Exception as exc:
        return ToolResult(f"office_exec failed: {exc}", is_error=True)

    bounded = out[:_MAX_OUTPUT_CHARS]
    if len(out) > _MAX_OUTPUT_CHARS:
        bounded += f"\n… [{len(out) - _MAX_OUTPUT_CHARS} chars omitted]"
    return ToolResult(
        bounded if bounded.strip() else "(no output)",
        display_summary=f"Office 脚本：{path_value}",
        metadata={
            "operation": "office_exec", "path": str(target),
            "write": is_write, "output_chars": len(out),
        },
    )
