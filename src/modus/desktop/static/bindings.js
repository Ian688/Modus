// ─── MCP Management ───
function sendCapabilityMutation(payload) {
  if (!beginControlMutation()) {
    showModalStatus("请等待当前任务或配置操作完成", "err");
    return false;
  }
  ws.send(JSON.stringify(payload));
  return true;
}
function renderMcpServers(servers) {
  const list = document.getElementById("mcpServerList");
  if (!servers || !servers.length) {
    list.innerHTML = '<div class="repo-empty">还没有 MCP 服务器。添加一个后即可使用外部工具。</div>'; return;
  }
  const transportLabel = {stdio:'子进程', sse:'HTTP'};
  list.innerHTML = servers.map(s => {
    const isConnected = s.status === 'connected';
    const statusColor = isConnected ? 'var(--green)' : s.enabled ? 'var(--amber)' : 'var(--text-tertiary)';
    const statusText = isConnected ? '已连接' : s.enabled ? '未连接' : '已禁用';
    const transport = transportLabel[s.transport] || s.transport;
    return '<div class="repo-model-row"><div class="repo-model-main"><b>' + escapeHtml(s.name) + '</b><span>' + transport + ' · ' + escapeHtml(s.command || s.url || '') + '</span></div>'
      + '<span style="color:' + statusColor + ';font-size:9px;white-space:nowrap;">● ' + statusText + '</span>'
      + (isConnected
        ? '<button class="plain-small mcp-disconnect" data-name="' + escapeHtml(s.name) + '">断开</button>'
        : '<button class="plain-small mcp-connect" data-name="' + escapeHtml(s.name) + '">连接</button>')
      + '<button class="icon-danger mcp-remove" data-name="' + escapeHtml(s.name) + '">×</button></div>';
  }).join('');
  // Connect button
  list.querySelectorAll('.mcp-connect').forEach(btn => btn.onclick = () => sendCapabilityMutation({type:"mcp_server_connect", name:btn.dataset.name}));
  // Disconnect button
  list.querySelectorAll('.mcp-disconnect').forEach(btn => btn.onclick = () => sendCapabilityMutation({type:"mcp_server_disconnect", name:btn.dataset.name}));
  // Remove button
  list.querySelectorAll('.mcp-remove').forEach(btn => btn.onclick = () => {
    showConfirm("移除 MCP", "确定移除「" + btn.dataset.name + "」？", "×", () => sendCapabilityMutation({type:"mcp_server_remove", name:btn.dataset.name}), btn);
  });
}
// MCP editor
document.getElementById("mcpAddBtn").onclick = () => {
  document.getElementById("mcpEditor").hidden = false;
  document.getElementById("mcpName").value = "";
  document.getElementById("mcpCommand").value = "";
  document.getElementById("mcpArgs").value = "";
  document.getElementById("mcpEnv").value = "";
  document.getElementById("mcpUrl").value = "";
};
document.getElementById("mcpCancelBtn").onclick = () => document.getElementById("mcpEditor").hidden = true;
document.getElementById("mcpTransport").onchange = function() {
  const isStdio = this.value === "stdio";
  document.getElementById("mcpStdioFields").style.display = isStdio ? "" : "none";
  document.getElementById("mcpSseFields").style.display = isStdio ? "none" : "";
};
document.getElementById("mcpSaveBtn").onclick = () => {
  const name = document.getElementById("mcpName").value.trim();
  if (!name) { showModalStatus("请填写名称", "err"); return; }
  const transport = document.getElementById("mcpTransport").value;
  const config = {name, transport, enabled: true};
  if (transport === "stdio") {
    config.command = document.getElementById("mcpCommand").value.trim();
    config.args = document.getElementById("mcpArgs").value.trim().split(/\s+/).filter(Boolean);
    try {
      config.env = Object.fromEntries(document.getElementById("mcpEnv").value.split(/\r?\n/).map(line=>line.trim()).filter(Boolean).map(line=>{
        const split=line.indexOf("=");if(split<1)throw new Error("环境变量格式应为 TARGET=env:SOURCE");
        const target=line.slice(0,split).trim(),reference=line.slice(split+1).trim();
        if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(target)||!/^env:[A-Za-z_][A-Za-z0-9_]*$/.test(reference))throw new Error("环境变量格式应为 TARGET=env:SOURCE");
        return [target,reference];
      }));
    } catch (error) { showModalStatus(error.message, "err"); return; }
  } else {
    config.url = document.getElementById("mcpUrl").value.trim();
  }
  if (sendCapabilityMutation({type:"mcp_server_add", config})) {
    document.getElementById("mcpEditor").hidden = true;
  }
};
// Request MCP servers on settings open
const _origOpenSettings = openSettings;
openSettings = function(tab) {
  _origOpenSettings(tab);
  ws?.send(JSON.stringify({type:"mcp_servers_list"}));
};

