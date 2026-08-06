// ═══ WebSocket ═══
function modusWebSocketUrl() {
  // Opening the source file directly leaves location.host empty. In that
  // case, connect to the default local Desktop server instead of ws:///ws.
  if (location.protocol === "file:") return "ws://127.0.0.1:3000/ws";
  return (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host + "/ws";
}
function setRepositoryConnectionStatus(message) {
  const summary = document.getElementById("repoSummary");
  if (summary && !modelRepository.models.length) summary.textContent = message;
}
function requestSessionCatalog({append=false}={}) {
  if (desktopProtocolCompatible === false
      || !ws || ws.readyState !== WebSocket.OPEN) return false;
  const requestId = nextTransientRequestId("session-catalog");
  const query = String(sessionCatalogQuery || "").trim();
  const includeArchived = Boolean(sessionCatalogIncludeArchived);
  const cursor = append ? sessionCatalogNextCursor : null;
  if (append && (!sessionCatalogHasMore || !cursor)) return false;
  pendingSessionCatalogRequest = {
    requestId, query, includeArchived, append, cursor,
  };
  if (append) {
    const loadMore = document.getElementById("sessionCatalogLoadMore");
    if (loadMore) {
      loadMore.disabled = true;
      loadMore.textContent = "正在加载…";
    }
  }
  ws.send(JSON.stringify({
    type:"sessions_list", request_id:requestId, query,
    include_archived:includeArchived, cursor,
    limit:SESSION_CATALOG_PAGE_SIZE,
  }));
  return true;
}
function refreshSessionCatalog() { return requestSessionCatalog({append:false}); }
function modusConnectSocket() {
  // A duplicate socket owns a different in-memory DaoSession. Close it before
  // reconnecting so approval replies always return to the run that requested it.
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    ws.onopen = null;
    ws.onmessage = null;
    ws.onclose = null;
    ws.onerror = null;
    ws.close();
  }
  try {
    ws = new WebSocket(modusWebSocketUrl());
  } catch (error) {
    resetTransientRequests("connect_failed");
    setRepositoryConnectionStatus("无法连接 Modus 服务，请先运行 ./start.sh");
    setActivity("○", "无法连接，正在重试…", "idle");
    setTimeout(modusConnectSocket, 3000);
    return;
  }
  ws.onopen = () => {
    setRepositoryConnectionStatus("正在读取模型仓库…");
    // The server speaks first with ``session_ready``.  Do not issue reads or
    // identity mutations until its explicit protocol version is accepted.
  };
  ws.onmessage = e => handleMsg(JSON.parse(e.data));
  ws.onclose = () => {
    clearPendingSessionResumeTimer();
    abandonPendingVerificationRetry("socket_close", {release:false});
    settleActiveVerificationRetryUi();
    resetTransientRequests("socket_close");
    finishRunReplay();
    setAgentRunPending(false); controlMutationPending = false;
    waiting = creatingSession; input.disabled = true; sendBtn.disabled = true;
    document.getElementById("runControl").hidden = true;
    setRepositoryConnectionStatus("无法连接 Modus 服务，请先运行 ./start.sh");
    setActivity("○", "连接中断，正在恢复…", "idle");
    setTimeout(modusConnectSocket, 3000);
  };
  ws.onerror = () => ws.close();
}

function handleControlMessage(msg) {
  protocolState.bindIdentity(msg);
  switch (msg.type) {
    case "models_list":
    case "model_repository_updated":
      if (msg.type === "model_repository_updated") {
        const revision = Number(msg.repository_revision || 0);
        if (revision && revision < repositoryRevision) return true;
        repositoryRevision = Math.max(repositoryRevision, revision);
      }
      applyRepository(msg.data);
      if (msg.runtime_session_id || msg.session_id) {
        sessionId = msg.runtime_session_id || msg.session_id || sessionId;
        currentDbId = msg.db_id ?? currentDbId;
        currentModelId = msg.model_id ?? currentModelId;
        currentModeConfig = msg.mode_config || currentModeConfig;
        currentReasoningEffort = msg.reasoning_effort || null;
        if (msg.mode) setMode(msg.mode);
      }
      if (msg.type === "model_repository_updated" && msg.origin_runtime_session_id === sessionId) finishControlMutation();
      if (ws) refreshSessionCatalog();
      return true;
    case "model_discovery_result":
      if (msg.request_id && msg.request_id === pendingModelDiscoveryRequestId) {
        renderDiscovery(msg);
      }
      return true;
    case "credential_migration_report":
      if (msg.request_id && msg.request_id === pendingCredentialMigrationRequestId) {
        renderCredMigrationReport(msg.report, msg.request_id);
      }
      return true;
    case "credential_migration_done":
      if (msg.request_id && msg.request_id === pendingCredentialMigrationRequestId) {
        renderCredentialMigrationDone(msg.result);
      }
      return true;
    case "artifact_content":
      if (settleArtifactResponse(msg)) {
        renderInlineArtifactContent(msg.artifact, msg.artifact_id);
      }
      return true;
    case "workbench_snapshot":
      {
      const responseSessionId = String(msg.session_id ?? msg.data?.session_id ?? "");
      const dataSessionId = String(msg.data?.session_id ?? "");
      if (msg.operation === "workbench_get") {
        if (!matchesWorkbenchIdentity(
          msg, pendingWorkbenchSnapshot, "workbench_get",
        ) || dataSessionId !== pendingWorkbenchSnapshot.sessionId) return true;
        pendingWorkbenchSnapshot = null;
      } else if (msg.operation === undefined || msg.operation === null || msg.operation === "") {
        // Session replay is the only unsolicited authoritative snapshot. It is
        // safe only for an exact persisted identity, including the transient
        // empty-string identity.
        if (responseSessionId !== String(currentDbId ?? "")
            || dataSessionId !== String(currentDbId ?? "")) return true;
      } else {
        return true;
      }
      workbenchStore.load(msg.data || {});
      }
      return true;
    case "workbench_run":
      {
      const pending = pendingWorkbenchRunDetail;
      const responseRunId = String(msg.run_id ?? msg.run?.run_id ?? "");
      const runSessionId = String(msg.run?.session_id ?? "");
      if (!matchesWorkbenchIdentity(msg, pending, "workbench_run_get")
          || responseRunId !== pending.runId
          || runSessionId !== pending.sessionId) return true;
      pendingWorkbenchRunDetail = null;
      if (msg.run) workbenchStore.applyAuthoritativeRun(msg.run);
      }
      return true;
    case "peri_git_readiness":
      if (msg.request_id && msg.request_id === pendingPeriReadinessRequestId) {
        renderGitReadiness(msg.readiness);
      }
      return true;
    case "skills_list":
    case "skills_updated":
      if (msg.type === "skills_updated") {
        const revision = Number(msg.skills_revision || 0);
        if (revision && revision < skillsRevision) return true;
        skillsRevision = Math.max(skillsRevision, revision);
      }
      renderSkills(msg.skills || []);
      if (msg.type === "skills_updated" && msg.origin_runtime_session_id === sessionId) finishControlMutation();
      return true;
    case "extensions_list":
    case "extensions_updated":
      if (msg.type === "extensions_updated") {
        const revision = Number(msg.extensions_revision || 0);
        if (revision && revision < extensionsRevision) return true;
        extensionsRevision = Math.max(extensionsRevision, revision);
        renderMcpServers(msg.servers || []);
        if (msg.origin_runtime_session_id === sessionId) finishControlMutation();
      }
      renderExtensions(msg.extensions || []);
      return true;
    case "skill_fetched":
      if (msg.request_id && msg.request_id === pendingSkillFetchRequestId) {
        renderFetchedSkill(msg);
      }
      return true;
    case "mcp_servers_list":
      renderMcpServers(msg.servers || []);
      return true;
    case "memory_list":
    case "memory_added":
    case "memory_updated":
      if (!msg.session_id || msg.session_id === currentDbId) renderMemories(msg.memories || []);
      if (msg.type === "memory_added") showModalStatus("会话记忆已添加", "ok");
      return true;
    case "agent_config":
      if (typeof applyAgentMemoryConfig === "function") applyAgentMemoryConfig(msg.memory || {});
      return true;
    case "agent_config_saved":
      if (typeof onAgentMemoryConfigSaved === "function") onAgentMemoryConfigSaved(msg);
      return true;
    case "auth_status":
      if (typeof onAuthStatus === "function") onAuthStatus(msg);
      return true;
    case "auth_login_ok":
    case "auth_logout_ok":
    case "auth_switch_ok":
    case "auth_set_password_ok":
      if (typeof onAuthChanged === "function") onAuthChanged(msg);
      return true;
    case "workspaces_list":
      if (window.ModusWorkspaceManager?.handleWorkspaceList) {
        window.ModusWorkspaceManager.handleWorkspaceList(msg);
      }
      return true;
    case "workspace_opened":
      if (window.ModusWorkspaceManager?.handleWorkspaceOpened) {
        window.ModusWorkspaceManager.handleWorkspaceOpened(msg);
      }
      return true;
    case "workspace_pick_cancelled":
      window.ModusWorkspaceManager?.handlePickCancelled?.(msg);
      return true;
    case "workspace_default_updated":
      window.ModusWorkspaceManager?.handleDefaultUpdated?.(msg);
      return true;
    case "workspace_forgotten":
      if (msg.active_cleared) {
        applyCurrentWorkspace(null);
        if (typeof workbenchStore !== "undefined") {
          workbenchStore.workspace = null;
          workbenchStore.render();
        }
      }
      window.ModusWorkspaceManager?.handleForgotten?.(msg);
      if (ws) requestWorkbenchSnapshot();
      return true;
    case "user_created":
      if (typeof onUserCreated === "function") onUserCreated(msg);
      return true;
    case "usage_summary":
      if (typeof onUsageSummary === "function") onUsageSummary(msg);
      return true;
    case "usage_summary_updated":
      if (typeof onUsageSummaryUpdated === "function") onUsageSummaryUpdated(msg);
      return true;
    case "recharge_done":
      if (typeof onRechargeDone === "function") onRechargeDone(msg);
      return true;
    case "provider_usage":
      if (typeof onProviderUsage === "function") onProviderUsage(msg);
      return true;
    case "user_renamed":
      if (typeof onUserRenamed === "function") onUserRenamed(msg);
      return true;
    case "user_deleted":
      if (msg.account_reset && typeof onAuthChanged === "function") onAuthChanged(msg);
      if (typeof onUserDeleted === "function") onUserDeleted(msg);
      return true;
    case "auth_deleted":
      if (typeof onUserDeleted === "function") onUserDeleted(msg);
      return true;
    case "demo_account":
      if (typeof onAuthDemo === "function") onAuthDemo(msg);
      return true;
    case "modus_account_status":
      if (typeof onModusAccountStatus === "function") onModusAccountStatus(msg);
      return true;
    default:
      return false;
  }
}

