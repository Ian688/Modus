from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def test_workspace_identity_is_stable_and_public(tmp_path):
    from modus.desktop.workspace import WorkspaceIdentity

    first = WorkspaceIdentity.from_path(tmp_path)
    second = WorkspaceIdentity.from_path(tmp_path / ".")

    assert first == second
    assert first.workspace_id.startswith("ws_")
    assert first.to_wire() == {
        "schema": "modus.workspace.v1",
        "workspace_id": first.workspace_id,
        "root": str(tmp_path.resolve()),
        "name": tmp_path.name,
    }


def test_workspace_identity_prefers_declared_project_name(tmp_path):
    from modus.desktop.workspace import WorkspaceIdentity

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "modus-demo"\n', encoding="utf-8",
    )

    assert WorkspaceIdentity.from_path(tmp_path).name == "modus-demo"


def test_new_runtime_session_has_no_implicit_process_workspace():
    from modus.desktop.session_state import SessionManager

    session = SessionManager().create(engine=None)

    assert session.workspace_id == ""
    assert session.workspace_root == ""
    assert session.engine is None


def test_new_runtime_session_accepts_only_an_explicit_workspace(tmp_path):
    from modus.desktop.session_state import SessionManager

    session = SessionManager().create(engine=None, workspace_root=str(tmp_path))

    assert session.workspace_root == str(tmp_path.resolve())
    assert session.workspace_id.startswith("ws_")


