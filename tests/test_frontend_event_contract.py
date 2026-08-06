from pathlib import Path

from _bundle import js_bundle, page_html


PAGE = Path(__file__).parents[1] / "src/modus/desktop/static/index.html"


def test_run_admission_protocol_requires_v2_on_both_sides() -> None:
    protocol = (PAGE.parent / "protocol.js").read_text(encoding="utf-8")
    server = (Path(__file__).parents[1] / "src/modus/desktop/server.py").read_text(
        encoding="utf-8",
    )

    assert "const DESKTOP_PROTOCOL_VERSION = 2;" in protocol
    assert "DESKTOP_PROTOCOL_VERSION = 2" in server


def test_frontend_uses_typed_agent_event_as_the_only_message_contract() -> None:
    page = js_bundle()

    assert 'case "agent_event":' in page
    assert "Typed AgentEvent is the sole message-rendering contract" in page
    assert "applyTranscriptEvent(msg.event)" in page
    assert "eventStore.push(event)" in page
    assert "Number(event.revision || 0) <= Number(prior.revision || 0)" in page
    assert "timelineRenderer.render(event)" in page
    assert "function sendMessage()" in page
    assert "The backend routes execution from the persisted session mode" in page


def test_browser_sends_one_run_command_and_never_selects_runner_by_message_type() -> None:
    page = js_bundle()

    payload_start = page.index("function runSubmissionPayload")
    payload = page[payload_start:page.index("function setRunSubmissionUi", payload_start)]
    start = page.index("function sendMessage()")
    send = page[start:page.index("function loadSessionMessages", start)]
    assert 'type:"run_message"' in payload
    assert "transmitPendingRunSubmission();" in send
    for obsolete in ('"user_message"', '"moa_message"', '"peri_message"'):
        assert obsolete not in payload + send

    from pathlib import Path
    server = (Path(__file__).parents[1] / "src/modus/desktop/server.py").read_text()
    assert 'msg_type == "run_message"' in server
    for obsolete in ("user_message", "moa_message", "peri_message"):
        assert f'msg_type == "{obsolete}"' not in server


def test_run_submission_keeps_draft_until_exact_admission_ack() -> None:
    page = js_bundle()
    core = (PAGE.parent / "core.js").read_text(encoding="utf-8")
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")

    payload = core[
        core.index("function runSubmissionPayload"):
        core.index("function setRunSubmissionUi")
    ]
    for field in (
        'type:"run_message"', "content:pending.content",
        "request_id:pending.requestId", "db_id:pending.sessionId",
        "session_id:pending.sessionId",
        "runtime_session_id:pending.runtimeSessionId",
    ):
        assert field in payload

    send = websocket[
        websocket.index("function sendMessage()"):
        websocket.index("function loadSessionMessages")
    ]
    assert "pendingRunSubmission = {" in send
    assert "content:t, draftValue, skillId:pendingSkillId || null" in send
    assert 'sessionId:String(currentDbId || "")' in send
    assert 'runtimeSessionId:String(sessionId || "")' in send
    assert 'requestId:nextTransientRequestId("run-message")' in send
    assert "transmitPendingRunSubmission();" in send
    assert 'input.value=""' not in send
    assert "pendingSkillId = null" not in send

    matcher = core[
        core.index("function matchesPendingRunSubmission"):
        core.index("function acceptPendingRunSubmission")
    ]
    for identity in (
        'message?.operation || ""', 'message?.request_id || ""',
        "message?.requested_db_id", "message?.db_id",
        'message?.runtime_session_id || ""', 'String(currentDbId || "")',
        'String(sessionId || "")',
    ):
        assert identity in matcher

    accepted = core[
        core.index("function acceptPendingRunSubmission"):
        core.index("function settlePendingRunSubmissionError")
    ]
    assert "matchesPendingRunSubmission(message, {requireAcceptedDb:true})" in accepted
    assert "if (input.value === pending.draftValue) input.value = \"\";" in accepted
    assert "if (pendingSkillId === pending.skillId)" in accepted
    assert 'case "run_accepted":' in websocket
    assert "acceptPendingRunSubmission(msg);" in websocket


def test_agent_events_cannot_ack_an_unconfirmed_run_submission() -> None:
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")
    branch = websocket[
        websocket.index('case "agent_event":'):
        websocket.index('case "transcript_reset":')
    ]

    assert "agentRunPending" in branch
    assert "setAgentRunPending(true)" not in branch
    assert "acceptPendingRunSubmission" not in branch
    assert "applyTranscriptEvent(msg.event)" in branch


def test_unacknowledged_run_retries_only_on_same_epoch_and_restored_identity() -> None:
    core = (PAGE.parent / "core.js").read_text(encoding="utf-8")
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")

    retry_guard = core[
        core.index("function canRetryPendingRunSubmission"):
        core.index("function transmitPendingRunSubmission")
    ]
    assert "pending.serverEpoch === serverEpoch" in retry_guard
    assert 'String(currentDbId || "") === currentRunSubmissionDbIdentity(pending)' in retry_guard
    transmit = core[
        core.index("function transmitPendingRunSubmission"):
        core.index("function retryPendingRunSubmission")
    ]
    assert 'pending.runtimeSessionId = String(sessionId || "")' in transmit
    assert "runSubmissionPayload(pending)" in transmit

    ready = websocket[
        websocket.index('case "session_ready":'):
        websocket.index('case "session_persisted":')
    ]
    restored = websocket[
        websocket.index('case "session_restored":'):
        websocket.index('case "session_history_reset":')
    ]
    assert "retryPendingRunSubmission()" in ready
    assert "retryPendingRunSubmission()" in restored
    epoch = core[core.index("function observeServerEpoch"):core.index("// Transcript cursors")]
    assert 'abandonPendingRunSubmission("server_epoch")' in epoch
    assert "runSubmissionRestartNotice = true" in core
    assert "未确认的任务没有自动重发；草稿已保留" in websocket


def test_run_submission_error_and_session_transition_preserve_draft() -> None:
    core = (PAGE.parent / "core.js").read_text(encoding="utf-8")
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")
    settings = (PAGE.parent / "settings.js").read_text(encoding="utf-8")

    restore = core[
        core.index("function restoreRunSubmissionDraft"):
        core.index("function abandonPendingRunSubmission")
    ]
    assert "if (!input.value)" in restore
    assert "pendingSkillId = pending.skillId" in restore
    error = websocket[
        websocket.index('case "error":'):
        websocket.index('case "session_reasoning_updated":')
    ]
    assert "settlePendingRunSubmissionError(msg)" in error
    assert "if (submissionError === false) break;" in error
    assert "提交失败，草稿已保留" in error
    assert 'abandonPendingRunSubmission("session_switch")' in websocket
    assert 'abandonPendingRunSubmission("session_create")' in settings