function isStaleSessionInvalidation(msg) {
  if (!msg?.active_reset || !["session_deleted", "session_archived"].includes(msg.type)) {
    return false;
  }
  const invalidatedDbId = String(
    msg.invalidated_db_id || msg.deleted_db_id || msg.archived_db_id || "",
  );
  return Boolean(invalidatedDbId && currentDbId !== invalidatedDbId);
}

function handleMsg(msg) {
  observeServerEpoch(msg);
  // Catalog broadcasts can race with an explicit switch on the target socket.
  // Never let a delayed invalidation overwrite the newer browser identity.
  if (isStaleSessionInvalidation(msg)) return;
  if (handleControlMessage(msg)) return;
  switch(msg.type) {
    case "agent_event":
      // Typed AgentEvent is the sole message-rendering contract.
      // Admission is owned exclusively by the correlated `run_accepted`
      // control packet. A delayed event may render, but it must never clear a
      // draft or turn an unacknowledged submission into an owned run.
      if (msg.event?.type === "run_started" && agentRunPending
          && (!activeAgentRunId || String(msg.event.run_id || "") === activeAgentRunId)) {
        activeAgentRunId = String(msg.event.run_id || "") || null;
      }
      applyTranscriptEvent(msg.event);
      break;
    case "transcript_reset":
      if (msg.session_id && currentDbId && msg.session_id !== currentDbId) break;
      loadSessionMessages(msg.session_id, []);
      transcriptCursors = {};
      transcriptGapRequests.clear();
      applyTranscriptEvents(msg.events);
      if (msg.cursors && typeof msg.cursors === "object") {
        transcriptCursors = msg.cursors;
        saveTranscriptCursors();
      }
      break;
    case "transcript_ops":
      if (msg.session_id && currentDbId && msg.session_id !== currentDbId) break;
      if (msg.run_id) transcriptGapRequests.delete(msg.run_id);
      {
        const isReplayAck = Boolean(
          replayingRunId
          && msg.run_id === replayingRunId
          && msg.request_id === replayRequestId
          && Number(msg.since_sequence || 0) === 0
        );
        if (isReplayAck) beginRunReplayReplacement();
        applyTranscriptEvents(msg.events);
        if (isReplayAck) finishRunReplay("回放完成", "done");
      }
      break;
    case "session_ready":
      {
      const protocolCompatible = acceptDesktopProtocol(msg);
      setAgentRunPending(false); controlMutationPending=false;
      sessionId=msg.runtime_session_id || msg.session_id; currentDbId=msg.db_id||"";
      currentModelId=msg.model_id||"";
      currentModeConfig=msg.mode_config||{};
      currentReasoningEffort = msg.reasoning_effort || null;
      applyCurrentWorkspace(msg.workspace);
      if(currentDbId) localStorage.setItem("modus_last_db_id",currentDbId);
      // 恢复会话的模式
      if (msg.mode) setMode(msg.mode);
      finishControlMutation(); input.focus();
      if (!pendingRunSubmission) setActivity("●","准备就绪","idle");
      addSystemMsg("会话已连接");
      setFocusState(msg.worldview || "等待任务...", msg.worldview ? "ready" : "idle");
      if (!protocolCompatible) {
        renderDesktopProtocolMismatch();
        break;
      }
      if(ws) {
        const last = localStorage.getItem("modus_last_db_id");
        loadTranscriptCursors(last || "");
        if (!creatingSession && last) beginPendingSessionResume(last);
        refreshSessionCatalog();
        ws.send(JSON.stringify({type:"skills_list"}));
        ws.send(JSON.stringify({type:"model_repository_get"}));
        ws.send(JSON.stringify({type:"extensions_list"}));
        if (!last) requestWorkbenchSnapshot();
        // No persisted session identity means a plain reconnect after a
        // server restart; the verification-retry notice only applies to a
        // fresh (non-resuming) connection.
        if (!last) showVerificationRetryReconnectNotice();
      }
      // Only a reconnect to the same server epoch may retry a create intent.
      // observeServerEpoch() clears it when the process (and its idempotency
      // registry) changed, preventing a duplicate blank conversation.
      if (protocolCompatible && creatingSession && pendingSessionCreateKey
          && ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type:"session_create", request_key:pendingSessionCreateKey,
          title:"新对话", mode:currentMode,
        }));
      }
      // A transient first-run submission has no database identity to restore.
      // On the same server process it can be retried immediately with the same
      // idempotency key and the new transport runtime identity.
      if (protocolCompatible && pendingRunSubmission) {
        if (!retryPendingRunSubmission()) setRunSubmissionUi("正在恢复会话后重试任务…");
      } else if (protocolCompatible && runSubmissionRestartNotice && !localStorage.getItem("modus_last_db_id")) {
        runSubmissionRestartNotice = false;
        addSystemMsg("服务已重启，未确认的任务没有自动重发；草稿已保留，请确认后重新发送");
        setActivity("○", "任务草稿已保留，请重新发送", "idle");
      }
      }
      break;
    case "session_persisted":
      sessionId=msg.runtime_session_id || msg.session_id || sessionId;
      currentDbId=msg.db_id || "";
      currentModelId=msg.model_id || currentModelId;
      currentModeConfig=msg.mode_config || currentModeConfig;
      applyCurrentWorkspace(msg.workspace);
      // The transient conversation kept the same DOM while gaining a database
      // identity, so future incremental reconnects may safely retain it.
      renderedSessionId=currentDbId;
      currentReasoningEffort = msg.reasoning_effort || currentReasoningEffort;
      if(currentDbId) localStorage.setItem("modus_last_db_id",currentDbId);
      if (pendingRunSubmission
          && !pendingRunSubmission.sessionId
          && String(msg.runtime_session_id || msg.session_id || "") === pendingRunSubmission.runtimeSessionId) {
        pendingRunSubmission.resolvedDbId = currentDbId;
      }
      if(msg.mode) setMode(msg.mode);
      if(ws && ws.readyState===WebSocket.OPEN) refreshSessionCatalog();
      if(ws && ws.readyState===WebSocket.OPEN) requestWorkbenchSnapshot();
      break;
    case "run_accepted":
      acceptPendingRunSubmission(msg);
      break;
    case "session_data":
      settleSessionSettingsRead(msg);
      break;
    case "session_updated":
      settleSessionSettingsWrite(msg);
      break;
    case "session_messages":
      // Load session messages into the chat area
      loadSessionMessages(msg.session_id, msg.messages || []);
      break;
    case "restored_message":
      // Add a restored message from resume_session to the chat area
      {
        const role = msg.role || "user";
        const content = msg.content || "";
        if (role === "user") {
          addUserBubble(content);
        } else if (role === "assistant") {
          addAssistantBubble(content, false);
        }
      }
      break;
    case "done":
      // `run_completed` / `run_error` own transcript rendering. This terminal
      // control signal carries the semantic outcome. Runtime ownership may
      // still be cleaning up, so only `run_settled` may release the composer.
      {
        const reason = msg.stop_reason || "completed";
        const labels = {completed:"回复完成",cancelled:"已取消",max_turns:"达到轮次上限",token_limit:"达到 Token 上限",wall_time:"达到时间上限",engine_error:"模型错误",failed:"运行失败",verification_required:"需要验证",verification_retry_limit:"验证重试上限"};
        setActivity(reason === "completed" ? "●" : "○", labels[reason] || `已停止：${reason}`, reason === "completed" ? "done" : "idle");
      }
      if(ws && ws.readyState===WebSocket.OPEN) requestWorkbenchSnapshot();
      break;
    case "run_settled":
      {
        const settledRuntimeId = String(msg.runtime_session_id || msg.session_id || "");
        const settledDbId = String(msg.db_id || "");
        const settledRunId = String(msg.run_id || "");
        if (!settledRunId
            || (settledRuntimeId && settledRuntimeId !== String(sessionId || ""))
            || !settledDbId
            || settledDbId !== String(currentDbId || "")
            || String(renderedSessionId || "") !== String(currentDbId || "")) break;
        const settlementRole = ModusProtocol.runSettlementConnectionRole(msg);
        const tracksSettledRun = Boolean(
          agentRunPending
          && activeAgentRunId
          && settledRunId === activeAgentRunId
        );
        const roleMatches = Boolean(
          settlementRole !== ModusProtocol.RUN_CONNECTION_ROLES.UNKNOWN
          && settlementRole === activeAgentRunRole
        );
        rememberAgentRunSettlement(settledRunId, settlementRole);
        // Every live view of this persisted conversation must refresh the two
        // authoritative projections. A correlated observer also releases its
        // local admission barrier, but this path never touches composer text or
        // pending submission data and therefore cannot clear an owner's draft.
        if (tracksSettledRun && roleMatches) {
          recentAgentRunSettlements.delete(settledRunId);
          settleActiveVerificationRetryUi();
          setAgentRunPending(false); waiting=false;
          const ready = Boolean(modelRepository?.selection?.default_model_id);
          input.disabled=!ready; sendBtn.disabled=!ready; input.focus();
          document.getElementById("stopBtn").disabled=false;
          document.getElementById("runControl").hidden=true;
        }
        syncAcceptedRunResult(settledRunId);
      }
      break;
    case "worldview_updated":
      setFocusState(msg.worldview || "当前聚焦已更新", "ready");
      setActivity("●", "当前聚焦已更新", "idle");
      break;
    case "cancel_requested":
      // Cancellation is asynchronous. Keep the composer locked until the
      // active runner has reaped workers and sends `run_settled`.
      setActivity("◌", "正在停止…", "busy");
      document.getElementById("stopBtn").disabled=true;
      break;
    case "session_restored":
      // A stale restore response is intentionally inert.  Keep the whole
      // transition inside the correlated branch so it cannot stop a newer
      // replay or replace the active conversation's identity.
      if (settlePendingSessionResume(msg)) {
        finishRunReplay();
        setAgentRunPending(false); controlMutationPending=false; waiting=false; document.getElementById("runControl").hidden=true;
        sessionId=msg.runtime_session_id || msg.session_id || sessionId;
        currentDbId=msg.db_id || "";
        currentModelId=msg.model_id || "";
        currentModeConfig=msg.mode_config || {};
        applyCurrentWorkspace(msg.workspace);
        if(currentDbId) localStorage.setItem("modus_last_db_id",currentDbId);
        currentReasoningEffort = msg.reasoning_effort || null;
        setFocusState(msg.worldview || "等待任务...", msg.worldview ? "ready" : "idle");
        renderComposerMenu();
        if (msg.mode && msg.mode !== currentMode) setMode(msg.mode);
        renderSessionRun(msg.last_run);
        finishControlMutation();
        if(ws && ws.readyState===WebSocket.OPEN) { refreshSessionCatalog(); requestWorkbenchSnapshot(); }
        if (pendingRunSubmission) {
          if (!retryPendingRunSubmission()) setRunSubmissionUi("正在恢复任务提交…");
        } else if (runSubmissionRestartNotice) {
          runSubmissionRestartNotice = false;
          addSystemMsg("服务已重启，未确认的任务没有自动重发；草稿已保留，请确认后重新发送");
          setActivity("○", "任务草稿已保留，请重新发送", "idle");
        }
        showVerificationRetryReconnectNotice();
      }
      break;
    case "session_history_reset":
      finishRunReplay();
      loadSessionMessages(msg.session_id, []);
      transcriptCursors = {};
      break;
    case "session_history_start":
      // Incremental replay sends only an inclusive suffix.  Keep an already
      // rendered matching transcript and let EventStore merge its revisions.
      if (msg.session_id && msg.session_id !== renderedSessionId) {
        finishRunReplay();
        loadSessionMessages(msg.session_id, []);
      }
      setPendingLegacyMessages(msg.session_id, msg.legacy_messages);
      break;
    case "session_history_end":
      if (msg.session_id && currentDbId && msg.session_id !== currentDbId) break;
      renderLegacyMessagePrefix(msg.session_id);
      _sessionHasMsgs = (msg.event_count || 0) > 0 || (msg.message_count || 0) > 0;
      finishControlMutation(); input.focus();
      document.getElementById("runControl").hidden = true;
      setActivity("●", "历史已恢复", "idle");
      break;
    case "session_switched":
      finishRunReplay();
      setAgentRunPending(false); controlMutationPending=false; waiting = false; input.disabled = false; sendBtn.disabled = false;
      document.getElementById("runControl").hidden = true;
      sessionId = msg.runtime_session_id || msg.session_id || sessionId;
      currentDbId = msg.db_id || "";
      currentModelId = msg.model_id || "";
      currentModeConfig = msg.mode_config || {};
      if(currentDbId) localStorage.setItem("modus_last_db_id",currentDbId);
      currentReasoningEffort = msg.reasoning_effort || null;
      renderComposerMenu();
      if (msg.mode) setMode(msg.mode);
      renderSessionRun(msg.last_run);
      setFocusState(msg.worldview || "等待任务...", msg.worldview ? "ready" : "idle");
      if(ws) refreshSessionCatalog();
      break;
    case "sessions_changed":
      {
        const catalogRevision = Number(msg.catalog_revision || 0);
        if (catalogRevision && catalogRevision <= sessionCatalogRevision) break;
        sessionCatalogRevision = Math.max(sessionCatalogRevision, catalogRevision);
        refreshSessionCatalog();
      }
      break;
    case "sessions_list":
      {
      const pendingCatalog = pendingSessionCatalogRequest;
      const responseQuery = String(msg.query || "").trim();
      if (!pendingCatalog || !msg.request_id
          || msg.request_id !== pendingCatalog.requestId
          || responseQuery !== pendingCatalog.query
          || Boolean(msg.include_archived) !== pendingCatalog.includeArchived) break;
      pendingSessionCatalogRequest = null;
      const catalogRevision = Number(msg.catalog_revision || 0);
      if (catalogRevision && catalogRevision < sessionCatalogRevision) {
        refreshSessionCatalog();
        break;
      }
      sessionCatalogRevision = Math.max(sessionCatalogRevision, catalogRevision);
      const incomingSessions = Array.isArray(msg.sessions) ? msg.sessions : [];
      if (pendingCatalog.append) {
        const loaded = new Map(sessionCatalogSessions.map(item => [String(item.id), item]));
        incomingSessions.forEach(item => loaded.set(String(item.id), item));
        sessionCatalogSessions = [...loaded.values()];
      } else {
        sessionCatalogSessions = incomingSessions.slice();
      }
      sessionCatalogNextCursor = msg.next_cursor || null;
      sessionCatalogHasMore = Boolean(msg.has_more && sessionCatalogNextCursor);
      sessionCatalogTotal = Number(msg.total ?? sessionCatalogSessions.length);
      sessionCatalogActiveTotal = Number(msg.active_total ?? 0);
      sessionCatalogArchivedTotal = Number(msg.archived_total ?? 0);
      const loadedIds = new Set(sessionCatalogSessions.map(item => String(item.id)));
      for (const selectedId of sessionCatalogSelectedIds) {
        if (!loadedIds.has(selectedId)) sessionCatalogSelectedIds.delete(selectedId);
      }
      const sessions = sessionCatalogSessions;
      const totalCount = sessionCatalogTotal;
      const list=document.getElementById("sbList");
      // 平铺列表：所有会话按更新时间倒序展示，不再按模式分组
      let batchMode = JSON.parse(localStorage.getItem("modus_batch_on") || "false");
      let batchOn = batchMode && sessions.length > 0;
      const showArchived = sessionCatalogIncludeArchived;
      const visibleSessions = sessions;
      let html = "";
      // 批量操作栏
      if (batchOn) {
        html += '<div class="sb-batch-bar on"><div style="display:flex;gap:4px;">'
          + '<button class="sb-batch-btn" id="batchSelectAll">全选已载入</button>'
          + '<button class="sb-batch-btn warning" id="batchArchive" disabled>归档所选</button>'
          + '<button class="sb-batch-btn primary" id="batchSkillsIndividual" disabled>逐个转 Skills</button>'
          + '<button class="sb-batch-btn primary" id="batchSkillMerged" disabled>合并为 Skill</button>'
          + '<button class="sb-batch-btn primary" id="batchExport" disabled>导出所选</button>'
          + '<button class="sb-batch-btn danger" id="batchDelete" disabled>永久删除(<span id="batchCount">0</span>)</button>'
          + '</div></div>';
      }
      const showBadges = JSON.parse(localStorage.getItem("modus_show_mode_badges") || "true");
      html += '<div class="sb-session-toolbar"><div class="sb-session-toolbar-title"><span>会话</span><span style="font-weight:400;font-size:8px;">(' + visibleSessions.length + '/' + totalCount + ')</span></div><div class="sb-session-toolbar-actions">'
        + '<button class="sb-session-more" type="button" aria-label="会话管理" aria-haspopup="menu" aria-expanded="false">⋮</button>'
        + '<div class="sb-session-menu" role="menu" hidden>'
        + '<button class="sb-badge-toggle ' + (showBadges ? 'active' : '') + '" type="button" role="menuitemcheckbox" aria-checked="' + showBadges + '">模型标签</button>'
        + '<button class="sb-archive-toggle ' + (showArchived ? 'active' : '') + '" type="button" role="menuitemcheckbox" aria-checked="' + showArchived + '">' + (showArchived ? '隐藏归档会话' : '显示归档会话') + '</button>'
        + '<button id="batchToggleBtn" class="' + (batchMode ? 'active' : '') + '" type="button" role="menuitemcheckbox" aria-checked="' + batchMode + '">' + (batchMode ? '退出批量管理' : '批量管理') + '</button>'
        + '</div></div></div>';
      // Sort sessions by updated_at DESC (latest first)
      const sorted = [...visibleSessions].sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
      sorted.forEach(s => {
        const title = escapeHtml(s.title || "对话");
        const displayTitle = (title === "新对话" || !title || title === "对话")
          ? escapeHtml((s.last_message || "").substring(0, 40) || "新对话")
          : title;
        const preview = escapeHtml((s.last_message || "").substring(0, 35));
        const time = timeAgo(s.updated_at || s.created_at || 0);
        const active = s.id === currentDbId ? ' active' : '';
        // The baseline is identified by its model; only advanced forms are named.
        const rawMode = s.mode || "default";
        let badgeLabel;
        let modeColor;
        if (rawMode === "moa") {
          badgeLabel = "MOA";
          modeColor = "var(--accent)";
        } else if (rawMode === "peri") {
          badgeLabel = "Peri";
          modeColor = "var(--green)";
        } else {
          // Default sessions render their persisted model snapshot; a later
          // global default change must not relabel historical conversations.
          const sessionModel = s.model_id ? modelRepository.models?.find(m => m.id === s.model_id) : null;
          badgeLabel = sessionModel?.name || sessionModel?.model || "未指定模型";
          modeColor = "var(--text-tertiary)";
        }
        html += '<div class="sb-item' + active + (s.archived ? ' archived' : '') + '" data-id="' + s.id + '">'
          + '<div style="display:flex;align-items:center;gap:4px;min-height:16px;">'
          + (batchOn ? '<input type="checkbox" class="sb-check" value="' + s.id + '"' + (sessionCatalogSelectedIds.has(String(s.id)) ? ' checked' : '') + '>' : '')
          + (showBadges ? '<span class="sb-mode-badge" style="color:' + modeColor + ';border:1px solid ' + modeColor + ';">' + escapeHtml(badgeLabel) + '</span>' : '')
          + '</div>'
          + '<div class="si-title">' + displayTitle + (s.archived ? ' <span style="color:var(--amber);font-size:9px;">· 已归档</span>' : '') + '</div>'
          + (preview ? '<div class="si-preview">' + preview + '</div>' : '')
          + '<div class="si-meta"><span>' + time + '</span>'
          + '<span style="margin-left:auto;position:relative;">'
          + '<button class="si-menu-btn" data-id="' + s.id + '" data-title="' + escapeHtml(s.title || "") + '" style="background:none;border:none;color:var(--text-tertiary);cursor:pointer;font-size:16px;padding:0 4px;line-height:1;" title="会话操作">\u22ee</button>'
          + '</span></div></div>';
      });
      if (sessionCatalogHasMore) {
        html += '<button class="sb-catalog-more" id="sessionCatalogLoadMore" type="button">加载更多 · 已载入 ' + visibleSessions.length + '/' + totalCount + '</button>';
      } else if (visibleSessions.length) {
        html += '<div class="sb-catalog-count">已载入全部 ' + visibleSessions.length + ' 个结果</div>';
      }
      list.innerHTML = html || '<div class="sb-empty" style="text-align:center;padding:12px;color:var(--text-tertiary);font-size:10px;">\u6682\u65e0\u4f1a\u8bdd</div>';
      document.getElementById("sessionCatalogLoadMore")?.addEventListener("click", () => {
        requestSessionCatalog({append:true});
      });
      // Click to switch
      list.querySelectorAll(".sb-item").forEach(el => {
        el.addEventListener("click", e => {
          if (e.target.closest(".si-menu-btn") || e.target.closest(".sb-check")) return;
          closeMobileSidebar();
          if (el.classList.contains("archived")) {
            addSystemMsg("该会话已归档，请先从会话菜单中取消归档");
            return;
          }
          const sid = el.dataset.id;
          if (sid && sid !== currentDbId && ws && ws.readyState === WebSocket.OPEN) {
            cancelPendingSessionResume("session_switch");
            abandonPendingRunSubmission("session_switch");
            beginWorkbenchSessionTransition("session_switch");
            waiting=true; input.disabled=true; sendBtn.disabled=true;
            ws.send(JSON.stringify({type:"session_switch",session_id:sid}));
            addSystemMsg("\ud83d\udd04 切换会话...");
          }
        });
      });
      // Session menu (⋮) — rename / archive / delete
      document.querySelectorAll(".si-menu-btn").forEach(btn => {
        btn.addEventListener("click", e => {
          e.stopPropagation();
          // Close any existing menu
          document.querySelectorAll(".si-menu-dropdown").forEach(m => m.remove());
          const sid = btn.dataset.id;
          const title = btn.dataset.title || "会话";
          const dropdown = document.createElement("div");
          dropdown.className = "si-menu-dropdown";
          dropdown.innerHTML = '<div class="si-menu-item" data-action="rename">\u270e \u91cd\u547d\u540d</div>'
            + '<div class="si-menu-item" data-action="copy-id">\ud83d\udccb \u590d\u5236 ID</div>'
            + '<div class="si-menu-item" data-action="reference">↗ 添加为当前会话参考</div>'
            + '<div class="si-menu-item" data-action="export-md">⬇ 导出 Markdown</div>'
            + '<div class="si-menu-item" data-action="export-json">⬇ 导出 JSON</div>'
            + '<div class="si-menu-item" data-action="to-skill">✦ 转为单个 Skill</div>'
            + '<div class="si-menu-item" data-action="archive">' + (btn.closest('.sb-item').classList.contains('archived') ? '↩ 取消归档' : '\ud83d\udce6 归档') + '</div>'
            + '<div class="si-menu-sep"></div>'
            + '<div class="si-menu-item danger" data-action="delete">\u2715 \u5220\u9664</div>';
          const rect = btn.getBoundingClientRect();
          dropdown.style.position = "fixed";
          dropdown.style.top = (rect.top - 4) + "px";
          dropdown.style.right = "inherit";
          dropdown.style.left = (rect.right - 180) + "px";
          document.body.appendChild(dropdown);
          // Action handlers
          dropdown.querySelectorAll(".si-menu-item").forEach(item => {
            item.addEventListener("click", ev => {
              ev.stopPropagation();
              dropdown.remove();
              const action = item.dataset.action;
              if (action === "rename") {
                const p = prompt("\u91cd\u547d\u540d\uff1a", title);
                if (p && ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:"session_rename",session_id:sid,title:p}));
              } else if (action === "copy-id") {
                navigator.clipboard.writeText(sid).then(() => addSystemMsg("\u2713 \u5df2\u590d\u5236\u4f1a\u8bdd ID"));
              } else if (action === "reference") {
                requestSessionReference(sid);
              } else if (action === "export-md" || action === "export-json") {
                requestSessionExport([sid], action === "export-json" ? "json" : "markdown");
              } else if (action === "to-skill") {
                requestSessionSkill([sid], "individual");
              } else if (action === "archive") {
                if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: btn.closest('.sb-item').classList.contains('archived') ? "session_restore_archive" : "session_archive", session_id:sid}));
              } else if (action === "delete") {
                showConfirm("\u5220\u9664\u4f1a\u8bdd", "\u786e\u5b9a\u5220\u9664\u6b64\u4f1a\u8bdd\uff1f\u5bf9\u8bdd\u5386\u53f2\u5c06\u6c38\u4e45\u4e22\u5931\u3002", "\ud83d\uddd1", () => {
                  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:"session_delete",session_id:sid}));
                }, btn);
              }
            });
          });
          // Click outside closes
          setTimeout(() => document.addEventListener("click", closeMenu = (ce) => {
            if (!dropdown.contains(ce.target) && ce.target !== btn) {
              dropdown.remove();
              document.removeEventListener("click", closeMenu);
            }
          }), 10);
        });
      });
      // Batch mode toggle
      const batchToggle = document.getElementById("batchToggleBtn");
      if (batchToggle) {
        batchToggle.onclick = () => {
          const current = JSON.parse(localStorage.getItem("modus_batch_on") || "false");
          localStorage.setItem("modus_batch_on", JSON.stringify(!current));
          if (ws) refreshSessionCatalog();
        };
      }
      const sessionMore = document.querySelector(".sb-session-more");
      const sessionMenu = document.querySelector(".sb-session-menu");
      if (sessionMore && sessionMenu) {
        const closeSessionMenu = () => {
          sessionMenu.hidden = true;
          sessionMore.setAttribute("aria-expanded", "false");
        };
        sessionMore.onclick = event => {
          event.stopPropagation();
          const open = sessionMenu.hidden;
          sessionMenu.hidden = !open;
          sessionMore.setAttribute("aria-expanded", String(open));
          if (open) {
            sessionMenu.querySelector("button")?.focus();
            setTimeout(() => document.addEventListener("click", closeSessionMenu, {once:true}), 0);
          }
        };
        sessionMenu.onclick = event => event.stopPropagation();
        sessionMenu.addEventListener("keydown", event => {
          if (event.key === "Escape") { closeSessionMenu(); sessionMore.focus(); }
        });
      }
      // Batch select all
      document.getElementById("batchSelectAll")?.addEventListener("click", () => {
        sessions.forEach(item => sessionCatalogSelectedIds.add(String(item.id)));
        list.querySelectorAll(".sb-check").forEach(cb => cb.checked = true);
        updateBatchDeleteBtn();
      });
      // Batch delete — only deletes checked sessions under current filter
      document.getElementById("batchDelete")?.addEventListener("click", () => {
        const checked = loadedSessionCatalogSelection();
        if (!checked.length) return;
        showConfirm("\u6279\u91cf\u5220\u9664", `\u786e\u5b9a\u5220\u9664 ${checked.length} \u4e2a\u4f1a\u8bdd\uff1f\u6b64\u64cd\u4f5c\u4e0d\u53ef\u6062\u590d\u3002`, "\ud83d\uddd1", () => {
          if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:"session_delete_batch", session_ids:checked}));
        }, document.getElementById("batchDelete"));
      });
      document.getElementById("batchArchive")?.addEventListener("click", () => {
        const checked = loadedSessionCatalogSelection();
        if (checked.length && ws?.readyState === WebSocket.OPEN) {
          const selected = checked.map(id => sessions.find(item => item.id === id)).filter(Boolean);
          const restore = selected.length > 0 && selected.every(item => Boolean(item.archived));
          ws.send(JSON.stringify({type:restore ? "session_restore_archive_batch" : "session_archive_batch",session_ids:checked}));
        }
      });
      document.getElementById("batchExport")?.addEventListener("click", () => {
        const checked = loadedSessionCatalogSelection();
        if (checked.length) requestSessionExport(checked, "markdown");
      });
      document.getElementById("batchSkillsIndividual")?.addEventListener("click", () => {
        const checked = loadedSessionCatalogSelection();
        if (checked.length) requestSessionSkill(checked, "individual");
      });
      document.getElementById("batchSkillMerged")?.addEventListener("click", () => {
        const checked = loadedSessionCatalogSelection();
        if (checked.length > 1) requestSessionSkill(checked, "merged");
      });
      // Individual checkbox -> update batch delete state
      list.querySelectorAll(".sb-check").forEach(cb => cb.addEventListener("change", () => {
        if (cb.checked) sessionCatalogSelectedIds.add(String(cb.value));
        else sessionCatalogSelectedIds.delete(String(cb.value));
        updateBatchDeleteBtn();
      }));
      updateBatchDeleteBtn();
      // Badge toggle
      document.querySelector(".sb-badge-toggle")?.addEventListener("click", () => {
        const current = JSON.parse(localStorage.getItem("modus_show_mode_badges") || "true");
        localStorage.setItem("modus_show_mode_badges", JSON.stringify(!current));
        if (ws) refreshSessionCatalog();
      });
      document.querySelector(".sb-archive-toggle")?.addEventListener("click", () => {
        sessionCatalogIncludeArchived = !sessionCatalogIncludeArchived;
        localStorage.setItem("modus_show_archived", JSON.stringify(sessionCatalogIncludeArchived));
        sessionCatalogSelectedIds.clear();
        refreshSessionCatalog();
      });
      }
      break;
    case "mode_updated":
      if (!settleSessionExecutionMutation(msg, "session_set_mode")) break;
      currentModelId = msg.model_id || currentModelId;
      currentModeConfig = msg.mode_config || {};
      setMode(msg.mode);
      currentReasoningEffort = msg.reasoning_effort || null;
      renderComposerMenu();
      finishControlMutation();
      if (ws) refreshSessionCatalog();
      break;
    case "session_created":
      finishRunReplay();
      controlMutationPending = false;
      clearSessionCreateIntent();
      if (ws) {
        waiting=false; input.disabled=false; sendBtn.disabled=false;
        document.getElementById("runControl").hidden=true;
        setActivity("●","已就绪","idle");
        transcriptCursors = {};
        sessionId = msg.runtime_session_id || msg.session_id || sessionId;
        currentDbId = msg.db_id || msg.session?.id || "";
        currentModelId = msg.model_id || msg.session?.model_id || currentModelId;
        currentModeConfig = msg.mode_config || msg.session?.mode_config || {};
        applyCurrentWorkspace(msg.workspace);
        loadSessionMessages(currentDbId, [], msg.worldview || "");
        currentReasoningEffort = msg.reasoning_effort || msg.session?.reasoning_effort || null;
        if(currentDbId) localStorage.setItem("modus_last_db_id",currentDbId);
        refreshSessionCatalog();
        if (msg.session?.mode) setMode(msg.session.mode);
        else if (typeof currentMode !== "undefined") setMode(currentMode);
        addSystemMsg("📝 新对话");
      }
      break;
    case "session_workspace_updated":
      applyCurrentWorkspace(msg.workspace);
      if (typeof workbenchStore !== "undefined") {
        workbenchStore.workspace = msg.workspace || null;
        workbenchStore.render();
      }
      addSystemMsg("工作区已切换到 " + (msg.workspace?.name || msg.workspace?.root || "所选项目"));
      setActivity("●", "工作区已就绪", "idle");
      window.ModusWorkspaceManager?.refresh?.();
      if (ws) requestWorkbenchSnapshot();
      break;
    case "session_deleted":
      {
      const invalidatedDbId = String(msg.invalidated_db_id || msg.deleted_db_id || "");
      if(msg.active_reset && (!invalidatedDbId || currentDbId === invalidatedDbId)) {
        setAgentRunPending(false); controlMutationPending = false; waiting = false;
        sessionId = msg.runtime_session_id || msg.session_id || sessionId;
        currentDbId = "";
        currentModelId = msg.model_id || "";
        currentModeConfig = msg.mode_config || {};
        currentReasoningEffort = null;
        if (!invalidatedDbId || localStorage.getItem("modus_last_db_id") === invalidatedDbId) {
          localStorage.removeItem("modus_last_db_id");
        }
        if (invalidatedDbId) delete transcriptCursorsBySession[invalidatedDbId];
        loadTranscriptCursors(""); transcriptGapRequests.clear(); finishRunReplay();
        loadSessionMessages("", []);
        setMode(msg.mode || "default");
        document.getElementById("runControl").hidden = true;
        finishControlMutation();
        setActivity("○", "当前会话已删除", "idle");
        if (msg.external_invalidation) addSystemMsg("会话已在另一窗口删除，已进入新对话");
      }
      if (ws && !msg.external_invalidation) refreshSessionCatalog();
      }
      break;
    case "session_archived":
      {
      const invalidatedDbId = String(msg.invalidated_db_id || msg.archived_db_id || "");
      const resetCurrent = msg.active_reset && (!invalidatedDbId || currentDbId === invalidatedDbId);
      if (resetCurrent) {
        setAgentRunPending(false); controlMutationPending = false; waiting = false;
        sessionId = msg.runtime_session_id || msg.session_id || sessionId;
        currentDbId = "";
        currentModelId = msg.model_id || "";
        currentModeConfig = msg.mode_config || {};
        currentReasoningEffort = null;
        if (!invalidatedDbId || localStorage.getItem("modus_last_db_id") === invalidatedDbId) {
          localStorage.removeItem("modus_last_db_id");
        }
        if (invalidatedDbId) delete transcriptCursorsBySession[invalidatedDbId];
        loadTranscriptCursors(""); transcriptGapRequests.clear(); finishRunReplay();
        loadSessionMessages("", []);
        setMode("default");
        document.getElementById("runControl").hidden = true;
        finishControlMutation();
        setActivity("○", "当前会话已归档", "idle");
      }
      if (!msg.active_reset || resetCurrent) addSystemMsg(msg.archived
        ? (msg.external_invalidation ? "会话已在另一窗口归档，已进入新对话" : "📦 会话已归档")
        : "↩ 会话已恢复");
      if (ws && !msg.external_invalidation) refreshSessionCatalog();
      }
      break;
    case "session_export_ready":
      downloadSessionExport(msg);
      break;
    case "session_skills_created":
      renderSkills(msg.all_skills || msg.skills || []);
      addSystemMsg("✓ 已生成 " + (msg.skills || []).length + " 个 Skill，可在设置 → Skills 中查看");
      break;
    case "session_reference_added":
      renderMemories(msg.memories || []);
      document.getElementById("sessionReferenceId").value = "";
      addSystemMsg(msg.created ? "↗ 已添加会话参考：" + (msg.source_title || "会话") : "↗ 此会话已在当前参考中");
      break;
    case "session_renamed":
      if (ws) refreshSessionCatalog();
      break;
    case "verification_retry_started":
      acceptPendingVerificationRetry(msg);
      break;
    case "error":
      {
        if (window.ModusWorkspaceManager?.handleError?.(msg)) break;
        const verificationRetryError = settlePendingVerificationRetryError(msg);
        // Retry errors are admissible only for the exact request + failed Run
        // intent. A stale packet cannot release a newer composer barrier.
        if (verificationRetryError === false) break;
        if (verificationRetryError === true) {
          addSystemMsg("⚠ " + (msg.message || "验证重试未能启动，请稍后再试"));
          setActivity("○", "验证重试未启动", "idle");
          break;
        }
        const resumeError = handlePendingSessionResumeError(msg);
        if (resumeError === "stale" || resumeError === "retrying") break;
        const submissionError = settlePendingRunSubmissionError(msg);
        // A run_message error with another request/session identity is stale
        // for this composer and must not unlock it or render into this session.
        if (submissionError === false) break;
        if (submissionError === true && !msg.active_reset) {
          addSystemMsg("⚠ " + (msg.message || "任务提交失败，草稿已保留"));
          setActivity("○", "提交失败，草稿已保留", "idle");
          break;
        }
      }
      if (settleTransientRequestError(msg)) break;
      if (settleWorkbenchError(msg)) break;
      if (msg.operation === "transcript_sync" && replayingRunId
          && msg.run_id === replayingRunId && msg.request_id === replayRequestId) {
        finishRunReplay("回放失败", "idle");
        addSystemMsg("⚠ " + (msg.message || "未能读取这次运行"));
        break;
      }
      if (msg.operation === "session_create") {
        clearSessionCreateIntent(String(msg.request_key || ""));
      }
      if (msg.code === "session_create_failed") {
        clearSessionCreateIntent();
      }
      if (["session_not_found", "session_archived"].includes(msg.code) && msg.operation === "resume_session") {
        const requestedDbId = String(msg.requested_db_id || "");
        if (requestedDbId && localStorage.getItem("modus_last_db_id") === requestedDbId) {
          localStorage.removeItem("modus_last_db_id");
          delete transcriptCursorsBySession[requestedDbId];
          loadTranscriptCursors("");
          loadSessionMessages("", [], "");
          addSystemMsg(msg.code === "session_archived" ? "原会话已归档，已进入新对话" : "原会话已不存在，已进入新对话");
          finishControlMutation();
          setActivity("●", "准备就绪", "idle");
          break;
        }
      }
      if (msg.active_reset && msg.operation === "run_message") {
        const requestedDbId = String(msg.requested_db_id || "");
        setAgentRunPending(false); controlMutationPending = false;
        sessionId = msg.runtime_session_id || msg.session_id || sessionId;
        currentDbId = "";
        currentModelId = msg.model_id || currentModelId;
        currentModeConfig = msg.mode_config || {};
        if (!requestedDbId || localStorage.getItem("modus_last_db_id") === requestedDbId) {
          localStorage.removeItem("modus_last_db_id");
        }
        if (requestedDbId) delete transcriptCursorsBySession[requestedDbId];
        loadTranscriptCursors("");
        loadSessionMessages("", [], msg.worldview || "");
        setMode(msg.mode || "default");
        addSystemMsg(msg.message || "原会话不可用，已进入新对话");
        finishControlMutation();
        document.getElementById("runControl").hidden = true;
        setActivity("●", "准备就绪", "idle");
        break;
      }
      controlMutationPending = false;
      if (msg.runtime_session_id || msg.session_id) {
        currentModelId = msg.model_id ?? currentModelId;
        currentModeConfig = msg.mode_config || currentModeConfig;
        currentReasoningEffort = msg.reasoning_effort ?? currentReasoningEffort;
        if (msg.mode) setMode(msg.mode);
      }
      addSystemMsg("⚠ "+msg.message);
      const rejectedByForeignRun = Boolean(
        msg.code === "session_busy"
        && msg.run_owned_by_connection === false
        && !agentRunPending
      );
      if (rejectedByForeignRun) setAgentRunPending(false);
      if (!rejectedByForeignRun && (agentRunPending || (msg.code === "session_busy" && msg.run_owned_by_connection === true))) {
        // A rejected control-plane mutation does not terminate a Run tracked by
        // this window. Observers remain locked but never gain Stop authority.
        const ownsTrackedRun = canCancelActiveAgentRun();
        waiting=true; input.disabled=true; sendBtn.disabled=true;
        document.getElementById("runControl").hidden=!ownsTrackedRun;
        setActivity(
          "◌",
          ownsTrackedRun ? "任务仍在运行" : "任务正在另一窗口运行，完成后自动同步…",
          "busy",
        );
      } else {
        finishControlMutation();
        document.getElementById("runControl").hidden=true;
        setActivity("○", "操作失败", "idle");
      }
      break;
    case "session_reasoning_updated":
      if (!settleSessionExecutionMutation(msg, "session_set_reasoning")) break;
      currentReasoningEffort = msg.reasoning_effort || null;
      renderComposerMenu();
      finishControlMutation();
      break;
    case "session_model_updated":
      if (!settleSessionExecutionMutation(msg, "session_set_model")) break;
      currentModelId = msg.model_id || currentModelId;
      currentModeConfig = msg.mode_config || {};
      currentReasoningEffort = msg.reasoning_effort || null;
      if (msg.mode) setMode(msg.mode);
      renderComposerMenu();
      finishControlMutation();
      break;
    case "model_test_result":
      {
        if (!msg.request_id || msg.request_id !== pendingModelTestRequestId) break;
        pendingModelTestRequestId = null;
        const status = document.getElementById("repoTestStatus");
        if (status) {
          status.style.display = "block";
          status.style.color = msg.success ? "var(--green)" : "var(--red)";
          status.textContent = msg.success ? "✓ 连接成功" + (msg.response ? " · " + msg.response : "") : "✕ " + (msg.error || "连接失败");
        }
        const button = document.getElementById("repoTestBtn");
        if (button) { button.disabled = false; button.textContent = "测试连接"; }
      }
      break;
    default:
      console.warn("[Modus] Unhandled WebSocket message", msg.type);
  }
}

