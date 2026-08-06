// ═══ Core State ═══
let ws = null, sessionId = null, currentDbId = null, currentModelId = "", renderedSessionId = "", waiting = false;
let currentWorkspace = null;
function applyCurrentWorkspace(workspace) {
  currentWorkspace = workspace && workspace.workspace_id && workspace.root
    ? {...workspace} : null;
  if (window.ModusWorkspaceManager?.setCurrent) {
    window.ModusWorkspaceManager.setCurrent(currentWorkspace);
    window.ModusWorkspaceManager.refresh?.();
  }
}
function hasCurrentWorkspace() {
  return Boolean(currentWorkspace?.workspace_id && currentWorkspace?.root);
}
const protocolState = new ModusProtocol.ProtocolStateStore();
// A correlated `run_settled` normally clears this flag. Terminal/detached
// duplicate ACKs also reconcile it because no later settlement is guaranteed.
let agentRunPending = false;
let activeAgentRunId = null;
let activeAgentRunRole = null;
const recentAgentRunSettlements = new Map();
// A run is not owned by this window until the server explicitly accepts the
// correlated command. Keep the user's draft and attached Skill intact while
// admission is in flight so disconnects and stale acknowledgements cannot
// silently lose or misroute work.
let pendingRunSubmission = null;
let runSubmissionRestartNotice = false;
// Verification repair is a separate admission intent.  The failed Run is
// only its input; cancellation authority belongs to the new Run returned by
// the correlated acknowledgement.
let pendingVerificationRetry = null;
let verificationRetryReconnectNotice = false;
let activeVerificationRetryPriorRunId = null;
const verificationRetryConsumedRuns = new Set();
const SESSION_RESUME_MAX_ATTEMPTS = 10;
const SESSION_RESUME_RETRY_MS = 700;
let pendingSessionResume = null;
function setAgentRunPending(pending) {
  agentRunPending = Boolean(pending);
  if (!agentRunPending) {
    activeAgentRunId = null;
    activeAgentRunRole = null;
  }
  if (typeof workbenchStore !== "undefined") workbenchStore.render();
}
function canCancelActiveAgentRun() {
  return Boolean(
    agentRunPending
    && activeAgentRunRole === ModusProtocol.RUN_CONNECTION_ROLES.OWNER
  );
}
function rememberAgentRunSettlement(runId, connectionRole) {
  const id = String(runId || "");
  if (!id || connectionRole === ModusProtocol.RUN_CONNECTION_ROLES.UNKNOWN) return;
  recentAgentRunSettlements.delete(id);
  recentAgentRunSettlements.set(id, connectionRole);
  while (recentAgentRunSettlements.size > 32) {
    recentAgentRunSettlements.delete(recentAgentRunSettlements.keys().next().value);
  }
}
function consumeAgentRunSettlement(runId, connectionRole) {
  const id = String(runId || "");
  if (!id || recentAgentRunSettlements.get(id) !== connectionRole) return false;
  recentAgentRunSettlements.delete(id);
  return true;
}
// A create request is a user intent, not a WebSocket packet. Keep its key
// across reconnects and disable the button while the server acknowledges it.
let pendingSessionCreateKey = null;
let creatingSession = false;
let repositoryRevision = 0;
let sessionCatalogRevision = 0;
const SESSION_CATALOG_PAGE_SIZE = 50;
let sessionCatalogQuery = "";
let sessionCatalogIncludeArchived = JSON.parse(localStorage.getItem("modus_show_archived") || "false");
let sessionCatalogSessions = [];
let sessionCatalogNextCursor = null;
let sessionCatalogHasMore = false;
let sessionCatalogTotal = 0;
let sessionCatalogActiveTotal = 0;
let sessionCatalogArchivedTotal = 0;
let pendingSessionCatalogRequest = null;
const sessionCatalogSelectedIds = new Set();
let skillsRevision = 0;
let extensionsRevision = 0;
let serverEpoch = "";
let desktopProtocolCompatible = null;
let pendingWorkbenchSnapshot = null;
let pendingWorkbenchRunDetail = null;
const pendingArtifactRequests = new Map();
let transientRequestSerial = 0;
const transientRequestResetters = new Map();
function nextTransientRequestId(prefix="request") {
  transientRequestSerial += 1;
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return prefix + "-" + Date.now() + "-" + transientRequestSerial;
}
function registerTransientRequestReset(name, resetter) {
  if (!name || typeof resetter !== "function") return;
  transientRequestResetters.set(name, resetter);
}
function resetTransientRequest(name, reason="reset") {
  const resetter = transientRequestResetters.get(name);
  if (typeof resetter !== "function") return;
  try { resetter(reason); }
  catch (error) { console.warn("[Modus] Failed to reset transient request", name, error); }
}
function resetTransientRequests(reason="reset") {
  transientRequestResetters.forEach((_resetter, name) => resetTransientRequest(name, reason));
}
function resetSessionCatalogRequest(_reason="reset") {
  pendingSessionCatalogRequest = null;
  const loadMore = document.getElementById("sessionCatalogLoadMore");
  if (loadMore) {
    loadMore.disabled = false;
    loadMore.textContent = "加载更多";
  }
}
registerTransientRequestReset("session-catalog", resetSessionCatalogRequest);
function renderDesktopProtocolMismatch() {
  desktopProtocolCompatible = false;
  resetSessionCatalogRequest("protocol_mismatch");
  waiting = true;
  input.disabled = true;
  sendBtn.disabled = true;
  document.getElementById("composerSelect").disabled = true;
  const list = document.getElementById("sbList");
  if (list) {
    const notice = document.createElement("div");
    notice.className = "sb-empty sb-protocol-mismatch";
    const message = document.createElement("strong");
    message.textContent = "服务版本较旧";
    const detail = document.createElement("span");
    detail.textContent = "请重启 Modus Desktop，再刷新此页面。";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "plain-small";
    retry.textContent = "我已重启，刷新";
    retry.addEventListener("click", () => location.reload());
    notice.append(message, detail, retry);
    list.replaceChildren(notice);
  }
  setActivity("⚠", "服务版本较旧，请重启后刷新", "idle");
}
function acceptDesktopProtocol(message) {
  const expected = Number(ModusProtocol.DESKTOP_PROTOCOL_VERSION || 0);
  const actual = Number(message?.desktop_protocol_version || 0);
  if (expected > 0 && actual === expected) {
    desktopProtocolCompatible = true;
    if (typeof renderComposerMenu === "function") renderComposerMenu();
    return true;
  }
  desktopProtocolCompatible = false;
  return false;
}
function resetWorkbenchRequests(_reason="reset") {
  pendingWorkbenchSnapshot = null;
  pendingWorkbenchRunDetail = null;
}
function beginWorkbenchSessionTransition(_reason="session_transition") {
  resetWorkbenchRequests(_reason);
  resetArtifactRequests(_reason);
  abandonPendingVerificationRetry(_reason, {release:false});
  // Composer execution settings are scoped to the conversation that was
  // current when the user clicked.  A switch/create supersedes that intent.
  resetTransientRequest("session-execution-mutation", _reason);
}
function setSessionResumeUi(message="正在恢复会话…") {
  waiting = true;
  input.disabled = true;
  sendBtn.disabled = true;
  document.getElementById("runControl").hidden = true;
  setActivity("◌", message, "busy");
}
function clearPendingSessionResumeTimer() {
  if (!pendingSessionResume?.timer) return;
  clearTimeout(pendingSessionResume.timer);
  pendingSessionResume.timer = null;
}
function cancelPendingSessionResume(_reason="cancelled", {release=true}={}) {
  if (!pendingSessionResume) return false;
  clearPendingSessionResumeTimer();
  pendingSessionResume = null;
  if (release) {
    waiting = false;
    finishControlMutation();
  }
  return true;
}
function beginPendingSessionResume(dbId) {
  const targetDbId = String(dbId || "");
  if (!targetDbId || !ws || ws.readyState !== WebSocket.OPEN) return false;
  if (!pendingSessionResume || pendingSessionResume.dbId !== targetDbId) {
    cancelPendingSessionResume("superseded", {release:false});
    pendingSessionResume = {
      requestId:nextTransientRequestId("resume-session"),
      dbId:targetDbId, runtimeSessionId:String(sessionId || ""),
      attempts:0, timer:null, exhaustedNoticeShown:false,
    };
  } else {
    clearPendingSessionResumeTimer();
    pendingSessionResume.runtimeSessionId = String(sessionId || "");
  }
  return transmitPendingSessionResume();
}
function transmitPendingSessionResume() {
  const pending = pendingSessionResume;
  if (!pending || !ws || ws.readyState !== WebSocket.OPEN
      || !pending.runtimeSessionId
      || pending.runtimeSessionId !== String(sessionId || "")) return false;
  if (pending.attempts >= SESSION_RESUME_MAX_ATTEMPTS) {
    setSessionResumeUi("会话仍在结束上次运行，请稍后刷新重试");
    if (!pending.exhaustedNoticeShown) {
      pending.exhaustedNoticeShown = true;
      addSystemMsg("会话恢复等待超时。上次运行可能仍在清理，请稍后刷新页面重试");
    }
    return false;
  }
  pending.attempts += 1;
  ws.send(JSON.stringify({
    type:"resume_session", db_id:pending.dbId,
    request_id:pending.requestId, cursors:transcriptCursors,
  }));
  setSessionResumeUi(pending.attempts > 1 ? "等待上次运行结束并恢复会话…" : "正在恢复会话…");
  return true;
}
function schedulePendingSessionResumeRetry() {
  const pending = pendingSessionResume;
  if (!pending) return false;
  clearPendingSessionResumeTimer();
  setSessionResumeUi("上次运行正在结束，稍后自动恢复…");
  if (pending.attempts >= SESSION_RESUME_MAX_ATTEMPTS) {
    return transmitPendingSessionResume();
  }
  pending.timer = setTimeout(() => {
    if (pendingSessionResume !== pending) return;
    pending.timer = null;
    transmitPendingSessionResume();
  }, SESSION_RESUME_RETRY_MS);
  return true;
}
function matchesPendingSessionResume(message) {
  const pending = pendingSessionResume;
  return Boolean(
    pending
    && String(message?.operation || "") === "resume_session"
    && String(message?.request_id || "") === pending.requestId
    && String(message?.requested_db_id || "") === pending.dbId
    && String(message?.runtime_session_id || "") === pending.runtimeSessionId
    && String(sessionId || "") === pending.runtimeSessionId
  );
}
function settlePendingSessionResume(message) {
  if (!matchesPendingSessionResume(message)
      || String(message?.db_id || "") !== pendingSessionResume.dbId) return false;
  cancelPendingSessionResume("restored", {release:false});
  return true;
}
function handlePendingSessionResumeError(message) {
  if (String(message?.operation || "") !== "resume_session") return null;
  // Protocol v2 resume transactions always carry a request ID. An uncorrelated
  // legacy-style packet must not settle or retry the current identity intent.
  if (!matchesPendingSessionResume(message)) return "stale";
  if (message?.code === "session_busy") {
    schedulePendingSessionResumeRetry();
    return "retrying";
  }
  cancelPendingSessionResume("error", {release:false});
  return "failed";
}
function runSubmissionPayload(pending) {
  const payload = {
    type:"run_message", content:pending.content,
    request_id:pending.requestId,
    db_id:pending.sessionId, session_id:pending.sessionId,
    runtime_session_id:pending.runtimeSessionId,
  };
  if (pending.skillId) payload.skill_id = pending.skillId;
  if (pending.context && pending.context.length) payload.context = pending.context;
  return payload;
}
function setRunSubmissionUi(message="正在提交任务…") {
  waiting = true;
  input.disabled = true;
  sendBtn.disabled = true;
  document.getElementById("runControl").hidden = true;
  setActivity("◌", message, "busy");
}
function restoreRunSubmissionDraft(pending) {
  if (!pending) return;
  // Never overwrite text or a Skill the user selected after the request was
  // sent. In the normal path both values are still present because admission
  // no longer clears the composer optimistically.
  if (!input.value) input.value = pending.draftValue || pending.content;
  if (pending.skillId && !pendingSkillId) {
    pendingSkillId = pending.skillId;
    document.getElementById("skillChipLabel").textContent = "⚡ " + pending.skillId;
    document.getElementById("skillChip").hidden = false;
  }
}
function abandonPendingRunSubmission(reason="transition") {
  const pending = pendingRunSubmission;
  if (!pending) return false;
  pendingRunSubmission = null;
  restoreRunSubmissionDraft(pending);
  setAgentRunPending(false);
  activeAgentRunId = null;
  waiting = false;
  document.getElementById("runControl").hidden = true;
  document.getElementById("stopBtn").disabled = false;
  finishControlMutation();
  if (reason === "server_epoch") runSubmissionRestartNotice = true;
  return true;
}
function currentRunSubmissionDbIdentity(pending) {
  return String(pending?.resolvedDbId || pending?.sessionId || "");
}
function canRetryPendingRunSubmission() {
  const pending = pendingRunSubmission;
  return Boolean(
    pending
    && pending.serverEpoch
    && pending.serverEpoch === serverEpoch
    && String(currentDbId || "") === currentRunSubmissionDbIdentity(pending)
    && String(sessionId || "")
    && ws?.readyState === WebSocket.OPEN
  );
}
function transmitPendingRunSubmission({retry=false}={}) {
  const pending = pendingRunSubmission;
  if (!pending || !ws || ws.readyState !== WebSocket.OPEN) return false;
  pending.runtimeSessionId = String(sessionId || "");
  if (!pending.runtimeSessionId) return false;
  ws.send(JSON.stringify(runSubmissionPayload(pending)));
  setRunSubmissionUi(retry ? "正在恢复任务提交…" : "正在提交任务…");
  return true;
}
function retryPendingRunSubmission() {
  if (pendingSessionResume || !canRetryPendingRunSubmission()) return false;
  return transmitPendingRunSubmission({retry:true});
}
function matchesPendingRunSubmission(message, {requireAcceptedDb=false, allowActiveReset=false}={}) {
  const pending = pendingRunSubmission;
  if (!pending
      || String(message?.operation || "") !== "run_message"
      || String(message?.request_id || "") !== pending.requestId
      || String(message?.requested_db_id ?? "") !== pending.sessionId
      || String(message?.runtime_session_id || "") !== pending.runtimeSessionId
      || String(sessionId || "") !== pending.runtimeSessionId) return false;
  // An active reset intentionally reports the new authoritative db_id (empty)
  // after the requested persisted conversation disappeared. The original
  // requested_db_id plus request/runtime identities still prove ownership.
  if (allowActiveReset && message?.active_reset) return true;
  const responseDbId = String(message?.db_id ?? "");
  const resolvedDbId = currentRunSubmissionDbIdentity(pending);
  if (requireAcceptedDb && !responseDbId) return false;
  if (resolvedDbId && responseDbId !== resolvedDbId) return false;
  const activeDbId = String(currentDbId || "");
  if (activeDbId && responseDbId !== activeDbId) return false;
  return true;
}
function syncAcceptedRunResult(runId) {
  const acceptedRunId = String(runId || "");
  if (!acceptedRunId || !ws || ws.readyState !== WebSocket.OPEN) return false;
  ws.send(JSON.stringify({
    type:"transcript_sync", run_id:acceptedRunId, since_sequence:0,
    request_id:nextTransientRequestId("transcript-sync"),
  }));
  requestWorkbenchSnapshot();
  return true;
}
function acceptPendingRunSubmission(message) {
  if (!matchesPendingRunSubmission(message, {requireAcceptedDb:true})) return false;
  const pending = pendingRunSubmission;
  const acceptedDbId = String(message.db_id || "");
  pendingRunSubmission = null;
  if (input.value === pending.draftValue) input.value = "";
  if (pendingSkillId === pending.skillId) {
    pendingSkillId = null;
    document.getElementById("skillChip").hidden = true;
  }
  if (!currentDbId && acceptedDbId) {
    currentDbId = acceptedDbId;
    renderedSessionId = acceptedDbId;
    localStorage.setItem("modus_last_db_id", acceptedDbId);
  }
  const acceptedRunId = String(message.run_id || "");
  activeAgentRunId = acceptedRunId || null;
  const admissionRole = ModusProtocol.runAdmissionConnectionRole(message);
  const acceptedConnectionRole = admissionRole === ModusProtocol.RUN_CONNECTION_ROLES.OWNER
    ? ModusProtocol.RUN_CONNECTION_ROLES.OWNER
    : ModusProtocol.RUN_CONNECTION_ROLES.OBSERVER;
  const runState = String(message.state || message.status || "running").toLowerCase();
  const terminal = [
    "completed", "failed", "cancelled", "interrupted", "error", "settled",
  ].includes(runState);
  // A duplicate persisted Run can still say `running` for the tiny interval
  // between runtime settlement and its terminal SQLite write. If no live owner
  // exists, or a role-correlated run_settled packet raced ahead of this ACK,
  // locking here would wait for a packet that will never be sent again.
  const detachedDuplicate = Boolean(
    message.duplicate
    && runState === "running"
    && admissionRole === ModusProtocol.RUN_CONNECTION_ROLES.DETACHED
  );
  const settlementAlreadyObserved = consumeAgentRunSettlement(
    acceptedRunId, acceptedConnectionRole,
  );
  if (terminal || detachedDuplicate || settlementAlreadyObserved) {
    setAgentRunPending(false);
    waiting = false;
    const ready = Boolean(modelRepository?.selection?.default_model_id);
    input.disabled = !ready;
    sendBtn.disabled = !ready;
    document.getElementById("stopBtn").disabled = false;
    document.getElementById("runControl").hidden = true;
    setActivity(
      "●",
      detachedDuplicate || settlementAlreadyObserved
        ? "任务已结束，正在同步结果…"
        : "任务结果已恢复",
      "done",
    );
    syncAcceptedRunResult(acceptedRunId);
    input.focus();
    _sessionHasMsgs = true;
    refreshSessionCatalog();
    return true;
  }
  // A duplicate ACK can confirm that this persisted Run is live while also
  // denying this new runtime cancellation authority. Both roles block another
  // submission, but only the explicit connection owner may expose Stop.
  activeAgentRunRole = acceptedConnectionRole;
  setAgentRunPending(true);
  const ownsAcceptedRun = canCancelActiveAgentRun();
  waiting = true;
  input.disabled = true;
  sendBtn.disabled = true;
  document.getElementById("stopBtn").disabled = !ownsAcceptedRun;
  document.getElementById("runControl").hidden = !ownsAcceptedRun;
  setActivity(
    "◌",
    ownsAcceptedRun
      ? (message.duplicate ? "任务已接收，正在恢复运行…" : "任务已接收，正在运行…")
      : "任务正在另一窗口运行，完成后自动同步…",
    "busy",
  );
  _sessionHasMsgs = true;
  refreshSessionCatalog();
  return true;
}
function settlePendingRunSubmissionError(message) {
  if (String(message?.operation || "") !== "run_message") return null;
  if (!matchesPendingRunSubmission(message, {allowActiveReset:true})) return false;
  const pending = pendingRunSubmission;
  pendingRunSubmission = null;
  restoreRunSubmissionDraft(pending);
  setAgentRunPending(false);
  activeAgentRunId = null;
  waiting = false;
  document.getElementById("runControl").hidden = true;
  document.getElementById("stopBtn").disabled = false;
  finishControlMutation();
  return true;
}
function setVerificationRetryButtonState(priorRunId, state="ready") {
  const id = String(priorRunId || "");
  if (!id) return;
  document.querySelectorAll("[data-verification-retry]").forEach(button => {
    if (String(button.dataset.verificationRetry || "") !== id) return;
    if (state === "pending") {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = "正在提交验证重试…";
    } else if (state === "running") {
      button.disabled = true;
      button.removeAttribute("aria-busy");
      button.textContent = "修复与验证运行中…";
    } else if (state === "settled") {
      button.disabled = true;
      button.removeAttribute("aria-busy");
      button.textContent = "已继续处理";
    } else {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = "继续修复并验证";
    }
  });
}
function setVerificationRetryPendingUi(message="正在提交验证重试…") {
  waiting = true;
  input.disabled = true;
  sendBtn.disabled = true;
  // Before the ACK there is no accepted Run and therefore no Stop authority.
  document.getElementById("runControl").hidden = true;
  document.getElementById("stopBtn").disabled = false;
  setActivity("⟳", message, "busy");
  if (typeof workbenchStore !== "undefined") workbenchStore.render();
}
function verificationRetryPayload(pending) {
  return {
    type:"retry_verification", run_id:pending.priorRunId,
    request_id:pending.requestId,
  };
}
function transmitPendingVerificationRetry() {
  const pending = pendingVerificationRetry;
  if (!pending || !ws || ws.readyState !== WebSocket.OPEN
      || pending.serverEpoch !== String(serverEpoch || "")
      || pending.sessionId !== String(currentDbId || "")
      || pending.runtimeSessionId !== String(sessionId || "")) return false;
  try {
    ws.send(JSON.stringify(verificationRetryPayload(pending)));
  } catch (_error) {
    abandonPendingVerificationRetry("send_failed");
    return false;
  }
  setVerificationRetryButtonState(pending.priorRunId, "pending");
  setVerificationRetryPendingUi();
  return true;
}
function beginVerificationRetry(priorRunId) {
  const id = String(priorRunId || "");
  if (!id || verificationRetryConsumedRuns.has(id)
      || pendingVerificationRetry || pendingRunSubmission
      || pendingSessionResume || agentRunPending || controlMutationPending || waiting
      || !currentDbId || String(renderedSessionId || "") !== String(currentDbId || "")
      || !ws || ws.readyState !== WebSocket.OPEN) return false;
  pendingVerificationRetry = {
    requestId:nextTransientRequestId("verification-retry"),
    priorRunId:id,
    sessionId:String(currentDbId || ""),
    runtimeSessionId:String(sessionId || ""),
    serverEpoch:String(serverEpoch || ""),
  };
  if (transmitPendingVerificationRetry()) return true;
  abandonPendingVerificationRetry("send_failed");
  return false;
}
function matchesPendingVerificationRetry(message) {
  const pending = pendingVerificationRetry;
  return Boolean(
    pending
    && String(message?.operation || "") === "retry_verification"
    && String(message?.request_id || "") === pending.requestId
    && String(message?.prior_run_id || "") === pending.priorRunId
    && String(message?.runtime_session_id || "") === pending.runtimeSessionId
    && String(message?.db_id || "") === pending.sessionId
    && String(sessionId || "") === pending.runtimeSessionId
    && String(currentDbId || "") === pending.sessionId
    && pending.serverEpoch === String(serverEpoch || "")
  );
}
function acceptPendingVerificationRetry(message) {
  if (!matchesPendingVerificationRetry(message)) return false;
  const acceptedRunId = String(message?.run_id || "");
  if (!acceptedRunId || acceptedRunId === pendingVerificationRetry.priorRunId) return false;
  const pending = pendingVerificationRetry;
  pendingVerificationRetry = null;
  activeVerificationRetryPriorRunId = pending.priorRunId;
  verificationRetryConsumedRuns.add(pending.priorRunId);
  setVerificationRetryButtonState(activeVerificationRetryPriorRunId, "running");
  activeAgentRunId = acceptedRunId;
  activeAgentRunRole = ModusProtocol.RUN_CONNECTION_ROLES.OWNER;
  setAgentRunPending(true);
  waiting = true;
  input.disabled = true;
  sendBtn.disabled = true;
  document.getElementById("stopBtn").disabled = false;
  document.getElementById("runControl").hidden = false;
  setActivity("⟳", "修复与验证运行中", "busy");
  return true;
}
function settlePendingVerificationRetryError(message) {
  if (String(message?.operation || "") !== "retry_verification") return null;
  if (!matchesPendingVerificationRetry(message)) return false;
  const pending = pendingVerificationRetry;
  pendingVerificationRetry = null;
  setVerificationRetryButtonState(pending.priorRunId, "ready");
  setAgentRunPending(false);
  waiting = false;
  document.getElementById("runControl").hidden = true;
  document.getElementById("stopBtn").disabled = false;
  finishControlMutation();
  return true;
}
function abandonPendingVerificationRetry(reason="transition", {release=true}={}) {
  const pending = pendingVerificationRetry;
  if (!pending) return false;
  pendingVerificationRetry = null;
  setVerificationRetryButtonState(pending.priorRunId, "ready");
  if (reason === "socket_close" || reason === "server_epoch") {
    verificationRetryReconnectNotice = true;
  }
  if (release) {
    waiting = false;
    document.getElementById("runControl").hidden = true;
    document.getElementById("stopBtn").disabled = false;
    finishControlMutation();
  }
  return true;
}
function showVerificationRetryReconnectNotice() {
  if (!verificationRetryReconnectNotice) return false;
  verificationRetryReconnectNotice = false;
  addSystemMsg("连接中断，未确认的验证重试没有自动重发；请检查最新运行记录后再试。");
  setActivity("○", "验证重试已恢复为可重试", "idle");
  return true;
}
function settleActiveVerificationRetryUi() {
  const priorRunId = activeVerificationRetryPriorRunId;
  if (!priorRunId) return false;
  activeVerificationRetryPriorRunId = null;
  setVerificationRetryButtonState(priorRunId, "settled");
  return true;
}
function requestWorkbenchSnapshot() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  const requestId = nextTransientRequestId("workbench-snapshot");
  const requestedSessionId = String(currentDbId ?? "");
  pendingWorkbenchSnapshot = {requestId, sessionId:requestedSessionId};
  ws.send(JSON.stringify({
    type:"workbench_get", request_id:requestId,
    session_id:requestedSessionId,
  }));
  return true;
}
function requestWorkbenchRun(runId) {
  const requestedRunId = String(runId ?? "");
  if (!requestedRunId || !ws || ws.readyState !== WebSocket.OPEN) return false;
  const requestId = nextTransientRequestId("workbench-run");
  const requestedSessionId = String(currentDbId ?? "");
  // Selecting another Run supersedes the previous detail intent. Only the
  // newest response may enrich the selected Run projection.
  pendingWorkbenchRunDetail = {
    requestId, sessionId:requestedSessionId, runId:requestedRunId,
  };
  ws.send(JSON.stringify({
    type:"workbench_run_get", request_id:requestId,
    session_id:requestedSessionId, run_id:requestedRunId,
  }));
  return true;
}
function matchesWorkbenchIdentity(message, pending, operation) {
  return Boolean(
    pending
    && String(message?.operation ?? "") === operation
    && String(message?.request_id ?? "") === pending.requestId
    && String(message?.requested_session_id ?? "") === pending.sessionId
    && String(message?.session_id ?? "") === pending.sessionId
    && String(currentDbId ?? "") === pending.sessionId
  );
}
function settleWorkbenchError(message) {
  if (message?.operation === "workbench_get") {
    if (!matchesWorkbenchIdentity(
      message, pendingWorkbenchSnapshot, "workbench_get",
    )) return true;
    pendingWorkbenchSnapshot = null;
    return true;
  }
  if (message?.operation === "workbench_run_get") {
    const pending = pendingWorkbenchRunDetail;
    if (!matchesWorkbenchIdentity(message, pending, "workbench_run_get")
        || String(message?.run_id ?? "") !== pending.runId) return true;
    pendingWorkbenchRunDetail = null;
    return true;
  }
  return false;
}
registerTransientRequestReset("workbench", resetWorkbenchRequests);
function artifactRequestMatches(message, pending, {requirePayload=false}={}) {
  const artifactId = String(message?.artifact_id ?? "");
  const payloadArtifactId = String(message?.artifact?.artifact_id ?? "");
  return Boolean(
    pending
    && String(message?.operation ?? "") === "artifact_get"
    && String(message?.request_id ?? "") === pending.requestId
    && artifactId === pending.artifactId
    && String(message?.requested_session_id ?? "") === pending.sessionId
    && String(message?.session_id ?? "") === pending.sessionId
    && String(currentDbId ?? "") === pending.sessionId
    && (!requirePayload || payloadArtifactId === pending.artifactId)
    && (pending.silent
        || (typeof getArtifactViewerState !== "function"
            || (getArtifactViewerState().open
                && String(getArtifactViewerState().artifact_id ?? "") === pending.artifactId)))
  );
}
function resetArtifactRequests(_reason="reset") {
  pendingArtifactRequests.clear();
  if (typeof closeArtifactViewerSilently === "function") closeArtifactViewerSilently();
  else if (typeof closeArtifactViewer === "function") closeArtifactViewer();
  document.querySelectorAll("[data-artifact-id] .artifact-load").forEach(button => {
    button.disabled = false;
    button.textContent = "读取内容";
  });
}
function settleArtifactResponse(message) {
  const artifactId = String(message?.artifact_id ?? "");
  const pending = pendingArtifactRequests.get(artifactId);
  if (!artifactRequestMatches(message, pending, {requirePayload:true})) return false;
  pendingArtifactRequests.delete(artifactId);
  if (typeof renderArtifactViewerContent === "function") {
    renderArtifactViewerContent(message.artifact || {});
  }
  return true;
}
function settleArtifactError(message) {
  const artifactId = String(message?.artifact_id ?? "");
  const pending = pendingArtifactRequests.get(artifactId);
  if (!artifactRequestMatches(message, pending)) return true;
  pendingArtifactRequests.delete(artifactId);
  document.querySelectorAll("[data-artifact-id]").forEach(details => {
    if (String(details.dataset.artifactId || "") !== artifactId) return;
    const button = details.querySelector(".artifact-load");
    if (button) { button.disabled = false; button.textContent = "重试读取"; }
  });
  const rawMessage = String(message?.message || "无法读取该产物，请稍后重试。");
  const userMessage = rawMessage === "artifact is too large to display"
    ? "该产物超过 200 KB，暂不支持在应用内查看。"
    : rawMessage === "artifact not found"
      ? "未找到当前会话中的该产物。"
      : rawMessage;
  if (typeof renderArtifactViewerError === "function") {
    renderArtifactViewerError(userMessage, {artifact_id:artifactId});
  }
  return true;
}
registerTransientRequestReset("artifact", resetArtifactRequests);
let controlMutationPending = false;
function beginControlMutation() {
  if (controlMutationPending || pendingSessionResume || pendingRunSubmission
      || pendingVerificationRetry || agentRunPending
      || !ws || ws.readyState !== WebSocket.OPEN) return false;
  controlMutationPending = true;
  waiting = true; input.disabled = true; sendBtn.disabled = true;
  return true;
}
function finishControlMutation() {
  controlMutationPending = false;
  if (pendingSessionResume) { setSessionResumeUi(); return; }
  if (pendingRunSubmission) { setRunSubmissionUi(); return; }
  if (pendingVerificationRetry) { setVerificationRetryPendingUi(); return; }
  if (agentRunPending) return;
  waiting = false;
  const ready = Boolean(modelRepository?.selection?.default_model_id);
  input.disabled = !ready; sendBtn.disabled = !ready;
}
function observeServerEpoch(message) {
  const nextEpoch = String(message?.server_epoch || "");
  if (!nextEpoch || nextEpoch === serverEpoch) return false;
  const changed = Boolean(serverEpoch);
  serverEpoch = nextEpoch;
  if (!changed) return false;

  // A restarted process begins every revision counter at zero and cannot
  // finish work owned by its predecessor.  Reset the gates before routing the
  // current packet, then release only client-side operations that cannot be
  // correlated across the process boundary.  User input is deliberately kept.
  repositoryRevision = 0;
  sessionCatalogRevision = 0;
  skillsRevision = 0;
  extensionsRevision = 0;
  desktopProtocolCompatible = null;
  cancelPendingSessionResume("server_epoch", {release:false});
  abandonPendingRunSubmission("server_epoch");
  abandonPendingVerificationRetry("server_epoch", {release:false});
  setAgentRunPending(false);
  controlMutationPending = false;
  finishRunReplay();
  transcriptGapRequests.clear();
  recentAgentRunSettlements.clear();
  if (typeof clearSessionCreateIntent === "function") clearSessionCreateIntent();
  resetTransientRequests("server_epoch");
  waiting = false;
  document.getElementById("runControl").hidden = true;
  document.getElementById("stopBtn").disabled = false;
  finishControlMutation();
  return true;
}
// Transcript cursors live for the lifetime of this page.  They make same-page
// WebSocket reconnects incremental while an actual page reload intentionally
// requests an authoritative bounded replay from SQLite.  Keeping them out of
// durable browser storage also prevents stale cursors surviving server/data
// migrations.
let transcriptCursors = {};
const transcriptCursorsBySession = {};
const transcriptGapRequests = new Set();
let pendingLegacySessionId = "";
let pendingLegacyMessages = [];
function loadTranscriptCursors(dbId) {
  transcriptCursors = dbId && transcriptCursorsBySession[dbId]
    ? transcriptCursorsBySession[dbId] : {};
}
function saveTranscriptCursors() {
  if (!currentDbId) return;
  transcriptCursorsBySession[currentDbId] = transcriptCursors;
}
function observeTranscriptEvent(event) {
  if (!event?.run_id) return;
  const runId = event.run_id;
  const seq = Number(event.sequence || 0);
  const cursor = Number(transcriptCursors[runId] || 0);
  if (seq > cursor + 1 && !transcriptGapRequests.has(runId)) {
    transcriptGapRequests.add(runId);
    ws?.send(JSON.stringify({type:"transcript_sync",run_id:runId,since_sequence:cursor}));
  }
  if (seq === cursor + 1) {
    transcriptCursors[runId] = seq;
    saveTranscriptCursors();
  }
}
function clearTimelineState() {
  if (typeof eventStore !== "undefined") {
    eventStore.byId = new Map(); eventStore.runs = new Map(); eventStore.channels = new Map();
  }
  if (typeof timelineRenderer !== "undefined") timelineRenderer.reset();
  if (typeof clearWorkspaceState === "function") clearWorkspaceState();
}
function clearTranscriptState() {
  clearTimelineState();
  if (typeof workbenchStore !== "undefined") workbenchStore.reset();
}
function setPendingLegacyMessages(sessionId, messages) {
  pendingLegacySessionId = String(sessionId || "");
  pendingLegacyMessages = Array.isArray(messages) ? messages : [];
}
function renderLegacyMessagePrefix(sessionId) {
  if (!sessionId || pendingLegacySessionId !== String(sessionId)
      || !pendingLegacyMessages.length) return;
  const ca = document.getElementById("chatArea");
  if (ca.querySelector('.msg[data-legacy-prefix="true"]')) return;
  const fragment = document.createDocumentFragment();
  pendingLegacyMessages.forEach(message => {
    if (!["user", "assistant"].includes(message?.role)) return;
    const node = document.createElement("div");
    node.className = "msg " + message.role;
    node.dataset.legacyPrefix = "true";
    node.innerHTML = '<div class="ava">' + (message.role === "user" ? "Y" : "M")
      + '</div><div class="block-text">' + escapeHtml(message.content || "") + '</div>';
    fragment.appendChild(node);
  });
  const firstTypedNode = ca.querySelector(".timeline-item, .run-completion");
  ca.insertBefore(fragment, firstTypedNode || null);
}
function applyTranscriptEvent(event) {
  event = ModusProtocol.normalizeAgentEvent(event);
  if (!event) return;
  observeTranscriptEvent(event);
  if (eventStore.push(event)) {
    timelineRenderer.render(event);
    // The workbench renders last because it owns the authoritative task rail;
    // the legacy collaboration projection remains temporarily for compatibility
    // but can no longer replace ledger-backed task state with text heuristics.
    workbenchStore.observe(event);
    observeWorkspaceEvent(event);
  }
}
function applyTranscriptEvents(events) { (events || []).forEach(applyTranscriptEvent); }
let replayingRunId = null;
let replayRequestId = null;
function beginRunReplayReplacement() {
  // Keep the current transcript visible until the server has acknowledged a
  // valid, session-scoped replay.  A failed request must never leave an empty
  // chat behind.
  const ca = document.getElementById("chatArea");
  ca.querySelectorAll(".msg, .timeline-item, .run-completion, .empty-state, .collab-msg, .timeline-expand-earlier").forEach(e => e.remove());
  const lower = document.getElementById("chatAreaLower");
  if (lower) lower.innerHTML = "";
  // The KANBAN board owns the right panel; close its drawer on replay reset.
  if (window.ModusKanban && typeof window.ModusKanban.closeDrawer === "function") window.ModusKanban.closeDrawer();
  clearTimelineState();
}
function finishRunReplay(message="", state="idle") {
  replayingRunId = null;
  replayRequestId = null;
  if (typeof workbenchStore !== "undefined") workbenchStore.render();
  if (message) setActivity(state === "done" ? "●" : "○", message, state);
}
function replayRun(runId) {
  if (!runId || agentRunPending || pendingVerificationRetry
      || controlMutationPending || replayingRunId
      || !ws || ws.readyState !== WebSocket.OPEN) return;
  replayingRunId = runId;
  replayRequestId = nextTransientRequestId("run-replay");
  if (typeof workbenchStore !== "undefined") workbenchStore.render();
  setActivity("⟳", "回放运行事件…", "busy");
  ws.send(JSON.stringify({
    type:"transcript_sync", run_id:runId, since_sequence:0,
    request_id:replayRequestId,
  }));
}
let currentMsg = null, currentBub = null, toolStep = 0;
const MODUS_STORAGE_SCHEMA = "1";
if (localStorage.getItem("modus_storage_schema") !== MODUS_STORAGE_SCHEMA) {
  localStorage.removeItem("modus_last_db_id");
  localStorage.removeItem("modus_current_mode");
  localStorage.setItem("modus_storage_schema", MODUS_STORAGE_SCHEMA);
}
let currentMode = "default";
let currentModeConfig = {};
let currentReasoningEffort = null;