composerSelect.onclick = () => { if (!composerSelect.disabled) { composerMenu.hidden = !composerMenu.hidden; composerSelect.setAttribute("aria-expanded", String(!composerMenu.hidden)); } };
document.addEventListener("click", event => { if (!event.target.closest(".composer-select-wrap")) { closeComposerMenu(); closeReasoningMenu(); } });

// ═══ 自定义确认弹窗 ═══
let _confirmCb = null;
function showConfirm(title, msg, icon, cb, btnEl) {
  document.getElementById("confirmTitle").textContent = title;
  document.getElementById("confirmMsg").textContent = msg;
  document.getElementById("confirmIcon").textContent = icon || "⚠";
  _confirmCb = cb;
  const modal = document.getElementById("confirmModal");
  modal.classList.add("on");
  // 定位到按钮旁边：优先下方，放不下则上方；始终不超出视口边界。
  if (btnEl) {
    const r = btnEl.getBoundingClientRect();
    const modalHeight = modal.querySelector(".modal")?.offsetHeight || 140;
    const pad = 8;
    const below = r.bottom + pad;
    const fitsBelow = below + modalHeight <= window.innerHeight - pad;
    let top;
    if (fitsBelow) {
      top = below;
    } else {
      // Flip above, clamped so the modal never leaves the viewport bottom.
      top = Math.max(pad, Math.min(r.top - modalHeight - pad, window.innerHeight - modalHeight - pad));
    }
    modal.style.position = "fixed";
    modal.style.top = top + "px";
    modal.style.left = Math.max(8, Math.min(r.left - 100, window.innerWidth - 330)) + "px";
    modal.style.transform = "none";
    modal.style.alignItems = "flex-start";
    modal.style.justifyContent = "flex-start";
  } else {
    modal.style.position = "fixed";
    modal.style.top = "0";
    modal.style.left = "0";
    modal.style.transform = "";
    modal.style.alignItems = "center";
    modal.style.justifyContent = "center";
  }
}
document.getElementById("confirmCancelBtn").onclick = () => {
  document.getElementById("confirmModal").classList.remove("on");
  _confirmCb = null;
};
document.getElementById("confirmOkBtn").onclick = () => {
  document.getElementById("confirmModal").classList.remove("on");
  if (_confirmCb) _confirmCb();
  _confirmCb = null;
};
// 点击遮罩关闭
document.getElementById("confirmModal").onclick = (e) => {
  if (e.target === document.getElementById("confirmModal")) {
    document.getElementById("confirmModal").classList.remove("on");
    _confirmCb = null;
  }
};

// ═══ Artifact Viewer UI shell ═══
// Transport code owns fetching and correlation. It can drive this shell with
// openArtifactViewer + one of the renderArtifactViewer* state functions.
const artifactViewerOverlay = document.getElementById("artifactViewerOverlay");
const artifactViewerDialog = document.getElementById("artifactViewerDialog");
const artifactViewerState = {
  open: false, status: "idle", artifactId: "", title: "", kind: "artifact",
  sizeBytes: null, content: "", error: "", previousFocus: null,
};