def test_duplicate_terminal_admission_ack_does_not_leave_composer_locked() -> None:
    core = (PAGE.parent / "core.js").read_text(encoding="utf-8")
    accepted = core[
        core.index("function acceptPendingRunSubmission"):
        core.index("function settlePendingRunSubmissionError")
    ]

    assert 'message.state || message.status || "running"' in accepted
    for state in ("completed", "failed", "cancelled", "interrupted"):
        assert f'"{state}"' in accepted
    assert "if (terminal || detachedDuplicate || settlementAlreadyObserved)" in accepted
    terminal = accepted[
        accepted.index("if (terminal || detachedDuplicate || settlementAlreadyObserved)"):
        accepted.index("setAgentRunPending(true)")
    ]
    assert 'const acceptedRunId = String(message.run_id || "")' in accepted
    assert "setAgentRunPending(false)" in terminal
    assert "input.disabled = !ready" in terminal
    assert "syncAcceptedRunResult(acceptedRunId)" in terminal


def test_detached_running_duplicate_reconciles_without_locking_composer() -> None:
    core = (PAGE.parent / "core.js").read_text(encoding="utf-8")
    accepted = core[
        core.index("function acceptPendingRunSubmission"):
        core.index("function settlePendingRunSubmissionError")
    ]
    detached = accepted[
        accepted.index("const detachedDuplicate"):
        accepted.index("setAgentRunPending(true)")
    ]
    sync = core[
        core.index("function syncAcceptedRunResult"):
        core.index("function acceptPendingRunSubmission")
    ]

    assert "ModusProtocol.runAdmissionConnectionRole(message)" in accepted
    assert "admissionRole === ModusProtocol.RUN_CONNECTION_ROLES.DETACHED" in detached
    assert "if (terminal || detachedDuplicate || settlementAlreadyObserved)" in detached
    assert "setAgentRunPending(false)" in detached
    assert "syncAcceptedRunResult(acceptedRunId)" in detached
    assert 'type:"transcript_sync"' in sync
    assert "run_id:acceptedRunId" in sync
    assert 'request_id:nextTransientRequestId("transcript-sync")' in sync
    assert "requestWorkbenchSnapshot()" in sync


def test_owned_run_settlement_unlocks_then_reconciles_both_projections() -> None:
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")
    settled = websocket[
        websocket.index('case "run_settled":'):
        websocket.index('case "worldview_updated":')
    ]

    assert 'const settledRunId = String(msg.run_id || "")' in settled
    assert "ModusProtocol.runSettlementConnectionRole(msg)" in settled
    assert "settlementRole === activeAgentRunRole" in settled
    assert "settledRunId === activeAgentRunId" in settled
    assert 'settledDbId !== String(currentDbId || "")' in settled
    assert "setAgentRunPending(false)" in settled
    assert "syncAcceptedRunResult(settledRunId)" in settled
    assert settled.index("setAgentRunPending(false)") < settled.index(
        "syncAcceptedRunResult(settledRunId)",
    )


def test_same_session_observer_settlement_unlocks_without_clearing_draft() -> None:
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")
    settled = websocket[
        websocket.index('case "run_settled":'):
        websocket.index('case "worldview_updated":')
    ]

    release_start = settled.index("if (tracksSettledRun && roleMatches)")
    sync_index = settled.index("syncAcceptedRunResult(settledRunId)")
    release_block = settled[release_start:sync_index]
    assert "setAgentRunPending(false)" in release_block
    assert "input.disabled=!ready" in release_block
    assert "pendingRunSubmission" not in release_block
    assert "input.value" not in release_block
    assert "pendingSkillId" not in release_block
    assert sync_index > release_start


def test_run_connection_roles_are_explicit_and_cancel_is_owner_only() -> None:
    protocol = (PAGE.parent / "protocol.js").read_text(encoding="utf-8")
    core = (PAGE.parent / "core.js").read_text(encoding="utf-8")
    bindings = (PAGE.parent / "bindings.js").read_text(encoding="utf-8")
    accepted = core[
        core.index("function acceptPendingRunSubmission"):
        core.index("function settlePendingRunSubmissionError")
    ]

    for role in ("OWNER", "OBSERVER", "DETACHED", "UNKNOWN"):
        assert f'{role}: "{role.lower()}"' in protocol
    admission = protocol[
        protocol.index("function runAdmissionConnectionRole"):
        protocol.index("function runSettlementConnectionRole")
    ]
    assert "packet?.run_owned_by_connection === true" in admission
    assert "packet?.owned === true" in admission
    assert "packet?.owned === false" in admission
    assert "activeAgentRunRole = acceptedConnectionRole" in accepted
    assert "document.getElementById(\"runControl\").hidden = !ownsAcceptedRun" in accepted
    assert "canCancelActiveAgentRun()&&ws" in bindings


def test_settlement_before_duplicate_ack_cannot_relock_composer() -> None:
    core = (PAGE.parent / "core.js").read_text(encoding="utf-8")
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")
    accepted = core[
        core.index("function acceptPendingRunSubmission"):
        core.index("function settlePendingRunSubmissionError")
    ]
    settled = websocket[
        websocket.index('case "run_settled":'):
        websocket.index('case "worldview_updated":')
    ]

    assert "rememberAgentRunSettlement(settledRunId, settlementRole)" in settled
    assert "consumeAgentRunSettlement(" in accepted
    assert "acceptedRunId, acceptedConnectionRole" in accepted
    race_release = accepted[
        accepted.index("const settlementAlreadyObserved"):
        accepted.index("setAgentRunPending(true)")
    ]
    assert "terminal || detachedDuplicate || settlementAlreadyObserved" in race_release
    assert "setAgentRunPending(false)" in race_release


def test_reconnect_resume_is_correlated_locked_and_bounded_until_restored() -> None:
    core = (PAGE.parent / "core.js").read_text(encoding="utf-8")
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")

    begin = core[
        core.index("function beginPendingSessionResume"):
        core.index("function transmitPendingSessionResume")
    ]
    transmit = core[
        core.index("function transmitPendingSessionResume"):
        core.index("function schedulePendingSessionResumeRetry")
    ]
    matcher = core[
        core.index("function matchesPendingSessionResume"):
        core.index("function settlePendingSessionResume")
    ]
    assert 'nextTransientRequestId("resume-session")' in begin
    assert "attempts:0" in begin
    assert "SESSION_RESUME_MAX_ATTEMPTS" in transmit
    assert 'type:"resume_session"' in transmit
    assert "request_id:pending.requestId" in transmit
    assert "setSessionResumeUi(" in transmit
    for field in (
        'message?.operation || ""', 'message?.request_id || ""',
        'message?.requested_db_id || ""',
        'message?.runtime_session_id || ""',
    ):
        assert field in matcher

    error = core[
        core.index("function handlePendingSessionResumeError"):
        core.index("function runSubmissionPayload")
    ]
    assert 'message?.code === "session_busy"' in error
    assert "schedulePendingSessionResumeRetry()" in error
    ready = websocket[
        websocket.index('case "session_ready":'):
        websocket.index('case "session_persisted":')
    ]
    restored = websocket[
        websocket.index('case "session_restored":'):
        websocket.index('case "session_history_reset":')
    ]
    assert "beginPendingSessionResume(last)" in ready
    assert "settlePendingSessionResume(msg)" in restored


def test_run_send_requires_rendered_and_authoritative_session_identity_match() -> None:
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")
    send = websocket[
        websocket.index("function sendMessage()"):
        websocket.index("function loadSessionMessages")
    ]

    assert "pendingSessionResume" in send
    assert 'String(renderedSessionId || "") !== String(currentDbId || "")' in send
    assert send.index('String(renderedSessionId || "")') < send.index(
        "pendingRunSubmission = {",
    )


