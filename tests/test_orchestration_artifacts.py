import hashlib
import os
import stat

import pytest


def _persisted_run():
    from modus.desktop.db import create_run, create_session

    session = create_session(mode="peri")
    run_id = "run_artifacts"
    create_run(run_id, session["id"], "peri")
    return session["id"], run_id


def test_artifact_store_is_private_redacted_and_session_scoped():
    from modus.desktop.artifacts import artifact_root, public_artifact, read_artifact, write_artifact

    session_id, run_id = _persisted_run()
    artifact = write_artifact(
        session_id=session_id, run_id=run_id, kind="task-context", title="Context",
        content="use api_key=super-secret and sk-abcdefghijklmnopqrstuvwxyz",
        summary="private context",
    )
    path = __import__("pathlib").Path(artifact["storage_path"])
    stored = path.read_text()

    assert path.is_relative_to(artifact_root())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "super-secret" not in stored
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in stored
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["content_hash"]
    assert read_artifact(artifact["artifact_id"], session_id=session_id) == stored
    with pytest.raises(ValueError, match="not found"):
        read_artifact(artifact["artifact_id"], session_id="another-session")
    assert "storage_path" not in public_artifact(artifact)
    assert "content_hash" not in public_artifact(artifact)


def test_artifact_read_rejects_content_tampering():
    from pathlib import Path

    from modus.desktop.artifacts import read_artifact, write_artifact

    session_id, run_id = _persisted_run()
    artifact = write_artifact(
        session_id=session_id, run_id=run_id, kind="worker-summary",
        title="Summary", content="verified content",
    )
    Path(artifact["storage_path"]).write_text("tampered content\n")

    with pytest.raises(ValueError, match="integrity"):
        read_artifact(artifact["artifact_id"], session_id=session_id)


def test_task_artifact_and_memory_ledger_round_trip():
    from modus.desktop.artifacts import write_artifact
    from modus.desktop.db import (
        add_memory_record, create_run_task, get_memory, list_memories,
        list_run_artifacts, list_run_tasks, update_run_task,
    )

    session_id, run_id = _persisted_run()
    context = write_artifact(
        session_id=session_id, run_id=run_id, kind="task-context", title="Context",
        content="scoped input",
    )
    task = create_run_task(
        run_id=run_id, session_id=session_id, ordinal=0, title="Inspect",
        description="inspect repo", success_criteria="cite evidence",
        context_artifact_id=context["artifact_id"], assigned_model_id="worker",
    )
    result = write_artifact(
        session_id=session_id, run_id=run_id, task_id=task["task_id"],
        kind="worker-response", title="Worker response", content="evidence",
    )
    assert update_run_task(
        task["task_id"], status="completed",
        result_artifact_id=result["artifact_id"], increment_attempt=True,
    )
    memory = add_memory_record(
        session_id=session_id, run_id=run_id, task_id=task["task_id"],
        scope="task", category="evidence", content="verified result",
        source_ids=[result["artifact_id"]], reference_only=True,
    )

    restored_task = list_run_tasks(run_id)[0]
    assert restored_task["status"] == "completed"
    assert restored_task["attempt"] == 1
    assert restored_task["result_artifact_id"] == result["artifact_id"]
    assert [item["kind"] for item in list_run_artifacts(run_id)] == ["task-context", "worker-response"]
    assert get_memory(memory["memory_id"])["source_ids"] == [result["artifact_id"]]
    assert list_memories(session_id, task_id=task["task_id"])[0]["reference_only"] is True


def test_session_memory_uses_sqlite_and_is_reference_only():
    from modus.desktop.db import create_session
    from modus.desktop.memory import (
        add_memory, archive_memory, clear_memories, get_memories, get_memories_text,
    )

    session_id = create_session()["id"]
    saved = add_memory(session_id, "Use Python 3.12", "constraint")

    memories = get_memories(session_id)
    assert memories[0]["content"] == "Use Python 3.12"
    assert memories[0]["reference_only"] is True
    text = get_memories_text(session_id)
    assert "REFERENCE ONLY" in text
    assert "not active user instructions" in text
    assert archive_memory(session_id, saved["memory_id"]) is True
    assert archive_memory(session_id, saved["memory_id"]) is False
    assert get_memories(session_id) == []
    add_memory(session_id, "Run pytest -q", "constraint")
    clear_memories(session_id)
    assert get_memories(session_id) == []


