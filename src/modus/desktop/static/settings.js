// ═══ Settings, model repository, and composer selection ═══
const settingsModal = document.getElementById("settingsModal");
const composerSelect = document.getElementById("composerSelect");
const composerMenu = document.getElementById("composerMenu");
let modelRepository = {models: [], selection: {default_model_id: null, moa_model_ids: [], peri_model_ids: []}};
let repoEditorId = null;
let pendingModelTestRequestId = null;
let pendingModelDiscoveryRequestId = null;
let pendingModelDiscoverySourceId = null;
let pendingCredentialMigrationRequestId = null;
let pendingCredentialMigrationPhase = null;
let pendingPeriReadinessRequestId = null;
let pendingSkillFetchRequestId = null;
let pendingSessionSettingsReadRequestId = null;
let pendingSessionSettingsWriteRequestId = null;
let pendingSessionSettingsSessionId = null;
let pendingSessionSettingsReadBaseline = null;
let pendingSessionSettingsPrompt = null;
let pendingSessionExecutionMutation = null;

function resetModelTestState() {
  pendingModelTestRequestId = null;
  const status = document.getElementById("repoTestStatus");
  if (status) { status.style.display = "none"; status.textContent = ""; }
  const button = document.getElementById("repoTestBtn");
  if (button) { button.disabled = false; button.textContent = "测试连接"; }
}
registerTransientRequestReset("model-test", resetModelTestState);

function providerLabel(provider) {
  return ({deepseek:"DeepSeek", openai:"OpenAI", anthropic:"Anthropic", google:"Google", groq:"Groq", xai:"xAI", ollama:"Ollama", custom:"Custom"})[provider] || provider || "其他";
}
function modelById(id) { return modelRepository.models.find(model => model.id === id) || null; }
function modelLabel(id) { const model = modelById(id); return model ? (model.name || model.model) : "选择模型"; }
function formatTokens(value) { const n=Number(value)||0; return n>=1000000?(n/1000000).toFixed(n%1000000?1:0)+"M":n>=1000?Math.round(n/1000)+"K":String(n); }
function usageOwnerLabel(key) {
  const parts = String(key || "").split(":");
  const role = parts[1] || parts[0];
  if (role === "aggregator") return "MOA 聚合";
  if (role === "host") return "主持人";
  if (/^worker_\d+$/.test(role)) return "Peri Worker " + role.split("_")[1];
  if (/^reference_\d+$/.test(role)) return "MOA 参考 " + role.split("_")[1];
  return escapeHtml(key);
}
function modelCapabilitySummary(model) {
  if (!model) return "";
  const items=[formatTokens(model.context_window)+" ctx"];
  if(model.supports_tools)items.push("tools");
  if(model.supports_images)items.push("vision");
  if((model.reasoning_efforts||[]).length)items.push("reasoning "+model.reasoning_efforts.join("/"));
  const sources=new Set(Object.values(model.capability_sources||{}));
  if(sources.has("unknown"))items.push("部分能力待确认");
  else if(sources.has("user_configuration"))items.push("用户校正");
  else if(sources.has("provider_api"))items.push("厂商元数据");
  else if(sources.has("modus_catalog"))items.push("Modus 目录");
  return items.join(" · ");
}
function modeConfigured(mode) {
  const selection = modelRepository.selection;
  const roles = mode === "moa" ? selection.moa_roles : selection.peri_roles;
  const participant = mode === "moa" ? "reference_1" : "worker_1";
  return Boolean(roles?.host?.model_id && roles?.[participant]?.model_id);
}
function applyRepository(data) {
  modelRepository = {models: Array.isArray(data?.models) ? data.models : [], selection: data?.selection || {default_model_id:null, moa_model_ids:[], peri_model_ids:[]}};
  renderComposerMenu(); renderRepository();
  const ready = Boolean(modelRepository.selection.default_model_id);
  composerSelect.disabled = !modelRepository.models.length;
  if (!ready) { input.disabled = true; sendBtn.disabled = true; input.placeholder = "先在设置中添加模型"; }
  else if (!waiting) { input.disabled = false; sendBtn.disabled = false; input.placeholder = "输入消息…"; }
  syncOnboarding();
}