def test_workspace_observes_typed_tool_events_not_legacy_transport() -> None:
    page = js_bundle()

    # The workspace observer is part of the typed event chain (kept as a no-op
    # now that file/service tracking is folded into the KANBAN board).
    assert "Workspace observes the same typed event contract as TimelineRenderer" in page
    assert "observeWorkspaceEvent(event);" in page
    assert "function observeWorkspaceEvent(event)" in page
    # The KANBAN board consumes tool activity via semantic projection instead.
    assert "countByCategory(activities)" in page


def test_websocket_messages_use_one_explicit_routing_entrypoint() -> None:
    page = js_bundle()

    assert page.count("function handleMsg(msg)") == 1
    assert "function handleControlMessage(msg)" in page
    assert "if (handleControlMessage(msg)) return;" in page
    assert "handleMsg = function" not in page
    assert "originalHandleMsg" not in page
    assert "_origHandleMsg_ws" not in page
    for message_type in (
        "models_list",
        "model_repository_updated",
        "model_discovery_result",
        "artifact_content",
        "peri_git_readiness",
        "skills_list",
        "extensions_list",
        "skill_fetched",
        "mcp_servers_list",
        "memory_list",
    ):
        assert f'case "{message_type}":' in page

    assert "nonRenderingCompatibilityMessages" not in page
    assert 'console.warn("[Modus] Unhandled WebSocket message", msg.type);' in page


def test_session_view_reset_clears_workspace_and_all_event_projections() -> None:
    page = js_bundle()

    timeline_reset = page[page.index("function clearTimelineState()"):page.index("function clearTranscriptState()")]
    reset = page[page.index("function clearTranscriptState()"):page.index("function applyTranscriptEvent")]
    assert "timelineRenderer.reset();" in timeline_reset
    assert "clearWorkspaceState();" in timeline_reset
    assert "clearTimelineState();" in reset
    assert "workbenchStore.reset();" in reset
    assert "function clearWorkspaceState()" in page
    # The workspace tracker is folded into the KANBAN board; reset re-renders it.
    assert "ModusKanban.refreshBoard" in page

    loader = page[page.index("function loadSessionMessages"):page.index("// Batch delete helper")]
    assert 'ca.querySelectorAll(".msg, .timeline-item, .run-completion, .empty-state, .collab-msg, .timeline-expand-earlier")' in loader
    assert "ModusKanban.closeDrawer" in loader
    assert "clearTranscriptState();" in loader

    for message_type in ("transcript_reset", "session_history_reset"):
        start = page.index(f'case "{message_type}":')
        end = page.index(f'case "{("transcript_ops" if message_type == "transcript_reset" else "session_history_start")}":', start)
        assert "loadSessionMessages(" in page[start:end]

    created = page[page.index('case "session_created":'):page.index('case "session_deleted":')]
    assert 'loadSessionMessages(currentDbId, [], msg.worldview || "");' in created


def test_archived_session_ui_requires_restore_before_open_or_run() -> None:
    page = js_bundle()

    assert 'el.classList.contains("archived")' in page
    assert "请先从会话菜单中取消归档" in page
    assert '["session_not_found", "session_archived"].includes(msg.code)' in page
    assert 'msg.active_reset && msg.operation === "run_message"' in page
    assert "setAgentRunPending(false); controlMutationPending = false;" in page


def test_external_session_invalidation_clears_stale_runtime_and_transcript_state() -> None:
    page = js_bundle()
    deleted = page[page.index('case "session_deleted":'):page.index('case "session_archived":')]
    archived = page[page.index('case "session_archived":'):page.index('case "session_export_ready":')]

    for branch in (deleted, archived):
        assert "msg.invalidated_db_id" in branch
        assert "currentDbId === invalidatedDbId" in branch
        assert "setAgentRunPending(false); controlMutationPending = false; waiting = false;" in branch
        assert "delete transcriptCursorsBySession[invalidatedDbId]" in branch
        assert 'loadTranscriptCursors("")' in branch
        assert "transcriptGapRequests.clear(); finishRunReplay();" in branch
        assert 'loadSessionMessages("", [])' in branch
        assert "finishControlMutation();" in branch
        assert "ws && !msg.external_invalidation" in branch
    assert "会话已在另一窗口删除，已进入新对话" in deleted
    assert "会话已在另一窗口归档，已进入新对话" in archived
    stale_guard = page[
        page.index("function isStaleSessionInvalidation"):
        page.index("function handleMsg(msg)")
    ]
    assert 'msg.invalidated_db_id || msg.deleted_db_id || msg.archived_db_id' in stale_guard
    assert "currentDbId !== invalidatedDbId" in stale_guard
    router = page[page.index("function handleMsg(msg)"):page.index("function sendMessage()")]
    assert router.index("isStaleSessionInvalidation(msg)") < router.index("handleControlMessage(msg)")


def test_mode_switch_has_no_rendering_ownership_fallback() -> None:
    page = js_bundle()
    message_router = page[page.index("function handleMsg(msg)"):page.index("function sendMessage()")]

    assert "function setMode(mode)" in page
    assert "activeTypedMode" not in page
    assert "hasTypedRendererFor" not in page
    assert "addUserMsg" not in page
    assert "case \"text_delta\":" not in message_router
    assert "case \"thinking_delta\":" not in message_router
    assert "case \"tool_call\":" not in message_router
    assert "case \"tool_result\":" not in message_router
    assert "case \"moa_start\":" not in message_router
    assert "case \"moa_done\":" not in message_router
    assert "case \"peri_sub_start\":" not in message_router


def test_run_completed_renders_authoritative_semantic_outcome() -> None:
    page = js_bundle()

    assert "_renderRunCompletion(event, container)" in page
    assert "this._renderRunCompletion(event, container);" in page
    assert "const semantic = run?.semantic || {}" in page
    assert "outcome.summary" in page
    assert "outcome.recovery_count" in page
    assert "自动恢复 " in page
    assert "data-completion-evidence" in page
    assert 'window.ModusWorkbenchWindows.activate("review")' in page
    assert "budget.elapsed_seconds" in page


def test_verification_failure_exposes_a_bounded_retry_action() -> None:
    page = js_bundle()

    assert 'type:"retry_verification"' in page
    assert 'data-verification-retry' in page
    assert "verification_retry_limit" in page
    assert "继续修复并验证" in page
    assert 'case "verification_retry_started":' in page
    assert "run-error-evidence" in page
    assert "last_evidence" in page


