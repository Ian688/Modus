"""Run-scoped artifact storage for multi-agent context exchange.

Artifacts live under Modus's private data directory, never in the user's
project. SQLite stores their identity and integrity metadata; event payloads
expose only logical IDs, not host filesystem paths.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from modus.redact import redact_text


_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_ARTIFACT_BYTES = 2_000_000


def _segment(value: str, fallback: str) -> str:
    cleaned = _SAFE_SEGMENT.sub("-", str(value or "").strip()).strip(".-")
    return (cleaned or fallback)[:96]


def artifact_root() -> Path:
    # Resolve DB_DIR at call time so tests and embedders can isolate all
    # desktop persistence by replacing one boundary.
    from modus.desktop import db

    return Path(db.DB_DIR) / "artifacts"


def write_artifact(
    *, session_id: str, run_id: str, kind: str, title: str, content: str,
    task_id: str | None = None, summary: str = "",
) -> dict[str, Any]:
    """Atomically write redacted UTF-8 content and register its metadata."""
    from modus.desktop.db import create_artifact_record, get_run, get_session

    if get_session(session_id) is None or get_run(run_id) is None:
        raise ValueError("artifact requires a persisted session and run")
    safe_content = redact_text(str(content))
    stored_content = safe_content if not safe_content or safe_content.endswith("\n") else safe_content + "\n"
    encoded = stored_content.encode("utf-8")
    if len(encoded) > _MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact exceeds {_MAX_ARTIFACT_BYTES} bytes")

    artifact_id = f"art_{uuid4().hex}"
    safe_kind = _segment(kind, "artifact")
    directory = artifact_root() / _segment(session_id, "session") / _segment(run_id, "run")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{artifact_id}-{safe_kind}.md"
    fd, temporary = tempfile.mkstemp(prefix=".artifact-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(stored_content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

    return create_artifact_record(
        artifact_id=artifact_id, run_id=run_id, session_id=session_id,
        task_id=task_id, kind=safe_kind, title=str(title)[:240],
        storage_path=str(path), content_hash=hashlib.sha256(encoded).hexdigest(),
        size_bytes=len(encoded), summary=redact_text(str(summary))[:1000],
    )


def read_artifact(artifact_id: str, *, session_id: str) -> str:
    """Read and verify one artifact belonging to the requesting session."""
    from modus.desktop.db import get_artifact

    artifact = get_artifact(artifact_id)
    if artifact is None or artifact.get("session_id") != session_id:
        raise ValueError("artifact not found")
    root = artifact_root().resolve()
    path = Path(str(artifact["storage_path"])).resolve()
    if path != root and root not in path.parents:
        raise ValueError("artifact path escaped the private store")
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise ValueError("artifact content unavailable") from exc
    recorded_size = artifact.get("size_bytes")
    if recorded_size is None or len(encoded) != int(recorded_size):
        raise ValueError("artifact integrity check failed")
    if hashlib.sha256(encoded).hexdigest() != str(artifact.get("content_hash") or ""):
        raise ValueError("artifact integrity check failed")
    try:
        return encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("artifact content is not UTF-8") from exc


def read_artifact_public(
    artifact_id: str, *, session_id: str, max_bytes: int = 200_000,
) -> dict[str, Any]:
    """Return bounded redacted content and metadata without its storage path."""
    from modus.desktop.db import get_artifact

    artifact = get_artifact(artifact_id)
    if artifact is None or artifact.get("session_id") != session_id:
        raise ValueError("artifact not found")
    if int(artifact.get("size_bytes") or 0) > max_bytes:
        raise ValueError("artifact is too large to display")
    return {**public_artifact(artifact), "content": read_artifact(artifact_id, session_id=session_id)}


def public_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Browser-safe artifact metadata (never expose storage_path or hashes)."""
    return {
        key: artifact.get(key) for key in (
            "artifact_id", "run_id", "task_id", "kind", "title",
            "size_bytes", "summary", "created_at",
        )
    }


# ── C3 result bridge: oversized tool results + content-addressed cache ─────
#
# Wave2 C3 (context economics).  Tool results larger than
# ``DEFAULT_PERSIST_THRESHOLD_BYTES`` never enter the conversation: they are
# written under Modus's private ``artifacts/`` directory and the model receives
# a compact handle ``{path, sha256, size, preview}``.  Read-only results for the
# same ``(tool, args)`` are content-addressed by a SHA-256 cache key and reused
# (after an integrity check of the persisted file).  The cache is invalidated by
# any write tool, so a mutation is always followed by a fresh read.