// ─── First-use onboarding ───
const ONBOARDING_KEY = "modus_onboarding_done";
let onboardingStep = 1;
let onboardingInProgress = false;
function onboardingEl() { return document.getElementById("onboarding"); }
function syncOnboarding() {
  if (!onboardingEl()) return;
  if (localStorage.getItem(ONBOARDING_KEY)) { onboardingEl().hidden = true; return; }
  if (!onboardingInProgress) {
    if (!modelRepository.models.length) {
      onboardingInProgress = true;
      onboardingStep = 1;
    } else {
      // Existing repository without the completion marker — treat as done.
      localStorage.setItem(ONBOARDING_KEY, "1");
      onboardingEl().hidden = true;
      return;
    }
  }
  // Mid-flow: a model now exists — advance to the ready step.
  if (onboardingStep === 1 && modelRepository.models.length) onboardingStep = 2;
  onboardingEl().hidden = false;
  renderOnboarding();
}
function renderOnboarding() {
  const body = document.getElementById("obBody");
  const steps = document.querySelectorAll("#obSteps .ob-step");
  steps.forEach((step, i) => step.classList.toggle("done", i < onboardingStep));
  if (onboardingStep === 1) {
    body.innerHTML = '<div class="ob-title">添加你的第一个模型</div>'
      + '<div class="ob-copy">Modus 需要一个模型才能开始对话。添加后它会自动设为默认模型。API Key 只会保存在本机，不会回传给页面。</div>'
      + '<div class="ob-actions"><button class="skip" type="button" id="obSkip1">跳过</button>'
      + '<button class="primary-small" type="button" id="obAddModel">添加模型</button></div>';
    document.getElementById("obAddModel").onclick = () => { openSettings("repo"); openModelEditor(); };
    document.getElementById("obSkip1").onclick = () => dismissOnboarding();
  } else if (onboardingStep === 2) {
    body.innerHTML = '<div class="ob-title">模型已就绪</div>'
      + '<div class="ob-copy">「' + escapeHtml(modelLabel(modelRepository.selection.default_model_id)) + '」已设为默认模型。你现在可以直接开始对话，也可以配置 MOA 或 Peri 增强方式。</div>'
      + '<div class="ob-mode-row">'
      + '<button class="ob-mode-card" type="button" id="obSetupMoa"><b>MOA</b><span>多模型独立参考，Host 综合并执行</span></button>'
      + '<button class="ob-mode-card" type="button" id="obSetupPeri"><b>Peri</b><span>多 Agent 分工、互审、修订并形成共识</span></button>'
      + '</div>'
      + '<div class="ob-actions"><button class="skip" type="button" id="obStart">开始使用</button></div>';
    document.getElementById("obSetupMoa").onclick = () => { finishOnboarding(); openSettings("moa"); };
    document.getElementById("obSetupPeri").onclick = () => { finishOnboarding(); openSettings("peri"); };
    document.getElementById("obStart").onclick = () => finishOnboarding();
  }
}
function finishOnboarding() { localStorage.setItem(ONBOARDING_KEY, "1"); onboardingInProgress = false; onboardingEl().hidden = true; }
function dismissOnboarding() { localStorage.setItem(ONBOARDING_KEY, "1"); onboardingInProgress = false; onboardingEl().hidden = true; openSettings("repo"); }
function selectedHostModel() { return modelById(currentModelId || modelRepository.selection.default_model_id); }
function reasoningLabel(value) { return value ? "思考：" + value : "思考：默认"; }
function renderReasoningMenu() {
  const wrap = document.getElementById("composerReasoningWrap");
  const button = document.getElementById("composerReasoning");
  const label = document.getElementById("composerReasoningLabel");
  const menu = document.getElementById("composerReasoningMenu");
  const hostModel = currentMode === "default" ? selectedHostModel() : modelById(_modeHostModelId(currentMode));
  const efforts = hostModel?.reasoning_efforts || [];
  if (!wrap || !button || !label || !menu) return;
  wrap.hidden = !efforts.length;
  label.textContent = reasoningLabel(currentReasoningEffort);
  if (!efforts.length) { menu.innerHTML = ""; return; }
  menu.innerHTML = '<div class="menu-heading">思考深度</div>'
    + '<button class="menu-option ' + (!currentReasoningEffort ? 'selected' : '') + '" type="button" data-reasoning=""><span>默认<small>使用模型默认设置</small></span><b>' + (!currentReasoningEffort ? '✓' : '') + '</b></button>'
    + efforts.map(value => '<button class="menu-option ' + (currentReasoningEffort === value ? 'selected' : '') + '" type="button" data-reasoning="' + escapeHtml(value) + '"><span>' + escapeHtml(value) + '</span><b>' + (currentReasoningEffort === value ? '✓' : '') + '</b></button>').join('');
  menu.querySelectorAll("[data-reasoning]").forEach(option => option.onclick = () => chooseReasoning(option.dataset.reasoning));
}
function _modeHostModelId(mode) {
  const repositoryRoles = mode === "moa" ? modelRepository.selection.moa_roles : modelRepository.selection.peri_roles;
  const roles = currentMode === mode && currentModeConfig?.host ? currentModeConfig : repositoryRoles;
  return roles?.host?.model_id || "";
}
function renderComposerMenu() {
  const label = currentMode === "moa" ? "MOA" : currentMode === "peri" ? "Peri" : modelLabel(currentModelId || modelRepository.selection.default_model_id);
  document.getElementById("composerSelectLabel").textContent = label;
  const groups = {};
  modelRepository.models.forEach(model => (groups[model.provider] ||= []).push(model));
  let html = '<div class="menu-heading">模型</div>';
  Object.entries(groups).forEach(([provider, models]) => {
    html += '<div class="menu-group"><div class="menu-group-label">' + escapeHtml(providerLabel(provider)) + '</div>';
    models.forEach(model => {
      const active = currentMode === "default" && model.id === (currentModelId || modelRepository.selection.default_model_id);
      html += '<button class="menu-option ' + (active ? 'selected' : '') + '" type="button" data-model-id="' + escapeHtml(model.id) + '"><span>' + escapeHtml(model.name || model.model) + '<small>' + escapeHtml(model.model) + ' · ' + escapeHtml(modelCapabilitySummary(model)) + '</small></span><b>' + (active ? '✓' : '') + '</b></button>';
    });
    html += '</div>';
  });
  html += '<div class="menu-heading">增强方式</div>';
  [["moa", "MOA", "多模型独立参考，Host 综合并执行"], ["peri", "Peri", "多 Agent 分工、互审、修订并形成共识"], ["agi", "AGI", "未来自主 Agent（预留）"]].forEach(([mode, title, detail]) => {
    html += '<button class="menu-option mode-option ' + (currentMode === mode ? 'selected' : '') + '" type="button" data-mode="' + mode + '"><span>' + title + '<small>' + detail + '</small></span><b>' + (modeConfigured(mode) ? '' : '设置') + '</b></button>';
  });
  composerMenu.innerHTML = html;
  composerMenu.querySelectorAll("[data-model-id]").forEach(button => button.onclick = () => chooseDefault(button.dataset.modelId));
  composerMenu.querySelectorAll("[data-mode]").forEach(button => button.onclick = () => chooseMode(button.dataset.mode));
  renderReasoningMenu();
}
function setMode(mode) {
  currentMode = ["moa", "peri", "agi"].includes(mode) ? mode : "default";
  localStorage.setItem("modus_current_mode", currentMode);
  // The collaboration area is content-driven: setMode only grants permission;
  // its actual visibility is decided by syncLowerVisibility (content present?).
  if (typeof syncLowerVisibility === "function") syncLowerVisibility();
  else {
    document.getElementById("chatDivider").style.display = "none";
    document.getElementById("chatAreaLower").style.display = "none";
  }
  renderComposerMenu();
}
function closeComposerMenu() { composerMenu.hidden = true; composerSelect.setAttribute("aria-expanded", "false"); }
function closeReasoningMenu() { const menu = document.getElementById("composerReasoningMenu"); const button = document.getElementById("composerReasoning"); if (menu) menu.hidden = true; if (button) button.setAttribute("aria-expanded", "false"); }
document.getElementById("composerReasoning").onclick = () => { const menu=document.getElementById("composerReasoningMenu"), button=document.getElementById("composerReasoning"); closeComposerMenu(); menu.hidden=!menu.hidden; button.setAttribute("aria-expanded", String(!menu.hidden)); };
function beginSessionExecutionMutation(operation) {
  if (!beginControlMutation()) return null;
  const pending = {
    operation,
    requestId:nextTransientRequestId(operation),
    dbId:String(currentDbId || ""),
    runtimeSessionId:String(sessionId || ""),
  };
  pendingSessionExecutionMutation = pending;
  return pending;
}
function matchesSessionExecutionMutation(message, operation) {
  const pending = pendingSessionExecutionMutation;
  return Boolean(
    pending
    && pending.operation === operation
    && String(message?.operation || "") === operation
    && String(message?.request_id || "") === pending.requestId
    && String(message?.requested_db_id ?? "") === pending.dbId
    && String(message?.db_id ?? "") === pending.dbId
    && String(message?.runtime_session_id || "") === pending.runtimeSessionId
    && String(currentDbId || "") === pending.dbId
    && String(sessionId || "") === pending.runtimeSessionId
  );
}
function settleSessionExecutionMutation(message, operation) {
  if (!matchesSessionExecutionMutation(message, operation)) return false;
  pendingSessionExecutionMutation = null;
  return true;
}
function resetSessionExecutionMutation(_reason="reset") {
  const ownedControlMutation = Boolean(pendingSessionExecutionMutation);
  pendingSessionExecutionMutation = null;
  if (ownedControlMutation) finishControlMutation();
}
registerTransientRequestReset("session-execution-mutation", resetSessionExecutionMutation);
function chooseDefault(id) {
  const pending = beginSessionExecutionMutation("session_set_model");
  if (!pending) return;
  ws.send(JSON.stringify({
    type:"session_set_model", model_id:id,
    request_id:pending.requestId, session_id:pending.dbId,
  }));
  // Don't create a session here — model_repository_updated will trigger
  // sessions_list which refreshes the sidebar badges with the correct model name.
  closeComposerMenu();
}
function chooseReasoning(value) {
  const pending = beginSessionExecutionMutation("session_set_reasoning");
  if (!pending) return;
  ws.send(JSON.stringify({
    type:"session_set_reasoning", session_id:pending.dbId,
    request_id:pending.requestId, reasoning_effort:value || "",
  }));
  closeReasoningMenu();
}
function chooseMode(mode) {
  if (!modeConfigured(mode)) { closeComposerMenu(); openSettings(mode); return; }
  const prevMode = typeof currentMode !== "undefined" ? currentMode : "default";
  if (mode === prevMode) { closeComposerMenu(); return; }
  // A populated conversation starts a new session for the requested mode;
  // an empty conversation can change its authoritative runtime in place.
  if (ws.readyState === WebSocket.OPEN) {
    if (_sessionHasMsgs) {
      if (!beginControlMutation()) { closeComposerMenu(); return; }
      if (!sendSessionCreate(mode, "新对话", true)) finishControlMutation();
    } else {
      const pending = beginSessionExecutionMutation("session_set_mode");
      if (!pending) { closeComposerMenu(); return; }
      ws.send(JSON.stringify({
        type:"session_set_mode", session_id:pending.dbId, mode,
        request_id:pending.requestId,
      }));
    }
  }
  closeComposerMenu();
}

