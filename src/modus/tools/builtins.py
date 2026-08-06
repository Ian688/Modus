from __future__ import annotations

import glob as glob_module
import difflib
import json
import os
import signal
import asyncio
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from modus.policy import CommandGuard, PathGuard
from modus.policy.path_guard import PathPolicyError
from modus.redact import redact_text
from modus.sandbox import rlimit_preexec
from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
from modus.tools.payload import bounded_for_model

from modus.web.search import search_web
from modus.web.fetch import fetch_url


async def list_dir(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        path = _resolve_path(context, str(payload["path"]))
        if not path.is_dir():
            return ToolResult(f"Not a directory: {path}", is_error=True)
        rows = []
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            marker = "/" if child.is_dir() else ""
            rows.append(f"{child.name}{marker}")
        return ToolResult("\n".join(rows) or "(empty directory)")
    except PathPolicyError as exc:
        return _path_error(exc)


async def glob_files(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Glob inside the home-anchored boundary and reject a symlinked root."""
    pattern = str(payload["pattern"])
    try:
        pattern_path = Path(pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            raise PathPolicyError(f"path escapes home: {pattern}")
        _resolve_path(context, _glob_anchor(pattern))
        base = Path(context.workspace_root or context.cwd).resolve()
        limit = int(payload.get("limit") or 100)
        matches = glob_module.glob(str(base / pattern), recursive=True)
        rels: list[str] = []
        guard = PathGuard()
        for match in sorted(matches):
            path = guard.validate(match, base=base)
            rels.append(str(path.relative_to(base)))
            if len(rels) >= limit:
                break
        return ToolResult("\n".join(rels) or "(no matches)")
    except PathPolicyError as exc:
        return _path_error(exc)


async def read_file(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        path = _resolve_path(context, str(payload["path"]))
        offset = max(int(payload.get("offset") or 1), 1)
        limit = int(payload.get("limit") or 500)
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = content[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{idx + offset}: {line}" for idx, line in enumerate(selected))
        # The file's bytes are never re-read; the decoded lines above already
        # crossed the workspace boundary, so count disclosure from them.
        selected_text = "\n".join(selected)
        return ToolResult(
            numbered,
            disclosure={
                "local_bytes_read": path.stat().st_size,
                "model_bytes_sent": len(selected_text),
                "raw_content_sent": True,
            },
        )
    except PathPolicyError as exc:
        return _path_error(exc)


async def write_file(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Atomically create or replace a UTF-8 file inside the workspace."""
    if context.cancel_event is not None and context.cancel_event.is_set():
        return ToolResult("Run cancelled: write_file will not start.", is_error=True)
    try:
        path = _resolve_path(context, str(payload["path"]))
        content = str(payload["content"])
        if context.cancel_event is not None and context.cancel_event.is_set():
            return ToolResult("Run cancelled: write_file will not start.", is_error=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        existed = path.exists()
        previous_mode = path.stat().st_mode if existed else None
        previous_content = path.read_text(encoding="utf-8", errors="replace") if existed and path.is_file() else ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
                suffix=".modus-write", delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if previous_mode is not None:
                os.chmod(temp_name, previous_mode)
            if context.cancel_event is not None and context.cancel_event.is_set():
                return ToolResult("Run cancelled: write_file was not committed.", is_error=True)
            os.replace(temp_name, path)
            temp_name = None
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
        relative = _display_path(path, Path(context.workspace_root or context.cwd).resolve())
        diff = _bounded_unified_diff(relative, previous_content, content)
        return ToolResult(
            f"Wrote {relative}",
            display_summary=f"写入 {relative}",
            metadata={
                "operation": "write", "path": str(relative), "changed": True,
                "change_type": "update" if existed else "create", **diff,
            },
        )
    except PathPolicyError as exc:
        return _path_error(exc)
    except OSError as exc:
        return ToolResult(f"write_file failed: {exc}", is_error=True)


async def edit_file(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Replace an exact text fragment using an ambiguity-safe atomic write.

    ``write_file`` remains available for intentional full-file creation or
    replacement.  This operation is for small code edits: it refuses a missing
    target and refuses multiple matches unless the caller explicitly opts into
    ``replace_all``.  The original file is never truncated in place.
    """
    path_value = str(payload.get("path") or "")
    old_text = str(payload.get("old_text") or "")
    new_text = str(payload.get("new_text") or "")
    if not path_value:
        return ToolResult("edit_file requires a path", is_error=True)
    if not old_text:
        return ToolResult("edit_file requires non-empty old_text", is_error=True)
    if context.cancel_event is not None and context.cancel_event.is_set():
        return ToolResult("Run cancelled: edit_file will not start.", is_error=True)

    try:
        path = _resolve_path(context, path_value)
        if not path.exists():
            return ToolResult(f"file not found: {path_value}", is_error=True)
        if not path.is_file():
            return ToolResult(f"not a regular file: {path_value}", is_error=True)
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult(f"could not read {path_value}: {exc}", is_error=True)

        occurrences = original.count(old_text)
        expected_count = payload.get("expected_count")
        if expected_count is not None:
            try:
                expected_count = int(expected_count)
            except (TypeError, ValueError):
                return ToolResult("expected_count must be an integer", is_error=True)
            if occurrences != expected_count:
                return ToolResult(
                    f"edit_file expected {expected_count} matches but found {occurrences}",
                    is_error=True,
                )
        replace_all = bool(payload.get("replace_all", False))
        if occurrences == 0:
            return ToolResult("edit_file target text was not found", is_error=True)
        if occurrences > 1 and not replace_all:
            return ToolResult(
                f"edit_file found {occurrences} matches; provide more context or set replace_all=true",
                is_error=True,
            )
        updated = original.replace(old_text, new_text, -1 if replace_all else 1)
        if updated == original:
            return ToolResult("edit_file produced no change", is_error=True)
        if context.cancel_event is not None and context.cancel_event.is_set():
            return ToolResult("Run cancelled: edit_file was not committed.", is_error=True)

        # Keep the destination directory and replace the file in one rename so
        # readers never observe a partially written file.  Preserve mode bits.
        mode = path.stat().st_mode
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
                suffix=".modus-edit", delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, mode)
            if context.cancel_event is not None and context.cancel_event.is_set():
                return ToolResult("Run cancelled: edit_file was not committed.", is_error=True)
            os.replace(temp_name, path)
            temp_name = None
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
        relative = _display_path(path, Path(context.workspace_root or context.cwd).resolve())
        replacement_count = occurrences if replace_all else 1
        diff = _bounded_unified_diff(relative, original, updated)
        return ToolResult(
            f"Edited {relative}: replaced {replacement_count} exact match"
            f"{'es' if replacement_count != 1 else ''}.",
            display_summary=f"编辑 {relative}（{replacement_count} 处）",
            metadata={
                "operation": "edit", "path": str(relative),
                "changed": True, "replacement_count": replacement_count,
                "change_type": "update", **diff,
            },
        )
    except PathPolicyError as exc:
        return _path_error(exc)
    except OSError as exc:
        return ToolResult(f"edit_file write failed: {exc}", is_error=True)


async def grep(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    root = Path(context.workspace_root or context.cwd).resolve()
    try:
        start = _resolve_path(context, str(payload.get("path") or "."))
        pattern = str(payload["pattern"])
        limit = int(payload.get("limit") or 100)
        use_regex = bool(payload.get("regex", True))
        try:
            compiled = re.compile(pattern) if use_regex else None
        except re.error as exc:
            return ToolResult(f"invalid regex: {exc}", is_error=True)

        matches: list[str] = []
        files = [start] if start.is_file() else [p for p in start.rglob("*") if p.is_file()]
        guard = PathGuard()
        for file_path in files:
            # A nested symlink can appear during recursive traversal; validate
            # every discovered path, not only the requested starting directory.
            resolved = guard.validate(file_path)
            if _skip_file(resolved):
                continue
            lines = resolved.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line_number, line in enumerate(lines, start=1):
                found = bool(compiled.search(line)) if compiled else pattern in line
                if found:
                    matches.append(f"{_display_path(resolved, root)}:{line_number}: {line.strip()}")
                    if len(matches) >= limit:
                        return ToolResult("\n".join(matches))
        return ToolResult("\n".join(matches) or "(no matches)")
    except PathPolicyError as exc:
        return _path_error(exc)


async def save_memory(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Persist a fact/preference/constraint to the session's semantic memory."""
    content = str(payload.get("content") or "").strip()
    if not content:
        return ToolResult("save_memory requires 'content'", is_error=True)
    if not context.session_id:
        return ToolResult(
            "Memory persistence requires a persisted Desktop session; "
            "this run has none.", is_error=True,
        )
    category = str(payload.get("category") or "fact").strip().lower()
    if category not in {"general", "constraint", "preference", "fact"}:
        return ToolResult(
            "category must be general, constraint, preference or fact", is_error=True,
        )
    try:
        from modus.desktop.memory import add_memory
        add_memory(context.session_id, content, category)
    except Exception as exc:
        return ToolResult(f"save_memory failed: {exc}", is_error=True)
    return ToolResult(f"Memory saved ({category}): {content[:200]}")


async def search_memory(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Retrieve the most relevant stored memories for a query."""
    query = str(payload.get("query") or "").strip()
    if not query:
        return ToolResult("search_memory requires 'query'", is_error=True)
    if not context.session_id:
        return ToolResult(
            "Memory retrieval requires a persisted Desktop session.", is_error=True,
        )
    try:
        from modus.desktop.memory import search_memories
        results = search_memories(context.session_id, query, limit=6)
    except Exception as exc:
        return ToolResult(f"search_memory failed: {exc}", is_error=True)
    if not results:
        return ToolResult("No matching memories found.")
    lines = [f"[{m['category']}] {m['content']}" for m in results]
    return ToolResult("\n".join(lines))


def _path_error(exc: PathPolicyError) -> ToolResult:
    return ToolResult(str(exc), is_error=True)


def _glob_anchor(pattern: str) -> str:
    """Return the concrete prefix before glob metacharacters for guard validation."""
    concrete: list[str] = []
    for part in Path(pattern).parts:
        if part == "**" or any(marker in part for marker in ("*", "?", "[")):
            break
        concrete.append(part)
    return str(Path(*concrete)) if concrete else "."


def _resolve_path(context: ToolContext, value: str) -> Path:
    """Resolve one tool path against the home-anchored boundary.

    Absolute paths are used directly.  Relative paths anchor to the explicit
    workspace when one is bound, otherwise to the home directory (matching the
    engine's cwd fallback).  The guard rejects escapes out of home and system
    roots regardless of the anchor.
    """
    return PathGuard().validate(value, base=context.workspace_root or context.cwd)

def _skip_file(path: Path) -> bool:
    skip_dirs = {".git", ".venv", "node_modules", "dist", "build"}
    if any(part in skip_dirs for part in path.parts):
        return True
    return path.stat().st_size > 1_000_000


def _display_path(path: Path, base: Path) -> str:
    """Render a resolved path relative to ``base`` when inside it, else absolute.

    A home-anchored Agent can touch files outside the explicit workspace; the
    display must not crash with ``ValueError`` when ``relative_to`` misses.
    """
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)

def get_builtin_tools() -> list[Tool]:
    return [
        Tool(
            name="list_dir",
            description="List entry names in a workspace directory as bounded metadata for the current model.",
            parameters=object_schema({"path": {"type": "string", "description": "Directory path"}}, ["path"]),
            required_keys=["path"],
            handler=list_dir,
            data_disclosure="workspace_metadata",
        ),
        Tool(
            name="glob",
            description="Find files by glob pattern inside the workspace.",
            parameters=object_schema(
                {"pattern": {"type": "string", "description": "Glob pattern"}, "limit": {"type": "number"}},
                ["pattern"],
            ),
            required_keys=["pattern"],
            handler=glob_files,
            data_disclosure="workspace_metadata",
        ),
        Tool(
            name="read_file",
            description="Read selected text lines from a workspace file and provide them to the current model.",
            parameters=object_schema(
                {"path": {"type": "string"}, "offset": {"type": "number"}, "limit": {"type": "number"}},
                ["path"],
            ),
            required_keys=["path"],
            handler=read_file,
            data_disclosure="workspace_content",
        ),
        Tool(
            name="write_file",
            description="Write a UTF-8 text file inside the workspace.",
            parameters=object_schema(
                {"path": {"type": "string"}, "content": {"type": "string"}},
                ["path", "content"],
            ),
            required_keys=["path", "content"],
            handler=write_file,
            is_read_only=False,
            danger_level="medium",
        ),
        Tool(
            name="edit_file",
            description="Replace an exact text fragment in a workspace file; refuses ambiguous matches by default.",
            parameters=object_schema(
                {
                    "path": {"type": "string", "description": "Workspace-relative file path"},
                    "old_text": {"type": "string", "description": "Exact text to replace"},
                    "new_text": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Explicitly replace every match"},
                    "expected_count": {"type": "number", "description": "Expected number of matches"},
                },
                ["path", "old_text", "new_text"],
            ),
            required_keys=["path", "old_text", "new_text"],
            handler=edit_file,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
        ),
        Tool(
            name="grep",
            description="Search workspace files and provide matching text excerpts to the current model.",
            parameters=object_schema(
                {"pattern": {"type": "string"}, "path": {"type": "string"}, "regex": {"type": "boolean"}, "limit": {"type": "number"}},
                ["pattern"],
            ),
            required_keys=["pattern"],
            handler=grep,
            data_disclosure="workspace_content",
        ),
        Tool(
            name="save_memory",
            description="Save a fact, preference or constraint to the session's long-term memory.",
            parameters=object_schema(
                {
                    "content": {"type": "string", "description": "The fact to remember"},
                    "category": {"type": "string", "description": "general, constraint, preference or fact"},
                },
                ["content"],
            ),
            required_keys=["content"],
            handler=save_memory,
            is_read_only=False,
            danger_level="medium",
        ),
        Tool(
            name="search_memory",
            description="Retrieve the most relevant stored memories for a query.",
            parameters=object_schema(
                {"query": {"type": "string", "description": "What to recall"}},
                ["query"],
            ),
            required_keys=["query"],
            handler=search_memory,
            is_read_only=True,
            is_concurrency_safe=True,
        ),
        Tool(
            name="search_code",
            description="Search source files and provide matching code lines to the current model.",
            parameters=object_schema(
                {
                    "query": {"type": "string", "description": "Literal text or regular expression"},
                    "path": {"type": "string", "description": "Workspace-relative file or directory"},
                    "regex": {"type": "boolean", "description": "Interpret query as a regular expression"},
                    "case_sensitive": {"type": "boolean"},
                    "context_lines": {"type": "number", "description": "Lines around each match (0-3)"},
                    "limit": {"type": "number", "description": "Maximum matches (1-1000)"},
                },
                ["query"],
            ),
            required_keys=["query"],
            handler=search_code,
            data_disclosure="workspace_content",
        ),
        Tool(
            name="load_skill",
            description="Load a named skill manual from skill directories.",
            parameters=object_schema(
                {"name": {"type": "string", "description": "Skill name"}},
                ["name"],
            ),
            required_keys=["name"],
            handler=load_skill,
        ),

        Tool(
            name="bash",
            description="Execute a shell command in the workspace.",
            parameters=object_schema(
                {"command": {"type": "string", "description": "Shell command"}, "timeout": {"type": "number"}},
                ["command"],
            ),
            required_keys=["command"],
            handler=bash,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
        ),
        Tool(
            name="run_tests",
            description="Run a test or validation command and return structured exit code, duration, counts, and output.",
            parameters=object_schema(
                {
                    "command": {"type": "string", "description": "Exact test or validation command"},
                    "path": {"type": "string", "description": "Workspace-relative working directory"},
                    "timeout": {"type": "number", "description": "Timeout in seconds (1-3600)"},
                },
                ["command"],
            ),
            required_keys=["command"],
            handler=run_tests,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            timeout=3700.0,
        ),
        Tool(
            name="web_search",
            description="Search the web for current information.",
            parameters=object_schema(
                {"query": {"type": "string", "description": "Search query"}, "max_results": {"type": "number"}},
                ["query"],
            ),
            required_keys=["query"],
            handler=web_search,
        ),
        Tool(
            name="web_fetch",
            description="Fetch a public HTTP/HTTPS page and return readable text.",
            parameters=object_schema(
                {"url": {"type": "string", "description": "URL to fetch"}, "max_length": {"type": "number"}},
                ["url"],
            ),
            required_keys=["url"],
            handler=web_fetch,
        ),
    ] + _clone_tools()


def _clone_tools() -> list[Tool]:
    """Host-level git tools: clone + remote/branch/credential management.

    Mutating operations (push / pull / merge / credential write) are
    approval-gated; read-only listings are free.
    """
    from modus.tools.git_tools import (
        git_branch_checkout, git_branch_create, git_branch_list,
        git_branch_merge, git_clone, git_credential_clear,
        git_credential_set, git_fetch, git_pull, git_push,
        git_remote_add, git_remote_list, git_remote_remove,
    )

    def _schema(props, required):
        return object_schema(props, required)

    def _tool(name, description, props, required, *, read_only=False, high=False):
        return Tool(
            name=name,
            description=description,
            parameters=_schema(props, required),
            required_keys=required,
            handler={
                "git_clone": git_clone,
                "git_remote_list": git_remote_list,
                "git_remote_add": git_remote_add,
                "git_remote_remove": git_remote_remove,
                "git_fetch": git_fetch,
                "git_pull": git_pull,
                "git_push": git_push,
                "git_branch_list": git_branch_list,
                "git_branch_create": git_branch_create,
                "git_branch_checkout": git_branch_checkout,
                "git_branch_merge": git_branch_merge,
                "git_credential_set": git_credential_set,
                "git_credential_clear": git_credential_clear,
            }[name],
            is_read_only=read_only,
            is_concurrency_safe=False,
            danger_level="high" if high else "medium",
            requires_approval=not read_only,
        )

    return [
        _tool("git_clone", "Clone a public GitHub/Gitee repository into the project area.",
              {"url": {"type": "string", "description": "https:// or git@ repo URL"},
               "path": {"type": "string", "description": "Optional destination directory"}},
              ["url"], high=True),
        _tool("git_remote_list", "List configured git remotes.",
              {}, [], read_only=True),
        _tool("git_remote_add", "Add a git remote (https:// or git@ only, no file paths).",
              {"name": {"type": "string", "description": "Remote name (default origin)"},
               "url": {"type": "string", "description": "Remote URL"}},
              ["url"], high=True),
        _tool("git_remote_remove", "Remove a non-origin git remote.",
              {"name": {"type": "string", "description": "Remote name"}},
              ["name"], high=True),
        _tool("git_fetch", "Fetch references from a remote.",
              {"remote": {"type": "string", "description": "Remote name (optional)"}},
              [], read_only=True),
        _tool("git_pull", "Pull latest changes from a remote.",
              {"remote": {"type": "string", "description": "Remote (default origin)"},
               "branch": {"type": "string", "description": "Branch (optional)"}},
              [], high=True),
        _tool("git_push", "Push commits to a remote.",
              {"remote": {"type": "string", "description": "Remote (default origin)"},
               "branch": {"type": "string", "description": "Branch (optional)"}},
              [], high=True),
        _tool("git_branch_list", "List local and remote branches.",
              {}, [], read_only=True),
        _tool("git_branch_create", "Create and switch to a new branch.",
              {"branch": {"type": "string", "description": "New branch name"},
               "base": {"type": "string", "description": "Base branch (optional)"}},
              ["branch"], high=True),
        _tool("git_branch_checkout", "Switch to an existing branch.",
              {"branch": {"type": "string", "description": "Branch name"}},
              ["branch"], high=True),
        _tool("git_branch_merge", "Merge a branch into HEAD (never pushes).",
              {"branch": {"type": "string", "description": "Branch to merge"},
               "message": {"type": "string", "description": "Optional merge message"}},
              ["branch"], high=True),
        _tool("git_credential_set", "Store git credentials for a remote (Keychain/JSON).",
              {"remote": {"type": "string", "description": "Remote name or URL"},
               "username": {"type": "string", "description": "Username"},
               "password": {"type": "string", "description": "Password or token"}},
              ["remote", "username", "password"], high=True),
        _tool("git_credential_clear", "Remove stored git credentials for a remote.",
              {"remote": {"type": "string", "description": "Remote name or URL"}},
              ["remote"], high=True),
    ]

async def search_code(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Search workspace source files without requiring a pre-built index.

    The first implementation deliberately uses a bounded filesystem scan.  It
    keeps the tool useful in a fresh checkout (where an index does not exist)
    while preserving the same workspace and symlink policy as ``grep``.  A
    future persistent index can replace the scan behind this contract without
    changing the model-facing tool name or result shape.
    """
    query = str(payload.get("query") or "")
    if not query.strip():
        return ToolResult("search_code requires a non-empty query", is_error=True)

    try:
        start = _resolve_path(context, str(payload.get("path") or "."))
        if not start.exists():
            return ToolResult(f"path not found: {payload.get('path') or '.'}", is_error=True)
        limit = max(1, min(int(payload.get("limit") or 100), 1000))
        use_regex = bool(payload.get("regex", False))
        case_sensitive = bool(payload.get("case_sensitive", False))
        context_lines = max(0, min(int(payload.get("context_lines") or 0), 3))
        try:
            compiled = re.compile(query, 0 if case_sensitive else re.IGNORECASE) if use_regex else None
        except re.error as exc:
            return ToolResult(f"invalid regex: {exc}", is_error=True)

        root = Path(context.workspace_root or context.cwd).resolve()
        files = [start] if start.is_file() else sorted(
            (item for item in start.rglob("*") if item.is_file()),
            key=lambda item: str(item),
        )
        guard = PathGuard()
        matches: list[str] = []
        for candidate in files:
            resolved = guard.validate(candidate)
            if _skip_file(resolved):
                continue
            try:
                lines = resolved.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                # Binary/unreadable files are not useful code-search results.
                continue
            for line_number, line in enumerate(lines, start=1):
                found = bool(compiled.search(line)) if compiled else (
                    query in line if case_sensitive else query.casefold() in line.casefold()
                )
                if not found:
                    continue
                relative = _display_path(resolved, root)
                if context_lines:
                    lo = max(0, line_number - 1 - context_lines)
                    hi = min(len(lines), line_number + context_lines)
                    body = "\n".join(f"{idx + 1}: {lines[idx]}" for idx in range(lo, hi))
                    matches.append(f"{relative}:{line_number}:\n{body}")
                else:
                    matches.append(f"{relative}:{line_number}: {line.strip()}")
                if len(matches) >= limit:
                    return ToolResult("\n".join(matches) + f"\n... [limited to {limit} matches]")
        return ToolResult("\n".join(matches) or "(no matches)")
    except PathPolicyError as exc:
        return _path_error(exc)

async def load_skill(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Load a user skill prompt from the non-executable local skill repository."""
    from modus.skills import SkillRepository

    try:
        skill = SkillRepository().get(str(payload["name"]))
    except ValueError as exc:
        return ToolResult(str(exc), is_error=True)
    if skill is None:
        return ToolResult(f'Skill "{payload["name"]}" not found.', is_error=True)
    return ToolResult(skill.prompt, display_summary=skill.description or skill.name)


async def bash(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    command = str(payload["command"])
    if context.cancel_event is not None and context.cancel_event.is_set():
        return ToolResult("Run cancelled: bash will not start.", is_error=True)
    CommandGuard(context.config.policy.command_blacklist).validate(command)
    timeout = float(payload.get("timeout") or context.config.tools.timeout)
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=context.cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
        preexec_fn=rlimit_preexec(context.config),
        **_process_group_kwargs(),
    )
    communicate_task = asyncio.create_task(proc.communicate())
    cancel_task = (
        asyncio.create_task(context.cancel_event.wait())
        if context.cancel_event is not None else None
    )
    waiters = {communicate_task, *([cancel_task] if cancel_task is not None else [])}
    try:
        done, _pending = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )
        if communicate_task in done:
            stdout, stderr = communicate_task.result()
        else:
            cancelled = cancel_task is not None and cancel_task in done
            stdout, stderr = await _stop_process_group(proc, communicate_task)
            if cancelled:
                return ToolResult("Command cancelled.", is_error=True)
            return ToolResult(f"Command timed out after {timeout:.0f}s", is_error=True)
    except asyncio.CancelledError:
        # ``Tool.execute`` may cancel a handler on its own I/O deadline.  That
        # must have the same process-tree cleanup guarantee as a run cancel.
        await _stop_process_group(proc, communicate_task)
        raise
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
        if cancel_task is not None:
            await asyncio.gather(cancel_task, return_exceptions=True)
    output = (stdout + stderr).decode("utf-8", errors="replace")
    full_output = output
    # Keep the legacy visible ``content`` (hard 20k cut) so existing consumers
    # and tests see the same summary they always did.  The model additionally
    # receives a bounded head/tail payload; oversized raw text is persisted as
    # a local artifact instead of being discarded.
    threshold = max(1, int(context.config.tools.tool_result_artifact_chars or 20_000))
    disclosure: dict[str, Any] = {"local_bytes_read": len(full_output)}
    if proc.returncode in (-24, 152):  # SIGXCPU: RLIMIT_CPU exceeded
        disclosure["rlimit"] = "cpu"
    elif proc.returncode in (-25, 153):  # SIGXFSZ: RLIMIT_FSIZE exceeded
        disclosure["rlimit"] = "fsize"
    artifact = None
    if len(output) > 20_000:
        output = output[:20_000] + "\n... [truncated]"
    if len(full_output) > threshold:
        artifact = _persist_tool_result(
            context, kind="tool-result",
            title=f"bash · {command[:80]}",
            content=full_output,
            summary=_shell_result_text(full_output, proc.returncode)[:1000],
        )
        bounded, bounded_disclosure = bounded_for_model(full_output, limit=threshold)
        disclosure.update(bounded_disclosure)
        return ToolResult(
            _shell_result_text(output, proc.returncode),
            is_error=proc.returncode != 0,
            raw_result=full_output,
            model_payload=_shell_result_text(bounded, proc.returncode),
            artifacts=[artifact] if artifact else [],
            disclosure=disclosure,
        )
    return ToolResult(
        _shell_result_text(output, proc.returncode),
        is_error=proc.returncode != 0,
        disclosure=disclosure,
    )


def _shell_result_text(output: str, returncode: int | None) -> str:
    """Annotate RLIMIT signals so a sandbox kill reads as a resource error."""
    if returncode in (-24, 152):  # SIGXCPU: RLIMIT_CPU exceeded
        return f"[CPU limit exceeded]\n{output}" if output else "CPU limit exceeded."
    if returncode in (-25, 153):  # SIGXFSZ: RLIMIT_FSIZE exceeded
        return f"[output size limit exceeded]\n{output}" if output else "Output size limit exceeded."
    return output or f"(exit {returncode}, no output)"


def _persist_tool_result(
    context: ToolContext, *, kind: str, title: str, content: str,
    summary: str = "",
) -> dict[str, Any] | None:
    """Persist a full tool output as a local artifact when the run is persisted.

    Returns browser-safe artifact metadata, or ``None`` when the run has no
    persisted Desktop session (CLI/embedders), so the caller falls back to the
    existing truncation instead of dropping the result.
    """
    if not context.session_id or not context.run_id:
        return None
    try:
        from modus.desktop.artifacts import public_artifact
        from modus.desktop.orchestration_ledger import persist_artifact

        artifact = persist_artifact(
            session_id=context.session_id,
            run_id=context.run_id,
            kind=kind,
            title=title,
            content=content,
            summary=summary,
        )
        return public_artifact(artifact) if artifact else None
    except Exception:
        # Never let a persistence failure turn a completed tool into an error.
        return None


async def run_tests(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Run an approved validation command and return machine-readable evidence."""
    command = str(payload.get("command") or "").strip()
    if not command:
        return ToolResult("run_tests requires a command", is_error=True)
    if context.cancel_event is not None and context.cancel_event.is_set():
        return ToolResult("Run cancelled: tests will not start.", is_error=True)
    try:
        CommandGuard(context.config.policy.command_blacklist).validate(command)
        workdir = _resolve_path(context, str(payload.get("path") or "."))
        if not workdir.is_dir():
            return ToolResult(f"test path is not a directory: {payload.get('path') or '.'}", is_error=True)
    except PathPolicyError as exc:
        return _path_error(exc)
    except ValueError as exc:
        return ToolResult(str(exc), is_error=True)

    try:
        timeout = max(1.0, min(float(payload.get("timeout") or context.config.tools.batch_timeout), 3600.0))
    except (TypeError, ValueError):
        return ToolResult("run_tests timeout must be a number", is_error=True)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
        preexec_fn=rlimit_preexec(context.config),
        **_process_group_kwargs(),
    )
    communicate_task = asyncio.create_task(proc.communicate())
    cancel_task = (
        asyncio.create_task(context.cancel_event.wait())
        if context.cancel_event is not None else None
    )
    waiters = {communicate_task, *([cancel_task] if cancel_task is not None else [])}
    status = "failed"
    timed_out = False
    cancelled = False
    try:
        done, _pending = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if communicate_task in done:
            stdout, stderr = communicate_task.result()
        else:
            cancelled = cancel_task is not None and cancel_task in done
            timed_out = not cancelled
            _terminate_process_group(proc)
            stdout, stderr = await communicate_task
        status = "cancelled" if cancelled else "timed_out" if timed_out else "passed" if proc.returncode == 0 else "failed"
    except asyncio.CancelledError:
        await _stop_process_group(proc, communicate_task)
        raise
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
        if cancel_task is not None:
            await asyncio.gather(cancel_task, return_exceptions=True)

    output = (stdout + stderr).decode("utf-8", errors="replace")
    full_output = output
    if len(output) > 30_000:
        output = output[:15_000] + "\n... [output truncated by Modus] ...\n" + output[-15_000:]
    duration = round(time.monotonic() - started, 3)
    counts = _test_counts(full_output)
    evidence = {
        "schema": "modus.verification.v1",
        "kind": "tests",
        "status": status,
        "command": command,
        "path": _display_path(workdir, Path(context.workspace_root or context.cwd).resolve()) or ".",
        "exit_code": proc.returncode,
        "duration_seconds": duration,
        "counts": counts,
        "output": output or f"(exit {proc.returncode}, no output)",
    }
    summary_bits = []
    for key in ("passed", "failed", "skipped", "warnings"):
        if counts.get(key):
            summary_bits.append(f"{counts[key]} {key}")
    summary = " · ".join(summary_bits) or f"exit {proc.returncode}"
    status_labels = {
        "passed": "验证通过",
        "failed": "验证失败",
        "timed_out": "验证超时",
        "cancelled": "验证已取消",
    }
    disclosure: dict[str, Any] = {"local_bytes_read": len(full_output)}
    threshold = max(1, int(context.config.tools.tool_result_artifact_chars or 20_000))
    if len(full_output) > threshold:
        # The verification JSON must stay parseable for the verification gate,
        # workbench review, and the frontend summary: only the ``output`` field
        # is bounded for the model, while the full text is persisted locally.
        artifact = _persist_tool_result(
            context, kind="tool-result",
            title=f"run_tests · {command[:80]}",
            content=full_output,
            summary=summary[:1000],
        )
        model_evidence = dict(evidence)
        bounded, bounded_disclosure = bounded_for_model(full_output, limit=threshold)
        model_evidence["output"] = bounded
        disclosure.update(bounded_disclosure)
        disclosure["raw_content_sent"] = False
        return ToolResult(
            json.dumps(evidence, ensure_ascii=False),
            is_error=status != "passed",
            display_summary=f"{status_labels[status]} · {summary} · {duration:.2f}s",
            raw_result=full_output,
            model_payload=json.dumps(model_evidence, ensure_ascii=False),
            artifacts=[artifact] if artifact else [],
            disclosure=disclosure,
            metadata={
                "operation": "verification", "status": status, "changed": False,
                "verification": {
                    "schema": evidence["schema"], "status": status,
                    "exit_code": evidence["exit_code"],
                    "duration_seconds": evidence["duration_seconds"],
                    "counts": evidence["counts"],
                },
            },
        )
    return ToolResult(
        json.dumps(evidence, ensure_ascii=False),
        is_error=status != "passed",
        display_summary=f"{status_labels[status]} · {summary} · {duration:.2f}s",
        disclosure=disclosure,
        metadata={
            "operation": "verification", "status": status, "changed": False,
            "verification": {
                "schema": evidence["schema"], "status": status,
                "exit_code": evidence["exit_code"],
                "duration_seconds": evidence["duration_seconds"],
                "counts": evidence["counts"],
            },
        },
    )


def _process_group_kwargs() -> dict[str, Any]:
    """Create a dedicated command tree on each supported desktop platform."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


async def _stop_process_group(
    proc: asyncio.subprocess.Process, communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> tuple[bytes, bytes]:
    """Terminate a command tree and reap its pipe reader even in test doubles."""
    _terminate_process_group(proc)
    try:
        return await asyncio.wait_for(asyncio.shield(communicate_task), timeout=1.0)
    except TimeoutError:
        # A real process normally makes communicate finish after termination.
        # Keep cancellation cleanup bounded for a broken child/test double.
        if not communicate_task.done():
            communicate_task.cancel()
        await asyncio.gather(communicate_task, return_exceptions=True)
        try:
            await proc.wait()
        except ProcessLookupError:
            pass
        return b"", b""


def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            # Windows has no POSIX process groups. ``/T`` kills the complete
            # descendant tree created with CREATE_NEW_PROCESS_GROUP.
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False,
            )
        elif hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def _test_counts(output: str) -> dict[str, int]:
    patterns = {
        "passed": r"(?<![\w])([0-9]+)\s+passed\b",
        "failed": r"(?<![\w])([0-9]+)\s+failed\b",
        "skipped": r"(?<![\w])([0-9]+)\s+skipped\b",
        "warnings": r"(?<![\w])([0-9]+)\s+warnings?\b",
    }
    counts: dict[str, int] = {}
    for key, pattern in patterns.items():
        matches = re.findall(pattern, output, flags=re.IGNORECASE)
        if matches:
            counts[key] = int(matches[-1])
    return counts


def _bounded_unified_diff(path: Path, before: str, after: str, *, limit: int = 12_000) -> dict[str, Any]:
    """Return review metadata without letting a full file dominate an event."""
    lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
    ))
    additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    full = redact_text("\n".join(lines))
    truncated = len(full) > limit
    preview = full[:limit].rstrip() + ("\n... [diff truncated by Modus] ..." if truncated else "")
    return {
        "diff": preview,
        "diff_truncated": truncated,
        "additions": additions,
        "deletions": deletions,
    }

# Retained as a private placeholder for the future checkpoint subsystem.
# Do not register this with ``get_builtin_tools`` until restoration has an
# auditable snapshot model, conflict handling, and a verified implementation.
async def revert_turn(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    return ToolResult(f"Snapshot restore feature not yet implemented")

async def web_search(payload: dict[str, Any], _context: ToolContext) -> ToolResult:
    max_results = int(payload.get("max_results") or 5)
    try:
        results = await search_web(str(payload["query"]), max_results=max_results)
    except Exception as exc:
        return ToolResult(f"Search error: {exc}", is_error=True)
    if not results:
        return ToolResult(f'No search results found for "{payload["query"]}".')
    content = "\n\n".join(
        f"{index}. {result.title}\n{result.url}\n{result.snippet}"
        for index, result in enumerate(results, start=1)
    )
    return ToolResult(content)

async def web_fetch(payload: dict[str, Any], _context: ToolContext) -> ToolResult:
    max_length = int(payload.get("max_length") or 10_000)
    try:
        content = await fetch_url(str(payload["url"]), max_length=max_length)
    except Exception as exc:
        return ToolResult(f"Fetch error: {exc}", is_error=True)
    return ToolResult(content)
