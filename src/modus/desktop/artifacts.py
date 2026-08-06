"""Run-scoped artifact storage for multi-agent context exchange.

Artifacts live under Modus's private data directory, never in the user's
project. SQLite stores their identity and integrity metadata; event payloads
expose only logical IDs, not host filesystem paths.
"""
from __future__ import annotations

import hashlib
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
