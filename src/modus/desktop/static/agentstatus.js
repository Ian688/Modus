// ─── Agent status chip: ambient "what the agent is doing now" ───
// Drives #agentStatusChip from the typed event stream.  The latest in-flight
// (running/streaming) event determines the chip's state; a terminal event
// settles it to done/error until the next run starts.
(function () {
  const chip = document.getElementById("agentStatusChip");
  if (!chip) return;

  const STATES = {
    thinking: { cls: "thinking", label: "思考中…" },
    tool: { cls: "tool", label: "执行工具" },
    artifact: { cls: "artifact", label: "生成产物" },
    approval: { cls: "approval", label: "等待审批" },
    done: { cls: "done", label: "已完成" },
    error: { cls: "error", label: "出错" },
  };

  function show(state, detail) {
    const s = STATES[state] || STATES.thinking;
    chip.className = "agent-status-chip " + s.cls;
    chip.innerHTML = `<span class="asc-dot"></span>${s.label}${detail ? " " + detail : ""}`;
    chip.hidden = false;
  }
  function hide() { chip.hidden = true; }

  function observe(event) {
    const type = event.type || "";
    const status = event.status || "";
    if (type === "run_started") { show("thinking"); return; }
    if (type === "run_completed") { show("done"); return; }
    if (type === "run_error") { show("error"); return; }
    if (type === "approval_request") { show("approval"); return; }
    if (type === "tool_call" || type === "subagent_tool_call") { show("tool", (event.payload && event.payload.name) || ""); return; }
    if (type === "artifact") {
      const kind = (event.payload && event.payload.kind) || "";
      show("artifact", kind);
      return;
    }
    if (type === "host_thinking" || type === "subagent_progress") { show("thinking"); return; }
    // Default idle: hide the chip.
    if (type === "done" || status === "completed") hide();
  }

  // Wire into the global event application point if present.
  if (window.__modusObserveAgentStatus) {
    // already installed by an embedder
    return;
  }
  window.__modusObserveAgentStatus = observe;

  // Patch applyTranscriptEvent to also drive the status chip.  This must be
  // defensive: the patch installs only if the function exists and is not ours.
  const applyFn = window.applyTranscriptEvent;
  if (applyFn && !window.__agentStatusPatched) {
    const original = applyFn;
    window.applyTranscriptEvent = function (event) {
      const result = original(event);
      observe(event);
      return result;
    };
    window.__agentStatusPatched = true;
  }
})();