def test_verification_retry_is_a_correlated_admission_intent() -> None:
    core = (PAGE.parent / "core.js").read_text(encoding="utf-8")
    timeline = (PAGE.parent / "timeline.js").read_text(encoding="utf-8")
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")

    begin = core[
        core.index("function beginVerificationRetry"):
        core.index("function matchesPendingVerificationRetry")
    ]
    matcher = core[
        core.index("function matchesPendingVerificationRetry"):
        core.index("function acceptPendingVerificationRetry")
    ]
    accept = core[
        core.index("function acceptPendingVerificationRetry"):
        core.index("function settlePendingVerificationRetryError")
    ]
    payload = core[
        core.index("function verificationRetryPayload"):
        core.index("function transmitPendingVerificationRetry")
    ]
    retry_case = websocket[
        websocket.index('case "verification_retry_started":'):
        websocket.index('case "error":')
    ]

    assert 'requestId:nextTransientRequestId("verification-retry")' in begin
    assert 'type:"retry_verification"' in payload
    assert "run_id:pending.priorRunId" in payload
    assert "request_id:pending.requestId" in payload
    for identity in (
        'message?.operation || ""', 'message?.request_id || ""',
        'message?.prior_run_id || ""', 'message?.runtime_session_id || ""',
        'message?.db_id || ""', 'String(sessionId || "")',
        'String(currentDbId || "")', 'String(serverEpoch || "")',
    ):
        assert identity in matcher
    assert 'const acceptedRunId = String(message?.run_id || "")' in accept
    assert "activeAgentRunId = acceptedRunId" in accept
    assert "activeVerificationRetryPriorRunId = pending.priorRunId" in accept
    assert "verificationRetryConsumedRuns.add(pending.priorRunId)" in accept
    assert "activeAgentRunRole = ModusProtocol.RUN_CONNECTION_ROLES.OWNER" in accept
    assert "setAgentRunPending(true)" in accept
    assert 'document.getElementById("runControl").hidden = false' in accept
    assert "acceptPendingVerificationRetry(msg)" in retry_case
    assert "beginVerificationRetry(runId)" in timeline
    assert 'ws.send(JSON.stringify({type:"retry_verification"' not in timeline


def test_verification_retry_error_and_disconnect_release_only_matching_intent() -> None:
    core = (PAGE.parent / "core.js").read_text(encoding="utf-8")
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")

    error_settler = core[
        core.index("function settlePendingVerificationRetryError"):
        core.index("function abandonPendingVerificationRetry")
    ]
    abandon = core[
        core.index("function abandonPendingVerificationRetry"):
        core.index("function showVerificationRetryReconnectNotice")
    ]
    error_case = websocket[
        websocket.index('case "error":'):
        websocket.index('case "session_reasoning_updated":')
    ]
    close = websocket[
        websocket.index("ws.onclose = () => {"):
        websocket.index("ws.onerror = () => ws.close();")
    ]

    assert 'String(message?.operation || "") !== "retry_verification"' in error_settler
    assert "if (!matchesPendingVerificationRetry(message)) return false" in error_settler
    assert 'setVerificationRetryButtonState(pending.priorRunId, "ready")' in error_settler
    assert "pendingVerificationRetry = null" in error_settler
    assert "finishControlMutation()" in error_settler
    assert "settlePendingVerificationRetryError(msg)" in error_case
    assert "if (verificationRetryError === false) break" in error_case
    assert 'abandonPendingVerificationRetry("socket_close", {release:false})' in close
    assert "verificationRetryReconnectNotice = true" in abandon
    assert "showVerificationRetryReconnectNotice()" in websocket
    assert "settleActiveVerificationRetryUi()" in close
    settled = websocket[
        websocket.index('case "run_settled":'):
        websocket.index('case "worldview_updated":')
    ]
    assert "settleActiveVerificationRetryUi()" in settled


def test_task_tree_is_owned_by_authoritative_workbench_projection() -> None:
    page = js_bundle()
    workbench = (PAGE.parent / "workbench.js").read_text()

    assert "class CollaborationProcessStore" not in page
    assert "collaborationProcessStore.observe(event);" not in page
    assert "taskContainer: null" in page
    assert "workbenchStore.observe(event);" in page
    assert "_taskHtml(run, task, depth)" in workbench
    assert "function applyTranscriptEvents(events)" in page
    assert "kbBoard" in page


def test_collaboration_process_has_no_manual_child_side_channel() -> None:
    page = js_bundle()

    for obsolete in (
        "legacyTasks", "observeLegacy(", "let children", "activeChild",
        'case "child_spawned":', 'case "child_status":',
        'case "child_text_delta":', 'case "child_done":',
        'case "child_system":', 'case "child_dismissed":',
        'case "contradiction_reported":',
    ):
        assert obsolete not in page


def test_worldview_control_messages_project_to_current_focus_without_chat_noise() -> None:
    page = js_bundle()

    assert 'case "worldview_updated":' in page
    assert 'case "worldview_evolving":' not in page
    assert 'case "worldview_evolved":' not in page
    assert "function setFocusState" in page
    updated = page[page.index('case "worldview_updated":'):page.index('case "cancel_requested":')]
    assert "addSystemMsg(" not in updated


def test_cancel_request_keeps_composer_locked_until_run_settled() -> None:
    page = js_bundle()

    start = page.index('case "cancel_requested":')
    end = page.index("break;", start)
    cancel_case = page[start:end]
    assert 'setActivity("◌", "正在停止…", "busy")' in cancel_case
    assert 'document.getElementById("stopBtn").disabled=true' in cancel_case
    assert "waiting=false" not in cancel_case
    assert "input.disabled=false" not in cancel_case

    done = page[page.index('case "done":'):page.index('case "run_settled":')]
    assert "waiting=false" not in done
    assert "input.disabled=false" not in done
    assert 'document.getElementById("stopBtn").disabled=false' not in done
    assert "requestWorkbenchSnapshot();" in done

    settled = page[page.index('case "run_settled":'):page.index('case "worldview_updated":')]
    assert "setAgentRunPending(false)" in settled
    assert "waiting=false" in settled
    assert "input.disabled=!ready" in settled
    assert 'document.getElementById("stopBtn").disabled=false' in settled
    assert "function setAgentRunPending(pending)" in page
    pending_setter = page[
        page.index("function setAgentRunPending(pending)"):
        page.index("let pendingSessionCreateKey")
    ]
    assert "workbenchStore.render();" in pending_setter
    assert "activeAgentRunId = null" in pending_setter

    agent_event = page[page.index('case "agent_event":'):page.index('case "transcript_reset":')]
    assert 'msg.event?.type === "run_started"' in agent_event
    assert "activeAgentRunId = String(msg.event.run_id" in agent_event
    assert 'settledRuntimeId !== String(sessionId || "")' in settled
    assert "activeAgentRunId" in settled
    assert "settledRunId === activeAgentRunId" in settled


def test_control_errors_preserve_only_the_run_owned_by_this_window() -> None:
    page = js_bundle()

    start = page.index('case "error":')
    end = page.index('case "session_reasoning_updated":', start)
    error_case = page[start:end]
    assert 'msg.run_owned_by_connection === false' in error_case
    assert "&& !agentRunPending" in error_case
    assert "if (rejectedByForeignRun) setAgentRunPending(false);" in error_case
    busy_start = error_case.index("if (!rejectedByForeignRun")
    busy_end = error_case.index("} else {", busy_start)
    busy_branch = error_case[busy_start:busy_end]

    assert "agentRunPending" in busy_branch
    assert "waiting=true" in busy_branch
    assert "input.disabled=true" in busy_branch
    assert "sendBtn.disabled=true" in busy_branch
    assert "const ownsTrackedRun = canCancelActiveAgentRun();" in busy_branch
    assert 'document.getElementById("runControl").hidden=!ownsTrackedRun' in busy_branch
    assert "waiting=false" not in busy_branch
    assert "input.disabled=false" not in busy_branch