function sendSessionCreate(mode = currentMode, title = "新对话", fromControlMutation = false) {
  // Creating another conversation supersedes an unacknowledged run intent,
  // but the composer draft and attached Skill deliberately move with the user.
  if (pendingSessionResume) cancelPendingSessionResume("session_create");
  if (pendingRunSubmission) abandonPendingRunSubmission("session_create");
  if (creatingSession || agentRunPending
      || (!fromControlMutation && (controlMutationPending || waiting))
      || !ws || ws.readyState !== WebSocket.OPEN) return false;
  beginWorkbenchSessionTransition("session_create");
  creatingSession = true;
  pendingSessionCreateKey = pendingSessionCreateKey || (globalThis.crypto?.randomUUID ? crypto.randomUUID() : "create-" + Date.now() + "-" + Math.random().toString(16).slice(2));
  const button = document.getElementById("newChatBtn");
  if (button) button.disabled = true;
  ws.send(JSON.stringify({type:"session_create",request_key:pendingSessionCreateKey,title,mode}));
  return true;
}
function clearSessionCreateIntent(requestKey = "") {
  if (requestKey && requestKey !== pendingSessionCreateKey) return false;
  pendingSessionCreateKey = null;
  creatingSession = false;
  document.getElementById("newChatBtn")?.removeAttribute("disabled");
  return true;
}
const MODE_SUBTABS = ["session", "moa", "peri"];
function showSettingsPanel(name) {
  // 一级 Tab 激活。mode 是会话/MOA/Peri 的父分类：点它默认落到"会话"子 Tab。
  const topName = MODE_SUBTABS.includes(name) ? "mode" : name;
  document.querySelectorAll(".settings-tab").forEach(tab => {
    const active = tab.dataset.tab === topName;
    tab.classList.toggle("active", active);
  });
  document.querySelectorAll(".settings-panel").forEach(panel => {
    panel.style.display = panel.id === "panel-" + topName ? "block" : "none";
  });
  // 落到模式分类时，切换对应子 Tab（openSettings("moa") 也能直达）。
  if (topName === "mode") showModeSubtab(MODE_SUBTABS.includes(name) ? name : "session");
}
function showModeSubtab(name) {
  document.querySelectorAll(".mode-subtab").forEach(tab => {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".mode-panel").forEach(panel => {
    panel.style.display = panel.dataset.modePanel === name ? "block" : "none";
  });
}
function resetSessionSettingsRequests(_reason="reset") {
  pendingSessionSettingsReadRequestId = null;
  pendingSessionSettingsWriteRequestId = null;
  pendingSessionSettingsSessionId = null;
  pendingSessionSettingsReadBaseline = null;
  pendingSessionSettingsPrompt = null;
  const button = document.getElementById("sessionPromptSaveBtn");
  if (button) { button.disabled = false; button.textContent = "保存会话提示词"; }
}
function settleSessionSettingsRead(message) {
  const requestId = String(message?.request_id || "");
  const targetSessionId = String(message?.session_id || "");
  if (!requestId || requestId !== pendingSessionSettingsReadRequestId
      || targetSessionId !== pendingSessionSettingsSessionId
      || targetSessionId !== String(currentDbId || "")) return false;
  pendingSessionSettingsReadRequestId = null;
  const promptEl = document.getElementById("sessionSystemPrompt");
  if (promptEl && promptEl.value === pendingSessionSettingsReadBaseline) {
    promptEl.value = message.system_prompt || "";
  }
  pendingSessionSettingsReadBaseline = null;
  currentReasoningEffort = message.reasoning_effort || null;
  renderReasoningMenu();
  return true;
}
function settleSessionSettingsWrite(message) {
  const requestId = String(message?.request_id || "");
  const targetSessionId = String(message?.session_id || "");
  if (!requestId || requestId !== pendingSessionSettingsWriteRequestId
      || targetSessionId !== pendingSessionSettingsSessionId
      || targetSessionId !== String(currentDbId || "")) return false;
  const submittedPrompt = pendingSessionSettingsPrompt;
  resetSessionSettingsRequests("settled");
  // Do not replace text typed after Save was clicked.  The acknowledgement
  // describes the submitted value, while the editor may already contain the
  // user's next revision.
  const promptEl = document.getElementById("sessionSystemPrompt");
  if (promptEl && promptEl.value === submittedPrompt
      && message.system_prompt !== null && message.system_prompt !== undefined) {
    promptEl.value = message.system_prompt;
  }
  showModalStatus("会话提示词已保存", "ok");
  return true;
}
function requestSessionSettings() {
  const targetSessionId = String(currentDbId || "");
  if (!targetSessionId || !ws || ws.readyState !== WebSocket.OPEN) return false;
  pendingSessionSettingsReadRequestId = nextTransientRequestId("session-settings-read");
  pendingSessionSettingsSessionId = targetSessionId;
  pendingSessionSettingsReadBaseline = document.getElementById("sessionSystemPrompt")?.value || "";
  ws.send(JSON.stringify({
    type:"session_get", session_id:targetSessionId,
    request_id:pendingSessionSettingsReadRequestId,
  }));
  return true;
}
registerTransientRequestReset("session-settings", resetSessionSettingsRequests);

function openSettings(tab="repo") {
  closeMobileSidebar();
  closeWorkbenchPanel();
  settingsModal.classList.add("on"); showSettingsPanel(tab);
  ws?.send(JSON.stringify({type:"model_repository_get"}));
  ws?.send(JSON.stringify({type:"skills_list"}));
  if (currentDbId) requestSessionSettings();
  if (tab === "memory") requestCurrentMemories();
  if (tab === "memory") requestAgentMemoryConfig();
}
function setMobileSidebar(open) {
  if (open) setWorkbenchPanel(false);
  document.body.classList.toggle("mobile-sidebar-open", open);
  const button = document.getElementById("mobileSessionsBtn");
  if (button) button.setAttribute("aria-expanded", String(open));
}
function closeMobileSidebar() { setMobileSidebar(false); }
function setWorkbenchPanel(open) {
  if (open) closeMobileSidebar();
  document.body.classList.toggle("workbench-open", open);
  const button = document.getElementById("workbenchToggleBtn");
  if (button) {
    button.setAttribute("aria-expanded", String(open));
    button.setAttribute("aria-label", open ? "关闭任务面板" : "打开任务面板");
  }
}
function closeWorkbenchPanel() { setWorkbenchPanel(false); }
document.getElementById("mobileSessionsBtn").onclick = () => setMobileSidebar(!document.body.classList.contains("mobile-sidebar-open"));
document.getElementById("mobileSidebarScrim").onclick = closeMobileSidebar;
document.getElementById("workbenchToggleBtn").onclick = () => setWorkbenchPanel(!document.body.classList.contains("workbench-open"));
document.getElementById("workbenchScrim").onclick = closeWorkbenchPanel;
document.getElementById("workbenchCloseBtn").onclick = closeWorkbenchPanel;
document.getElementById("mobileSettingsBtn").onclick = () => openSettings();
window.addEventListener("resize", () => { if (window.innerWidth > 800) closeMobileSidebar(); if (window.innerWidth > 1100) closeWorkbenchPanel(); });
document.getElementById("settingsBtn").onclick = () => openSettings();
document.getElementById("closeModal").onclick = () => closeSettings();
window.addEventListener("keydown", event => {
  if (event.key === "Escape" && settingsModal.classList.contains("on")) closeSettings();
});
document.querySelectorAll(".settings-tab").forEach(tab => tab.onclick = () => {
  showSettingsPanel(tab.dataset.tab);
  if (tab.dataset.tab === "memory") { requestCurrentMemories(); requestAgentMemoryConfig(); }
});
// 模式分类下的子 Tab：会话 / MOA / Peri 共用 panel-mode 容器。
document.querySelectorAll(".mode-subtab").forEach(tab => tab.onclick = () => {
  showModeSubtab(tab.dataset.tab);
  if (tab.dataset.tab === "session" && currentDbId) {
    requestSessionSettings();
  }
});
function closeSettings() { settingsModal.classList.remove("on"); }
function renderSessionRun(run) {
  const el = document.getElementById("sessionRunStatus");
  if (!el) return;
  if (!run) { el.textContent = "当前会话还没有运行记录。"; return; }
  const labels = {running:"运行中",completed:"已完成",failed:"运行失败",cancelled:"已取消",interrupted:"进程中断"};
  const modeLabels = {default:"",moa:"MOA",peri:"Peri"};
  const state = labels[run.state] || run.state || "未知";
  const reason = run.stop_reason ? " · 原因：" + run.stop_reason : "";
  const tokens = run.budget?.total_tokens != null ? " · " + run.budget.total_tokens + " tokens" : "";
  const snapshot = run.config_snapshot || {};
  const rawMode = snapshot.mode || run.mode || "default";
  const mode = modeLabels[rawMode] ?? rawMode;
  const host = snapshot.roles?.host || {};
  const hostLabel = host.name || host.model || snapshot.host_model_id || "未记录";
  const effort = snapshot.reasoning_effort || host.reasoning_effort || "默认";
  const verification = snapshot.verification?.required
    ? "必须验证 · 最多 " + (snapshot.verification.max_attempts || 0) + " 次"
    : "按需验证";
  const summary = "最近运行：" + state + reason + tokens;
  const audit = snapshot.schema
    ? '<div style="margin-top:5px;color:var(--text-tertiary)">' + [mode, hostLabel, "思考 " + effort, verification].filter(Boolean).map(escapeHtml).join(" · ") + '</div>'
    : '<div style="margin-top:5px;color:var(--text-tertiary)">此运行未记录配置快照</div>';
  el.innerHTML = '<div>' + escapeHtml(summary) + '</div>' + audit;
  el.style.color = run.state === "failed" ? "var(--red)" : run.state === "running" ? "var(--amber)" : "var(--text-secondary)";
}
document.getElementById("sessionPromptSaveBtn").onclick = () => {
  if (!currentDbId || !ws || ws.readyState !== WebSocket.OPEN) { showModalStatus("当前会话尚未持久化", "err"); return; }
  const targetSessionId = String(currentDbId);
  const prompt = document.getElementById("sessionSystemPrompt").value;
  // A confirmed write supersedes any older read of the same form.  Keep the
  // captured value only for correlation; never clear or roll back user input.
  pendingSessionSettingsReadRequestId = null;
  pendingSessionSettingsWriteRequestId = nextTransientRequestId("session-settings-write");
  pendingSessionSettingsSessionId = targetSessionId;
  pendingSessionSettingsPrompt = prompt;
  const button = document.getElementById("sessionPromptSaveBtn");
  button.disabled = true; button.textContent = "保存中…";
  ws.send(JSON.stringify({
    type:"session_update", session_id:targetSessionId, system_prompt:prompt,
    request_id:pendingSessionSettingsWriteRequestId,
  }));
};
document.getElementById("sessionPromptResetBtn").onclick = () => { document.getElementById("sessionSystemPrompt").value = ""; };

function memoryCategoryLabel(category) {
  return ({general:"一般", constraint:"约束", preference:"偏好", fact:"事实", reference:"会话参考"})[category] || category || "一般";
}
function renderMemories(memories) {
  const list = document.getElementById("memoryList");
  if (!list) return;
  list.innerHTML = memories.length ? memories.map(memory => {
    const sources = Array.isArray(memory.source_ids) && memory.source_ids.length
      ? " · 来源 " + memory.source_ids.length + " 项" : "";
    const isReference = memory.category === "reference";
    const sourceLabel = Array.isArray(memory.source_ids) && memory.source_ids.length
      ? "来源会话 " + memory.source_ids.join(", ") : "会话参考";
    const content = isReference
      ? '<details class="memory-reference"><summary>查看脱敏参考 · ' + escapeHtml(sourceLabel) + '</summary><pre>' + escapeHtml(memory.content || "") + '</pre></details>'
      : '<span>' + escapeHtml(memory.content || "") + escapeHtml(sources) + '</span>';
    return '<div class="repo-model-row"><div class="repo-model-main"><b>'
      + escapeHtml(memoryCategoryLabel(memory.category))
      + ' <span class="ext-kind">仅供参考</span></b><span>'
      + (isReference ? escapeHtml(sources) : "")
      + '</span>' + content + '</div><button class="icon-danger" type="button" data-memory-archive="'
      + escapeHtml(memory.memory_id || "") + '" title="归档这条记忆">×</button></div>';
  }).join("") : '<div class="repo-empty">当前会话还没有记忆。</div>';
  list.querySelectorAll("[data-memory-archive]").forEach(button => button.onclick = () => {
    showConfirm("归档会话记忆", "这条记忆将不再注入后续 Agent 上下文。", "×", () => {
      ws?.send(JSON.stringify({type:"memory_archive",memory_id:button.dataset.memoryArchive}));
    }, button);
  });
}
function requestCurrentMemories() {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:"memory_get"}));
}
function requestAgentMemoryConfig() {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:"agent_config_get"}));
}
function applyAgentMemoryConfig(config) {
  const auto = document.getElementById("memAutoMemorize");
  const retrieval = document.getElementById("memRetrieval");
  const limit = document.getElementById("memRetrievalLimit");
  if (!auto || !retrieval || !limit) return;
  auto.checked = Boolean(config.auto_memorize);
  retrieval.checked = Boolean(config.retrieval_enabled);
  limit.value = String(Number(config.max_retrieval_results) || 8);
  document.getElementById("memoryConfigStatus").style.display = "none";
}
function onAgentMemoryConfigSaved(msg) {
  const status = document.getElementById("memoryConfigStatus");
  if (!status) return;
  if (msg?.memory) applyAgentMemoryConfig(msg.memory);
  status.textContent = "记忆配置已保存";
  status.style.display = "block";
  status.style.color = "var(--green)";
}
document.getElementById("memoryConfigSaveBtn").onclick = () => {
  const auto = document.getElementById("memAutoMemorize");
  const retrieval = document.getElementById("memRetrieval");
  const limit = document.getElementById("memRetrievalLimit");
  if (!auto || !retrieval || !limit) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) { showModalStatus("Desktop 连接已断开", "err"); return; }
  const payload = {
    type: "agent_config_set",
    auto_memorize: auto.checked,
    retrieval_enabled: retrieval.checked,
    max_retrieval_results: Math.min(50, Math.max(1, Number(limit.value) || 8)),
  };
  const status = document.getElementById("memoryConfigStatus");
  if (status) { status.textContent = "保存中…"; status.style.display = "block"; status.style.color = "var(--text-tertiary)"; }
  ws.send(JSON.stringify(payload));
};
function requestSessionReference(sourceSessionId) {
  const sourceId = String(sourceSessionId || "").trim();
  if (!sourceId) { showModalStatus("请粘贴要引用的会话 ID", "err"); return; }
  if (!ws || ws.readyState !== WebSocket.OPEN) { showModalStatus("Desktop 连接已断开", "err"); return; }
  ws.send(JSON.stringify({type:"session_reference_add",source_session_id:sourceId}));
}
document.getElementById("sessionReferenceAddBtn").onclick = () => {
  requestSessionReference(document.getElementById("sessionReferenceId").value);
};
document.getElementById("memoryAddBtn").onclick = () => {
  const fact = document.getElementById("memoryFact").value.trim();
  const category = document.getElementById("memoryCategory").value;
  if (!fact) { showModalStatus("请填写记忆内容", "err"); return; }
  if (!ws || ws.readyState !== WebSocket.OPEN) { showModalStatus("Desktop 连接已断开", "err"); return; }
  ws.send(JSON.stringify({type:"memory_add",fact,category}));
  document.getElementById("memoryFact").value = "";
};
document.getElementById("memoryClearBtn").onclick = event => {
  showConfirm("清空会话记忆", "当前会话的所有记忆都将停止注入 Agent 上下文。", "×", () => {
    ws?.send(JSON.stringify({type:"memory_clear"}));
  }, event.currentTarget);
};