DEFAULT_PERSIST_THRESHOLD_BYTES = 100 * 1024
_CACHE_SUBDIR = "result-cache"
_CACHE_STAMP = ".c3-cache-v1"

# Write tools whose execution can change what a later read sees.  A mutating
# invocation invalidates the result cache so the next identical read re-executes
# instead of serving a stale handle.  Mirrors the compressor's list plus the
# process/network/approval-gated tools that mutate workspace or host state.
_MUTATING_TOOLS = frozenset({
    "write_file", "edit_file", "patch",
    "spawn_process", "kill_process", "restart_process",
    "git_clone", "git_pull", "git_push", "git_branch_create",
    "git_branch_checkout", "git_branch_merge", "git_remote_add",
    "git_remote_remove", "git_credential_set", "git_credential_clear",
    "service_restart", "office_exec", "word_edit", "pptx_build",
    "browser_navigate", "browser_click", "browser_type", "browser_eval",
})

# Arguments that must never influence the content-addressed cache key.  Only
# secret-bearing names are stripped: everything else (pattern, path, regex,
# limit, context lines) changes the result content and must bind the key.  A
# non-secret knob left out of the key would let one request silently reuse
# another request's different-sized result.
def _cache_relevant_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Canonicalized args for hashing: sorted keys, secrets stripped."""
    normalized: dict[str, Any] = {}
    for key, value in dict(args or {}).items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in ("key", "token", "secret", "password", "auth")):
            continue
        normalized[key] = value
    return normalized


def cache_key(tool_name: str, args: dict[str, Any]) -> str:
    """Content-addressed cache key for a read-only ``(tool, args)`` pair.

    ``args`` is canonicalized before hashing: keys are sorted, values are
    serialized with stable separators, and secret-bearing keys are stripped.
    The SHA-256 digest binds the tool name and the canonicalized arguments, so
    the same query always produces the same key and a different query never
    collides with it.
    """
    serialized = json.dumps(
        _cache_relevant_args(args), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(f"{tool_name}|{serialized}".encode("utf-8")).hexdigest()


def _cache_stamp_file(root: Path) -> Path:
    """Return the version-stamp file that guards the cache against eviction."""
    return root / _CACHE_SUBDIR / _CACHE_STAMP


def cached_handle(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Return the persisted handle for ``(tool_name, args)`` if it is intact.

    The cache lives under Modus's private ``artifacts/result-cache`` directory
    and is versioned by a stamp file: a data-dir eviction removes the stamp, so
    a rebuilt directory never serves a stale handle.  A handle whose file is
    missing or fails the recorded SHA-256 is treated as a cache miss.
    """
    root = artifact_root()
    stamp = _cache_stamp_file(root)
    if not stamp.is_file():
        return None
    try:
        with open(stamp, encoding="utf-8") as handle:
            stamp_value = handle.read().strip()
    except OSError:
        return None
    if stamp_value != _CACHE_STAMP.lstrip("."):
        return None
    entry_path = root / _CACHE_SUBDIR / f"{cache_key(tool_name, args)}.json"
    if not entry_path.is_file():
        return None
    try:
        with open(entry_path, encoding="utf-8") as handle:
            entry = json.load(handle)
    except (OSError, ValueError):
        return None
    content_path = Path(str(entry.get("path") or ""))
    if not content_path.is_file():
        return None
    if not artifact_is_intact(content_path, str(entry.get("sha256") or "")):
        return None
    entry["cached"] = True
    return entry