function sendMessage() {
  const t=input.value.trim();
  // The visible transcript and the authoritative conversation must agree
  // before a task can be admitted. This prevents a reconnect race from
  // sending an old view into a new transient database conversation.
  if(!t||waiting||pendingSessionResume||pendingVerificationRetry
      || String(renderedSessionId || "") !== String(currentDbId || "")
      || !ws||ws.readyState!==WebSocket.OPEN)return;
  if (!hasCurrentWorkspace() && messageNeedsWorkspace(t)) {
    addSystemMsg("⚠ 这个任务可能需要访问文件或运行代码，请先在右侧工作区中添加或启用一个文件夹");
    setActivity("○", "需要启用工作区", "idle");
    window.ModusWorkspaceManager?.open?.({focus:true});
    return;
  }
  const draftValue = input.value;
  pendingRunSubmission = {
    content:t, draftValue, skillId:pendingSkillId || null,
    sessionId:String(currentDbId || ""),
    resolvedDbId:String(currentDbId || ""),
    runtimeSessionId:String(sessionId || ""),
    context:[],
    requestId:nextTransientRequestId("run-message"),
    serverEpoch:String(serverEpoch || ""),
  };
  activeAgentRunId=null;
  // The backend routes execution from the persisted session mode. The browser
  // never chooses a runner by changing the message type. Text and Skill remain
  // visible until the correlated run_accepted packet confirms admission.
  transmitPendingRunSubmission();
}