function renderRepository() {
  const {models, selection} = modelRepository;
  const summary = document.getElementById("repoSummary");
  summary.textContent = models.length ? `${models.length} 个模型 · ${new Set(models.map(model => model.provider)).size} 个提供商 · 默认：${modelLabel(selection.default_model_id)}` : "添加第一个模型后即可开始对话";
  const groups = {}; models.forEach(model => (groups[model.provider] ||= []).push(model));
  const list = document.getElementById("repoModelList");
  list.innerHTML = models.length ? Object.entries(groups).map(([provider, items]) => '<section class="provider-group"><div class="provider-group-title">' + escapeHtml(providerLabel(provider)) + '</div>' + items.map(model => '<div class="repo-model-row"><div class="repo-model-main"><b>' + escapeHtml(model.name || model.model) + '</b><span>' + escapeHtml(model.model) + ' · ' + escapeHtml(modelCapabilitySummary(model)) + '</span></div><span class="credential-state ' + (model.has_credential ? 'configured' : 'missing') + '">' + (model.has_credential ? '已配置 ' + escapeHtml(model.credential_hint || '') : '未配置凭据') + '</span><button class="plain-small" data-discover="' + escapeHtml(model.id) + '">发现模型</button>' + (model.id === selection.default_model_id ? '<span class="default-tag">默认</span>' : '<button class="plain-small" data-default="' + escapeHtml(model.id) + '">设为默认</button>') + '<button class="icon-plain" data-edit="' + escapeHtml(model.id) + '">⌘</button><button class="icon-danger" data-delete="' + escapeHtml(model.id) + '">×</button></div>').join('') + '</section>').join('') : '<div class="repo-empty">还没有模型。添加一个模型即可直接开始对话。</div>';
  list.querySelectorAll("[data-default]").forEach(button => button.onclick = () => ws?.send(JSON.stringify({type:"model_select_default",model_id:button.dataset.default})));
  list.querySelectorAll("[data-edit]").forEach(button => button.onclick = () => openModelEditor(button.dataset.edit));
  list.querySelectorAll("[data-discover]").forEach(button => {
    const sourceId = button.dataset.discover;
    if (pendingModelDiscoveryRequestId && sourceId === pendingModelDiscoverySourceId) {
      button.disabled = true; button.textContent = "发现中…";
    }
    button.onclick = () => startModelDiscovery(sourceId);
  });
  list.querySelectorAll("[data-delete]").forEach(button => button.onclick = () => { const model = modelById(button.dataset.delete); showConfirm("删除模型", `将删除「${model?.name || "此模型"}」并移除相关模式引用。`, "×", () => ws?.send(JSON.stringify({type:"model_delete", id:button.dataset.delete})), button); });
  populateModeSelectors(selection.moa_model_ids || [], selection.peri_model_ids || [], selection);
}
function populateModelSelect(selectId, chosenIds) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">— 选择 —</option>' + modelRepository.models.map(m =>
    '<option value="' + escapeHtml(m.id) + '" ' + (chosenIds.includes(m.id) ? 'selected' : '') + '>'
    + escapeHtml(m.name || m.model) + ' (' + escapeHtml(providerLabel(m.provider)) + ' · ' + escapeHtml(m.model) + ')</option>'
  ).join('');
  if (chosenIds.length > 0 && !current) sel.value = chosenIds[0] || "";
}
function reasoningOptions(selectId, model, selected) {
  const select=document.getElementById(selectId); if(!select)return;
  const values=model?.reasoning_efforts||[];
  select.innerHTML='<option value="">默认 / 无</option>'+values.map(value=>'<option value="'+escapeHtml(value)+'">'+escapeHtml(value)+'</option>').join('');
  select.value=selected||"";
}
function hydrateRole(prefix, role, defaultTemp) {
  const model=modelById(role?.model_id);
  const temp=document.getElementById(prefix+"Temp"), context=document.getElementById(prefix+"Context");
  if(temp)temp.value=role?.temperature ?? defaultTemp;
  if(context){context.value=role?.context_tokens ?? model?.context_window ?? 128000;context.max=model?.context_window ?? 10000000;}
  reasoningOptions(prefix+"Reasoning",model,role?.reasoning_effort);
}
function populateModeSelectors(moaIds, periIds, selection) {
  populateModelSelect("moaHostModel", moaIds.slice(0,1));
  populateModelSelect("moaRef1", moaIds.slice(1,2));
  populateModelSelect("moaRef2", moaIds.slice(2,3));
  populateModelSelect("periHostModel", periIds.slice(0,1));
  populateModelSelect("periSub1", periIds.slice(1,2));
  populateModelSelect("periSub2", periIds.slice(2,3));
  const moa=selection?.moa_roles||{}, peri=selection?.peri_roles||{};
  hydrateRole("moaHost",moa.host,0.4);hydrateRole("moaRef1",moa.reference_1,0.7);hydrateRole("moaRef2",moa.reference_2,0.7);
  hydrateRole("periHost",peri.host,0.4);hydrateRole("periSub1",peri.worker_1,0.7);hydrateRole("periSub2",peri.worker_2,0.7);
  [["moaHostModel","moaHost",0.4],["moaRef1","moaRef1",0.7],["moaRef2","moaRef2",0.7],["periHostModel","periHost",0.4],["periSub1","periSub1",0.7],["periSub2","periSub2",0.7]].forEach(([selectId,prefix,temp])=>{const select=document.getElementById(selectId);select.onchange=()=>hydrateRole(prefix,{model_id:select.value},temp);});
}
let discoveredDraft = null;
function openModelEditor(id=null, draft=null) {
  repoEditorId = id; const model = id ? modelById(id) : null;
  discoveredDraft = draft;
  resetModelTestState();
  document.getElementById("repoEditor").hidden = false;
  document.getElementById("repoName").value = model?.name || draft?.name || "";
  document.getElementById("repoProvider").value = model?.provider || draft?.provider || "deepseek";
  document.getElementById("repoModel").value = model?.model || draft?.id || "";
  document.getElementById("repoEndpoint").value = model?.base_url || draft?.base_url || "";
  document.getElementById("repoContextWindow").value = model?.context_window || draft?.capabilities?.context_window || 128000;
  document.getElementById("repoMaxOutput").value = model?.max_output_tokens || draft?.capabilities?.max_output_tokens || 8192;
  document.getElementById("repoReasoningEfforts").value = (model?.reasoning_efforts || draft?.capabilities?.reasoning_efforts || []).join(",");
  document.getElementById("repoDefaultReasoning").value = model?.default_reasoning_effort || "";
  document.getElementById("repoSupportsTools").checked = model?.supports_tools ?? true;
  document.getElementById("repoSupportsImages").checked = model?.supports_images ?? false;
  document.getElementById("repoKey").value = "";
  document.getElementById("repoKey").placeholder = draft ? "将安全复用来源记录的服务端凭据" : model?.has_credential ? "已配置；留空则保留，填写则替换" : "输入 API Key";
  document.getElementById("repoKey").disabled = Boolean(draft);
  document.getElementById("repoProvider").disabled = Boolean(draft);
  document.getElementById("repoModel").disabled = Boolean(draft);
  document.getElementById("repoEndpoint").disabled = Boolean(draft);
}
document.getElementById("repoAddBtn").onclick = () => openModelEditor();
document.getElementById("repoCancelBtn").onclick = () => { resetModelTestState(); document.getElementById("repoEditor").hidden = true; repoEditorId = null; discoveredDraft = null; ["repoKey","repoProvider","repoModel","repoEndpoint"].forEach(id=>document.getElementById(id).disabled=false); };
document.getElementById("repoTestBtn").onclick = () => {
  const status = document.getElementById("repoTestStatus");
  const button = document.getElementById("repoTestBtn");
  const payload = {
    type: "model_test_connection",
    id: repoEditorId || "",
    provider: document.getElementById("repoProvider").value,
    model: document.getElementById("repoModel").value.trim(),
    base_url: document.getElementById("repoEndpoint").value.trim() || null,
    api_key: document.getElementById("repoKey").value,
    context_window: +document.getElementById("repoContextWindow").value,
    max_output_tokens: +document.getElementById("repoMaxOutput").value,
    supports_tools: document.getElementById("repoSupportsTools").checked,
    supports_images: document.getElementById("repoSupportsImages").checked,
    reasoning_effort: document.getElementById("repoDefaultReasoning").value.trim() || null,
  };
  if (!payload.model) { showModalStatus("请先填写模型 ID", "err"); return; }
  if (!ws || ws.readyState !== WebSocket.OPEN) { showModalStatus("Desktop 连接已断开", "err"); return; }
  pendingModelTestRequestId = nextTransientRequestId("model-test");
  payload.request_id = pendingModelTestRequestId;
  if (status) { status.style.display = "block"; status.style.color = "var(--text-tertiary)"; status.textContent = "正在测试连接…"; }
  button.disabled = true; button.textContent = "测试中…";
  ws.send(JSON.stringify(payload));
};
document.getElementById("repoSaveBtn").onclick = () => {
  const data = {name:document.getElementById("repoName").value.trim(), provider:document.getElementById("repoProvider").value, model:document.getElementById("repoModel").value.trim(), base_url:document.getElementById("repoEndpoint").value.trim() || null, context_window:+document.getElementById("repoContextWindow").value, max_output_tokens:+document.getElementById("repoMaxOutput").value, supports_tools:document.getElementById("repoSupportsTools").checked, supports_images:document.getElementById("repoSupportsImages").checked, reasoning_efforts:document.getElementById("repoReasoningEfforts").value.split(",").map(v=>v.trim()).filter(Boolean), default_reasoning_effort:document.getElementById("repoDefaultReasoning").value.trim() || null};
  const key = document.getElementById("repoKey").value; if (key) data.api_key = key;
  if (!data.name || !data.model) { showModalStatus("请填写名称和模型 ID", "err"); return; }
  ws?.send(JSON.stringify(discoveredDraft ? {type:"model_create_discovered",source_model_id:discoveredDraft.source_model_id,discovered_model_id:discoveredDraft.id,...data} : repoEditorId ? {type:"model_update", id:repoEditorId, ...data} : {type:"model_create", ...data}));
  resetModelTestState(); document.getElementById("repoEditor").hidden = true; repoEditorId = null; discoveredDraft = null; ["repoKey","repoProvider","repoModel","repoEndpoint"].forEach(id=>document.getElementById(id).disabled=false);
};
function resetModelDiscoveryState(_reason="reset") {
  pendingModelDiscoveryRequestId = null;
  pendingModelDiscoverySourceId = null;
  document.querySelectorAll("[data-discover]").forEach(button => {
    button.disabled = false; button.textContent = "发现模型";
  });
}
function setModelDiscoveryLoading(sourceId) {
  document.querySelectorAll("[data-discover]").forEach(button => {
    const pending = button.dataset.discover === sourceId;
    button.disabled = pending;
    button.textContent = pending ? "发现中…" : "发现模型";
  });
}
function startModelDiscovery(sourceId) {
  if (!sourceId) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    showModalStatus("Desktop 连接已断开", "err"); return;
  }
  resetModelDiscoveryState("superseded");
  document.getElementById("repoDiscovery").hidden = true;
  discoveryResult = null;
  pendingModelDiscoveryRequestId = nextTransientRequestId("model-discovery");
  pendingModelDiscoverySourceId = sourceId;
  setModelDiscoveryLoading(sourceId);
  showModalStatus("正在从厂商读取模型…", "ok");
  ws.send(JSON.stringify({
    type:"model_discover", model_id:sourceId,
    request_id:pendingModelDiscoveryRequestId,
  }));
}
registerTransientRequestReset("model-discovery", resetModelDiscoveryState);

