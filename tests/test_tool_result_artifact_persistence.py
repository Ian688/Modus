"""Oversized tool results are persisted locally and the model sees a bounded payload."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from modus.config import ModusConfig
from modus.desktop import db
from modus.desktop.artifacts import read_artifact
from modus.tools.base import ToolContext
from modus.tools.builtins import bash, read_file, run_tests


@pytest.fixture
def persisted_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session(title="tool-result")
    db.create_run("run-1", session["id"], "default")
    return {"session_id": session["id"], "run_id": "run-1", "root": tmp_path}


def _context(persisted_db, workspace: Path) -> ToolContext:
    return ToolContext(
        cwd=str(workspace),
        config=ModusConfig(),
        session_id=persisted_db["session_id"],
        run_id=persisted_db["run_id"],
    )


@pytest.mark.asyncio
async def test_bash_large_output_is_persisted_and_model_sees_bounded(persisted_db, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = f'"{sys.executable}" -c "print(\'A\' * 50000)"'
    config = ModusConfig()
    config.tools.tool_result_artifact_chars = 1000

    result = await bash(
        {"command": command},
        ToolContext(
            cwd=str(workspace), config=config,
            session_id=persisted_db["session_id"], run_id=persisted_db["run_id"],
        ),
    )

    # Legacy visible content still carries the hard 20k cut.
    assert len(result.content) < 50_000
    assert "truncated" in result.content
    # The model reads a bounded head/tail payload, not the raw 50k output.
    model = result.model_text()
    assert len(model) < 50_000
    assert "characters omitted" in model  # bounded_summary marker
    # The full output is persisted and recoverable.
    assert len(result.artifacts) == 1
    artifact_id = result.artifacts[0]["artifact_id"]
    restored = read_artifact(artifact_id, session_id=persisted_db["session_id"])
    assert restored.count("A") == 50_000
    # Disclosure records the data-flow facts (50000 A's + trailing newline).
    assert result.disclosure["local_bytes_read"] == 50_001
    assert result.disclosure["truncated"] is True


@pytest.mark.asyncio
async def test_bash_without_persisted_session_degrades_to_truncation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = f'"{sys.executable}" -c "print(\'B\' * 50000)"'
    config = ModusConfig()
    config.tools.tool_result_artifact_chars = 1000

    result = await bash(
        {"command": command},
        ToolContext(cwd=str(workspace), config=config),
    )

    assert result.artifacts == []
    assert len(result.content) < 50_000
    # No persisted session means the full output is not written anywhere; the
    # visible content still carried the truncation marker.
    assert "truncated" in result.content


@pytest.mark.asyncio
async def test_run_tests_model_payload_stays_valid_verification_json(persisted_db, tmp_path, monkeypatch):
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # ~50k of output text so the artifact threshold is crossed.
    command = (
        f'"{sys.executable}" -c "print(\'3 passed, 1 warning in 0.12s\'); '
        f"print('D' * 50000)\""
    )
    config = ModusConfig()
    config.tools.tool_result_artifact_chars = 1000

    result = await run_tests(
        {"command": command},
        ToolContext(
            cwd=str(workspace), config=config,
            session_id=persisted_db["session_id"], run_id=persisted_db["run_id"],
        ),
    )

    # The legacy content remains valid verification JSON.
    content_evidence = json.loads(result.content)
    assert content_evidence["schema"] == "modus.verification.v1"
    assert content_evidence["status"] == "passed"
    # The model payload is also valid verification JSON with bounded output.
    model_evidence = json.loads(result.model_text())
    assert model_evidence["schema"] == "modus.verification.v1"
    assert model_evidence["status"] == "passed"
    assert model_evidence["counts"] == {"passed": 3, "warnings": 1}
    assert len(model_evidence["output"]) < 50_000
    assert "characters omitted" in model_evidence["output"]
    # Full output persisted.
    assert len(result.artifacts) == 1
    restored = read_artifact(result.artifacts[0]["artifact_id"], session_id=persisted_db["session_id"])
    assert restored.count("D") == 50_000
    assert result.disclosure["raw_content_sent"] is False


@pytest.mark.asyncio
async def test_read_file_keeps_content_and_records_disclosure(persisted_db, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Home-anchored guard: the workspace lives inside a fake home.
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    target = workspace / "brief.txt"
    target.write_text("inside\n", encoding="utf-8")

    result = await read_file(
        {"path": "brief.txt"},
        ToolContext(
            cwd=str(workspace), workspace_root=str(workspace), config=ModusConfig(),
            session_id=persisted_db["session_id"], run_id=persisted_db["run_id"],
        ),
    )

    # Content semantics are unchanged (line-numbered view).
    assert result.content == "1: inside"
    # Disclosure facts recorded without re-reading the file.
    assert result.disclosure["local_bytes_read"] == 7
    assert result.disclosure["model_bytes_sent"] == 6
    assert result.disclosure["raw_content_sent"] is True


@pytest.mark.asyncio
async def test_small_bash_output_has_no_artifact_and_matches_model(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await bash(
        {"command": f'"{sys.executable}" -c "print(\'ok\')"'},
        ToolContext(cwd=str(workspace), config=ModusConfig()),
    )
    assert result.artifacts == []
    assert result.is_error is False
    # Below threshold: model sees exactly the visible content.
    assert result.model_text() == result.content