function getArtifactViewerState() {
  return {
    open: artifactViewerState.open,
    status: artifactViewerState.status,
    artifact_id: artifactViewerState.artifactId,
    title: artifactViewerState.title,
    kind: artifactViewerState.kind,
    size_bytes: artifactViewerState.sizeBytes,
    content: artifactViewerState.content,
    error: artifactViewerState.error,
  };
}

function formatArtifactViewerSize(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return Math.round(bytes) + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0) + " KB";
  return (bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0) + " MB";
}

function applyArtifactViewerMetadata(metadata={}) {
  if (Object.prototype.hasOwnProperty.call(metadata, "artifact_id") || Object.prototype.hasOwnProperty.call(metadata, "artifactId")) {
    artifactViewerState.artifactId = String(metadata.artifact_id || metadata.artifactId || "");
  }
  if (Object.prototype.hasOwnProperty.call(metadata, "title")) artifactViewerState.title = String(metadata.title || "");
  if (Object.prototype.hasOwnProperty.call(metadata, "kind")) artifactViewerState.kind = String(metadata.kind || "artifact");
  if (Object.prototype.hasOwnProperty.call(metadata, "size_bytes") || Object.prototype.hasOwnProperty.call(metadata, "sizeBytes")) {
    const size = Number(metadata.size_bytes ?? metadata.sizeBytes);
    artifactViewerState.sizeBytes = Number.isFinite(size) && size >= 0 ? size : null;
  }
  document.getElementById("artifactViewerTitle").textContent = artifactViewerState.title || "运行产物";
  document.getElementById("artifactViewerKind").textContent = artifactViewerState.kind || "artifact";
  document.getElementById("artifactViewerSize").textContent = formatArtifactViewerSize(artifactViewerState.sizeBytes);
  document.getElementById("artifactViewerId").textContent = artifactViewerState.artifactId || "—";
}

function setArtifactViewerVisualState(status, message="") {
  const loading = document.getElementById("artifactViewerLoading");
  const error = document.getElementById("artifactViewerError");
  const content = document.getElementById("artifactViewerContent");
  const copy = document.getElementById("artifactViewerCopyBtn");
  const download = document.getElementById("artifactViewerDownloadBtn");
  artifactViewerState.status = status;
  loading.hidden = status !== "loading";
  error.hidden = status !== "error";
  content.hidden = status !== "content";
  copy.disabled = status !== "content";
  download.disabled = status !== "content";
  artifactViewerDialog.setAttribute("aria-busy", String(status === "loading"));
  document.getElementById("artifactViewerStatus").textContent = message || {
    loading: "正在读取并校验产物内容",
    error: "产物读取失败",
    content: "产物已就绪",
  }[status] || "等待读取产物";
  document.getElementById("artifactViewerActionStatus").textContent = "";
}

function openArtifactViewer(metadata={}) {
  if (!artifactViewerState.open) {
    artifactViewerState.previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement : null;
  }
  artifactViewerState.open = true;
  artifactViewerState.content = "";
  artifactViewerState.error = "";
  applyArtifactViewerMetadata(metadata);
  document.getElementById("artifactViewerContent").textContent = "";
  artifactViewerOverlay.hidden = false;
  document.body.classList.add("artifact-viewer-open");
  setArtifactViewerVisualState("loading");
  requestAnimationFrame(() => artifactViewerDialog.focus());
  return getArtifactViewerState();
}

function renderArtifactViewerLoading(metadata={}) {
  if (!artifactViewerState.open) return openArtifactViewer(metadata);
  applyArtifactViewerMetadata(metadata);
  artifactViewerState.error = "";
  setArtifactViewerVisualState("loading");
  return getArtifactViewerState();
}