function resetCredentialMigrationState(reason="reset") {
  const interruptedPhase = pendingCredentialMigrationPhase;
  const hadReport = Boolean(_credMigrationReport);
  pendingCredentialMigrationRequestId = null;
  pendingCredentialMigrationPhase = null;
  const report = document.getElementById("credMigrationReport");
  if (["error", "socket_close", "connect_failed", "server_epoch"].includes(reason)) {
    _credMigrationReport = null;
    report?.querySelectorAll("[data-credential-migration-run]").forEach(button => button.remove());
  } else {
    report?.querySelectorAll("[data-credential-migration-run]").forEach(button => {
      button.disabled = false; button.textContent = "确认迁移到钥匙串";
    });
  }
  const btn = document.getElementById("credMigrationBtn");
  if (btn) { btn.disabled = false; btn.textContent = _credMigrationReport ? "重新生成报告…" : "生成迁移报告…"; }
  if (["socket_close", "connect_failed", "server_epoch"].includes(reason)) {
    if (report && (interruptedPhase || hadReport)) {
      report.style.display = "block";
      report.textContent = interruptedPhase === "run"
        ? "连接已中断。本次迁移状态未知，请重新生成报告后确认当前状态。"
        : "连接已中断，原迁移报告已经失效，请重新生成后再操作。";
    }
  }
}
document.getElementById("credMigrationBtn").onclick = () => {
  const btn = document.getElementById("credMigrationBtn");
  if (!ws || ws.readyState !== WebSocket.OPEN) { showModalStatus("Desktop 连接已断开", "err"); return; }
  pendingCredentialMigrationRequestId = nextTransientRequestId("credential-report");
  pendingCredentialMigrationPhase = "report";
  _credMigrationReport = null;
  const report = document.getElementById("credMigrationReport");
  report.style.display = "block";
  report.textContent = "正在生成迁移报告…";
  btn.disabled = true; btn.textContent = "生成中…";
  ws.send(JSON.stringify({type:"credential_migration_report",request_id:pendingCredentialMigrationRequestId}));
};
let _credMigrationReport = null;
function renderCredMigrationReport(report, requestId) {
  if (!requestId || requestId !== pendingCredentialMigrationRequestId || pendingCredentialMigrationPhase !== "report") return;
  pendingCredentialMigrationRequestId = null;
  pendingCredentialMigrationPhase = null;
  const el = document.getElementById("credMigrationReport");
  const btn = document.getElementById("credMigrationBtn");
  btn.disabled = false; btn.textContent = "重新生成报告…";
  _credMigrationReport = report;
  if (!report) return;
  const creds = (report.records || []).filter(r => r.has_credential);
  const already = (report.records || []).filter(r => r.storage === "keychain");
  const lines = [
    "目标：macOS 系统钥匙串",
    "模型总数：" + report.total_models + " · 将迁移 " + creds.length + " 条明文 Key · 已在钥匙串 " + already.length + " 条",
    "",
  ];
  if (creds.length) {
    lines.push("受影响模型：");
    creds.forEach(r => lines.push("  • " + r.provider + "/" + r.model + "（" + r.name + "）凭据 " + (r.credential_hint || "存在")));
    lines.push("");
    lines.push("迁移会把 Key 写入系统钥匙串并从 models.json 移除明文；执行前会创建时间戳备份。");
  } else if (report.total_models === 0) {
    lines.push("仓库为空，无需迁移。");
  } else {
    lines.push("没有可迁移的明文凭据。");
  }
  el.style.display = "block";
  el.textContent = lines.join("\n");
  if (creds.length) {
    const migrateBtn = document.createElement("button");
    migrateBtn.type = "button";
    migrateBtn.className = "primary-small";
    migrateBtn.dataset.credentialMigrationRun = "true";
    migrateBtn.textContent = "确认迁移到钥匙串";
    migrateBtn.onclick = () => {
      showConfirm("确认迁移凭据？", "将把 " + creds.length + " 个模型的 API Key 写入系统钥匙串，并从 models.json 移除明文。此操作会修改系统凭据存储。", "⚠", () => {
        if (!ws || ws.readyState !== WebSocket.OPEN) { showModalStatus("Desktop 连接已断开", "err"); return; }
        pendingCredentialMigrationRequestId = nextTransientRequestId("credential-run");
        pendingCredentialMigrationPhase = "run";
        migrateBtn.disabled = true; migrateBtn.textContent = "迁移中…";
        ws.send(JSON.stringify({type:"credential_migration_run",request_id:pendingCredentialMigrationRequestId}));
      }, migrateBtn);
    };
    el.appendChild(document.createElement("br"));
    el.appendChild(migrateBtn);
  }
}
function renderCredentialMigrationDone(result) {
  if (pendingCredentialMigrationPhase !== "run") return;
  pendingCredentialMigrationRequestId = null;
  pendingCredentialMigrationPhase = null;
  const btn = document.getElementById("credMigrationBtn");
  btn.disabled = false; btn.textContent = "生成迁移报告…";
  const report = document.getElementById("credMigrationReport");
  report.style.display = "block";
  report.textContent = "已迁移 " + (result?.moved || 0) + " 条凭据到系统钥匙串。models.json 中不再保留明文。";
  _credMigrationReport = null;
}
registerTransientRequestReset("credential-migration", resetCredentialMigrationState);

