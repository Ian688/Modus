// ─── Multi-window renderers: document / browser / activity cards ───
// These panes complement the task-rail windows in workbenchwindows.js.  Each
// is event-driven: the document window renders plan/design/spec artifacts,
// the browser window opens dev-server URLs, and the activity window shows
// parallel reference / worker cards.
(function (global) {
  "use strict";

  const DOC_KINDS = ["plan", "design", "spec"];
  const LOCALHOST_RE = /https?:\/\/localhost:\d+/i;

  function html(value) {
    const node = document.createElement("span");
    node.textContent = value === null || value === undefined ? "" : String(value);
    return node.innerHTML;
  }

  function markdownHtml(text) {
    if (typeof window.renderMd === "function") {
      try { return renderMd(String(text || "")); } catch (e) { /* fall through */ }
    }
    return "<pre>" + html(text) + "</pre>";
  }

  // ─── Document window (renders into the KANBAN drawer) ───
  const documentView = document.getElementById("kbDocument");
  let lastDocumentArtifactId = "";
  function renderDocument(artifact) {
    if (!artifact) return;
    const kind = String(artifact.kind || "artifact");
    const title = String(artifact.title || "文档");
    lastDocumentArtifactId = String(artifact.artifact_id || "");
    const source = artifact.task_id ? '来源任务 <code>' + html(artifact.task_id) + '</code>' : "";
    const body = artifact.content != null && artifact.content !== ""
      ? markdownHtml(artifact.content)
      : '<div class="rp-empty">正在读取文档内容…</div>';
    const docHtml =
      '<article class="wb-doc-card">'
      + '<header class="wb-doc-head"><span class="wb-doc-kind">' + html(kind) + '</span><h3>' + html(title) + '</h3></header>'
      + (source ? '<div class="wb-doc-meta">' + source + '</div>' : "")
      + '<div class="wb-doc-body">' + body + '</div>'
      + '</article>';
    if (documentView) {
      documentView.innerHTML = docHtml;
      documentView.hidden = false;
    }
  }

  // ─── Browser preview (renders into the KANBAN drawer) ───
  let currentPreviewUrl = "";
  function loadPreview(url) {
    const target = String(url || "").trim();
    if (!target) return;
    if (!/^https?:\/\/localhost:\d+/i.test(target)) return;
    currentPreviewUrl = target;
    const frame = document.getElementById("kbPreviewFrame");
    if (frame) {
      // Route through the same-origin proxy so iframe loads never hit CORS.
      frame.src = "/api/preview?url=" + encodeURIComponent(target);
      frame.hidden = false;
    }
  }
  function previewFromEvent(event) {
    if (event.type !== "tool_result") return;
    const result = String(event.payload?.result || event.payload?.output || "");
    const display = String(event.payload?.display_summary || "");
    const match = (result + "\n" + display).match(LOCALHOST_RE);
    if (match) loadPreview(match[0]);
  }

  // ─── Activity cards (folded into the KANBAN run card) ───
  const activityGrid = document.getElementById("rpActivityCards");
  const activityCards = new Map();
  function statusIcon(status) {
    if (status === "completed") return "✓";
    if (status === "failed") return "!";
    if (status === "running") return "◌";
    return "·";
  }
  function activityKey(event) {
    // Key on the agent identity, not the event type, so each parallel agent
    // owns exactly one card that updates as its events arrive.
    return String(event.actor?.id || event.task_id || event.actor?.label || event.run_id || "");
  }
  function renderActivity(event) {
    if (!activityGrid) return;
    const key = activityKey(event);
    const payload = event.payload || {};
    const card = activityCards.get(key) || {
      title: event.actor?.label || payload.title || "Agent",
      role: event.actor?.role || "agent",
      status: "running",
    };
    if (event.type === "reference_started") card.title = event.actor?.label || "参考模型";
    if (event.type === "reference_response") card.status = "completed";
    if (event.type === "subtask_assignment") card.title = payload.title || card.title;
    if (event.type === "subagent_progress") card.status = "running";
    if (event.type === "subagent_response") card.status = "completed";
    if (event.type === "subagent_tool_call" || event.type === "subagent_tool_result") card.status = "running";
    activityCards.set(key, card);
    activityGrid.innerHTML = [...activityCards.values()].map(c =>
      '<div class="wb-activity-card" data-status="' + html(c.status) + '">'
      + '<span class="wb-activity-ic">' + statusIcon(c.status) + '</span>'
      + '<div class="wb-activity-copy"><strong>' + html(c.title) + '</strong><small>' + html(c.role) + ' · ' + html(c.status === "completed" ? "已完成" : c.status === "failed" ? "失败" : "进行中") + '</small></div>'
      + '</div>'
    ).join("") || '<div class="rp-empty">暂无活动</div>';
  }

  // Wire document + browser + activity into the existing event application.
  // applyTranscriptEvent is defined in core.js (loaded before this file), so a
  // safe patch keeps the original chain intact.
  const applyFn = window.applyTranscriptEvent;
  if (applyFn && !window.__modusWindowsPatched) {
    const original = applyFn;
    window.applyTranscriptEvent = function (event) {
      const result = original(event);
      const type = event && event.type;
      if (type === "artifact" && DOC_KINDS.includes(String(event.payload?.kind || "").toLowerCase())) {
        renderDocument(event.payload);
        // Announcement carries metadata only; request the body so the doc
        // window can fill in via renderInlineArtifactContent once it arrives.
        // Silent: the document window is the surface, not the modal viewer.
        const artifactId = String(event.payload?.artifact_id || "");
        if (artifactId && typeof window.requestArtifactContent === "function") {
          window.requestArtifactContent(artifactId, {silent: true});
        }
      }
      if (type === "tool_result") previewFromEvent(event);
      if (["subtask_assignment", "subagent_progress", "subagent_tool_call", "subagent_tool_result", "subagent_response", "reference_started", "reference_response"].includes(type)) {
        renderActivity(event);
      }
      return result;
    };
    window.__modusWindowsPatched = true;
  }

  // Browser preview controls moved into the KANBAN drawer; keep a system-open
  // affordance on the current preview URL for callers that kept the old API.
  global.ModusWindows = { renderDocument, loadPreview, renderActivity };
  Object.defineProperty(global.ModusWindows, "currentPreviewUrl", {
    get: function () { return currentPreviewUrl; },
  });
})(window);