function renderArtifactViewerContent(artifact={}) {
  if (!artifactViewerState.open) openArtifactViewer(artifact);
  applyArtifactViewerMetadata(artifact);
  artifactViewerState.content = String(artifact.content ?? "");
  artifactViewerState.error = "";
  // Text content is deliberate: persisted artifacts never become executable HTML.
  document.getElementById("artifactViewerContent").textContent = artifactViewerState.content;
  setArtifactViewerVisualState("content");
  return getArtifactViewerState();
}

function renderArtifactViewerError(error, metadata={}) {
  if (!artifactViewerState.open) openArtifactViewer(metadata);
  applyArtifactViewerMetadata(metadata);
  artifactViewerState.content = "";
  artifactViewerState.error = String(error?.message || error || "无法读取该产物，请稍后重试。");
  document.getElementById("artifactViewerErrorMessage").textContent = artifactViewerState.error;
  setArtifactViewerVisualState("error", artifactViewerState.error);
  return getArtifactViewerState();
}

function closeArtifactViewer() {
  if (!artifactViewerState.open) return;
  const prior = artifactViewerState.previousFocus;
  artifactViewerState.open = false;
  artifactViewerState.status = "idle";
  artifactViewerState.previousFocus = null;
  artifactViewerOverlay.hidden = true;
  document.body.classList.remove("artifact-viewer-open");
  artifactViewerDialog.setAttribute("aria-busy", "false");
  artifactViewerOverlay.dispatchEvent(new CustomEvent("modus:artifact-viewer-close"));
  if (prior?.isConnected && typeof prior.focus === "function") prior.focus();
}

function closeArtifactViewerSilently() {
  artifactViewerState.open = false;
  artifactViewerState.status = "idle";
  artifactViewerState.previousFocus = null;
  artifactViewerOverlay.hidden = true;
  document.body.classList.remove("artifact-viewer-open");
  artifactViewerDialog.setAttribute("aria-busy", "false");
}