def test_frontend_renders_artifacts_as_collapsible_metadata_only_cards():
    from pathlib import Path

    from _bundle import js_bundle

    page = js_bundle()
    start = page.index('case "artifact":')
    block = page[start:page.index('case "host_dispatch":', start)]
    assert "<details" in block
    assert "artifact_id" in block
    assert "storage_path" not in block
    assert 'type:"artifact_get"' in page
    assert "artifact_content" in page


@pytest.mark.asyncio
async def test_peri_persists_complete_task_and_artifact_exchange(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import create_session, list_run_artifacts, list_run_tasks

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "host", "model_id": "host", "name": "Host", "provider": "test", "model": "host"},
            "worker_1": {"id": "worker", "model_id": "worker", "name": "Worker", "provider": "test", "model": "worker"},
        },
    })

    async def decompose(*_args, **_kwargs):
        return [{
            "name": "Inspect", "description": "inspect project",
            "context": "focus on source", "success_criteria": "cite evidence",
        }]

    async def execute(*_args, **_kwargs):
        return "Worker found verified evidence."

    async def review(*_args, **_kwargs):
        return True, [], [8.0]

    async def merge(*_args, **_kwargs):
        return "Host merged the verified evidence."

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", merge)

    persisted = create_session(mode="peri")
    socket = Socket()
    await server._run_peri_stream(
        socket, server.DaoSession(id="runtime", db_id=persisted["id"]), "Inspect project",
    )

    run_id = next(
        packet["event"]["run_id"] for packet in socket.sent
        if packet.get("type") == "agent_event"
    )
    tasks = list_run_tasks(run_id)
    artifacts = list_run_artifacts(run_id)
    assert [task["task_kind"] for task in tasks] == ["root", "worker"]
    root, worker = tasks
    assert root["status"] == "completed"
    assert worker["parent_task_id"] == root["task_id"]
    assert worker["status"] == "completed"
    assert worker["attempt"] == 1
    assert worker["context_artifact_id"]
    assert worker["result_artifact_id"]
    assert {item["kind"] for item in artifacts} == {
        "task-analysis", "task-context", "worker-response", "worker-summary",
        "stop-decision", "host-review", "host-final",
    }
    artifact_events = [
        packet["event"] for packet in socket.sent
        if packet.get("type") == "agent_event" and packet["event"]["type"] == "artifact"
    ]
    assert len(artifact_events) == len(artifacts)
    assert all("storage_path" not in event["payload"] for event in artifact_events)


@pytest.mark.asyncio
async def test_peri_reads_worker_context_and_host_inputs_from_artifacts(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import create_session

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)

    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "host", "model_id": "host", "name": "Host", "provider": "test", "model": "host"},
            "worker_1": {"id": "worker", "model_id": "worker", "name": "Worker", "provider": "test", "model": "worker"},
        },
    })
    observed = {}
    full_output = "FULL-WORKER-OUTPUT\n" + ("x" * 8_000)

    async def decompose(*_args, **_kwargs):
        return [{
            "name": "Artifact task", "description": "narrow scope",
            "context": "artifact-only-marker", "success_criteria": "verified",
        }]

    async def execute(_task, _model, worker_context, **_kwargs):
        observed["worker_context"] = worker_context
        return full_output

    async def review(_tasks, summaries, *_args, **_kwargs):
        observed["review"] = summaries
        return True, [], [8.0]

    async def merge(_tasks, summaries, *_args, **_kwargs):
        observed["merge"] = summaries
        return "merged from artifacts"

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", execute)
    monkeypatch.setattr("modus.desktop.peri.review_subtask_outputs", review)
    monkeypatch.setattr("modus.desktop.peri.merge_outputs", merge)

    persisted = create_session(mode="peri")
    await server._run_peri_stream(
        Socket(), server.DaoSession(id="runtime", db_id=persisted["id"]), "user request",
    )

    assert "# Artifact task" in observed["worker_context"]
    assert "artifact-only-marker" in observed["worker_context"]
    assert observed["worker_context"].endswith("\n")
    assert len(observed["review"][0]) < len(full_output)
    assert len(observed["merge"][0]) < len(full_output)
    assert "characters omitted" in observed["review"][0]
    assert "characters omitted" in observed["merge"][0]


