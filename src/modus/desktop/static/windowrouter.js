// ─── Contextual window router: right panel follows the agent's actions ───
// The right-panel window (workspace / focus / subtasks / runs / tasks /
// artifacts / review) auto-switches based on the typed event stream, so the
// user sees the agent's CURRENT focus instead of a static tab.  Manual clicks
// pin the window (📌) until the user unlocks it.
(function (global) {
  "use strict";
  const DOC_KINDS = ["plan", "design", "spec"];
  const LOCALHOST_RE = /https?:\/\/localhost:\d+/i;
  const ROUTE = {
    run_started: "tasks",
    run_completed: "runs",
    run_error: "runs",
    subtask_assignment: "subtasks",
    subagent_progress: "subtasks",
    subagent_response: "subtasks",
    reference_started: "activity",
    host_review: "review",
    approval_request: null, // keep current window, do not jump
  };
  let locked = false;
  let lastDocumentId = "";

  function routeFor(event) {
    if (locked) return null;
    if (event.channel_id === "user_host") return null; // narrative owns main chat
    const target = ROUTE[event.type];
    if (target) return target;
    // Contextual overrides: document/browser windows are kind-driven.
    if (event.type === "artifact") {
      const kind = String(event.payload?.kind || "artifact").toLowerCase();
      if (DOC_KINDS.includes(kind)) {
        const id = String(event.payload?.artifact_id || "");
        // Do not re-jump when the same document is being re-announced.
        if (id && id === lastDocumentId) return null;
        if (id) lastDocumentId = id;
        return "document";
      }
      return "artifacts";
    }
    if (event.type === "tool_result") {
      const result = String(event.payload?.result || event.payload?.output || "");
      const display = String(event.payload?.display_summary || "");
      if (LOCALHOST_RE.test(result) || LOCALHOST_RE.test(display)) return "browser";
    }
    return null;
  }

  function observe(event) {
    if (locked) return null;
    if (event.channel_id === "user_host") return null; // narrative owns main chat
    const target = routeFor(event);
    // The board highlights the column the event drives; it never switches tabs.
    const columnHint = columnForEvent(event);
    if (columnHint && global.ModusKanban && typeof global.ModusKanban.setActiveColumn === "function") {
      global.ModusKanban.setActiveColumn(columnHint);
    }
    if (target && global.ModusWorkbenchWindows) {
      global.ModusWorkbenchWindows.activate(target);
    }
    return target;
  }

  function columnForEvent(event) {
    const type = event && event.type;
    if (type === "run_started") return "analyzing";
    if (type === "tool_call" || type === "tool_result" || type === "subagent_tool_call" || type === "subagent_tool_result") return "executing";
    if (type === "host_review" || type === "run_completed") return "verifying";
    if (type === "approval_request") return "analyzing";
    if (type === "run_error") return "completed";
    return null;
  }

  // Patch applyTranscriptEvent defensively (same pattern as agentstatus.js).
  const applyFn = window.applyTranscriptEvent;
  if (applyFn && !window.__modusWindowRouterPatched) {
    const original = applyFn;
    window.applyTranscriptEvent = function (event) {
      const result = original(event);
      observe(event);
      return result;
    };
    window.__modusWindowRouterPatched = true;
  }

  // Expose a lock/unlock so the 📌 affordance can pin the current window.
  global.ModusWindowRouter = {
    setLocked(value) { locked = Boolean(value); },
    isLocked() { return locked; },
    routeFor,
  };
})(window);
