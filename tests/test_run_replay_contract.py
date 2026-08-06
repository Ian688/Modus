from pathlib import Path

from _bundle import js_bundle

PAGE = Path(__file__).parents[1] / "src/modus/desktop/static/index.html"
WORKBENCH = PAGE.parent / "workbench.js"


def test_run_history_offers_explicit_replay_affordance() -> None:
    page = js_bundle()
    workbench = WORKBENCH.read_text()

    assert "onRunReplay" in workbench
    assert "data-run-replay" in workbench
    assert "wb-run-replay" in workbench
    assert "回放该次运行的事件" in workbench
    assert '<div class="wb-run-row">' in workbench
    assert '<button class="wb-run-replay" type="button"' in workbench
    assert 'aria-label="回放该次运行的事件"' in workbench
    assert 'role="button"' not in workbench


def test_replay_waits_for_correlated_ack_before_replacing_timeline() -> None:
    page = js_bundle()

    assert "function replayRun(runId)" in page
    assert "function beginRunReplayReplacement()" in page
    assert "function finishRunReplay(" in page
    assert "replayingRunId" in page
    assert 'type:"transcript_sync", run_id:runId, since_sequence:0' in page
    assert 'request_id:replayRequestId' in page
    start = page.index("function replayRun(runId)")
    replay = page[start:page.index("let currentMsg", start)]
    # The old transcript remains usable when the server rejects the request.
    assert "beginRunReplayReplacement();" not in replay
    assert "clearTimelineState();" not in replay
    assert "clearTranscriptState();" not in replay
    assert "workbenchStore.reset();" not in replay

    replacement = page[
        page.index("function beginRunReplayReplacement()"):
        page.index("function finishRunReplay(")
    ]
    assert "clearTimelineState();" in replacement
    assert ".timeline-expand-earlier" in replacement

    ops = page[page.index('case "transcript_ops":'):page.index('case "session_ready":')]
    assert "msg.request_id === replayRequestId" in ops
    assert "Number(msg.since_sequence || 0) === 0" in ops
    assert ops.index("beginRunReplayReplacement();") < ops.index("applyTranscriptEvents(msg.events);")
    assert ops.index("applyTranscriptEvents(msg.events);") < ops.index('finishRunReplay("回放完成", "done");')


def test_replay_is_blocked_while_a_run_or_control_action_is_active() -> None:
    page = js_bundle()
    workbench = WORKBENCH.read_text()

    start = page.index("function replayRun(runId)")
    replay = page[start:page.index("let currentMsg", start)]
    assert "agentRunPending" in replay
    assert "pendingVerificationRetry" in replay
    assert "controlMutationPending" in replay
    assert "replayingRunId" in replay
    can_replay = page[
        page.index("canReplay: () =>"):
        page.index("});", page.index("canReplay: () =>"))
    ]
    assert "!agentRunPending" in can_replay
    assert "!pendingVerificationRetry" in can_replay
    assert "!controlMutationPending" in can_replay
    assert "!replayingRunId" in can_replay
    retry_pending = page[
        page.index("function setVerificationRetryPendingUi"):
        page.index("function verificationRetryPayload")
    ]
    assert "workbenchStore.render()" in retry_pending
    assert "this.canReplay = options.canReplay" in workbench
    assert "this.canReplay() ? '' : ' disabled'" in workbench


def test_replay_failure_disconnect_and_restart_preserve_timeline_and_unlock_ui() -> None:
    page = js_bundle()
    websocket = (PAGE.parent / "websocket.js").read_text()
    core = (PAGE.parent / "core.js").read_text()

    error = page[page.index('case "error":'):page.index('case "session_reasoning_updated":')]
    assert 'msg.operation === "transcript_sync"' in error
    assert "msg.request_id === replayRequestId" in error
    assert 'finishRunReplay("回放失败", "idle");' in error
    assert "beginRunReplayReplacement();" not in error

    close = websocket[websocket.index("ws.onclose = () => {"):websocket.index("ws.onerror", websocket.index("ws.onclose = () => {"))]
    assert "finishRunReplay();" in close
    epoch = core[core.index("function observeServerEpoch"):core.index("// Transcript cursors")]
    assert "finishRunReplay();" in epoch
    for message_type in (
        "session_restored", "session_history_reset", "session_switched",
        "session_created", "session_deleted", "session_archived",
    ):
        start = page.index(f'case "{message_type}":')
        end = page.index("break;", start)
        assert "finishRunReplay();" in page[start:end]

    for message_type in ("transcript_reset", "transcript_ops", "session_history_end"):
        start = page.index(f'case "{message_type}":')
        end = page.index("break;", start)
        branch = page[start:end]
        assert "msg.session_id" in branch
        assert "currentDbId" in branch

    snapshot = page[page.index('case "workbench_snapshot":'):page.index('case "workbench_run":')]
    assert "msg.data?.session_id" in snapshot
    assert "currentDbId" in snapshot


def test_transcript_sync_server_responses_echo_replay_identity() -> None:
    server = (PAGE.parents[1] / "server.py").read_text()
    branch = server[
        server.index('elif msg_type == "transcript_sync":'):
        server.index('elif msg_type == "run_message":')
    ]

    assert 'request_id = str(msg.get("request_id") or "")[:128]' in branch
    for value in (
        '"operation": "transcript_sync"',
        '"run_id": run_id',
        '"request_id": request_id',
    ):
        assert value in branch