def test_artifact_get_websocket_is_session_scoped(monkeypatch):
    from fastapi.testclient import TestClient

    from modus.desktop import server
    from modus.desktop.artifacts import write_artifact
    from modus.desktop.db import create_run, create_session

    async def fake_registry(**_kwargs):
        return object()

    class Engine:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", Engine)

    owner = create_session()
    other = create_session()
    create_run("run_owner", owner["id"], "peri")
    artifact = write_artifact(
        session_id=owner["id"], run_id="run_owner", kind="host-final",
        title="Final", content="safe visible content",
    )

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": owner["id"]})
            for _ in range(20):
                if socket.receive_json()["type"] == "session_restored":
                    break
            socket.send_json({
                "type": "artifact_get", "artifact_id": artifact["artifact_id"],
                "request_id": "artifact-owner", "session_id": owner["id"],
            })
            allowed = socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": other["id"]})
            for _ in range(20):
                if socket.receive_json()["type"] == "session_restored":
                    break
            socket.send_json({
                "type": "artifact_get", "artifact_id": artifact["artifact_id"],
                "request_id": "artifact-other", "session_id": other["id"],
            })
            denied = socket.receive_json()

    assert allowed["type"] == "artifact_content"
    assert allowed["operation"] == "artifact_get"
    assert allowed["request_id"] == "artifact-owner"
    assert allowed["session_id"] == allowed["requested_session_id"] == owner["id"]
    assert allowed["artifact_id"] == artifact["artifact_id"]
    assert allowed["artifact"]["content"] == "safe visible content\n"
    assert "storage_path" not in str(allowed)
    assert "content_hash" not in str(allowed)
    assert denied["type"] == "error"
    assert denied["operation"] == "artifact_get"
    assert denied["request_id"] == "artifact-other"
    assert denied["session_id"] == denied["requested_session_id"] == other["id"]
    assert denied["artifact_id"] == artifact["artifact_id"]
    assert denied["message"] == "artifact not found"


def test_artifact_get_unexpected_failure_keeps_request_identity(monkeypatch):
    from fastapi.testclient import TestClient

    from modus.desktop import server
    from modus.desktop import artifacts as artifact_module
    from modus.desktop.db import create_session

    async def fake_registry(**_kwargs):
        return object()

    class Engine:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", Engine)
    monkeypatch.setattr(
        artifact_module, "read_artifact_public",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected read failure")),
    )
    owner = create_session()

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": owner["id"]})
            for _ in range(20):
                if socket.receive_json()["type"] == "session_restored":
                    break
            socket.send_json({
                "type": "artifact_get", "artifact_id": "art_failure",
                "request_id": "artifact-failure", "session_id": owner["id"],
            })
            failed = socket.receive_json()

    assert failed["type"] == "error"
    assert failed["operation"] == "artifact_get"
    assert failed["request_id"] == "artifact-failure"
    assert failed["session_id"] == failed["requested_session_id"] == owner["id"]
    assert failed["artifact_id"] == "art_failure"
    assert failed["message"] == "unexpected read failure"


def test_empty_artifact_is_readable(tmp_path, monkeypatch):
    from modus.desktop import db
    from modus.desktop.artifacts import read_artifact_public, write_artifact

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("Empty artifact")
    db.create_run("run-empty-artifact", session["id"], "default")

    artifact = write_artifact(
        session_id=session["id"], run_id="run-empty-artifact",
        kind="empty", title="Empty", content="",
    )

    assert artifact["size_bytes"] == 0
    assert read_artifact_public(
        artifact["artifact_id"], session_id=session["id"],
    )["content"] == ""


def test_artifact_public_read_limit_is_byte_exact(tmp_path, monkeypatch):
    from modus.desktop import db
    from modus.desktop.artifacts import read_artifact_public, write_artifact

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("Artifact display limit")
    db.create_run("run-artifact-limit", session["id"], "default")
    at_limit = write_artifact(
        session_id=session["id"], run_id="run-artifact-limit",
        kind="limit", title="At limit", content="a" * 199_999,
    )
    over_limit = write_artifact(
        session_id=session["id"], run_id="run-artifact-limit",
        kind="limit", title="Over limit", content="界" * 66_667,
    )

    assert at_limit["size_bytes"] == 200_000
    assert len(read_artifact_public(
        at_limit["artifact_id"], session_id=session["id"],
    )["content"].encode("utf-8")) == 200_000
    assert over_limit["size_bytes"] == 200_002
    with pytest.raises(ValueError, match="too large"):
        read_artifact_public(over_limit["artifact_id"], session_id=session["id"])