def record_cache(tool_name: str, args: dict[str, Any], handle: dict[str, Any]) -> None:
    """Persist a content-addressed handle so the same read can be reused."""
    key = cache_key(tool_name, args)
    directory = artifact_root() / _CACHE_SUBDIR
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = _cache_stamp_file(artifact_root())
        if not stamp.exists():
            fd, temp_stamp = tempfile.mkstemp(prefix=".c3-stamp-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    out.write(_CACHE_STAMP.lstrip("."))
                    out.flush()
                    os.fsync(out.fileno())
                os.chmod(temp_stamp, 0o600)
                os.replace(temp_stamp, stamp)
            except Exception:
                try:
                    os.unlink(temp_stamp)
                except OSError:
                    pass
                raise
        entry_path = directory / f"{key}.json"
        temp_fd, temp_entry = tempfile.mkstemp(prefix=".cache-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as out:
                json.dump(handle, out, ensure_ascii=False, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temp_entry, 0o600)
            os.replace(temp_entry, entry_path)
        except Exception:
            try:
                os.unlink(temp_entry)
            except OSError:
                pass
            raise
    except OSError:
        # A failed cache write never fails the tool result itself.
        return


def invalidate_cache(tool_name: str, args: dict[str, Any] | None = None) -> None:
    """Invalidate the result cache after a mutating tool runs.

    Without ``args`` this clears the whole cache (a global reset, e.g. after a
    workspace rewrite or a best-effort fallback).  With ``args`` it clears only
    the entries whose ``tool`` matches and whose cached args contain every
    supplied argument with an equal value — read results the mutating call may
    have rendered stale (e.g. ``write_file(path=X)`` invalidating
    ``grep(path=X, ...)``).  Missing or corrupt entries are ignored.
    """
    directory = artifact_root() / _CACHE_SUBDIR
    if args is None:
        try:
            for entry_path in directory.glob("*"):
                if entry_path.is_file():
                    try:
                        entry_path.unlink()
                    except OSError:
                        pass
            return
        except OSError:
            return
    for entry_path in directory.glob("*.json"):
        try:
            with open(entry_path, encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, ValueError):
            continue
        if str(entry.get("tool") or "") != tool_name:
            continue
        entry_args = dict(entry.get("args") or {})
        if all(key in entry_args and entry_args[key] == value for key, value in args.items()):
            try:
                entry_path.unlink()
            except OSError:
                pass


def artifact_is_intact(path: Path | str, sha256: str) -> bool:
    """True when ``path`` exists and its bytes match the recorded SHA-256.

    Read results are only reused after this check; a file that was edited in
    place (or replaced by another writer) fails the check and forces a re-run.
    """
    try:
        encoded = Path(path).read_bytes()
    except OSError:
        return False
    if not sha256:
        return False
    return hashlib.sha256(encoded).hexdigest() == sha256


def persist_oversized(
    name: str,
    content: str,
    suffix: str = "txt",
    *,
    args: dict[str, Any] | None = None,
    cache: bool = False,
) -> dict[str, Any] | None:
    """Persist a result and return a compact handle ``{path, sha256, size, preview}``.

    Only results larger than ``DEFAULT_PERSIST_THRESHOLD_BYTES`` are persisted;
    smaller content returns ``None`` so the caller keeps the raw text.  The file
    is written atomically under Modus's private ``artifacts/result-cache``
    directory (never the user's workspace), named by its SHA-256 prefix, and its
    full digest is recorded so a consumer can verify it before reuse.

    When ``cache=True`` and ``args`` are given, the handle is stored under the
    content-addressed cache key and ``cached_handle`` will serve it on the next
    identical read.  Tool-internal calls (grep/search_code) pass ``cache=True``;
    the executor fallback passes ``cache=False`` because the tool already ran
    and there is nothing to reuse.

    ``name`` also names the handle's ``tool`` field (for cache invalidation) and
    the ``preview`` is a deterministic head/tail excerpt plus a count of the
    lines and characters withheld.
    """
    stored = redact_text(str(content))
    if not stored or not stored.endswith("\n"):
        stored += "\n"
    encoded = stored.encode("utf-8")
    if len(encoded) <= DEFAULT_PERSIST_THRESHOLD_BYTES:
        return None

    digest = hashlib.sha256(encoded).hexdigest()
    directory = artifact_root() / _CACHE_SUBDIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe_suffix = _SAFE_SEGMENT.sub("", str(suffix or "")).strip(".-")[:16] or "txt"
    path = directory / f"{digest[:16]}.{safe_suffix}"
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix=".c3-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    lines = str(content).splitlines()
    total = len(lines)
    head = lines[:6]
    tail = lines[-6:] if total > 12 else []
    # A single result line can itself be huge (a long match line); cap each
    # preview line so the handle stays compact no matter the content shape.
    preview_lines = [_preview_line(line) for line in head]
    if tail:
        preview_lines.append("…")
        preview_lines.extend(_preview_line(line) for line in tail)
    preview = "\n".join(preview_lines)
    handle = {
        "kind": "oversized-result",
        "tool": name,
        "path": str(path),
        "sha256": digest,
        "size": len(encoded),
        "preview": preview,
        "chars_total": len(str(content)),
        "lines_total": total,
        "lines_shown": len(preview_lines),
        "cached": False,
    }
    if cache and args is not None:
        handle["args"] = _cache_args_snapshot(args)
        record_cache(name, args, handle)
    return handle


def _preview_line(line: str, limit: int = 240) -> str:
    """Bound one preview line so a huge result line cannot balloon the handle."""
    if len(line) <= limit:
        return line
    return line[:limit] + "…"


def _cache_args_snapshot(args: dict[str, Any]) -> dict[str, Any]:
    """A cache-entry copy of the args, never containing secret values."""
    return _cache_relevant_args(args)
