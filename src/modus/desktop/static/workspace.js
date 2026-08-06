// Workspace observes the same typed event contract as TimelineRenderer.  This
// also rebuilds the projection when transcript history is replayed.  The old
// file/service/tool tracker (#wsSection) was folded into the KANBAN board, so
// the observer stays a no-op while keeping the applyTranscriptEvent chain safe.
function observeWorkspaceEvent(event) {
  return event;
}
function clearWorkspaceState() {
  // The KANBAN board owns workspace projections; reset is a board re-render.
  if (window.ModusKanban && typeof window.ModusKanban.refreshBoard === "function") {
    window.ModusKanban.refreshBoard();
  }
}
function addSystemMsg(t) {
  const message = document.createElement("div");
  message.className = "msg system";
  const body = document.createElement("div");
  body.className = "block-text";
  body.style.cssText = "font-size:10px;color:var(--text-tertiary);padding:2px 12px;";
  body.textContent = String(t || "");
  message.appendChild(body);
  chatArea.appendChild(message);
  _autoScroll();
}
function addUserBubble(t) { const d=document.createElement("div");d.className="msg user";d.dataset.eventId="restored-"+Date.now();d.innerHTML='<div class="ava">Y</div><div class="block-text">'+escapeHtml(t)+'</div>';document.getElementById("chatArea").appendChild(d);_autoScroll(); }
function addAssistantBubble(t,s) { const d=document.createElement("div");d.className="msg assistant";d.dataset.eventId="restored-"+Date.now();d.innerHTML='<div class="ava">M</div><div class="block-text">'+(s?renderTimelineMarkdown(t,true):escapeHtml(t))+'</div>';document.getElementById("chatArea").appendChild(d);_autoScroll(); }

function setActivity(i,t,s) { document.getElementById("actIcon").textContent=i; document.getElementById("actText").textContent=t; }
function setFocusState(text, state="idle") {
  wvBody.textContent = String(text || "等待任务...");
  wvBody.dataset.state = state;
}
// Track whether current session has user/assistant messages (not DOM-based)
let _sessionHasMsgs = false;