def test_new_session_intent_is_not_created_during_active_work_and_is_cleared_on_rejection() -> None:
    page = js_bundle()

    create = page[page.index("function sendSessionCreate"):page.index("function clearSessionCreateIntent")]
    assert "agentRunPending" in create
    assert "controlMutationPending" in create
    assert "waiting" in create
    assert "pendingSessionCreateKey" in create
    clear = page[page.index("function clearSessionCreateIntent"):page.index("function showSettingsPanel")]
    assert "pendingSessionCreateKey = null" in clear
    assert "creatingSession = false" in clear
    assert 'removeAttribute("disabled")' in clear
    error_case = page[page.index('case "error":'):page.index('case "session_reasoning_updated":')]
    assert 'msg.operation === "session_create"' in error_case
    assert 'clearSessionCreateIntent(String(msg.request_key || ""))' in error_case


def test_frontend_tracks_session_host_separately_from_repository_default() -> None:
    page = js_bundle()

    assert 'currentModelId = ""' in page
    assert "function selectedHostModel()" in page
    assert "currentModelId || modelRepository.selection.default_model_id" in page
    assert "repository_revision" in page
    assert "catalog_revision" in page
    assert "skills_revision" in page
    assert "extensions_revision" in page


def test_frontend_session_catalog_uses_correlated_server_search_and_pagination() -> None:
    page = js_bundle()
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")
    bindings = (PAGE.parent / "bindings.js").read_text(encoding="utf-8")

    assert "function requestSessionCatalog({append=false}={})" in websocket
    assert 'nextTransientRequestId("session-catalog")' in websocket
    for field in (
        'type:"sessions_list"', "request_id:requestId", "query",
        "include_archived:includeArchived", "cursor", "limit:SESSION_CATALOG_PAGE_SIZE",
    ):
        assert field in websocket
    assert 'case "sessions_changed":' in websocket
    assert "refreshSessionCatalog();" in websocket
    response = websocket[
        websocket.index('case "sessions_list":'):
        websocket.index('case "mode_updated":')
    ]
    assert "msg.request_id !== pendingCatalog.requestId" in response
    assert "responseQuery !== pendingCatalog.query" in response
    assert "Boolean(msg.include_archived) !== pendingCatalog.includeArchived" in response
    assert "const loaded = new Map(sessionCatalogSessions.map" in response
    assert "sessionCatalogNextCursor = msg.next_cursor || null" in response
    assert 'id="sessionCatalogLoadMore"' in response
    assert "requestSessionCatalog({append:true})" in response
    assert "全选已载入" in response
    assert "loadedSessionCatalogSelection()" in response
    assert "sessionCatalogSelectedIds" in response
    assert "visibleSessions.length + '/' + totalCount" in response

    assert "setTimeout(() =>" in bindings
    assert ", 250);" in bindings
    assert "sessionCatalogQuery = String(query || \"\").trim()" in bindings
    search = bindings[
        bindings.index("sessionSearchInput.oninput"):
        bindings.index('document.getElementById("newChatBtn")')
    ]
    assert "filterSessions(" not in search


def test_server_epoch_resets_process_local_revision_gates_and_stale_intents() -> None:
    page = js_bundle()

    observer = page[
        page.index("function observeServerEpoch"):
        page.index("// Transcript cursors live", page.index("function observeServerEpoch"))
    ]
    assert 'let serverEpoch = ""' in page
    assert 'message?.server_epoch' in observer
    for revision in (
        "repositoryRevision", "sessionCatalogRevision", "skillsRevision",
        "extensionsRevision",
    ):
        assert f"{revision} = 0" in observer
    assert "setAgentRunPending(false)" in observer
    assert "controlMutationPending = false" in observer
    assert "clearSessionCreateIntent()" in observer
    assert 'resetTransientRequests("server_epoch")' in observer

    router = page[page.index("function handleMsg(msg)"):page.index("function sendMessage()")]
    assert router.index("observeServerEpoch(msg)") < router.index("handleControlMessage(msg)")
    connect = page[page.index("function modusConnectSocket()"):page.index("function handleControlMessage")]
    onopen = connect[connect.index("ws.onopen"):connect.index("ws.onmessage")]
    assert 'type:"session_create"' not in onopen
    assert 'type:"resume_session"' not in onopen
    assert 'type:"model_repository_get"' not in onopen
    ready = page[page.index('case "session_ready":'):page.index('case "session_persisted":')]
    assert "creatingSession && pendingSessionCreateKey" in ready
    assert 'type:"session_create"' in ready
    assert "beginPendingSessionResume(last)" in ready
    resume_sender = page[
        page.index("function transmitPendingSessionResume"):
        page.index("function schedulePendingSessionResumeRetry")
    ]
    assert 'type:"resume_session"' in resume_sender
    assert 'if (!protocolCompatible)' in ready
    assert "renderDesktopProtocolMismatch()" in ready


def test_desktop_protocol_handshake_replaces_infinite_catalog_loading_with_action() -> None:
    from modus.desktop import server
    from _bundle import css_bundle, page_html

    page = js_bundle()
    html = page_html()
    identity = server._session_identity(
        server.DaoSession(id="runtime", engine=None),
    )

    assert identity["desktop_protocol_version"] == server.DESKTOP_PROTOCOL_VERSION
    assert "DESKTOP_PROTOCOL_VERSION = 2" in page
    assert "function acceptDesktopProtocol" in page
    assert "function renderDesktopProtocolMismatch" in page
    assert "服务版本较旧" in page
    assert "请重启 Modus Desktop，再刷新此页面。" in page
    assert "desktopProtocolCompatible === false" in page
    assert "sb-protocol-mismatch" in css_bundle()


def test_transient_async_controls_reset_on_disconnect_and_ignore_stale_responses() -> None:
    page = js_bundle()

    core = page[
        page.index("const transientRequestResetters"):
        page.index("let controlMutationPending")
    ]
    assert "function nextTransientRequestId" in core
    assert "function registerTransientRequestReset" in core
    assert "function resetTransientRequests" in core

    connect = page[page.index("function modusConnectSocket()"):page.index("function handleControlMessage")]
    assert 'resetTransientRequests("socket_close")' in connect
    assert 'resetTransientRequests("connect_failed")' in connect
    for registration in (
        'registerTransientRequestReset("model-test"',
        'registerTransientRequestReset("model-discovery"',
        'registerTransientRequestReset("credential-migration"',
        'registerTransientRequestReset("peri-readiness"',
        'registerTransientRequestReset("skill-fetch"',
        'registerTransientRequestReset("artifact"',
    ):
        assert registration in page

    router = page[page.index("function handleControlMessage"):page.index("function isStaleSessionInvalidation")]
    assert "msg.request_id === pendingModelDiscoveryRequestId" in router
    assert "msg.request_id === pendingCredentialMigrationRequestId" in router
    assert "msg.request_id === pendingPeriReadinessRequestId" in router
    assert "msg.request_id === pendingSkillFetchRequestId" in router
    assert "settleArtifactResponse(msg)" in router

    error_case = page[page.index('case "error":'):page.index('case "session_reasoning_updated":')]
    assert "settleTransientRequestError(msg)" in error_case
    for operation in (
        "model_discover", "credential_migration_report", "credential_migration_run",
        "peri_git_readiness", "skill_fetch_url", "artifact_get",
    ):
        assert f'case "{operation}":' in page
    settlement = page[
        page.index("function settleTransientRequestError"):
        page.index("// Skills import: template")
    ]
    assert settlement.count("requestId !== pending") == 5
    assert settlement.count("requestId !== pendingModelDiscoveryRequestId) return true") == 1
    assert settlement.count("requestId !== pendingCredentialMigrationRequestId) return true") == 1
    assert settlement.count("requestId !== pendingPeriReadinessRequestId) return true") == 1
    assert settlement.count("requestId !== pendingSkillFetchRequestId) return true") == 1
    assert "return settleArtifactError(message)" in settlement


