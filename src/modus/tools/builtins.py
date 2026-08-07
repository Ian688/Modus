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
from modus.tools.browser import (
    browser_click,
    browser_close,
    browser_eval,
    browser_extract,
    browser_navigate,
    browser_screenshot,
    browser_state,
    browser_type,
)
from modus.tools.office import (
    excel_analyze,
    excel_query,
    pptx_build,
    pptx_extract,
    word_edit,
    word_extract,
)
from modus.tools.office_exec import office_exec
from modus.tools.payload import bounded_for_model
from modus.tools.system_control import (
    port_list,
    service_restart,
    service_status,
)
from modus.tools.process_tools import (
    kill_process,
    list_processes,
    spawn_process,
    tail_process,
)
from modus.tools.system_probe import system_probe

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
        rels: list[str] = []

        if "**" in pattern:
            # Recursive glob: use the bounded walker so ``**`` on a huge tree
            # cannot peg the CPU, then fnmatch the pattern against each file.
            import fnmatch

            pattern_str = str(base / pattern)
            truncated = {"hit": False}

            def _mark_truncated() -> None:
                truncated["hit"] = True

            async for path in _iter_bounded_files(
                base, _scan_cap(context), on_truncate=_mark_truncated,
            ):
                if fnmatch.fnmatch(str(path), pattern_str):
                    rels.append(str(path.relative_to(base)))
                    if len(rels) >= limit:
                        break
            if truncated["hit"]:
                cap = _scan_cap(context)
                rels.append(f"... [扫描达上限 {cap} 文件，结果不完整]")
        else:
            raw_matches = glob_module.glob(str(base / pattern), recursive=False)
            guard = PathGuard()
            for match in sorted(raw_matches):
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
        stat = path.stat()
        if stat.st_size > 1_000_000:
            # Match grep/search_code's size boundary: refuse a full read of a
            # multi-MB file instead of buffering it entirely in memory.  The
            # model can still grep/search_code into large files.
            return ToolResult(
                f"read_file refused: file is {stat.st_size:,} bytes (>1MB); "
                "use grep or search_code to probe it.",
                is_error=True,
                disclosure={"local_bytes_read": 0, "model_bytes_sent": 0},
            )
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = content[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{idx + offset}: {line}" for idx, line in enumerate(selected))
        # The file's bytes are never re-read; the decoded lines above already
        # crossed the workspace boundary, so count disclosure from them.
        selected_text = "\n".join(selected)
        return ToolResult(
            numbered,
            disclosure={
                "local_bytes_read": stat.st_size,
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
        truncated = {"hit": False}

        def _mark_truncated() -> None:
            truncated["hit"] = True

        async def _all_candidates():
            if start.is_file():
                yield start
            else:
                async for path in _iter_bounded_files(
                    start, _scan_cap(context), on_truncate=_mark_truncated,
                ):
                    yield path

        async for file_path in _all_candidates():
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line_number, line in enumerate(lines, start=1):
                found = bool(compiled.search(line)) if compiled else pattern in line
                if found:
                    matches.append(f"{_display_path(file_path, root)}:{line_number}: {line.strip()}")
                    if len(matches) >= limit:
                        return ToolResult("\n".join(matches))
        if truncated["hit"]:
            cap = _scan_cap(context)
            footer = f"\n... [扫描达上限 {cap} 文件，结果不完整]"
        else:
            footer = ""
        return ToolResult("\n".join(matches) + footer if matches else (f"(no matches){footer}"))
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

_SKIP_DIRS = frozenset({
    ".git", ".venv", "node_modules", "dist", "build", "__pycache__",
    ".next", ".cache", "target", "Pods", "vendor", ".tox",
})

# Hard cap on files scanned by any recursive walker, so a single tool call on a
# huge tree cannot pin a core for minutes (see audit MF3).  Raised from 5k to
# cover real source trees (Modus itself has ~4k non-venv files); the
# ``tools.max_scan_files`` config overrides.
_MAX_SCAN_FILES = 20_000

# Hard cap on shell output buffered per call.  A command that streams output
# without exiting is killed at this boundary instead of OOMing the process.
_STREAM_OUTPUT_CAP = 50 * 1024 * 1024

# Environment variable names that must never leak into a shell subprocess.
_ENV_SECRET_MARKERS = ("key", "token", "secret", "password", "credential",
                       "auth", "bearer", "session")


def _safe_shell_env() -> dict[str, str]:
    """Return a sanitized environment for shell subprocesses.

    A command the model runs must not inherit every host secret (API keys,
    tokens, credentials).  We keep the base path/locale machinery but drop any
    variable whose name carries a secret marker, so ``env`` / ``printenv`` can
    never exfiltrate ``MODUS_API_KEY``, ``OPENAI_API_KEY`` etc. to the model.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.lower() for marker in _ENV_SECRET_MARKERS)
    }


def _scan_cap(context: ToolContext) -> int:
    """Read the configured file-scan cap, clamped to a sane range."""
    value = getattr(getattr(context.config, "tools", None), "max_scan_files", _MAX_SCAN_FILES)
    return max(100, min(int(value or _MAX_SCAN_FILES), 200_000))


def _skip_file(path: Path) -> bool:
    if any(part in _SKIP_DIRS for part in path.parts):
        return True
    return path.stat().st_size > 1_000_000


async def _iter_bounded_files(start: Path, cap: int = _MAX_SCAN_FILES, *, on_truncate=None):
    """Yield files under ``start`` with pruning and a hard scan cap.

    Asynchronous generator: yields to the event loop every 128 entries so the
    tool timeout and run-cancel can interrupt a large scan, prunes
    ``_SKIP_DIRS`` during traversal (never descends into them), and stops after
    ``cap`` files so a runaway directory cannot peg the CPU.

    ``on_truncate`` is invoked (with no args) when the scan stops because the
    cap was reached, so the caller can disclose that the result is incomplete —
    a silent cap would read as a complete scan and mislead the model.
    """
    scanned = 0
    visited = 0
    stack = [start]
    guard = PathGuard()
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    visited += 1
                    if visited % 128 == 0:
                        await asyncio.sleep(0)
                    name = entry.name
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir:
                        if name in _SKIP_DIRS:
                            continue
                        stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    try:
                        resolved = guard.validate(path)
                    except PathPolicyError:
                        continue
                    if _skip_file(resolved):
                        continue
                    yield resolved
                    scanned += 1
                    if scanned >= cap:
                        if on_truncate is not None:
                            on_truncate()
                        return
        except (OSError, PermissionError):
            continue


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
            capabilities=("filesystem",),
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
            capabilities=("filesystem",),
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
            capabilities=("filesystem",),
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
            capabilities=("filesystem",),
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
            capabilities=("filesystem",),
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
            capabilities=("filesystem",),
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
            capabilities=("memory",),
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
            capabilities=("memory",),
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
                    "word_boundary": {"type": "boolean", "description": "Match whole identifiers only (find_me misses find_me_again)"},
                    "use_index": {"type": "boolean", "description": "Use the persisted code index instead of a live scan (default false; falls back to scan if no index exists)"},
                    "context_lines": {"type": "number", "description": "Lines around each match (0-3)"},
                    "limit": {"type": "number", "description": "Maximum matches (1-1000)"},
                },
                ["query"],
            ),
            required_keys=["query"],
            handler=search_code,
            data_disclosure="workspace_content",
            capabilities=("filesystem",),
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
            capabilities=("filesystem",),
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
            capabilities=("exec",),
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
            capabilities=("exec",),
        ),
        Tool(
            name="revert_turn",
            description=(
                "List side-git snapshots (action=list) or restore the workspace "
                "to a pre-turn snapshot (action=restore, optional commit_id). "
                "Restores the workspace tree to a state captured before this "
                "run's mutations began. Never touches the user's own git history."
            ),
            parameters=object_schema(
                {
                    "action": {"type": "string", "description": "list or restore"},
                    "commit_id": {"type": "string", "description": "Snapshot commit (restore only)"},
                },
                ["action"],
            ),
            required_keys=["action"],
            handler=revert_turn,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            capabilities=("filesystem", "exec"),
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
            capabilities=("network",),
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
            capabilities=("network",),
        ),
        Tool(
            name="system_probe",
            description=(
                "Return a bounded JSON snapshot of the host: cpu/load, memory, "
                "disk usage, top processes, and log directory summary. "
                "Read-only lens — never reads file or log contents."
            ),
            parameters=object_schema(
                {
                    "max_processes": {"type": "number", "description": "Max process rows (default 20)"},
                    "include_logs": {"type": "boolean", "description": "Include log directory summary (default true)"},
                },
                [],
            ),
            handler=system_probe,
            data_disclosure="none",
            capabilities=("filesystem",),
            timeout=15.0,
        ),
        Tool(
            name="spawn_process",
            description=(
                "Launch a command as a background process detached from this "
                "call, logging stdout/stderr to Modus's private directory. "
                "Returns a process_id for list/tail/kill_process. Use for "
                "dev servers, long builds, or anything that outlives bash."
            ),
            parameters=object_schema(
                {
                    "command": {"type": "string", "description": "Shell command"},
                    "cwd": {"type": "string", "description": "Working directory"},
                    "task_name": {"type": "string", "description": "Optional task name for the process registry"},
                    "description": {"type": "string", "description": "Optional human-readable task description"},
                },
                ["command"],
            ),
            required_keys=["command"],
            handler=spawn_process,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            capabilities=("exec",),
        ),
        Tool(
            name="list_processes",
            description=(
                "List background processes spawned by spawn_process with live "
                "status (running/stopped/orphaned/exited). Read-only."
            ),
            parameters=object_schema(
                {"limit": {"type": "number", "description": "Max rows (default 20)"}},
                [],
            ),
            handler=list_processes,
            data_disclosure="none",
            capabilities=("exec",),
        ),
        Tool(
            name="tail_process",
            description=(
                "Read a bounded tail of a spawned process's stdout (or stderr) "
                "log. Non-blocking."
            ),
            parameters=object_schema(
                {
                    "process_id": {"type": "string"},
                    "stream": {"type": "string", "description": "stdout or stderr"},
                },
                ["process_id"],
            ),
            required_keys=["process_id"],
            handler=tail_process,
            data_disclosure="none",
            capabilities=("exec",),
        ),
        Tool(
            name="kill_process",
            description=(
                "Terminate a spawned process group and mark its registry entry "
                "as exited."
            ),
            parameters=object_schema(
                {"process_id": {"type": "string"}},
                ["process_id"],
            ),
            required_keys=["process_id"],
            handler=kill_process,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            capabilities=("exec",),
        ),
        Tool(
            name="rebuild_code_index",
            description=(
                "Build or refresh the persistent code-search index for the "
                "workspace, so search_code can use use_index for fast queries. "
                "Read-only (writes only Modus's private index dir)."
            ),
            parameters=object_schema({}, []),
            handler=rebuild_code_index,
            data_disclosure="none",
            capabilities=("filesystem",),
        ),
        # Browser tools: shared headless Chrome driven by the agent.  All are
        # non-concurrency-safe because they share one page; browser_eval is the
        # only approval-gated one (arbitrary JS).
        Tool(
            name="browser_navigate",
            description=(
                "Navigate the shared headless browser to a URL. Use after "
                "spawn_process started a dev server: pass its localhost URL. "
                "Sets metadata.preview_url so the desktop preview iframe opens "
                "the same page."
            ),
            parameters=object_schema(
                {"url": {"type": "string", "description": "http/https URL to open"}},
                ["url"],
            ),
            required_keys=["url"],
            handler=browser_navigate,
            is_concurrency_safe=False,
            capabilities=("exec", "network"),
        ),
        Tool(
            name="browser_state",
            description="Return the current page URL, title, visible links and inputs.",
            parameters=object_schema({}, []),
            handler=browser_state,
            is_concurrency_safe=False,
            capabilities=("exec", "network"),
        ),
        Tool(
            name="browser_extract",
            description="Extract text or an attribute from all elements matching a CSS selector.",
            parameters=object_schema(
                {
                    "selector": {"type": "string"},
                    "attr": {"type": "string", "description": "Attribute to extract (default: innerText)"},
                    "limit": {"type": "number", "description": "Max results (1-100)"},
                },
                ["selector"],
            ),
            required_keys=["selector"],
            handler=browser_extract,
            is_concurrency_safe=False,
            capabilities=("exec", "network"),
        ),
        Tool(
            name="browser_screenshot",
            description="Capture a viewport screenshot of the current page as an artifact.",
            parameters=object_schema({}, []),
            handler=browser_screenshot,
            is_concurrency_safe=False,
            capabilities=("exec", "network"),
        ),
        Tool(
            name="browser_click",
            description="Click the first element matching a CSS selector.",
            parameters=object_schema(
                {"selector": {"type": "string"}},
                ["selector"],
            ),
            required_keys=["selector"],
            handler=browser_click,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            capabilities=("exec", "network"),
        ),
        Tool(
            name="browser_type",
            description="Type text into the first element matching a CSS selector.",
            parameters=object_schema(
                {"selector": {"type": "string"}, "text": {"type": "string"}},
                ["selector", "text"],
            ),
            required_keys=["selector", "text"],
            handler=browser_type,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            capabilities=("exec", "network"),
        ),
        Tool(
            name="browser_eval",
            description=(
                "Evaluate a JS expression in the page and return the result. "
                "Arbitrary code execution — requires approval."
            ),
            parameters=object_schema(
                {"js": {"type": "string", "description": "JS expression (max 4096 chars)"}},
                ["js"],
            ),
            required_keys=["js"],
            handler=browser_eval,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            capabilities=("exec", "network"),
        ),
        Tool(
            name="browser_close",
            description="Close the shared headless browser and release its resources.",
            parameters=object_schema({}, []),
            handler=browser_close,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            capabilities=("exec", "network"),
        ),
        # Office document tools: binary formats (xlsx/docx/pptx) that the text
        # tools cannot read.  Read tools auto-ALLOW and disclose workspace
        # content; write tools go through the approval gate.
        Tool(
            name="excel_analyze",
            description=(
                "Analyze an Excel workbook: sheets, dimensions, header, preview "
                "rows, and numeric stats. Read-only."
            ),
            parameters=object_schema(
                {"path": {"type": "string", "description": "Path to .xlsx"}},
                ["path"],
            ),
            required_keys=["path"],
            handler=excel_analyze,
            data_disclosure="workspace_content",
            capabilities=("filesystem",),
        ),
        Tool(
            name="excel_query",
            description=(
                "Filter Excel rows by a column value (equals) or numeric range "
                "(gt). Read-only."
            ),
            parameters=object_schema(
                {
                    "path": {"type": "string"},
                    "column": {"type": "string", "description": "Column name"},
                    "equals": {"type": "string", "description": "Exact match value"},
                    "gt": {"type": "number", "description": "Rows where column > gt"},
                    "limit": {"type": "number", "description": "Max rows (1-200)"},
                },
                ["path", "column"],
            ),
            required_keys=["path", "column"],
            handler=excel_query,
            data_disclosure="workspace_content",
            capabilities=("filesystem",),
        ),
        Tool(
            name="word_extract",
            description="Extract paragraphs, headings and table text from a .docx. Read-only.",
            parameters=object_schema(
                {"path": {"type": "string", "description": "Path to .docx"}},
                ["path"],
            ),
            required_keys=["path"],
            handler=word_extract,
            data_disclosure="workspace_content",
            capabilities=("filesystem",),
        ),
        Tool(
            name="word_edit",
            description="Replace an exact text occurrence across a .docx's paragraphs.",
            parameters=object_schema(
                {
                    "path": {"type": "string"},
                    "find": {"type": "string", "description": "Exact text to find"},
                    "replace": {"type": "string", "description": "Replacement text"},
                },
                ["path", "find", "replace"],
            ),
            required_keys=["path", "find", "replace"],
            handler=word_edit,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            capabilities=("filesystem",),
        ),
        Tool(
            name="pptx_extract",
            description="Extract slide titles and text bodies from a .pptx. Read-only.",
            parameters=object_schema(
                {"path": {"type": "string", "description": "Path to .pptx"}},
                ["path"],
            ),
            required_keys=["path"],
            handler=pptx_extract,
            data_disclosure="workspace_content",
            capabilities=("filesystem",),
        ),
        Tool(
            name="pptx_build",
            description="Build a simple .pptx from a list of {title, body} slide specs.",
            parameters=object_schema(
                {
                    "path": {"type": "string", "description": "Output .pptx path"},
                    "slides": {"type": "array", "description": "List of {title, body} dicts"},
                },
                ["path", "slides"],
            ),
            required_keys=["path", "slides"],
            handler=pptx_build,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            capabilities=("filesystem",),
        ),
        Tool(
            name="office_exec",
            description=(
                "Run a sandboxed Python script that reads or writes ONE Office "
                "file via openpyxl/python-docx/python-pptx.  The script sees "
                "the target path as PATH; import a writing library (docx/pptx) "
                "or call .save() to make this approval-gated.  Use for analysis "
                "(aggregate/filter/group) and formatting the fixed tools cannot "
                "do."
            ),
            parameters=object_schema(
                {
                    "path": {"type": "string", "description": "Target .xlsx/.docx/.pptx"},
                    "script": {"type": "string", "description": "Python script (max 4000 chars)"},
                },
                ["path", "script"],
            ),
            required_keys=["path", "script"],
            handler=office_exec,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            requires_approval=True,
            capabilities=("filesystem", "exec"),
        ),
        # System control tools (Phase A5): ports read-only, service restart T4.
        Tool(
            name="port_list",
            description=(
                "List listening TCP/UDP ports and the owning process. Read-only."
            ),
            parameters=object_schema(
                {"port": {"type": "string", "description": "Optional specific port"}},
                [],
            ),
            handler=port_list,
            capabilities=("exec",),
        ),
        Tool(
            name="service_status",
            description="Inspect a system service's status (read-only).",
            parameters=object_schema(
                {"service": {"type": "string", "description": "Service name"}},
                ["service"],
            ),
            required_keys=["service"],
            handler=service_status,
            capabilities=("exec",),
        ),
        Tool(
            name="service_restart",
            description=(
                "Restart a system service. Destructive (T4) — requires approval."
            ),
            parameters=object_schema(
                {"service": {"type": "string", "description": "Service name"}},
                ["service"],
            ),
            required_keys=["service"],
            handler=service_restart,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            capabilities=("exec",),
        ),
    ] + _clone_tools()


def _clone_tools() -> list[Tool]:
    """Host-level git tools: clone + remote/branch/credential management.

    Mutating operations (push / pull / merge / credential write) are
    approval-gated; read-only listings are free.
    """
    from modus.tools.git_tools import (
        git_blame, git_branch_checkout, git_branch_create, git_branch_list,
        git_branch_merge, git_clone, git_credential_clear,
        git_credential_set, git_fetch, git_log, git_pull, git_push,
        git_remote_add, git_remote_list, git_remote_remove, git_show,
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
                "git_log": git_log,
                "git_show": git_show,
                "git_blame": git_blame,
            }[name],
            is_read_only=read_only,
            is_concurrency_safe=False,
            danger_level="safe" if read_only else ("high" if high else "medium"),
            requires_approval=not read_only,
            # git shelled out + host network + (for writes) workspace mutation
            capabilities=("filesystem", "exec", "network"),
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
        _tool("git_fetch", "Fetch references from a remote (mutates the local ref store).",
              {"remote": {"type": "string", "description": "Remote name (optional)"}},
              [], high=True, read_only=False),
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
        # Read-only history tools: recent commits, one commit's diff, and
        # per-line attribution.  Local-only (no network), bounded output.
        _tool("git_log", "List recent commits (oneline + decoration).",
              {"count": {"type": "number", "description": "Max commits (default 20)"},
               "path": {"type": "string", "description": "Restrict to a path"}},
              [], read_only=True),
        _tool("git_show", "Show one commit's message, files and bounded diff.",
              {"rev": {"type": "string", "description": "Commit hash or ref"},
               "stat": {"type": "string", "description": "'stat' for a compact stat-only view"}},
              ["rev"], read_only=True),
        _tool("git_blame", "Per-line attribution for a file (who/when changed each line).",
              {"path": {"type": "string", "description": "File path"},
               "rev": {"type": "string", "description": "Optional revision"}},
              ["path"], read_only=True),
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
        word_boundary = bool(payload.get("word_boundary", False))
        context_lines = max(0, min(int(payload.get("context_lines") or 0), 3))
        try:
            compiled = re.compile(query, 0 if case_sensitive else re.IGNORECASE) if use_regex else None
        except re.error as exc:
            return ToolResult(f"invalid regex: {exc}", is_error=True)
        # Exact-symbol mode: only match whole words/identifiers, so ``find_me``
        # does not hit ``find_me_again`` and ``user`` does not hit ``User``.
        # For a literal query this becomes a word-boundary regex; a regex query
        # with word_boundary is wrapped with the same identifier fences.
        if word_boundary:
            if use_regex:
                try:
                    compiled = re.compile(
                        rf"(?<!\w)(?:{query})(?!\w)",
                        0 if case_sensitive else re.IGNORECASE,
                    )
                except re.error as exc:
                    return ToolResult(f"invalid regex: {exc}", is_error=True)
            else:
                escaped = re.escape(query)
                compiled = re.compile(
                    rf"(?<!\w){escaped}(?!\w)",
                    0 if case_sensitive else re.IGNORECASE,
                )

        root = Path(context.workspace_root or context.cwd).resolve()
        matches: list[str] = []
        truncated = {"hit": False}

        def _mark_truncated() -> None:
            truncated["hit"] = True

        # Indexed fast path: when ``use_index`` is set and a persisted index
        # exists for this root, narrow candidates to indexed lines instead of
        # re-walking the filesystem.  Falls back to the scan when no index
        # exists (and reports that the result was scan-based).
        use_index = bool(payload.get("use_index", False))
        index_hit = False
        if use_index and not start.is_file():
            from modus.tools.code_index import CodeIndex

            from modus.paths import data_dir

            idx = CodeIndex(root, data_dir() / "code_index")
            if idx.exists():
                index_hit = True
                candidates = idx.query(
                    path_prefix=str(start.relative_to(root)) if start != root else "",
                    limit=500_000,
                )

                def _match(line: str) -> bool:
                    return bool(compiled.search(line)) if compiled else (
                        query in line if case_sensitive else query.casefold() in line.casefold()
                    )

                for row in candidates:
                    if not _match(row.content):
                        continue
                    relative = row.path
                    if context_lines == 0:
                        matches.append(f"{relative}:{row.line}: {row.content.strip()}")
                    else:
                        matches.append(f"{relative}:{row.line}:\n{row.content}")
                    if len(matches) >= limit:
                        return ToolResult(
                            "\n".join(matches) + f"\n... [limited to {limit} matches]"
                        )
                if not matches:
                    return ToolResult("(no matches)")
                return ToolResult("\n".join(matches))

        async def _all_candidates():
            if start.is_file():
                yield start
            else:
                async for path in _iter_bounded_files(
                    start, _scan_cap(context), on_truncate=_mark_truncated,
                ):
                    yield path

        async for resolved in _all_candidates():
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
        if truncated["hit"]:
            cap = _scan_cap(context)
            footer = f"\n... [扫描达上限 {cap} 文件，结果不完整]"
        else:
            footer = ""
        return ToolResult("\n".join(matches) + footer if matches else (f"(no matches){footer}"))
    except PathPolicyError as exc:
        return _path_error(exc)


async def rebuild_code_index(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Build or refresh the persistent code-search index for the workspace."""
    from modus.tools.code_index import CodeIndex

    from modus.paths import data_dir

    root = Path(context.workspace_root or context.cwd).resolve()
    idx = CodeIndex(root, data_dir() / "code_index")

    async def _walker():
        async for path in _iter_bounded_files(root, _scan_cap(context)):
            yield path

    paths = [p async for p in _walker()]
    try:
        lines = idx.rebuild(paths, cap=_scan_cap(context))
    except Exception as exc:
        return ToolResult(f"rebuild_code_index failed: {exc}", is_error=True)
    stats = idx.stats()
    return ToolResult(
        f"Indexed {stats['files']} files, {lines} lines for {root}",
        metadata={"operation": "rebuild_code_index", "files": stats["files"], "lines": lines},
    )

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
        env=_safe_shell_env(),
        preexec_fn=rlimit_preexec(context.config),
        **_process_group_kwargs(),
    )
    cancelled = False
    try:
        stdout, stderr, truncated, timed_out = await _capture_stream_output(
            proc, context.cancel_event, timeout,
        )
        if context.cancel_event is not None and context.cancel_event.is_set():
            cancelled = True
        elif truncated:
            return ToolResult(
                "Command output exceeded the cap and was terminated.",
                is_error=True,
                disclosure={"output_capped": True},
            )
        elif timed_out:
            return ToolResult(f"Command timed out after {timeout:.0f}s", is_error=True)
    except asyncio.CancelledError:
        # ``Tool.execute`` may cancel a handler on its own I/O deadline.  That
        # must have the same process-tree cleanup guarantee as a run cancel.
        _terminate_process_group(proc)
        raise
    if cancelled:
        return ToolResult("Command cancelled.", is_error=True)
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
        env=_safe_shell_env(),
        preexec_fn=rlimit_preexec(context.config),
        **_process_group_kwargs(),
    )
    cancel_task = (
        asyncio.create_task(context.cancel_event.wait())
        if context.cancel_event is not None else None
    )
    status = "failed"
    try:
        stdout, stderr, truncated, timed_out = await _capture_stream_output(
            proc, context.cancel_event, timeout,
        )
        if context.cancel_event is not None and context.cancel_event.is_set():
            status = "cancelled"
        elif truncated:
            status = "failed"
        elif timed_out:
            status = "timed_out"
        else:
            status = "passed" if proc.returncode == 0 else "failed"
    except asyncio.CancelledError:
        _terminate_process_group(proc)
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


async def _capture_stream_output(
    proc: asyncio.subprocess.Process, cancel_event: asyncio.Event | None,
    timeout: float,
    cap: int = _STREAM_OUTPUT_CAP,
) -> tuple[bytes, bytes, bool]:
    """Capture stdout+stderr incrementally, killing the tree on overflow.

    Unlike ``proc.communicate()`` (which buffers everything until exit, so a
    streaming command can OOM the process), this reads in chunks and stops the
    moment the combined output exceeds ``cap``, killing the process group and
    marking the result truncated.  Returns (stdout, stderr, truncated, timed_out).
    """
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    total = 0
    truncated = False

    async def drain(reader: asyncio.StreamReader | None, sink: list[bytes]) -> bool:
        nonlocal total, truncated
        if reader is None:
            return True
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return True
            total += len(chunk)
            if total > cap:
                truncated = True
                return False
            sink.append(chunk)

    stdout_stream = getattr(proc, "stdout", None)
    stderr_stream = getattr(proc, "stderr", None)
    drain_task = asyncio.gather(
        drain(stdout_stream, stdout_chunks),
        drain(stderr_stream, stderr_chunks),
        return_exceptions=True,
    )
    cancel_task = (
        asyncio.create_task(cancel_event.wait())
        if cancel_event is not None else None
    )
    waiters = {drain_task, *([cancel_task] if cancel_task is not None else [])}
    try:
        done, _pending = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
        if cancel_task is not None:
            await asyncio.gather(cancel_task, return_exceptions=True)
    # Either the streams ended cleanly, the timeout fired, or we hit the cap.
    timed_out = False
    cancelled = cancel_task is not None and cancel_task in done
    if truncated or cancelled or drain_task not in done or getattr(proc, "returncode", None) is None:
        if drain_task not in done:
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
        if not truncated and not cancelled:
            timed_out = True
        _terminate_process_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except (TimeoutError, ProcessLookupError):
            pass
    return b"".join(stdout_chunks), b"".join(stderr_chunks), truncated, timed_out


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

async def revert_turn(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    """Restore the workspace to a side-git pre-turn snapshot.

    ``action`` is ``list`` (default) or ``restore``.  Restore requires a
    ``commit_id``; when omitted, the most recent pre-turn snapshot is used.
    The side repository lives under Modus's own data directory and never
    touches the user's git history.
    """
    from modus.tools.snapshot import list_snapshots, restore_snapshot

    workspace_root = context.workspace_root or context.cwd
    action = str(payload.get("action") or "list").strip().lower()
    if action == "list":
        snaps = list_snapshots(workspace_root, limit=10)
        if not snaps:
            return ToolResult("无可用快照。")
        lines = [f"{s.commit_id[:12]}  {s.phase}  {s.summary}" for s in snaps]
        return ToolResult("可用快照（最近在前）：\n" + "\n".join(lines))
    if action == "restore":
        commit_id = str(payload.get("commit_id") or "").strip()
        if not commit_id:
            snaps = list_snapshots(workspace_root, limit=1)
            if not snaps:
                return ToolResult("无可用快照，无法恢复。", is_error=True)
            commit_id = snaps[0].commit_id
        restored, removed = restore_snapshot(workspace_root, commit_id)
        return ToolResult(
            f"已恢复到快照 {commit_id[:12]}（恢复 {restored} 个文件，移除 {removed} 个）。",
            display_summary="已恢复工作区快照",
            metadata={"operation": "revert_turn", "commit_id": commit_id,
                      "restored": restored, "removed": removed},
        )
    return ToolResult(f"revert_turn action 必须是 list 或 restore，收到：{action}", is_error=True)

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