let discoveryResult = null;
function renderDiscovery(result) { resetModelDiscoveryState("settled"); discoveryResult=result;const panel=document.getElementById("repoDiscovery"),select=document.getElementById("repoDiscoveredModel");panel.hidden=false;document.getElementById("repoDiscoveryStatus").textContent=`${providerLabel(result.provider)} 返回 ${result.models.length} 个模型。${result.warning||""}`;select.innerHTML=result.models.map(model=>'<option value="'+escapeHtml(model.id)+'">'+escapeHtml(model.name||model.id)+' · '+escapeHtml(model.id)+'</option>').join('');document.getElementById("repoUseDiscoveredBtn").disabled=!result.models.length;}
document.getElementById("repoDiscoveryCloseBtn").onclick=()=>{document.getElementById("repoDiscovery").hidden=true;discoveryResult=null;};
document.getElementById("repoUseDiscoveredBtn").onclick=()=>{const id=document.getElementById("repoDiscoveredModel").value,model=discoveryResult?.models?.find(item=>item.id===id);if(!model)return;document.getElementById("repoDiscovery").hidden=true;openModelEditor(null,{...model,source_model_id:discoveryResult.source_model_id,base_url:modelById(discoveryResult.source_model_id)?.base_url||""});};
function rolePayload(modelSelect,prefix) { const model_id=document.getElementById(modelSelect).value;if(!model_id)return null;return {model_id,temperature:+document.getElementById(prefix+"Temp").value,context_tokens:+document.getElementById(prefix+"Context").value,reasoning_effort:document.getElementById(prefix+"Reasoning").value||null}; }
function saveModeModelConfiguration(mode, roles) {
  Object.keys(roles).forEach(key => { if (!roles[key]) delete roles[key]; });
  if (!beginControlMutation()) return;
  ws.send(JSON.stringify({type:"mode_models_set",mode,roles}));
}
document.getElementById("moaSaveBtn").onclick = () => {
  saveModeModelConfiguration("moa", {host:rolePayload("moaHostModel","moaHost"),reference_1:rolePayload("moaRef1","moaRef1"),reference_2:rolePayload("moaRef2","moaRef2")});
};
document.getElementById("periSaveBtn").onclick = () => {
  saveModeModelConfiguration("peri", {host:rolePayload("periHostModel","periHost"),worker_1:rolePayload("periSub1","periSub1"),worker_2:rolePayload("periSub2","periSub2")});
};
function resetPeriReadinessState(reason="reset") {
  pendingPeriReadinessRequestId = null;
  const button = document.getElementById("periReadinessBtn");
  if (button) { button.disabled = false; button.textContent = "检查 Git 隔离就绪度"; }
  if (["socket_close", "connect_failed", "server_epoch"].includes(reason)) {
    const el = document.getElementById("periReadiness");
    if (el?.textContent === "正在只读检查仓库状态…") el.textContent = "检查已中断，可在连接恢复后重试。";
  }
}
document.getElementById("periReadinessBtn").onclick=()=>{
  if (!ws || ws.readyState !== WebSocket.OPEN) { showModalStatus("Desktop 连接已断开", "err"); return; }
  pendingPeriReadinessRequestId=nextTransientRequestId("peri-readiness");
  const button=document.getElementById("periReadinessBtn");button.disabled=true;button.textContent="检查中…";
  document.getElementById("periReadiness").textContent="正在只读检查仓库状态…";
  ws.send(JSON.stringify({type:"peri_git_readiness",worker_count:2,request_id:pendingPeriReadinessRequestId}));
};
function renderGitReadiness(data){resetPeriReadinessState("settled");const el=document.getElementById("periReadiness"),repo=data?.repository||{},blockers=data?.blockers||[],workers=data?.workers||[];const status=data?.ready?"✓ 可以进入 worktree 创建审批":"⚠ 尚未满足可写 Worker 条件";const blockerText=blockers.length?"\n阻塞项：\n"+blockers.map(item=>"• "+item.message).join("\n"):"";const workerText=workers.length?"\n计划：\n"+workers.map(item=>"• Worker "+item.ordinal+" → "+item.branch).join("\n"):"";el.textContent=status+"\n仓库："+(repo.name||"未知")+" · "+(repo.branch||"无分支")+" · "+(repo.head?repo.head.slice(0,12):"无 HEAD")+blockerText+workerText+"\n策略：不 push、不自动合并、不强制清理；创建与合并分别审批。";el.style.whiteSpace="pre-wrap";}
registerTransientRequestReset("peri-readiness", resetPeriReadinessState);
function showModalStatus(text, type) { const element = document.getElementById("modalStatus"); element.style.display = "block"; element.style.color = type === "err" ? "var(--red)" : "var(--green)"; element.textContent = text; setTimeout(() => element.style.display = "none", 2600); }
function renderSkills(skills) {
  modusSkills = skills;
  const list = document.getElementById("skillList"), bar = document.getElementById("skillBar");
  const renderButtons = target => { target.replaceChildren(...skills.map(skill => { const button=document.createElement("button"); button.textContent=skill.name; button.title=skill.description; button.onclick=()=>{attachSkill(skill);}; return button; })); };
  renderButtons(bar);
  list.innerHTML = skills.length ? skills.map(skill => '<div class="repo-model-row"><div class="repo-model-main"><b>'+escapeHtml(skill.name)+'</b><span>'+escapeHtml(skill.description || "无说明")+'</span></div><button class="icon-danger" data-skill-delete="'+escapeHtml(skill.name)+'">×</button></div>').join('') : '<div class="repo-empty">还没有 Skill。创建后可从输入框上方直接使用。</div>';
  list.querySelectorAll("[data-skill-delete]").forEach(button => button.onclick=()=>showConfirm("删除 Skill", `将删除「${button.dataset.skillDelete}」。`, "×", ()=>sendCapabilityMutation({type:"skill_delete",name:button.dataset.skillDelete}), button));
}