def test_artifact_viewer_transport_rejects_stale_or_cross_session_responses() -> None:
    page = js_bundle()

    request = page[
        page.index("function requestArtifactContent"):
        page.index("function renderInlineArtifactContent")
    ]
    assert 'nextTransientRequestId("artifact")' in request
    assert "pendingArtifactRequests.clear()" in request
    assert "pendingArtifactRequests.set(id" in request
    assert 'type:"artifact_get"' in request
    assert "request_id:requestId" in request
    assert "session_id:requestedSessionId" in request
    assert "openArtifactViewer(metadata)" in request

    matcher = page[
        page.index("function artifactRequestMatches"):
        page.index("registerTransientRequestReset(\"artifact\"")
    ]
    for identity in (
        'message?.operation ?? ""', 'message?.request_id ?? ""',
        'message?.artifact_id ?? ""', 'message?.requested_session_id ?? ""',
        'message?.session_id ?? ""', 'currentDbId ?? ""',
        'message?.artifact?.artifact_id ?? ""',
    ):
        assert identity in matcher
    assert "pendingArtifactRequests.delete(artifactId)" in matcher
    assert "renderArtifactViewerContent" in matcher
    assert "renderArtifactViewerError" in matcher
    assert "超过 200 KB" in matcher

    content_case = page[
        page.index('case "artifact_content":'):
        page.index('case "workbench_snapshot":')
    ]
    assert "settleArtifactResponse(msg)" in content_case
    assert "renderInlineArtifactContent" in content_case
    assert 'registerTransientRequestReset("artifact", resetArtifactRequests)' in page
    transition = page[
        page.index("function beginWorkbenchSessionTransition"):
        page.index("function requestWorkbenchSnapshot")
    ]
    assert "resetArtifactRequests" in transition
    assert 'beginWorkbenchSessionTransition("session_create")' in page


def test_transient_requests_enter_loading_only_after_an_open_socket_check() -> None:
    page = js_bundle()

    ranges = (
        ("function startModelDiscovery", 'registerTransientRequestReset("model-discovery"'),
        ('document.getElementById("credMigrationBtn").onclick', "let _credMigrationReport"),
        ('document.getElementById("periReadinessBtn").onclick', "function renderGitReadiness"),
        ('document.getElementById("skillFetchBtn").onclick', 'registerTransientRequestReset("skill-fetch"'),
    )
    for start_marker, end_marker in ranges:
        block = page[page.index(start_marker):page.index(end_marker, page.index(start_marker))]
        assert "ws.readyState !== WebSocket.OPEN" in block
        assert block.index("ws.readyState !== WebSocket.OPEN") < block.index("ws.send(")

    discovery = page[page.index("function startModelDiscovery"):page.index('registerTransientRequestReset("model-discovery"')]
    assert "request_id:pendingModelDiscoveryRequestId" in discovery
    readiness = page[page.index('document.getElementById("periReadinessBtn").onclick'):page.index("function renderGitReadiness")]
    assert "request_id:pendingPeriReadinessRequestId" in readiness
    skill = page[page.index('document.getElementById("skillFetchBtn").onclick'):page.index('registerTransientRequestReset("skill-fetch"')]
    assert "request_id:pendingSkillFetchRequestId" in skill


def test_model_and_mode_selection_wait_for_backend_confirmation() -> None:
    page = js_bundle()

    assert "function beginControlMutation()" in page
    assert "function finishControlMutation()" in page
    choose_default = page[page.index("function chooseDefault"):page.index("function chooseReasoning")]
    choose_reasoning = page[page.index("function chooseReasoning"):page.index("function chooseMode")]
    choose_mode = page[page.index("function chooseMode"):page.index("function sendSessionCreate")]
    assert 'beginSessionExecutionMutation("session_set_model")' in choose_default
    assert 'type:"session_set_model"' in choose_default
    assert 'type:"model_select_default"' not in choose_default
    assert 'setMode("default")' not in choose_default
    assert "currentReasoningEffort =" not in choose_reasoning
    assert 'beginSessionExecutionMutation("session_set_reasoning")' in choose_reasoning
    assert "setMode(mode)" not in choose_mode
    assert "beginControlMutation()" in choose_mode
    assert '(msg.message_count || 0) > 0' in page
    error_case = page[page.index('case "error":'):page.index('case "session_reasoning_updated":')]
    assert "finishControlMutation();" in error_case


def test_session_execution_mutations_reject_late_or_cross_session_responses() -> None:
    page = js_bundle()

    correlation = page[
        page.index("function beginSessionExecutionMutation"):
        page.index("function chooseDefault")
    ]
    for identity in (
        "message?.operation", "message?.request_id", "message?.requested_db_id",
        "message?.db_id", "message?.runtime_session_id", "currentDbId", "sessionId",
    ):
        assert identity in correlation
    assert 'nextTransientRequestId(operation)' in correlation
    assert 'registerTransientRequestReset("session-execution-mutation"' in correlation

    choose_default = page[page.index("function chooseDefault"):page.index("function chooseReasoning")]
    choose_reasoning = page[page.index("function chooseReasoning"):page.index("function chooseMode")]
    choose_mode = page[page.index("function chooseMode"):page.index("function sendSessionCreate")]
    for operation, block in (
        ("session_set_model", choose_default),
        ("session_set_reasoning", choose_reasoning),
        ("session_set_mode", choose_mode),
    ):
        assert f'beginSessionExecutionMutation("{operation}")' in block
        assert "request_id:pending.requestId" in block
        assert "session_id:pending.dbId" in block

    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")
    success_cases = (
        ("mode_updated", "session_set_mode"),
        ("session_reasoning_updated", "session_set_reasoning"),
        ("session_model_updated", "session_set_model"),
    )
    for message_type, operation in success_cases:
        start = websocket.index(f'case "{message_type}":')
        end = websocket.index('case "', start + len(f'case "{message_type}":'))
        branch = websocket[start:end]
        guard = f'if (!settleSessionExecutionMutation(msg, "{operation}")) break;'
        assert guard in branch
        assert branch.index(guard) < branch.index("current")

    errors = page[page.index("function settleTransientRequestError"):page.index("// Skills import: template")]
    for operation in ("session_set_model", "session_set_reasoning", "session_set_mode"):
        assert f'case "{operation}":' in errors
    assert "if (!settleSessionExecutionMutation(message, message.operation)) return true;" in errors