function artifactViewerDownloadName() {
  const raw = artifactViewerState.title || artifactViewerState.artifactId || "modus-artifact";
  const safe = raw.replace(/[<>:"/\\|?*\u0000-\u001f]/g, "-").trim().slice(0, 120) || "modus-artifact";
  return /\.[a-z0-9]{1,8}$/i.test(safe) ? safe : safe + ".md";
}

document.getElementById("artifactViewerCloseBtn").onclick = closeArtifactViewer;
document.getElementById("artifactViewerDoneBtn").onclick = closeArtifactViewer;
artifactViewerOverlay.onclick = event => {
  if (event.target === artifactViewerOverlay) closeArtifactViewer();
};
document.getElementById("artifactViewerRetryBtn").onclick = () => {
  artifactViewerOverlay.dispatchEvent(new CustomEvent("modus:artifact-viewer-retry", {
    bubbles: true,
    detail: {artifact_id: artifactViewerState.artifactId},
  }));
};
artifactViewerOverlay.addEventListener("modus:artifact-viewer-retry", event => {
  requestArtifactContent(event.detail?.artifact_id || artifactViewerState.artifactId);
});
artifactViewerOverlay.addEventListener("modus:artifact-viewer-close", () => {
  pendingArtifactRequests.clear();
});
document.getElementById("artifactViewerCopyBtn").onclick = async () => {
  if (artifactViewerState.status !== "content") return;
  const status = document.getElementById("artifactViewerActionStatus");
  try {
    await navigator.clipboard.writeText(artifactViewerState.content);
    status.textContent = "已复制到剪贴板";
  } catch (_error) {
    status.textContent = "复制失败，请在正文中手动选择";
  }
};
document.getElementById("artifactViewerDownloadBtn").onclick = () => {
  if (artifactViewerState.status !== "content") return;
  const url = URL.createObjectURL(new Blob([artifactViewerState.content], {type:"text/markdown;charset=utf-8"}));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = artifactViewerDownloadName();
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
  document.getElementById("artifactViewerActionStatus").textContent = "已准备下载";
};
artifactViewerDialog.addEventListener("keydown", event => {
  if (event.key === "Escape") {
    event.preventDefault(); event.stopPropagation(); closeArtifactViewer(); return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...artifactViewerDialog.querySelectorAll("button:not(:disabled), [tabindex='0']")]
    .filter(element => !element.hidden && element.getClientRects().length);
  if (!focusable.length) { event.preventDefault(); artifactViewerDialog.focus(); return; }
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});

// ═══ Event Bindings ═══
sendBtn.onclick=sendMessage;
input.onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();sendMessage();}};
document.getElementById("stopBtn").onclick=()=>{
  if(canCancelActiveAgentRun()&&ws&&ws.readyState===WebSocket.OPEN) {
    ws.send(JSON.stringify({type:"cancel"}));
  }
};
const sessionSearchInput = document.getElementById("sbSearch");
let sessionSearchTouched = false;
let sessionSearchDebounce = null;
const sessionSearchBox = sessionSearchInput.closest(".sb-search");
function syncSessionSearchExpansion() {
  sessionSearchBox?.classList.toggle("has-query", Boolean(sessionSearchInput.value.trim()));
}
sessionSearchInput.oninput = function() {
  sessionSearchTouched = true;
  const query = this.value;
  const clearButton = document.getElementById("sbSearchClear");
  if (clearButton) clearButton.hidden = !String(query || "").trim();
  syncSessionSearchExpansion();
  clearTimeout(sessionSearchDebounce);
  sessionSearchDebounce = setTimeout(() => {
    sessionCatalogQuery = String(query || "").trim();
    sessionCatalogSelectedIds.clear();
    refreshSessionCatalog();
  }, 250);
};
document.getElementById("sbSearchClear").onclick = () => {
  sessionSearchTouched = true;
  clearTimeout(sessionSearchDebounce);
  sessionSearchInput.value = "";
  sessionCatalogQuery = "";
  sessionCatalogSelectedIds.clear();
  document.getElementById("sbSearchClear").hidden = true;
  syncSessionSearchExpansion();
  refreshSessionCatalog();
  sessionSearchInput.focus();
};
// Search is a transient view filter. Some browsers restore an old input value
// on reload (for example, the previous "模型 1"), which is not application
// state and can be mistaken for a model label.
function discardRestoredSessionSearch() {
  if (sessionSearchTouched) return;
  sessionSearchInput.value = "";
  sessionCatalogQuery = "";
  document.getElementById("sbSearchClear").hidden = true;
  syncSessionSearchExpansion();
}
discardRestoredSessionSearch();
window.addEventListener("pageshow", () => {
  sessionSearchTouched = false;
  discardRestoredSessionSearch();
  // Chromium can restore form values after pageshow. Recheck only while the
  // user has not interacted with this field, so genuine input is preserved.
  [0, 200, 800].forEach(delay => setTimeout(discardRestoredSessionSearch, delay));
});
document.getElementById("newChatBtn").onclick=()=>{
  if (!sendSessionCreate(currentMode, "新对话")) return;
  waiting=true; input.disabled=true; sendBtn.disabled=true;
  document.getElementById("runControl").hidden=false; setActivity("⟳","创建会话...","busy");
};

// Skills are loaded from the WebSocket-backed local repository. No static
// skills.json fetch remains, so the displayed list cannot race a stale file.

// ═══ Keyboard ═══
document.addEventListener("keydown",e=>{
  const m=e.metaKey||e.ctrlKey;
  if(m&&e.key===","){e.preventDefault();if(settingsModal.classList.contains("on"))settingsModal.classList.remove("on");else openSettings();}
  if(e.key==="Escape"&&document.body.classList.contains("mobile-sidebar-open"))closeMobileSidebar();
  if(e.key==="Escape"&&document.body.classList.contains("workbench-open"))closeWorkbenchPanel();
  if(e.key==="?"&&!m)addSystemMsg("⌨ Enter=发送 | Shift+Enter=换行 | ⌘,=设置 | ⌘I=创建子 Agent");
});