// ─── Skill attachment (@ affordance) ───
let modusSkills = [];
let pendingSkillId = null;
function attachSkill(skill) {
  pendingSkillId = skill.name;
  document.getElementById("skillChipLabel").textContent = "⚡ " + skill.name;
  document.getElementById("skillChip").hidden = false;
  closeSkillAtMenu();
  input.focus();
}
document.getElementById("skillChipRemove").onclick = () => { pendingSkillId = null; document.getElementById("skillChip").hidden = true; input.focus(); };
function openSkillAtMenu() {
  const menu = document.getElementById("skillAtMenu");
  if (!modusSkills.length) {
    menu.innerHTML = '<div class="skill-at-empty">还没有 Skill。在设置 → Skills 中创建。</div>';
  } else {
    menu.innerHTML = modusSkills.map(skill => '<button class="skill-at-option" type="button" data-skill-at="' + escapeHtml(skill.name) + '"><span>' + escapeHtml(skill.name) + '<small>' + escapeHtml(skill.description || "") + '</small></span><b>+</b></button>').join('');
    menu.querySelectorAll("[data-skill-at]").forEach(option => option.onclick = () => attachSkill({name: option.dataset.skillAt, description: ""}));
  }
  menu.hidden = false;
}
function closeSkillAtMenu() { document.getElementById("skillAtMenu").hidden = true; }
const inputEl = document.getElementById("input");
if (inputEl) {
  inputEl.addEventListener("input", () => {
    const caret = inputEl.selectionStart ?? inputEl.value.length;
    const before = inputEl.value.slice(0, caret);
    const match = before.match(/@([a-z0-9_-]{0,24})$/i);
    if (match) { openSkillAtMenu(); } else { closeSkillAtMenu(); }
  });
}
document.addEventListener("click", event => { if (!event.target.closest("#input") && !event.target.closest(".skill-at-menu")) closeSkillAtMenu(); });
inputEl.addEventListener("keydown", event => {
  const menu = document.getElementById("skillAtMenu");
  if (!menu.hidden && event.key === "Escape") { closeSkillAtMenu(); event.preventDefault(); }
});
document.getElementById("skillAddBtn").onclick=()=>{document.getElementById("skillEditor").hidden=false;document.getElementById("skillName").value="";document.getElementById("skillDescription").value="";document.getElementById("skillPrompt").value="";};
document.getElementById("skillCancelBtn").onclick=()=>document.getElementById("skillEditor").hidden=true;
document.getElementById("skillSaveBtn").onclick=()=>{const payload={name:document.getElementById("skillName").value.trim(),description:document.getElementById("skillDescription").value.trim(),prompt:document.getElementById("skillPrompt").value.trim()};if(!payload.name||!payload.prompt){showModalStatus("请填写名称和提示词","err");return;}if(sendCapabilityMutation({type:"skill_create",...payload}))document.getElementById("skillEditor").hidden=true;};
// Skills import: file
document.getElementById("skillFileInput").onchange = function() {
  const file = this.files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    document.getElementById("skillEditor").hidden = false;
    document.getElementById("skillName").value = file.name.replace(/\.[^.]+$/, "").toLowerCase().replace(/[^a-z0-9_-]/g, "-").substring(0, 40) || "imported";
    document.getElementById("skillDescription").value = "从 " + file.name + " 导入";
    document.getElementById("skillPrompt").value = e.target.result;
  };
  reader.readAsText(file);
};
// Skills import: URL fetch
function resetSkillFetchState(_reason="reset") {
  pendingSkillFetchRequestId = null;
  const button = document.getElementById("skillFetchBtn");
  if (button) { button.disabled = false; button.textContent = "获取"; }
}
function renderFetchedSkill(message) {
  resetSkillFetchState("settled");
  document.getElementById("skillEditor").hidden = false;
  document.getElementById("skillName").value = message.name || "imported";
  document.getElementById("skillDescription").value = "从 " + (message.source || "URL") + " 导入";
  document.getElementById("skillPrompt").value = message.content;
}
document.getElementById("skillFetchBtn").onclick = () => {
  const url = document.getElementById("skillUrlInput").value.trim();
  if (!url) { showModalStatus("请输入网址", "err"); return; }
  if (!ws || ws.readyState !== WebSocket.OPEN) { showModalStatus("Desktop 连接已断开", "err"); return; }
  pendingSkillFetchRequestId = nextTransientRequestId("skill-fetch");
  document.getElementById("skillFetchBtn").disabled = true;
  document.getElementById("skillFetchBtn").textContent = "获取中…";
  ws.send(JSON.stringify({type:"skill_fetch_url",url,request_id:pendingSkillFetchRequestId}));
};
registerTransientRequestReset("skill-fetch", resetSkillFetchState);