def test_session_execution_mutation_is_cleared_on_switch_disconnect_and_restart() -> None:
    page = js_bundle()
    core = (PAGE.parent / "core.js").read_text(encoding="utf-8")
    websocket = (PAGE.parent / "websocket.js").read_text(encoding="utf-8")

    transition = core[
        core.index("function beginWorkbenchSessionTransition"):
        core.index("function requestWorkbenchSnapshot")
    ]
    assert 'resetTransientRequest("session-execution-mutation", _reason)' in transition
    switch = websocket[
        websocket.index('beginWorkbenchSessionTransition("session_switch")') - 200:
        websocket.index('beginWorkbenchSessionTransition("session_switch")') + 250
    ]
    assert 'beginWorkbenchSessionTransition("session_switch")' in switch
    connect = websocket[
        websocket.index("function modusConnectSocket()"):
        websocket.index("function handleControlMessage")
    ]
    assert 'resetTransientRequests("socket_close")' in connect
    epoch = core[core.index("function observeServerEpoch"):core.index("// Transcript cursors")]
    assert 'resetTransientRequests("server_epoch")' in epoch


def test_mode_configuration_saves_wait_for_repository_confirmation() -> None:
    page = js_bundle()

    save_mode = page[
        page.index("function saveModeModelConfiguration"):
        page.index('document.getElementById("moaSaveBtn")')
    ]
    assert "beginControlMutation()" in save_mode
    assert 'type:"mode_models_set"' in save_mode
    assert "ws.send(" in save_mode
    assert 'saveModeModelConfiguration("moa"' in page
    assert 'saveModeModelConfiguration("peri"' in page
    mode_controls = page[
        page.index('document.getElementById("moaSaveBtn")'):
        page.index('document.getElementById("periReadinessBtn")')
    ]
    assert 'type:"mode_models_set"' not in mode_controls

    repository_case = page[
        page.index('case "model_repository_updated":'):
        page.index('case "model_discovery_result":')
    ]
    assert "msg.origin_runtime_session_id === sessionId" in repository_case
    assert "finishControlMutation();" in repository_case
    error_case = page[
        page.index('case "error":'):
        page.index('case "session_reasoning_updated":')
    ]
    assert "finishControlMutation();" in error_case


def test_skill_and_mcp_mutations_wait_for_shared_capability_confirmation() -> None:
    page = js_bundle()

    assert "function sendCapabilityMutation(payload)" in page
    assert 'sendCapabilityMutation({type:"skill_create"' in page
    assert 'sendCapabilityMutation({type:"skill_delete"' in page
    for message_type in (
        "mcp_server_add", "mcp_server_remove", "mcp_server_connect",
        "mcp_server_disconnect",
    ):
        assert f'sendCapabilityMutation({{type:"{message_type}"' in page
    assert 'msg.origin_runtime_session_id === sessionId) finishControlMutation();' in page


def test_every_top_level_runner_refreshes_the_session_catalog() -> None:
    server = (PAGE.parents[1] / "server.py").read_text()

    moa = server[server.index("async def _run_moa_session"):server.index("async def _run_peri_session")]
    default = server[server.index("async def _stream_to_ws"):server.index("def _extract_worldview")]
    assert "broadcast_catalog=False" in moa
    assert "_broadcast_sessions_list(completed_runtime=session)" in moa
    assert "if broadcast_catalog:" in default


def test_system_messages_are_text_only_and_have_one_renderer() -> None:
    page = js_bundle()

    assert page.count("function addSystemMsg(") == 1
    renderer = page[page.index("function addSystemMsg("):page.index("function addUserBubble")]
    assert "textContent" in renderer
    assert "innerHTML" not in renderer
    assert 'addSystemMsg("✓ 会话已导出：" + anchor.download)' in page


def test_session_memory_has_a_reference_only_management_loop() -> None:
    page = js_bundle()
    html = page_html()

    assert 'data-tab="memory"' in html
    assert 'id="memoryList"' in html
    assert 'id="memoryAddBtn"' in html
    assert 'id="memoryClearBtn"' in html
    assert "function renderMemories" in page
    assert 'type:"memory_get"' in page
    assert 'type:"memory_add"' in page
    assert 'type:"memory_archive"' in page
    assert 'type:"memory_clear"' in page
    assert "仅供参考" in page


def test_session_reference_can_be_added_by_id_or_from_a_session_menu() -> None:
    page = js_bundle()
    html = page_html()

    assert 'id="sessionReferenceId"' in html
    assert 'id="sessionReferenceAddBtn"' in html
    assert "function requestSessionReference(sourceSessionId)" in page
    assert 'type:"session_reference_add"' in page
    assert 'data-action="reference"' in page
    assert 'case "session_reference_added":' in page
    assert 'class="memory-reference"' in page
    assert "查看脱敏参考" in page


def test_change_review_card_is_owned_by_authoritative_workbench_projection() -> None:
    page = js_bundle()
    html = page_html()
    workbench = (PAGE.parent / "workbench.js").read_text()

    assert "reviewContainer: null" in page
    assert "changeReviewStore.observe(event);" not in page
    assert "_reviewHtml(review)" in workbench
    assert "查看 Diff" in workbench
    assert "review.verifications" in workbench
    assert "review.files" in workbench
    assert "modus.change-review.v1" in (PAGE.parents[1] / "workbench.py").read_text()


def test_new_session_resets_all_typed_event_projections_through_one_lifecycle_function() -> None:
    page = js_bundle()

    created = page[page.index('case "session_created":'):page.index('case "session_deleted":')]
    assert 'loadSessionMessages(currentDbId, [], msg.worldview || "");' in created
    assert "eventStore.byId = new Map()" not in created
    assert "timelineRenderer.elements = new Map()" not in created


def test_settings_is_a_standalone_page_not_a_floating_modal() -> None:
    html = page_html()

    # The settings container is a fixed full-page view (settings-view), not a
    # centered modal-overlay stacking on top of the chat.
    assert 'class="settings-view" id="settingsModal"' in html
    assert '<div class="settings-layout">' in html
    assert '<nav class="settings-nav"' in html
    assert 'class="settings-nav-logo"' not in html
    assert '<main class="settings-content">' in html
    # The old modal wrapper (.modal-hd / .modal-body) is gone from settings.
    settings_markup = html[html.index('id="settingsModal"'):html.index('id="confirmModal"')]
    assert 'class="modal-hd"' not in settings_markup
    assert 'class="modal-body"' not in settings_markup


def test_settings_page_keeps_nav_and_panel_contract() -> None:
    html = page_html()

    # Every data-tab maps to a matching settings-panel id (the contract the
    # tab switcher and the settings.js handlers rely on).
    import re

    tabs = re.findall(r'data-tab="([a-z]+)"', html)
    assert len(tabs) >= 8
    for tab in tabs:
        assert f'id="panel-{tab}"' in html, tab
    # Every settings panel must be nested inside the settings-content <main>,
    # otherwise a tab's content escapes the left-nav layout and stacks above
    # the sidebar (the regression where non-repo tabs rendered top-to-bottom).
    content_open = html.index('<main class="settings-content">')
    content_close_marker = "</main>"
    content_section = html[content_open:html.index(content_close_marker, content_open)]
    for tab in tabs:
        assert f'id="panel-{tab}"' in content_section, f"panel-{tab} outside settings-content"
    # The memory management loop keeps its IDs even though the page is full-screen.
    assert 'id="memoryList"' in html
    assert 'id="memoryAddBtn"' in html
    assert 'id="memoryClearBtn"' in html