def test_database_migrates_existing_history_to_workspace_identity(tmp_path, monkeypatch):
    import sqlite3

    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '新对话',
                mode TEXT NOT NULL DEFAULT 'default', archived INTEGER NOT NULL DEFAULT 0,
                worldview TEXT NOT NULL DEFAULT '', world_view_history TEXT NOT NULL DEFAULT '[]',
                system_prompt TEXT NOT NULL DEFAULT '', model_id TEXT NOT NULL DEFAULT '',
                mode_config TEXT NOT NULL DEFAULT '{}', reasoning_effort TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )""",
        )
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES ('old', 'Old', 1, 1)",
        )

    db.init_db()

    restored = db.get_session("old")
    assert restored is not None
    workspace = db.get_workspace(restored["workspace_id"])
    assert workspace is not None
    assert workspace["root"] == str(Path.cwd().resolve())


def test_workbench_snapshot_joins_run_task_and_artifact_ledgers(tmp_path, monkeypatch):
    from modus.desktop import db
    from modus.desktop.artifacts import write_artifact
    from modus.desktop.workbench import build_workbench_snapshot

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("Workbench")
    db.create_run("run-wb", session["id"], "peri", config_snapshot={
        "schema": "modus.run-config.v1", "mode": "peri",
        "host_model_id": "host-id", "reasoning_effort": "high",
        "roles": {
            "host": {"model_id": "host-id", "name": "Host Frozen"},
            "worker_1": {"model_id": "worker-id", "model": "worker-frozen"},
        },
        "budget": {"max_turns": 12, "max_tokens": 34_000, "max_wall_seconds": 90},
        "verification": {"required": True, "max_attempts": 4},
    })
    root = db.create_run_task(
        task_id="task-root", run_id="run-wb", session_id=session["id"],
        ordinal=-1, task_kind="root", title="用户任务",
    )
    worker = db.create_run_task(
        task_id="task-worker", run_id="run-wb", session_id=session["id"],
        ordinal=0, parent_task_id=root["task_id"], task_kind="worker",
        title="检查项目", actor_id="worker-a", actor_label="Worker A",
    )
    artifact = write_artifact(
        session_id=session["id"], run_id="run-wb", task_id=worker["task_id"],
        kind="worker-response", title="检查结果", content="evidence",
        summary="verified",
    )
    db.update_run_task(
        worker["task_id"], status="completed",
        result_artifact_id=artifact["artifact_id"],
    )

    snapshot = build_workbench_snapshot(session["id"])

    assert snapshot["schema"] == "modus.workbench.v1"
    assert snapshot["workspace"]["schema"] == "modus.workspace.v1"
    assert snapshot["runs"][0]["projection_cursor"] == {
        "ledger_revision": 4, "sequence": 0, "event_revision": 0, "revision": 0,
    }
    assert snapshot["runs"][0]["config_snapshot"] == {
        "schema": "modus.run-config.v1", "mode": "peri",
        "host_model_id": "host-id", "reasoning_effort": "high",
        "roles": {
            "host": {"model_id": "host-id", "name": "Host Frozen"},
            "worker_1": {"model_id": "worker-id", "model": "worker-frozen"},
        },
        "budget": {"max_turns": 12, "max_tokens": 34_000, "max_wall_seconds": 90},
        "verification": {"required": True, "max_attempts": 4},
    }
    assert [item["task_kind"] for item in snapshot["runs"][0]["tasks"]] == ["root", "worker"]
    assert snapshot["runs"][0]["artifacts"][0]["task_id"] == "task-worker"
    assert "storage_path" not in snapshot["runs"][0]["artifacts"][0]
    assert snapshot["runs"][0]["semantic"]["schema"] == "modus.semantic-run.v1"
    assert snapshot["runs"][0]["semantic"]["run_id"] == "run-wb"
    assert snapshot["runs"][0]["semantic"]["goal"] == {
        "summary": "用户任务", "source": "task",
    }
    assert snapshot["runs"][0]["review"] == {
        "schema": "modus.change-review.v1",
        "status": "clean",
        "run_state": "running",
        "mutation_count": 0,
        "file_count": 0,
        "additions": 0,
        "deletions": 0,
        "files": [],
        "verifications": [],
        "latest_verification": None,
    }


def test_workbench_review_is_rebuilt_from_persisted_events(tmp_path, monkeypatch):
    from modus.desktop import db
    from modus.desktop.workbench import build_workbench_snapshot

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("Review")
    db.create_run("run-review", session["id"], "default")
    base = {
        "run_id": "run-review", "session_id": session["id"],
        "workspace_id": session["workspace_id"], "channel_id": "host:models",
        "parent_event_id": None, "timestamp": "2026-08-02T00:00:00Z",
        "mode": "default", "actor": {"kind": "tool", "id": "tool"},
        "status": "completed", "part_id": "part", "artifact_ids": [],
        "schema": "modus.agent-event.v2", "revision": 0, "task_id": None,
    }
    db.upsert_run_event(session["id"], {
        **base, "event_id": "evt-edit", "sequence": 1, "type": "tool_result",
        "payload": {"name": "edit_file", "metadata": {
            "changed": True, "operation": "edit", "change_type": "update",
            "path": "src/app.py", "additions": 2, "deletions": 1,
            "diff": "--- a/src/app.py\n+++ b/src/app.py\n@@\n-old\n+new",
        }},
    })
    db.upsert_run_event(session["id"], {
        **base, "event_id": "evt-test", "sequence": 2, "type": "tool_result",
        "payload": {"name": "run_tests", "result": "{}", "metadata": {
            "verification": {"schema": "modus.verification.v1", "status": "passed",
                             "exit_code": 0, "duration_seconds": 1.2,
                             "counts": {"passed": 4}},
        }},
    })

    review = build_workbench_snapshot(session["id"])["runs"][0]["review"]

    assert review["status"] == "verified"
    assert review["file_count"] == 1
    assert review["additions"] == 2
    assert review["deletions"] == 1
    assert review["files"][0]["path"] == "src/app.py"
    assert review["files"][0]["diff"].startswith("--- a/src/app.py")
    assert review["latest_verification"]["counts"] == {"passed": 4}


def test_workbench_review_marks_a_mutation_after_tests_unverified(tmp_path, monkeypatch):
    from modus.desktop import db
    from modus.desktop.workbench import build_run_review

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    review = build_run_review([
        {"sequence": 1, "type": "tool_result", "payload": {
            "name": "run_tests", "metadata": {"verification": {
                "status": "passed", "counts": {"passed": 2},
            }},
        }},
        {"sequence": 2, "type": "tool_result", "payload": {
            "name": "edit_file", "metadata": {
                "changed": True, "path": "src/later.py", "additions": 1,
            },
        }},
    ], state="completed")

    assert review["status"] == "unverified"


def test_workbench_review_preserves_verification_without_file_mutations():
    from modus.desktop.workbench import build_run_review

    review = build_run_review([{
        "sequence": 1, "type": "tool_result", "payload": {
            "name": "run_tests", "metadata": {"verification": {
                "status": "passed", "command": "pytest -q",
                "counts": {"passed": 4},
            }},
        },
    }], state="completed")

    assert review["status"] == "verified"
    assert review["file_count"] == 0
    assert review["latest_verification"]["status"] == "passed"


def test_frontend_distinguishes_test_only_evidence_from_verified_files():
    workbench = (
        ROOT / "src/modus/desktop/static/workbench.js"
    ).read_text(encoding="utf-8")

    assert 'return {label:"验证通过", historyLabel:"验证通过", fileCount:0}' in workbench
    assert 'verified:"文件已验证"' in workbench
    assert "presentation.fileCount ? presentation.fileCount + ' 个文件' : '无文件改动'" in workbench


def test_frontend_workbench_renders_frozen_run_configuration_without_empty_noise():
    workbench = (
        ROOT / "src/modus/desktop/static/workbench.js"
    ).read_text(encoding="utf-8")
    config = workbench[
        workbench.index("_runConfigHtml(snapshot)"):
        workbench.index("_children(run, parentId)")
    ]

    assert 'return ""' in config
    assert 'snapshot.host_model_id' in config
    assert 'snapshot.reasoning_effort' in config
    assert 'budget.max_turns' in config
    assert 'budget.max_tokens' in config
    assert 'budget.max_wall_seconds' in config
    assert 'verification.required === true' in config
    assert 'verification.max_attempts' in config
    assert 'roles.host?.reasoning_effort' in config
    assert 'api_key' not in config
    assert "启动配置" in config
    assert "(run ? this._runDetailHtml(run) : \"\")" in workbench


@pytest.mark.asyncio
async def test_command_router_is_transport_neutral_and_rejects_duplicates():
    from modus.desktop.command_router import DesktopCommandRouter

    router = DesktopCommandRouter()
    seen = []

    async def handler(socket, session, message):
        seen.append((socket, session, message["type"]))

    router.register("workbench_get", handler)
    assert await router.dispatch("socket", "session", {"type": "workbench_get"}) is True
    assert await router.dispatch("socket", "session", {"type": "unknown"}) is False
    assert seen == [("socket", "session", "workbench_get")]
    with pytest.raises(ValueError, match="already registered"):
        router.register("workbench_get", handler)


@pytest.mark.asyncio
async def test_workbench_run_get_is_scoped_to_active_session(tmp_path, monkeypatch):
    from modus.desktop import db, server

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    current = db.create_session("Current")
    other = db.create_session("Other")
    db.create_run("run-current", current["id"], "default")
    db.create_run("run-other", other["id"], "default")

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    socket = Socket()
    session = server.DaoSession(id="runtime", db_id=current["id"])
    assert await server.command_router.dispatch(
        socket, session, {
            "type": "workbench_run_get", "run_id": "run-current",
            "session_id": current["id"], "request_id": "run-request-current",
        },
    ) is True
    assert socket.sent[-1]["type"] == "workbench_run"
    assert socket.sent[-1]["run"]["run_id"] == "run-current"
    assert socket.sent[-1]["operation"] == "workbench_run_get"
    assert socket.sent[-1]["request_id"] == "run-request-current"
    assert socket.sent[-1]["session_id"] == current["id"]
    assert socket.sent[-1]["run_id"] == "run-current"

    await server.command_router.dispatch(
        socket, session, {
            "type": "workbench_run_get", "run_id": "run-other",
            "session_id": current["id"], "request_id": "run-request-other",
        },
    )
    assert socket.sent[-1]["code"] == "workbench_run_not_found"
    assert socket.sent[-1]["operation"] == "workbench_run_get"
    assert socket.sent[-1]["request_id"] == "run-request-other"
    assert socket.sent[-1]["session_id"] == current["id"]
    assert socket.sent[-1]["run_id"] == "run-other"


@pytest.mark.asyncio
async def test_workbench_get_echoes_request_and_session_identity(tmp_path, monkeypatch):
    from modus.desktop import db, server

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    current = db.create_session("Current")

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    socket = Socket()
    session = server.DaoSession(id="runtime", db_id=current["id"])
    await server.command_router.dispatch(socket, session, {
        "type": "workbench_get", "session_id": current["id"],
        "request_id": "workbench-request",
    })

    packet = socket.sent[-1]
    assert packet["type"] == "workbench_snapshot"
    assert packet["operation"] == "workbench_get"
    assert packet["request_id"] == "workbench-request"
    assert packet["session_id"] == current["id"]
    assert packet["requested_session_id"] == current["id"]
    assert packet["data"]["session_id"] == current["id"]


def test_workbench_projection_cursor_orders_sequence_then_revision(tmp_path, monkeypatch):
    from modus.desktop import db
    from modus.desktop.workbench import build_workbench_snapshot

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("Cursor")
    db.create_run("run-cursor", session["id"], "default")
    base = {
        "run_id": "run-cursor", "session_id": session["id"],
        "workspace_id": session["workspace_id"], "channel_id": "user_host",
        "parent_event_id": None, "timestamp": "2026-08-03T00:00:00Z",
        "mode": "default", "actor": {"kind": "host", "id": "host"},
        "status": "streaming", "payload": {}, "part_id": "part",
        "artifact_ids": [], "schema": "modus.agent-event.v2", "task_id": None,
    }
    db.upsert_run_event(session["id"], {
        **base, "event_id": "event-1", "sequence": 1,
        "type": "host_thinking", "revision": 5,
    })
    db.upsert_run_event(session["id"], {
        **base, "event_id": "event-2", "sequence": 2,
        "type": "host_response", "revision": 1,
    })

    run = build_workbench_snapshot(session["id"])["runs"][0]
    assert run["projection_cursor"] == {
        "ledger_revision": 2, "sequence": 2, "event_revision": 1, "revision": 1,
    }


def test_frontend_workbench_is_split_into_protocol_state_and_render_modules():
    from _bundle import js_bundle

    page = (ROOT / "src/modus/desktop/static/index.html").read_text()
    js = js_bundle()
    protocol = (ROOT / "src/modus/desktop/static/protocol.js").read_text()
    workbench = (ROOT / "src/modus/desktop/static/workbench.js").read_text()

    assert '<script src="/static/protocol.js"></script>' in page
    assert '<script src="/static/workbench.js"></script>' in page
    assert '<link rel="stylesheet" href="/static/workbench.css?v=27">' in page
    assert "normalizeAgentEvent" in protocol
    assert "class ProtocolStateStore" in protocol
    assert "class WorkbenchStore" in workbench
    assert "reviewContainer" in workbench
    assert "selectedRunId" in workbench
    assert "data-run-select" in workbench
    assert "priorSelection" in workbench
    assert "selectionPinned" in workbench
    assert "_compareProjectionCursor" in workbench
    assert "ledger_revision" in workbench
    assert "cursor?.event_revision ?? cursor?.revision" in workbench
    assert "run.projection_cursor" in workbench
    assert "_fallbackEvent" not in workbench
    assert "_runDetailHtml(run)" in workbench
    assert "data-task-select" in workbench
    assert "focusReviewFile(path)" in workbench
    assert "data-task-artifact" in workbench
    assert "data-task-review" in workbench
    assert 'type:"workbench_get"' in js
    assert 'case "workbench_snapshot":' in js
    assert 'case "workbench_run":' in js
    assert 'type:"workbench_run_get"' in js
    assert "workbenchStore.observe(event);" in js
    assert 'id="kbBoard"' in page
    assert 'id="kbColumns"' in page
    assert 'id="workbenchToggleBtn"' in page
    assert 'body.workbench-open .right-panel' in (
        ROOT / "src/modus/desktop/static/workbench.css"
    ).read_text()
    server = (ROOT / "src/modus/desktop/server.py").read_text()
    assert '"tool_result", "subagent_tool_result", "artifact"' in server


def test_frontend_workbench_requests_are_correlated_to_exact_session_identity():
    from _bundle import js_bundle

    js = js_bundle()
    snapshot_request = js[
        js.index("function requestWorkbenchSnapshot"):
        js.index("function requestWorkbenchRun")
    ]
    run_request = js[
        js.index("function requestWorkbenchRun"):
        js.index("function matchesWorkbenchIdentity")
    ]
    identity = js[
        js.index("function matchesWorkbenchIdentity"):
        js.index("function settleWorkbenchError")
    ]

    assert 'nextTransientRequestId("workbench-snapshot")' in snapshot_request
    assert 'type:"workbench_get", request_id:requestId' in snapshot_request
    assert "session_id:requestedSessionId" in snapshot_request
    assert 'nextTransientRequestId("workbench-run")' in run_request
    assert 'type:"workbench_run_get", request_id:requestId' in run_request
    assert "run_id:requestedRunId" in run_request
    for field in (
        "message?.operation", "message?.request_id",
        "message?.requested_session_id", "message?.session_id", "currentDbId",
    ):
        assert field in identity
    assert 'String(currentDbId ?? "") === pending.sessionId' in identity


def test_workbench_snapshot_acceptance_separates_correlated_and_replay_pushes():
    from _bundle import js_bundle

    js = js_bundle()
    branch = js[
        js.index('case "workbench_snapshot":'):
        js.index('case "workbench_run":')
    ]

    assert 'msg.operation === "workbench_get"' in branch
    assert 'matchesWorkbenchIdentity(' in branch
    assert 'dataSessionId !== pendingWorkbenchSnapshot.sessionId' in branch
    assert 'msg.operation === undefined || msg.operation === null || msg.operation === ""' in branch
    assert 'responseSessionId !== String(currentDbId ?? "")' in branch
    assert 'dataSessionId !== String(currentDbId ?? "")' in branch
    assert "workbenchStore.load(msg.data || {});" in branch


def test_only_latest_workbench_run_detail_response_can_apply():
    from _bundle import js_bundle

    js = js_bundle()
    request = js[
        js.index("function requestWorkbenchRun"):
        js.index("function matchesWorkbenchIdentity")
    ]
    branch = js[
        js.index('case "workbench_run":'):
        js.index('case "peri_git_readiness":')
    ]

    assert "pendingWorkbenchRunDetail = {" in request
    assert "requestId, sessionId:requestedSessionId, runId:requestedRunId" in request
    assert "const pending = pendingWorkbenchRunDetail" in branch
    assert 'matchesWorkbenchIdentity(msg, pending, "workbench_run_get")' in branch
    assert "responseRunId !== pending.runId" in branch
    assert "runSessionId !== pending.sessionId" in branch
    assert "pendingWorkbenchRunDetail = null" in branch
    assert branch.index("pendingWorkbenchRunDetail = null") < branch.index(
        "workbenchStore.applyAuthoritativeRun(msg.run)"
    )


def test_workbench_pending_requests_reset_on_every_identity_boundary():
    from _bundle import js_bundle

    js = js_bundle()
    reset = js[
        js.index("function resetWorkbenchRequests"):
        js.index("function requestWorkbenchSnapshot")
    ]
    assert "pendingWorkbenchSnapshot = null" in reset
    assert "pendingWorkbenchRunDetail = null" in reset
    assert 'registerTransientRequestReset("workbench", resetWorkbenchRequests)' in js
    assert 'resetTransientRequests("socket_close")' in js
    assert 'resetTransientRequests("server_epoch")' in js
    assert 'beginWorkbenchSessionTransition("session_switch")' in js
    load = js[
        js.index("function loadSessionMessages"):
        js.index("// Batch delete helper")
    ]
    assert 'beginWorkbenchSessionTransition("session_identity")' in load


def test_correlated_workbench_errors_settle_only_the_matching_request():
    from _bundle import js_bundle

    js = js_bundle()
    settle = js[
        js.index("function settleWorkbenchError"):
        js.index('registerTransientRequestReset("workbench"')
    ]
    assert 'message?.operation === "workbench_get"' in settle
    assert 'matchesWorkbenchIdentity(' in settle
    assert "pendingWorkbenchSnapshot = null" in settle
    assert 'message?.operation === "workbench_run_get"' in settle
    assert 'String(message?.run_id ?? "") !== pending.runId' in settle
    assert "pendingWorkbenchRunDetail = null" in settle
    error = js[
        js.index('case "error":'):
        js.index('case "session_reasoning_updated":')
    ]
    assert "settleWorkbenchError(msg)" in error


def test_workbench_window_switcher_covers_all_panes_and_persists():
    switcher = (
        ROOT / "src/modus/desktop/static/workbenchwindows.js"
    ).read_text(encoding="utf-8")

    # The right panel is a single KANBAN board now; the switcher is a
    # compatibility adapter that keeps the legacy activate/setSubtab API and
    # delegates window names to board actions.
    assert "global.ModusWorkbenchWindows = { activate, init, setSubtab }" in switcher
    assert "function activate(name)" in switcher
    assert "handleLegacyRoute" in switcher

    # The board containers exist in the HTML.
    page = (
        ROOT / "src/modus/desktop/static/index.html"
    ).read_text(encoding="utf-8")
    assert 'id="kbBoard"' in page
    assert 'id="kbColumns"' in page
    assert 'id="kbEmptyState"' in page
    assert 'id="kbRunSelect"' in page
    assert 'id="kbDrawer"' in page
    # The five flow columns are declared in kanban.js.
    kanban = (
        ROOT / "src/modus/desktop/static/kanban.js"
    ).read_text(encoding="utf-8")
    for col in ("todo", "analyzing", "executing", "verifying", "completed"):
        assert col in kanban
    # The board reuses the workspace manager in its empty state.
    assert 'id="workspaceMemoryList"' in page
    assert 'id="wvBody"' in page


def test_subtask_cards_render_into_right_panel_window():
    timeline = (
        ROOT / "src/modus/desktop/static/timeline.js"
    ).read_text(encoding="utf-8")

    # Sub-agent coordination still produces typed subtask cards; the container
    # is inert under the KANBAN board but the card logic is preserved.
    assert 'document.getElementById("rpSubtasks")' in timeline
    assert 'card.className = "subtask-card"' in timeline
    # Cards carry a meaningful title + live status meta.
    assert 'this._subtaskTitle(event)' in timeline
    assert 'this._subtaskStatus(event)' in timeline
    # Cards are ordered by run id within the window.
    assert 'siblingCards.find(item =>' in timeline
    # Run-level events stay flat and never open a card.
    assert 'event.type === "run_started"' in timeline
    assert 'event.type === "run_completed"' in timeline

    css = (
        ROOT / "src/modus/desktop/static/workbench.css"
    ).read_text(encoding="utf-8")
    assert ".subtask-card" in css
    assert ".subtask-card > summary" in css


def test_contextual_windows_cover_document_activity_and_browser():
    """Document / activity / browser features survive the KANBAN redesign: they
    fold into the run-card detail drawer instead of owning separate tabs."""
    router = (
        ROOT / "src/modus/desktop/static/windowrouter.js"
    ).read_text(encoding="utf-8")
    # Plan/design/spec artifacts still route to the document surface.
    assert 'const DOC_KINDS = ["plan", "design", "spec"]' in router
    assert 'return "document"' in router
    assert 'return "artifacts"' in router
    # Dev-server URLs in tool results still route to the browser surface.
    assert "LOCALHOST_RE" in router
    assert 'return "browser"' in router

    moduswindows = (
        ROOT / "src/modus/desktop/static/moduswindows.js"
    ).read_text(encoding="utf-8")
    # The preview iframe routes through the same-origin proxy.
    assert 'src = "/api/preview?url="' in moduswindows
    # Document rendering moves into the drawer container.
    assert 'document.getElementById("kbDocument")' in moduswindows


def test_window_router_maps_kind_and_localhost_events():
    router = (
        ROOT / "src/modus/desktop/static/windowrouter.js"
    ).read_text(encoding="utf-8")
    # Plan/design/spec artifacts open the document surface; other kinds stay in
    # the artifacts dock.
    assert 'const DOC_KINDS = ["plan", "design", "spec"]' in router
    assert 'return "document"' in router
    assert 'return "artifacts"' in router
    # Dev-server URLs in tool results open the built-in browser preview.
    assert "LOCALHOST_RE" in router
    assert 'return "browser"' in router
    # The board highlights the active column from the event stream.
    assert "columnForEvent" in router
    assert "setActiveColumn" in router
    # The router is bound to window.
    assert "(function (global) {" in router


def test_document_window_renders_markdown_and_silent_fetch():
    windows = (
        ROOT / "src/modus/desktop/static/moduswindows.js"
    ).read_text(encoding="utf-8")
    # Document window renders via the shared markdown renderer.
    assert "window.renderMd" in windows
    assert 'renderDocument' in windows
    # Metadata-only artifact announcements request content without opening the
    # modal viewer (silent request).
    assert "requestArtifactContent(artifactId, {silent: true})" in windows
    assert "silent" in windows
    # Browser preview is loopback-only, routed through the proxy.
    assert '^https?:\\/\\/localhost:\\d+' in windows
    assert "/api/preview?url=" in windows
    # Activity cards key on agent identity so one agent owns one card.
    assert "Key on the agent identity" in windows


def test_silent_artifact_fetch_bypasses_viewer_gate():
    """A document-window read must not require the artifact viewer modal to be
    open (the settle matcher gates on viewer state for interactive reads)."""
    core = (
        ROOT / "src/modus/desktop/static/core.js"
    ).read_text(encoding="utf-8")
    assert "pending.silent" in core
    timeline = (
        ROOT / "src/modus/desktop/static/timeline.js"
    ).read_text(encoding="utf-8")
    # requestArtifactContent gains a silent option that skips openArtifactViewer.
    assert "silent = Boolean(opts && opts.silent)" in timeline
    assert "if (!silent && typeof openArtifactViewer" in timeline


def test_agent_status_chip_drives_from_event_stream():
    status = (
        ROOT / "src/modus/desktop/static/agentstatus.js"
    ).read_text(encoding="utf-8")
    # The chip is driven by typed events, mapping each event type to a state.
    assert 'document.getElementById("agentStatusChip")' in status
    assert 'thinking: { cls: "thinking", label: "思考中…" }' in status
    assert 'tool: { cls: "tool", label: "执行工具" }' in status
    assert 'approval: { cls: "approval", label: "等待审批" }' in status
    assert 'type === "tool_call"' in status
    assert 'type === "approval_request"' in status
    assert 'type === "artifact"' in status
    # It patches applyTranscriptEvent so the chip stays ambient (no user action).
    assert "window.__agentStatusPatched" in status

    page = (
        ROOT / "src/modus/desktop/static/index.html"
    ).read_text(encoding="utf-8")
    assert 'id="agentStatusChip"' in page
    css = (
        ROOT / "src/modus/desktop/static/workbench.css"
    ).read_text(encoding="utf-8")
    assert ".agent-status-chip.thinking" in css
    assert ".agent-status-chip.tool" in css
    assert ".agent-status-chip.approval" in css


def test_workspace_management_lives_in_workbench_not_composer():
    bar = (
        ROOT / "src/modus/desktop/static/contextbar.js"
    ).read_text(encoding="utf-8")
    assert "global.ModusWorkspaceManager = {" in bar
    assert "handleWorkspaceList" in bar
    assert "handleWorkspaceOpened" in bar
    assert 'type:"workspace_forget"' in bar
    assert 'type:"session_set_workspace"' in bar
    assert "源文件不会被删除" in bar

    page = (
        ROOT / "src/modus/desktop/static/index.html"
    ).read_text(encoding="utf-8")
    assert 'id="ctxBar"' not in page
    assert 'id="ctxAddBtn"' not in page
    assert 'id="workspaceBrowseBtn"' not in page
    assert 'id="workspacePathBtn"' not in page
    assert 'id="workspaceMemoryList"' in page
    assert 'id="workspacePathForm"' in page
    assert 'id="workspaceSelectBtn"' not in page


def test_semantic_palette_maps_event_kinds_to_colors():
    css = (
        ROOT / "src/modus/desktop/static/workbench.css"
    ).read_text(encoding="utf-8")
    # tool=blue, artifact=green, approval=amber, error=red, thinking=accent.
    assert ".timeline-item.tool { border-left:3px solid var(--blue); }" in css
    assert ".timeline-item.reference_model { border-left:3px solid var(--blue); }" in css
    assert ".timeline-item.subagent { border-left:3px solid var(--green); }" in css
    assert ".timeline-item.system { border-left:3px solid var(--red); }" in css
    assert ".approval-card { border:1px solid #e8a547" in css or ".approval-card" in css
    assert "color:var(--blue)" in css
    assert ".timeline-tool.artifact-details" in css