function settleTransientRequestError(message) {
  const requestId = String(message?.request_id || "");
  switch (message?.operation) {
    case "sessions_list":
      if (!requestId || requestId !== pendingSessionCatalogRequest?.requestId) return true;
      resetSessionCatalogRequest("error");
      addSystemMsg("⚠ " + (message.message || "会话目录加载失败"));
      return true;
    case "model_discover":
      if (!requestId || requestId !== pendingModelDiscoveryRequestId) return true;
      resetModelDiscoveryState("error");
      showModalStatus(message.message || "模型发现失败", "err");
      return true;
    case "credential_migration_report":
    case "credential_migration_run":
      if (!requestId || requestId !== pendingCredentialMigrationRequestId) return true;
      resetCredentialMigrationState("error");
      document.getElementById("credMigrationReport").textContent = message.message || "凭据迁移失败，请重新生成报告。";
      showModalStatus(message.message || "凭据迁移失败", "err");
      return true;
    case "peri_git_readiness":
      if (!requestId || requestId !== pendingPeriReadinessRequestId) return true;
      resetPeriReadinessState("error");
      document.getElementById("periReadiness").textContent = message.message || "检查失败";
      return true;
    case "skill_fetch_url":
      if (!requestId || requestId !== pendingSkillFetchRequestId) return true;
      resetSkillFetchState("error");
      showModalStatus(message.message || "获取失败", "err");
      return true;
    case "session_get":
      if (!requestId || pendingSessionSettingsReadRequestId !== requestId
          || String(message.session_id || "") !== pendingSessionSettingsSessionId) return true;
      pendingSessionSettingsReadRequestId = null;
      pendingSessionSettingsReadBaseline = null;
      showModalStatus(message.message || "读取会话设置失败", "err");
      return true;
    case "session_update":
      if (!requestId || pendingSessionSettingsWriteRequestId !== requestId
          || String(message.session_id || "") !== pendingSessionSettingsSessionId) return true;
      resetSessionSettingsRequests("error");
      showModalStatus(message.message || "保存会话设置失败", "err");
      return true;
    case "session_set_model":
    case "session_set_reasoning":
    case "session_set_mode":
      // A stale composer error belongs to an earlier conversation/request and
      // must not release the current gate or appear in the current transcript.
      if (!settleSessionExecutionMutation(message, message.operation)) return true;
      // Let the generic error branch render the correlated failure and retain
      // its existing active-Run ownership semantics.
      return false;
    case "artifact_get":
      return settleArtifactError(message);
    default:
      return false;
  }
}
// Skills import: template
const TEMPLATES = {
  "review-code": {name:"review-code", description:"代码审查：检查逻辑缺陷、安全风险与代码风格", prompt:"请审查以下代码，检查：\n1. 逻辑缺陷与边界情况\n2. 安全风险（注入、越权）\n3. 代码风格与可维护性\n4. 性能建议\n\n```\n{{code}}\n```"},
  "arch-design": {name:"arch-design", description:"架构设计：系统模块划分与交互方案", prompt:"请设计以下需求的架构方案：\n\n## 需求\n{{requirement}}\n\n## 要求\n1. 模块划分与职责边界\n2. 数据流与接口设计\n3. 关键决策与取舍\n4. 扩展性考量"},
  "test-cover": {name:"test-cover", description:"测试覆盖：生成单元测试与集成测试方案", prompt:"请为以下代码生成测试覆盖：\n\n```\n{{code}}\n```\n\n包括：\n1. 核心逻辑的单元测试\n2. 边界情况\n3. 集成测试建议"},
  "doc-gen": {name:"doc-gen", description:"文档生成：从代码生成 API 文档与使用说明", prompt:"请为以下 API 生成使用文档：\n\n```\n{{code}}\n```\n\n包括：\n1. 功能说明\n2. 参数与返回值\n3. 使用示例"},
};
document.getElementById("skillTemplateBtn").onclick = () => {
  const key = document.getElementById("skillTemplateSelect").value;
  const tpl = TEMPLATES[key];
  if (!tpl) { showModalStatus("请选择一个模板", "err"); return; }
  document.getElementById("skillEditor").hidden = false;
  document.getElementById("skillName").value = tpl.name;
  document.getElementById("skillDescription").value = tpl.description;
  document.getElementById("skillPrompt").value = tpl.prompt;
};
function renderExtensions(exts) {
  const list = document.getElementById("extensionList");
  if (!exts || !exts.length) {
    list.innerHTML = '<div class="repo-empty">暂无已注册的扩展</div>'; return;
  }
  const kindLabel = {builtin:'内置', skill:'Skill', mcp:'MCP', plugin:'插件'};
  list.innerHTML = exts.map(ext => {
    const kind = kindLabel[ext.kind] || ext.kind;
    const statusColor = ['active','connected'].includes(ext.status) ? 'var(--green)' : ['not_connected','configured'].includes(ext.status) ? 'var(--amber)' : 'var(--text-tertiary)';
    const statusText = ({active:'已启用',connected:'已连接',configured:'已配置',not_connected:'未连接'})[ext.status] || ext.status;
    return '<div class="repo-model-row"><div class="repo-model-main"><b>' + escapeHtml(ext.name) + '</b><span>' + escapeHtml(ext.summary || '') + '</span></div><span class="ext-kind">' + escapeHtml(kind) + '</span><span style="color:' + statusColor + ';font-size:9px;white-space:nowrap;">● ' + statusText + '</span></div>';
  }).join('');
}
// Initial render with placeholder hint
renderExtensions();