def test_settings_mode_group_keeps_session_moa_peri_together() -> None:
    """会话 / MOA / Peri 聚合在"模式"分类下，由内部子 Tab 切换。

    The mode group is a single top-level panel that nests the three mode
    panels plus a subtab row; every other panel stays a top-level sibling so
    the left-nav layout is never broken by an escaped panel.
    """
    html = page_html()

    import re

    # The top-level nav has one "mode" entry carrying the three sub-tabs.
    assert 'data-tab="mode"' in html
    assert '<div class="mode-subtabs"' in html
    for subtab in ("session", "moa", "peri"):
        assert f'class="mode-subtab' in html, subtab
        assert f'data-tab="{subtab}"' in html, subtab
        assert f'id="panel-{subtab}"' in html, subtab
        assert f'data-mode-panel="{subtab}"' in html, subtab
    # Mode panels live inside panel-mode; other panels must NOT be inside it.
    mode_start = html.index('id="panel-mode"')
    memory_idx = html.index('id="panel-memory"')
    skills_idx = html.index('id="panel-skills"')
    # panel-mode closes before the next top-level panel begins, so memory and
    # skills are siblings of panel-mode rather than children.
    assert memory_idx > mode_start
    assert skills_idx > mode_start
    # The role-card structure for MOA/Peri keeps the model/temperature IDs.
    assert 'role-card' in html
    assert 'class="role-badge"' in html
    assert 'id="moaHostModel"' in html
    assert 'id="moaHostTemp"' in html
    assert 'id="periSub1Model"' in html or 'id="periSub1"' in html
    assert 'id="periSub1Temp"' in html

    page = js_bundle()
    # The subtab switcher drives the mode panels; a direct openSettings("moa")
    # still lands on the moa subtab.
    assert "function showModeSubtab" in page
    assert 'MODE_SUBTABS = ["session", "moa", "peri"]' in page
    assert 'showModeSubtab(MODE_SUBTABS.includes(name) ? name : "session")' in page


def test_settings_page_is_reachable_from_both_entries() -> None:
    page = js_bundle()
    html = page_html()

    # Desktop sidebar and the mobile top bar both open settings.
    assert 'document.getElementById("settingsBtn").onclick = () => openSettings();' in page
    assert 'document.getElementById("mobileSettingsBtn").onclick = () => openSettings();' in page
    assert 'id="themeBtn"' not in html
    assert 'data-tab="appearance"' in html
    assert 'data-theme-choice="glass"' in html
    # Closing is a dedicated action now (a page cannot be dismissed by clicking
    # its own background like a modal).
    assert "function closeSettings()" in page
    assert 'getElementById("closeModal").onclick = () => closeSettings();' in page
    assert "Escape" in page


def test_auth_level_card_contract() -> None:
    page = js_bundle()
    html = page_html()

    # Two-level card login: level-0 chooser + Local/Modus cards are generated
    # by auth.js at runtime, so assert on the JS bundle (not static HTML).
    assert '"authLevel0"' in page
    assert '"authLevelLocal"' in page
    assert '"authLevelModus"' in page
    assert 'id="authLocalCard"' in page or '"authLocalCard"' in page
    assert 'id="authModusCard"' in page or '"authModusCard"' in page
    assert "本机账户 · 演示账户 · 离线使用" in page
    assert "使用演示账户登录" in page
    # Demo + management WS commands are wired.
    assert '"auth_demo_account"' in page
    assert '"auth_rename_user"' in page
    assert '"auth_delete_user"' in page
    assert '"modus_account_status"' in page
    # Level switching + delete modal exist (delete modal is static HTML).
    assert "function showLevel" in page
    assert 'id="authDeleteModal"' in html
    assert 'id="authDeleteCascade"' in html


def test_websocket_auth_cases_are_append_only() -> None:
    page = js_bundle()
    # The new cases must appear after provider_usage (append-only discipline).
    idx = page.index('case "provider_usage":')
    tail = page[idx:]
    assert 'case "user_renamed":' in tail
    assert 'case "user_deleted":' in tail
    assert 'case "demo_account":' in tail
    assert 'case "modus_account_status":' in tail
    for handler in ("onUserRenamed", "onUserDeleted", "onAuthDemo", "onModusAccountStatus"):
        assert handler in page


def test_new_js_file_included_in_bundle_and_order() -> None:
    from _bundle import EXTERNAL_SCRIPTS

    assert "cloud_accounts.js" in EXTERNAL_SCRIPTS
    # Order: cloud_accounts before auth/account so its onModusAccountStatus
    # merge sees the previous handler.
    assert EXTERNAL_SCRIPTS.index("cloud_accounts.js") < EXTERNAL_SCRIPTS.index("auth.js")
    page = js_bundle()
    assert "Modus 云账户尚未配置" in page or "MODUS_CLOUD_API" in page
    assert "email_register" in page or "cloudRegisterBtn" in page


def test_delete_modal_has_cascade_checkbox() -> None:
    html = page_html()
    assert 'id="authDeleteModal"' in html
    assert 'id="authDeleteCascade"' in html
    assert 'id="authDeleteOkBtn"' in html
    assert 'id="authDeleteCancelBtn"' in html


def test_run_submission_payload_keeps_optional_context_protocol() -> None:
    """The protocol still accepts context, while the composer sends none."""
    from pathlib import Path

    core = js_bundle()
    PAGE = Path(__file__).resolve().parent.parent / "src" / "modus" / "desktop" / "static"
    core_src = (PAGE / "core.js").read_text(encoding="utf-8")
    websocket_src = (PAGE / "websocket.js").read_text(encoding="utf-8")

    payload = core_src[
        core_src.index("function runSubmissionPayload"):
        core_src.index("function setRunSubmissionUi")
    ]
    assert "payload.context = pending.context" in payload

    # Workspace selection is session state, not a message attachment.
    for site in ("function sendMessage()", "function submitChoice"):
        start = websocket_src.index(site)
        end = websocket_src.index("function loadSessionMessages", start)
        block = websocket_src[start:end]
        assert "context:[]" in block or "context: []" in block
        assert "modusContextBar" not in block


def test_workspace_manager_uses_account_bound_websocket_and_pickers() -> None:
    """Current-session workspace mutations use the account-bound WebSocket."""
    from pathlib import Path

    PAGE = Path(__file__).resolve().parent.parent / "src" / "modus" / "desktop" / "static"
    ctx = (PAGE / "contextbar.js").read_text(encoding="utf-8")
    assert 'type:"workspace_open"' in ctx
    assert 'type:"workspace_pick"' in ctx
    assert 'type:"workspace_forget"' in ctx
    assert '"/api/workspaces"' not in ctx
    assert "webkitdirectory" not in ctx
    assert 'input.type = "file"' not in ctx
    assert "data-workspace-forget" in ctx