// ─── Simple syntax highlighting ───
function _highlightCode(code, lang) {
  if (!code) return "";
  let s = escapeHtml(code);
  lang = (lang || "").toLowerCase();
  // Python keywords
  if (lang === "python" || lang === "py" || lang === "") {
    const kw = "\\b(def|class|import|from|return|if|elif|else|for|while|try|except|finally|with|as|pass|break|continue|and|or|not|in|is|None|True|False|async|await|yield|lambda|raise|global|nonlocal|del|print|range|len|type|int|str|list|dict|set|tuple|open|self|__init__|__str__|__repr__)\\b";
    s = s.replace(new RegExp(kw, "g"), '<span class="kw">$1</span>');
    s = s.replace(/(\'[^\']*\'|"[^"]*")/g, '<span class="str">$1</span>');
    s = s.replace(/(#[^\n]*)/g, '<span class="cm">$1</span>');
    s = s.replace(/\b(\d+(\.\d+)?)\b/g, '<span class="num">$1</span>');
    s = s.replace(/\b(def|class)\s+(\w+)/g, (m, kw, name) => '<span class="kw">' + kw + '</span> <span class="fn">' + name + '</span>');
  }
  // JavaScript/TypeScript
  if (lang === "javascript" || lang === "js" || lang === "typescript" || lang === "ts") {
    const kw = "\\b(const|let|var|function|return|if|else|for|while|async|await|import|export|default|from|class|extends|new|this|throw|try|catch|finally|typeof|instanceof|true|false|null|undefined|Promise|console|document|window|Array|Object|String|Number)\\b";
    s = s.replace(new RegExp(kw, "g"), '<span class="kw">$1</span>');
    s = s.replace(/(\`[^\`]*\`|\'[^\']*\'|"[^"]*")/g, '<span class="str">$1</span>');
    s = s.replace(/(\/\/[^\n]*)/g, '<span class="cm">$1</span>');
    s = s.replace(/\b(\d+(\.\d+)?)\b/g, '<span class="num">$1</span>');
  }
  // JSON
  if (lang === "json") {
    s = s.replace(/("(?:[^"\\\\]|\\\\.)*")\s*:/g, '<span class="fn">$1</span>:');
    s = s.replace(/:\s*("(?:[^"\\\\]|\\\\.)*")/g, ': <span class="str">$1</span>');
    s = s.replace(/:\s*(\d+(\.\d+)?)/g, ': <span class="num">$1</span>');
    s = s.replace(/\b(true|false|null)\b/g, '<span class="kw">$1</span>');
  }
  // Shell/Bash
  if (lang === "bash" || lang === "sh" || lang === "shell") {
    s = s.replace(/(#.*$)/gm, '<span class="cm">$1</span>');
    s = s.replace(/(\'(?:[^\'\\\\]|\\\\.)*\'|"(?:[^"\\\\]|\\\\.)*")/g, '<span class="str">$1</span>');
    s = s.replace(/\b(\d+)\b/g, '<span class="num">$1</span>');
  }
  return s;
}
// ═══ DOM Refs ═══
const chatArea = document.getElementById("chatArea");
const input = document.getElementById("input");
const sendBtn = document.getElementById("sendBtn");
const wvBody = document.getElementById("wvBody");
const rpWorkspace = document.getElementById("rpWorkspace");

/* Smart scroll: only auto-scroll when user is near bottom */
let _userScrolled = false;
chatArea.addEventListener("scroll", () => {
  const nearBottom = chatArea.scrollHeight - chatArea.scrollTop - chatArea.clientHeight < 100;
  _userScrolled = !nearBottom;
}, {passive:true});
function _autoScroll() {
  if (!_userScrolled) chatArea.scrollTop = chatArea.scrollHeight;
}
function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

// ─── CodeCard icons (Hermes-style SVG, stroke inherits currentColor) ───
const COPY_ICON_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5V4a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5"/></svg>';
const COPIED_ICON_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 8.5l3.5 3.5 7-7.5"/></svg>';