// Conservative client-side guidance only. The projectless engine exposes no
// local tools, so a false negative still cannot touch disk; the Agent can tell
// the user to enable a workspace if the request later proves operational.
function messageNeedsWorkspace(text) {
  const value = String(text || "").trim();
  if (!value) return false;
  const pathLike = /(?:^|\s)(?:\.{0,2}\/|~\/|[A-Za-z]:\\|\/Users\/|\/home\/|\/tmp\/)|\.(?:py|js|ts|tsx|jsx|java|go|rs|cpp|c|h|css|html|json|ya?ml|toml|md)(?:\s|$)/i;
  const operation = /(?:文件|文件夹|目录|项目|仓库|源码|代码|脚本|终端|命令|编译|构建|测试|运行|安装|依赖|修复|重构|实现|开发|创建|生成|写入|编辑|修改|删除|读取|查看.*(?:文件|目录|项目)|file|folder|directory|project|repo|source|code|script|terminal|command|build|compile|test|run|install|dependency|fix|refactor|implement|create|write|edit|delete|read)/i;
  return pathLike.test(value) || operation.test(value);
}

// Resend an edited user message from an expanded user card. Mirrors
// sendMessage()'s preconditions but bypasses the composer: the edited text IS
// the message, submitted as a fresh run.
function sendUserEditedMessage(text, _eventId="", attachments=[]) {
  const t = String(text || "").trim();
  if (!t || waiting || pendingSessionResume || pendingVerificationRetry
      || String(renderedSessionId || "") !== String(currentDbId || "")
      || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (!hasCurrentWorkspace() && messageNeedsWorkspace(t)) {
    addSystemMsg("⚠ 这个任务可能需要访问文件或运行代码，请先在右侧工作区中添加或启用一个文件夹");
    window.ModusWorkspaceManager?.open?.({focus:true});
    return;
  }
  pendingRunSubmission = {
    content: t, draftValue: "", skillId: pendingSkillId || null,
    sessionId: String(currentDbId || ""),
    resolvedDbId: String(currentDbId || ""),
    runtimeSessionId: String(sessionId || ""),
    context: Array.isArray(attachments) ? attachments.map(item => ({...item})) : [],
    requestId: nextTransientRequestId("run-message"),
    serverEpoch: String(serverEpoch || ""),
  };
  activeAgentRunId = null;
  transmitPendingRunSubmission();
}

// Submit a choice from a ```choice option card. Mirrors sendMessage()'s
// preconditions but bypasses the composer: the clicked option IS the message.
// The card is collapsed locally first, then the choice rides the standard
// pendingRunSubmission pipeline (reconnect retry / idempotency / run_accepted
// correlation all apply).
function submitChoice(choiceBtn) {
  const card = choiceBtn.closest("[data-choice-card]");
  const chosen = choiceBtn.dataset.choice;
  if (!chosen || !card || card.dataset.state === "chosen") return;
  if (waiting || pendingSessionResume || pendingVerificationRetry
      || String(renderedSessionId || "") !== String(currentDbId || "")
      || !ws || ws.readyState !== WebSocket.OPEN) return;
  // Rebuild the card from the visible buttons (each carries its raw option in
  // data-choice), then collapse it as chosen before sending. Replace the whole
  // node so the rebuilt card (also .choice-card) never nests inside itself.
  const container = card.parentNode;
  const lines = [...card.querySelectorAll(".choice-btn")].map(b => b.dataset.choice);
  const rebuilt = choiceCardHtml(lines, chosen);
  card.insertAdjacentHTML("afterend", rebuilt);
  card.remove();
  // Re-run addCopyHandlers so the rebuilt card's (disabled) buttons are wired.
  if (typeof addCopyHandlers === "function" && container) addCopyHandlers(container);
  pendingRunSubmission = {
    content: chosen, draftValue: "", skillId: null,
    sessionId: String(currentDbId || ""),
    resolvedDbId: String(currentDbId || ""),
    runtimeSessionId: String(sessionId || ""),
    context:[],
    requestId: nextTransientRequestId("run-message"),
    serverEpoch: String(serverEpoch || ""),
  };
  activeAgentRunId = null;
  transmitPendingRunSubmission();
}

function loadSessionMessages(sessionId, messages, focus="") {
  beginWorkbenchSessionTransition("session_identity");
  const ca = document.getElementById("chatArea");
  // Clear every element the timeline may have rendered so a new conversation
  // never inherits the previous session's transcript in the DOM.
  ca.querySelectorAll(".msg, .timeline-item, .run-completion, .empty-state, .collab-msg, .timeline-expand-earlier").forEach(e => e.remove());
  ca.querySelectorAll(".turn").forEach(e => e.remove());
  const lower = document.getElementById("chatAreaLower");
  if (lower) lower.innerHTML = "";
  // The KANBAN board owns the right panel; clear its drawer on session switch.
  if (window.ModusKanban && typeof window.ModusKanban.closeDrawer === "function") window.ModusKanban.closeDrawer();
  clearTranscriptState();
  if (pendingLegacySessionId !== String(sessionId || "")) {
    setPendingLegacyMessages("", []);
  }
  if (typeof currentDbId !== "undefined") currentDbId = sessionId;
  renderedSessionId = sessionId || "";
  setFocusState(focus || "等待任务...", focus ? "ready" : "idle");
  renderMemories([]);
  _sessionHasMsgs = messages.length > 0;
  messages.forEach(m => {
    if ((m.role || "") === "system") return;
    const c = m.content || "";
    const e = document.createElement("div");
    if (m.role === "user") {
      e.className = "msg user msg-centered";
      const frag = userCardHtml({markdown: c, attachments: m.attachments});
      if (frag && frag.appendChild) e.appendChild(frag);
      else if (frag) while (frag.firstChild) e.appendChild(frag.firstChild);
    }
    else { e.className="msg assistant msg-centered"; e.innerHTML='<div class="block-text">'+renderTimelineMarkdown(c, false)+'</div>'; }
    ca.appendChild(e);
  });
  _autoScroll();
  ca.querySelectorAll(".user-card").forEach(card => {
    if (typeof wireUserCardInteractions === "function") wireUserCardInteractions(card);
  });
  document.querySelectorAll(".sb-item").forEach(i => i.classList.remove("active"));
  const a = document.querySelector('.sb-item[data-id="'+CSS.escape(sessionId)+'"]');
  if (a) a.classList.add("active");
}
// Batch delete helper
function updateBatchDeleteBtn() {
  const checked = loadedSessionCatalogSelection().length;
  const count = document.getElementById("batchCount");
  ["batchDelete", "batchArchive", "batchSkillsIndividual", "batchExport"].forEach(id => {
    const button = document.getElementById(id);
    if (button) button.disabled = checked === 0;
  });
  const mergeSkillButton = document.getElementById("batchSkillMerged");
  if (mergeSkillButton) mergeSkillButton.disabled = checked < 2;
  const archiveButton = document.getElementById("batchArchive");
  if (archiveButton) {
    const selected = [...document.querySelectorAll(".sb-check:checked")];
    const allArchived = selected.length > 0 && selected.every(cb => cb.closest(".sb-item")?.classList.contains("archived"));
    archiveButton.textContent = allArchived ? "恢复所选" : "归档所选";
  }
  if (count) count.textContent = checked;
}
function loadedSessionCatalogSelection() {
  const loadedIds = new Set(sessionCatalogSessions.map(item => String(item.id)));
  return [...sessionCatalogSelectedIds].filter(id => loadedIds.has(String(id)));
}
function requestSessionExport(sessionIds, format="markdown") {
  const ids = [...new Set((sessionIds || []).filter(Boolean))];
  if (!ids.length || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({type:"session_export",session_ids:ids,format}));
}
function requestSessionSkill(sessionIds, conversion="individual") {
  const ids = [...new Set((sessionIds || []).filter(Boolean))];
  if (!ids.length || !ws || ws.readyState !== WebSocket.OPEN) return;
  let name = "";
  if (conversion === "merged") {
    name = (prompt("合并后的 Skill 名称（小写字母、数字、- 或 _）：", "session-collection") || "").trim();
    if (!name) return;
  }
  ws.send(JSON.stringify({type:"session_to_skill",session_ids:ids,conversion,name}));
}
function downloadSessionExport(message) {
  const blob = new Blob([String(message.content || "")], {type:String(message.mime || "text/plain") + ";charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = message.filename || "modus-session.md";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
  addSystemMsg("✓ 会话已导出：" + anchor.download);
}
