// ═══ Typed Event Store + ContentBlock Timeline ═══
// Event type/channel—not Markdown wording—determines all placement and visual hierarchy.
class EventStore {
  constructor() {
    this.byId = new Map();
    this.runs = new Map();
    this.channels = new Map();
  }
  push(event) {
    if (!event || !event.event_id) return false;
    const exists = this.byId.has(event.event_id);
    const prior = this.byId.get(event.event_id);
    // A reconnect or concurrent worker may deliver an older snapshot after a
    // newer one. Stable event_id + monotonic revision makes this idempotent.
    if (prior && Number(event.revision || 0) <= Number(prior.revision || 0)) return false;
    this.byId.set(event.event_id, event);
    if (exists) return true;
    const run = this.runs.get(event.run_id) || [];
    run.push(event.event_id);
    run.sort((a, b) => (this.byId.get(a).sequence || 0) - (this.byId.get(b).sequence || 0));
    this.runs.set(event.run_id, run);
    const channelKey = `${event.run_id}:${event.channel_id}`;
    const events = this.channels.get(channelKey) || [];
    events.push(event.event_id);
    events.sort((a, b) => (this.byId.get(a).sequence || 0) - (this.byId.get(b).sequence || 0));
    this.channels.set(channelKey, events);
    return true;
  }
  snapshot(runId) {
    return (this.runs.get(runId) || []).map(id => this.byId.get(id));
  }
  event(id) { return this.byId.get(id); }
  forChannel(runId, channelId) {
    return (this.channels.get(`${runId}:${channelId}`) || []).map(id => this.byId.get(id));
  }
}

// ─── Tool row (DisclosureRow-style, Hermes-inspired) ───
// status: null = running (shows timer), "ok" = settled success, "err" = red.
// The result summary is intentionally outside a disclosure: useful evidence is
// visible at a glance, while only long/raw output needs a second level.
function summarizeToolResult(name, result, isError) {
  const raw = String(result ?? "").replace(/\u001b\[[0-?]*[ -\/]*[@-~]/g, "").trim();
  if (name === "run_tests" && raw.startsWith("{")) {
    try {
      const evidence = JSON.parse(raw);
      if (evidence.schema === "modus.verification.v1") {
        const counts = evidence.counts || {};
        const parts = [];
        if (counts.passed) parts.push(counts.passed + " passed");
        if (counts.failed) parts.push(counts.failed + " failed");
        if (counts.skipped) parts.push(counts.skipped + " skipped");
        if (counts.warnings) parts.push(counts.warnings + " warning" + (counts.warnings === 1 ? "" : "s"));
        parts.push(Number(evidence.duration_seconds || 0).toFixed(2) + "s");
        const output = String(evidence.output || "");
        const outputLines = output ? output.split(/\r?\n/) : [];
        const statusLabel = evidence.status === "passed" ? "验证通过" : evidence.status === "cancelled" ? "验证已取消" : evidence.status === "timed_out" ? "验证超时" : "验证失败";
        const summary = statusLabel + (parts.length ? " · " + parts.join(" · ") : "");
        let preview = output;
        if (output.length > 900 || outputLines.length > 12) preview = [...outputLines.slice(0, 5), ...outputLines.slice(-5)].filter((line, index, all) => all.indexOf(line) === index).join("\n").slice(0, 820).trimEnd() + "\n…";
        return {summary, preview, full: output, truncated: preview !== output, lineCount: outputLines.length};
      }
    } catch (_) {}
  }
  if (name === "system_probe" && raw.startsWith("{")) {
    try {
      const snap = JSON.parse(raw);
      if (snap.schema === "modus.system.v1") {
        const cpu = snap.cpu || {};
        const parts = [];
        if (cpu.loadavg_1m != null) parts.push("负载 " + cpu.loadavg_1m);
        if (cpu.cpu_count) parts.push(cpu.cpu_count + " 核");
        const disk = (snap.disk || []).find(d => d.path === "/");
        if (disk && disk.free_pct != null) parts.push("磁盘余 " + disk.free_pct + "%");
        const procs = snap.processes || [];
        parts.push(procs.length + " 进程");
        return {summary: "系统快照 · " + parts.join(" · "), preview: raw, full: raw, truncated: false, lineCount: raw.split(/\r?\n/).length};
      }
    } catch (_) {}
  }
  const lines = raw.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const passed = raw.match(/(\d+)\s+passed\b/i);
  const failed = raw.match(/(\d+)\s+failed\b/i);
  const skipped = raw.match(/(\d+)\s+skipped\b/i);
  const warnings = raw.match(/(\d+)\s+warnings?\b/i);
  const duration = raw.match(/\bin\s+([\d.]+s)\b/i);
  let summary = "";
  if (isError) {
    summary = (lines.find(line => /error|failed|exception|traceback|fatal|denied|not found|错误|失败|拒绝/i.test(line))
      || lines.find(line => !/^[-=*_]+$/.test(line)) || "工具返回错误").replace(/^error[:：]?\s*/i, "");
    summary = "执行失败：" + summary;
  } else if (passed || failed || skipped || warnings || duration) {
    const parts = [];
    if (passed) parts.push(passed[1] + " passed");
    if (failed) parts.push(failed[1] + " failed");
    if (skipped) parts.push(skipped[1] + " skipped");
    if (warnings) parts.push(warnings[1] + " warning" + (warnings[1] === "1" ? "" : "s"));
    if (duration) parts.push(duration[1]);
    summary = parts.join(" · ");
  } else if (!raw) {
    summary = "执行完成，无输出";
  } else {
    summary = (lines[0] || raw).replace(/^(stdout|output|result)[:：]\s*/i, "");
    summary = "执行完成：" + summary;
  }
  if (summary.length > 180) summary = summary.slice(0, 177).trimEnd() + "…";
  const lineCount = raw ? raw.split(/\r?\n/).length : 0;
  let preview = raw;
  if (raw.length > 900 || lineCount > 12) {
    const rawLines = raw.split(/\r?\n/);
    const interesting = [];
    rawLines.forEach(line => {
      if (/error|failed|exception|traceback|fatal|warning|passed|skipped|summary|错误|失败|警告|通过/i.test(line) && !interesting.includes(line)) interesting.push(line);
    });
    const selected = [...rawLines.slice(0, 5), ...interesting.slice(0, 4), ...rawLines.slice(-3)]
      .filter((line, index, all) => all.indexOf(line) === index);
    preview = selected.join("\n").slice(0, 820).trimEnd() + "\n…";
  }
  return {summary, preview, full: raw, truncated: preview !== raw, lineCount};
}
function toolResultViewHtml(name, result, isError) {
  const displaySummary = arguments[3];
  const metadata = arguments[4];
  const view = summarizeToolResult(name, result, isError);
  const explicitSummary = String(displaySummary || "").trim();
  const operation = metadata?.operation || "";
  const path = metadata?.path || "";
  const operationLabel = operation === "write" ? "已写入" : operation === "edit" ? "已编辑" : "";
  const count = Number(metadata?.replacement_count || 0);
  const contextLabel = operationLabel && path
    ? operationLabel + " · " + path + (count > 0 ? " · " + count + " 处" : "")
    : "";
  if (contextLabel) view.summary = contextLabel;
  else if (explicitSummary) view.summary = explicitSummary;
  const meta = view.lineCount ? view.lineCount + " 行" : "";
  const glyph = isError ? '<span class="tool-result-glyph">!</span>' : "";
  const output = view.full || view.preview;
  const outputDisclosure = output
    ? '<details class="tool-result-output-details"><summary>' + (isError ? "查看错误输出" : "查看输出") + (view.lineCount ? " · " + view.lineCount + " 行" : "") + '</summary><pre class="tool-result-output"><code>' + escapeHtml(output) + '</code></pre></details>'
    : "";
  return '<section class="tool-result-view" data-error="' + String(Boolean(isError)) + '"><div class="tool-result-head">' + glyph + '<span class="tool-result-summary">' + escapeHtml(view.summary) + '</span>' + (meta ? '<span class="tool-result-meta">' + meta + '</span>' : "") + '</div>'
    + outputDisclosure + '</section>';
}
function toolRowHtml(name, status, runId, toolCallId, body, resultText, isError, displaySummary, metadata, artifactIds) {
  const statusAttr = status ? ' data-status="' + escapeHtml(status) + '"' : "";
  const timer = status === null ? '<span class="tl-timer"></span>' : "";
  const glyph = status === "err" || status === "stopped" ? "!" : status === null ? "◌" : "";
  const key = (runId || "") + ":" + (toolCallId || name);
  const header = '<div class="timeline-tool-head">' + (glyph ? '<span class="tl-glyph">' + glyph + '</span>' : "") + '<span class="tl-name">' + escapeHtml(name) + '</span>' + timer + '</div>';
  const input = body && resultText === undefined
    ? '<details class="timeline-tool-input" open><summary>查看调用参数</summary><pre><code>' + escapeHtml(body) + '</code></pre></details>'
    : body ? '<details class="timeline-tool-input"><summary>查看调用参数</summary><pre><code>' + escapeHtml(body) + '</code></pre></details>' : "";
  const result = resultText !== undefined ? toolResultViewHtml(name, resultText, Boolean(isError), displaySummary, metadata) : "";
  const reviewPath = metadata?.changed && metadata?.path ? ' data-review-path="' + escapeHtml(String(metadata.path)) + '"' : "";
  // A tool that persisted its oversized full result links to the local artifact
  // so the raw text stays discoverable without re-entering the model context.
  const artifactIdsList = Array.isArray(artifactIds) ? artifactIds.filter(Boolean) : [];
  const artifactLink = artifactIdsList.length
    ? '<span class="timeline-tool-artifacts">' + artifactIdsList.map(id =>
        '<button type="button" class="plain-small tool-result-artifact" data-artifact-id="' + escapeHtml(String(id)) + '">查看完整结果产物</button>'
      ).join("") + '</span>'
    : "";
  return '<div class="timeline-tool" data-tool-key="' + escapeHtml(key) + '"' + reviewPath + statusAttr + '>' + header + input + result + artifactLink + '</div>';
}
const toolTimers = new Map();
function formatDuration(sec) {
  if (sec < 60) return Math.round(sec) + "s";
  return Math.floor(sec / 60) + "m " + Math.round(sec % 60) + "s";
}
function startToolTimer(node) {
  const details = node.matches?.(".timeline-tool") ? node : node.querySelector(".timeline-tool");
  const timerEl = details ? details.querySelector(".tl-timer") : null;
  const key = details ? details.dataset.toolKey : "";
  if (!key || !timerEl || toolTimers.has(key)) return;
  const t0 = Date.now();
  const timer = setInterval(() => { timerEl.textContent = "⏱ " + formatDuration((Date.now() - t0) / 1000); }, 500);
  toolTimers.set(key, timer);
}
function stopToolTimer(node) {
  const details = node.matches?.(".timeline-tool") ? node : node.querySelector(".timeline-tool");
  const key = details ? details.dataset.toolKey : "";
  if (!key) return;
  const timer = toolTimers.get(key);
  if (timer) { clearInterval(timer); toolTimers.delete(key); }
  // Clear the frozen elapsed text so the row reads as settled
  const prev = document.querySelector('[data-tool-key="' + CSS.escape(key) + '"] .tl-timer');
  if (prev) prev.textContent = "";
}

// ─── Thinking status ───
// Ordinary conversation shows only a transient status line. Raw reasoning
// remains in the typed event ledger and never becomes a second assistant answer.
function thinkRowHtml(text) {
  return '<div class="thinking-status"><span class="tl-glyph">◌</span>'
    + '<span class="thinking-label">正在思考</span><span class="tl-think-timer"></span></div>'
    + '<div class="thinking-content" hidden>' + escapeHtml(text) + '</div>';
}
const thinkTimers = new Map();
function startThinkTimer(node, runId) {
  const timerEl = node.querySelector(".tl-think-timer");
  if (!runId || !timerEl || thinkTimers.has(runId)) return;
  const t0 = Date.now();
  const timer = setInterval(() => { timerEl.textContent = "⏱ " + formatDuration((Date.now() - t0) / 1000); }, 500);
  thinkTimers.set(runId, timer);
}
function stopThinkTimer(runId) {
  const timer = thinkTimers.get(runId);
  if (timer) { clearInterval(timer); thinkTimers.delete(runId); }
  document.querySelectorAll('[data-think-key="' + CSS.escape(runId) + '"] .tl-think-timer').forEach(el => el.textContent = "");
}
// Raw reasoning is transient process state. Once prose starts it leaves the
// conversation; conclusions belong in the assistant response.
function settleThinking(container) {
  container.querySelectorAll(".think-block.thinking:not(.thinking-settled)").forEach(row => {
    const runId = row.dataset.thinkKey;
    if (runId) stopThinkTimer(runId);
    row.remove();
  });
}

// Long sessions must not keep every rendered node mounted. Once a container
// exceeds this cap, settled nodes beyond the tail are detached into a cold
// region (kept in the event store) with an "expand earlier" affordance.
const MAX_MOUNTED_NODES = 400;

class TimelineRenderer {
  constructor(store) {
    this.store = store;
    this.elements = new Map();
    this.toolParts = new Map();
    this.activities = new Map();
    this.approvalsById = new Map();
    this.approvalsByTool = new Map();
    this.coldNodes = new Map();
    this.currentTurn = null;
    this.turnRuns = new Map();
    this._turnSeq = 0;
    // Reply segmentation: key = host_response event_id → {seg, text}. Segments
    // let tool rows render *between* reply paragraphs: a tool event seals the
    // current open segment and later reply deltas start a fresh one after it.
    this._replySegs = new Map();
    // Per-run insertion cursor: after a tool/approval row is inserted, later
    // reply/thinking events for the same run append after that cursor instead
    // of jumping back before it. reset to null when the run ends.
    this._runCursor = new Map();
    // Full accumulated reply text per host_response event_id (across segments)
    // remains available for message actions and replay bookkeeping.
    this._replyText = new Map();
  }
  reset() {
    toolTimers.forEach(timer => clearInterval(timer));
    thinkTimers.forEach(timer => clearInterval(timer));
    this.activities.forEach(activity => {
      if (activity.timer) clearInterval(activity.timer);
    });
    toolTimers.clear();
    thinkTimers.clear();
    this.elements = new Map();
    this.toolParts = new Map();
    this.activities = new Map();
    this.approvalsById = new Map();
    this.approvalsByTool = new Map();
    this.coldNodes = new Map();
    this.currentTurn = null;
    this.turnRuns = new Map();
    this._turnSeq = 0;
    this._replySegs = new Map();
    this._runCursor = new Map();
    this._replyText = new Map();
  }
  // ── Turn grouping: one user turn (thinking + tools + reply + footer) in a .turn ──
  _isFlatEvent(event) {
    return event.type === "approval_request" || event.type === "approval_resolved"
      || event.channel_id === "host_models"
      || event.type === "run_started" || event.type === "run_completed";
  }
  _openTurn(container, event) {
    this._closeTurn();
    const turn = document.createElement("section");
    turn.className = "turn";
    turn.dataset.turnId = "t" + (++this._turnSeq);
    if (event.event_id) turn.dataset.eventId = event.event_id;
    if (event.run_id) turn.dataset.runId = event.run_id;
    if (event.sequence) turn.dataset.anchorSeq = String(event.sequence);
    turn.innerHTML = '<div class="turn-bd"></div>';
    const before = this._nextNodeFor(container, event);
    container.insertBefore(turn, before);
    this.currentTurn = turn;
    this.currentTurnRunId = event.run_id || "";
    return turn;
  }
  _closeTurn() {
    const turn = this.currentTurn;
    if (!turn) return;
    turn.removeAttribute("data-open");
    this.currentTurn = null;
    this.currentTurnRunId = null;
    return turn;
  }
  _nextNodeFor(container, event) {
    const seq = event.sequence || 0;
    for (const node of container.children) {
      if (node.classList.contains("timeline-expand-earlier")) continue;
      const e = this.store.event(node.dataset.eventId);
      if (e && (e.sequence || 0) > seq) return node;
    }
    return null;
  }
  // Insert a node into its turn-bd ordered by event sequence. Siblings carry a
  // data-anchor-seq stamp set at creation, so the walk is O(1) and does not
  // depend on the EventStore. This is what keeps tools/reply segments/approvals
  // in true stream order instead of being appended after the reply.
  _insertBySeq(target, node, event) {
    const seq = Number(event?.sequence || 0);
    for (const child of target.children) {
      if (child === node) return;
      const anchor = Number(child.dataset.anchorSeq || 0);
      if (anchor) {
        if (seq < anchor) { target.insertBefore(node, child); return; }
        continue;
      }
      const e = this.store.event(child.dataset.eventId);
      if (e && (Number(e.sequence) || 0) > seq) { target.insertBefore(node, child); return; }
    }
    target.appendChild(node);
  }
  _turnAnchor(event) {
    const container = this._container(event);
    if (!container) return null;
    if (this._isFlatEvent(event)) return null;
    const runId = event.run_id || "";
    if (runId && this.turnRuns.has(runId)) {
      const turn = this.turnRuns.get(runId);
      if (turn && turn.parentNode === container) return turn.querySelector(".turn-bd");
    }
    if (event.type === "user_message") {
      const turn = this._openTurn(container, event);
      if (runId) this.turnRuns.set(runId, turn);
      return turn.querySelector(".turn-bd");
    }
    if (this.currentTurn && this.currentTurn.parentNode === container) {
      return this.currentTurn.querySelector(".turn-bd");
    }
    return null;
  }
  // Tools (host_models channel) and approval cards must stay inside the turn.
  // This mirrors _turnAnchor without the flat-event guard so they rejoin the
  // user → think → tools → reply sequence for the run.
  _toolTurnBd(event) {
    const container = this._container(event);
    if (!container) return null;
    const runId = event.run_id || "";
    if (runId && this.turnRuns.has(runId)) {
      const turn = this.turnRuns.get(runId);
      if (turn && turn.parentNode === container) return turn.querySelector(".turn-bd");
    }
    if (this.currentTurn && this.currentTurn.parentNode === container) {
      return this.currentTurn.querySelector(".turn-bd");
    }
    return null;
  }
  _absorbTrailingFlat(container) {
    const last = container.lastElementChild;
    if (!last) return;
    if (last.dataset.absorbable !== "true") return;
    const isFlatResult = (last.classList.contains("msg") && last.classList.contains("system")) || last.classList.contains("run-result");
    if (!isFlatResult) return;
    const turn = this.currentTurn;
    if (!turn || turn.parentNode !== container) return;
    turn.querySelector(".turn-bd").appendChild(last);
  }
  _toolKey(event) {
    return (event.run_id || "") + ":" + (event.payload?.tool_call_id || event.part_id || event.event_id);
  }
  _activityFor(event, target) {
    const runId = event.run_id || "default";
    let activity = this.activities.get(runId);
    if (activity?.node?.isConnected) return activity;
    const node = document.createElement("details");
    node.className = "run-activity";
    node.open = true;
    node.dataset.runId = runId;
    node.dataset.anchorSeq = String(event.sequence || 0);
    node.innerHTML = '<summary><span class="run-activity-pulse" aria-hidden="true"></span><span class="run-activity-label">正在工作</span><span class="run-activity-meta">0 项活动</span><span class="run-activity-timer">0s</span></summary><div class="run-activity-items"></div>';
    this._insertBySeq(target, node, event);
    activity = {node, body:node.querySelector(".run-activity-items"), startedAt:Date.now(), terminal:false, timer:null};
    activity.timer = setInterval(() => this._refreshActivity(activity), 1000);
    this.activities.set(runId, activity);
    this._refreshActivity(activity);
    return activity;
  }
  _refreshActivity(activity) {
    if (!activity?.node) return;
    const rows = [...activity.body.querySelectorAll(":scope > .collab-msg.tool")];
    const running = rows.filter(row => row.querySelector('.timeline-tool:not([data-status])')).length;
    const elapsed = Math.max(0, (Date.now() - activity.startedAt) / 1000);
    const label = activity.node.querySelector(".run-activity-label");
    const meta = activity.node.querySelector(".run-activity-meta");
    const timer = activity.node.querySelector(".run-activity-timer");
    if (label) label.textContent = activity.terminal ? "已完成活动" : "正在工作";
    if (meta) meta.textContent = rows.length + " 项活动";
    if (timer) timer.textContent = formatDuration(elapsed);
    activity.node.dataset.running = String(!activity.terminal && running > 0);
    activity.body.classList.toggle("run-activity-items-bounded", rows.length >= 3);
  }
  _finishActivity(runId) {
    const activity = this.activities.get(runId || "default");
    if (!activity) return;
    activity.terminal = true;
    if (activity.timer) clearInterval(activity.timer);
    activity.timer = null;
    activity.node.open = false;
    this._refreshActivity(activity);
  }
  _renderToolPart(event, container) {
    // Tools belong inside the current turn (user → think → tools → reply),
    // so they keep sequence with the bubbles instead of trailing the container.
    // Unlike _turnAnchor this must ignore the host_models flat-guard: tool
    // events ride that channel but still render inside the turn.
    const turnBd = this._toolTurnBd(event);
    const target = turnBd || container;
    const key = this._toolKey(event);
    const part = this.toolParts.get(key) || {call:null, result:null, node:null, activity:null};
    if (event.type === "tool_call" || event.type === "subagent_tool_call") part.call = event;
    else part.result = event;
    this.toolParts.set(key, part);
    const call = part.call, result = part.result, source = call || result;
    const name = result?.payload?.name || call?.payload?.name || source.actor?.label || "工具";
    const toolId = result?.payload?.tool_call_id || call?.payload?.tool_call_id || "";
    const status = result ? (result.payload?.is_error ? "err" : "ok") : null;
    const body = call ? JSON.stringify(call.payload?.input || {}, null, 2) : "";
    const resultText = result ? String(result.payload?.result || "") : undefined;
    const resultSummary = result?.payload?.display_summary || "";
    const resultMetadata = result?.payload?.metadata || {};
    const approval = this.approvalsByTool.get(key);
    if (approval) {
      if (part.node) {
        part.node.remove();
        part.node = null;
      }
      this._renderApprovalState(approval);
      return;
    }
    const activity = part.activity || this._activityFor(source, target);
    part.activity = activity;
    if (!part.node) {
      const node = document.createElement("div");
      node.className = "collab-msg tool";
      node.dataset.eventId = source.event_id;
      if (source.task_id) node.dataset.taskId = source.task_id;
      node.dataset.toolPartKey = key;
      if (source.sequence) node.dataset.anchorSeq = String(source.sequence);
      node.innerHTML = '<div class="collab-body compact">' + toolRowHtml(name, status, source.run_id, toolId, body, resultText, result?.payload?.is_error, resultSummary, resultMetadata, result?.artifact_ids || source.artifact_ids) + '</div>';
      // A tool row appearing mid-reply seals the open reply segment so the
      // next reply deltas open a fresh segment after this row.
      this._sealReplySegments(event.run_id || "");
      this._insertBySeq(activity.body, node, source);
      this._runCursor.set(event.run_id || "", activity.node);
      part.node = node;
      this.elements.set(source.event_id, node);
    } else {
      part.node.querySelector(".collab-body").innerHTML = toolRowHtml(name, status, source.run_id, toolId, body, resultText, result?.payload?.is_error, resultSummary, resultMetadata);
    }
    if (result) stopToolTimer(part.node); else startToolTimer(part.node);
    this._refreshActivity(activity);
    addCopyHandlers(part.node);
    container.scrollTop = container.scrollHeight;
  }
  // The open reply segment for a run (if any) is sealed by a tool/approval row:
  // new deltas must not rewrite text already shown before the tool row.
  _sealReplySegments(runId) {
    for (const [key, seg] of this._replySegs) {
      if (seg._sealed) continue;
      seg._sealed = true;
      this._runCursor.set(runId, seg.seg);
    }
  }
  // Mark the whole turn as streaming / done so CSS can switch presentation.
  _setReplyState(runId, state) {
    const turn = this.turnRuns.get(runId || "");
    if (turn) turn.dataset.replyState = state;
  }
  // Return the live (unsealed) segment for a reply event, opening a fresh
  // segment after the current run cursor when the previous one was sealed.
  _openReplySegment(target, event) {
    let seg = this._replySegs.get(event.event_id);
    if (seg && !seg._sealed && seg.seg.parentNode === target) return seg;
    const node = document.createElement("div");
    node.className = "msg assistant assistant-continuation";
    node.dataset.messageRole = "assistant";
    node.dataset.eventId = event.event_id;
    if (event.task_id) node.dataset.taskId = event.task_id;
    if (event.sequence) node.dataset.anchorSeq = String(event.sequence);
    node.innerHTML = '<div class="block-text"></div>';
    const runId = event.run_id || "";
    const cursor = this._runCursor.get(runId);
    if (cursor && cursor.parentNode === target) {
      target.insertBefore(node, cursor.nextSibling || null);
    } else {
      this._insertBySeq(target, node, event);
    }
    this._runCursor.set(runId, node);
    if (!seg) seg = {seg: node, text: ""};
    else { seg.seg = node; seg.text = ""; }
    seg._sealed = false;
    this._replySegs.set(event.event_id, seg);
    return seg;
  }
  _approvalToolKey(event) {
    const toolCallId = event.payload?.tool_call_id || "";
    return toolCallId ? (event.run_id || "") + ":" + toolCallId : "";
  }
  _approvalHtml(state) {
    const request = state.request;
    const resolution = state.resolution;
    const toolPart = state.toolKey ? this.toolParts.get(state.toolKey) : null;
    const result = toolPart?.result || null;
    let stateName = "pending", statusText = "等待你的确认";
    if (resolution?.payload?.decision === "deny" || state.localDecision === "deny") {
      stateName = "denied";
      statusText = resolution?.payload?.resolution_reason === "approval_timeout" ? "已超时拒绝" : "已拒绝";
    } else if (result) {
      stateName = result.payload?.is_error ? "failed" : "completed";
      statusText = result.payload?.is_error ? "执行失败" : "执行完成";
    } else if (state.terminal) {
      stateName = "stopped";
      statusText = "运行已结束，未执行";
    } else if (resolution?.payload?.decision === "allow" || state.localDecision === "approve") {
      stateName = "running";
      statusText = "已允许，执行中";
    }
    const payload = request.payload || {};
    const toolName = payload.tool_name || result?.payload?.name || "工具";
    const disclosesWorkspaceContent = payload.data_disclosure === "workspace_content";
    const startedAt = Date.parse(request.timestamp || "");
    const finishedAt = Date.parse(result?.timestamp || resolution?.timestamp || "");
    const elapsedSeconds = Number.isFinite(startedAt) && Number.isFinite(finishedAt)
      ? Math.max(0, (finishedAt - startedAt) / 1000) : 0;
    const resultView = result
      ? summarizeToolResult(toolName, String(result.payload?.result || ""), Boolean(result.payload?.is_error))
      : null;
    const receiptMeta = [
      elapsedSeconds > 0 ? formatDuration(elapsedSeconds) : "",
      resultView?.lineCount ? resultView.lineCount + " 行" : "",
    ].filter(Boolean).join(" · ");
    const actions = stateName === "pending"
      ? '<div class="approval-actions"><button type="button" data-approval-decision="approve">' + (disclosesWorkspaceContent ? '允许提供' : '允许执行') + '</button><button type="button" data-approval-decision="deny">拒绝</button></div>'
      : '<div class="approval-status" data-state="' + stateName + '">' + statusText + '</div>';
    const resultHtml = result
      ? toolResultViewHtml(toolName, String(result.payload?.result || ""), Boolean(result.payload?.is_error), result.payload?.display_summary, result.payload?.metadata)
      : "";
    const requestJson = escapeHtml(JSON.stringify(payload.input || {}, null, 2));
    const inputHtml = stateName === "pending"
      ? '<pre><code>' + requestJson + '</code></pre>'
      : '<details class="approval-result"><summary>查看请求参数</summary><pre><code>' + requestJson + '</code></pre></details>';
    const cardStart = '<section class="approval-card" data-approval-id="' + escapeHtml(payload.approval_id || "")
      + '" data-run-id="' + escapeHtml(payload.run_id || request.run_id || "") + '" data-state="' + stateName + '">'
    if (stateName === "pending") {
      return cardStart
        + '<strong>' + escapeHtml(disclosesWorkspaceContent ? "向当前模型提供工作区内容" : toolName) + '</strong><span> · ' + escapeHtml(payload.danger_level || "medium") + '</span>'
        + '<p>' + escapeHtml(disclosesWorkspaceContent ? (payload.description || "所选文件内容或片段将进入当前模型上下文。") : (payload.description || "此操作可能产生副作用。")) + '</p>'
        + inputHtml + actions + '</section>';
    }
    if (stateName === "running") {
      return cardStart + '<div class="execution-receipt-row"><span class="execution-receipt-mark running">◌</span>'
        + '<strong>' + escapeHtml(toolName) + '</strong><span class="execution-receipt-state">执行中</span></div>'
        + '<details class="execution-receipt-details"><summary>查看请求详情</summary>'
        + '<p>' + escapeHtml(payload.description || "此操作可能产生副作用。") + '</p>'
        + inputHtml + '</details></section>';
    }
    const successful = stateName === "completed";
    const mark = successful ? "✓" : stateName === "denied" ? "×" : "!";
    const terminalLabel = successful ? "已完成" : statusText;
    const open = stateName === "failed" ? " open" : "";
    return cardStart + '<details class="execution-receipt"' + open + '><summary>'
      + '<span class="execution-receipt-mark ' + stateName + '">' + mark + '</span>'
      + '<strong>' + escapeHtml(toolName) + '</strong>'
      + '<span class="execution-receipt-state">' + escapeHtml(terminalLabel) + '</span>'
      + (receiptMeta ? '<span class="execution-receipt-meta">' + escapeHtml(receiptMeta) + '</span>' : '')
      + '<span class="execution-receipt-chevron" aria-hidden="true">›</span></summary>'
      + '<div class="execution-receipt-body"><div class="execution-receipt-risk">风险级别 · ' + escapeHtml(payload.danger_level || "medium")
      + (payload.impact_class ? '　影响 · ' + escapeHtml(payload.impact_class) : '') + '</div>'
      + '<p>' + escapeHtml(payload.description || "此操作可能产生副作用。") + '</p>'
      + inputHtml + resultHtml + '</div></details></section>';
  }
  _renderApprovalState(state) {
    if (!state.node || !state.request) return;
    state.node.innerHTML = '<div class="block-text">' + this._approvalHtml(state) + '</div>';
    addCopyHandlers(state.node);
  }
  _renderApproval(event, container) {
    const approvalId = event.payload?.approval_id || "";
    let state = this.approvalsById.get(approvalId);
    if (!state) {
      state = {request:null, resolution:null, node:null, toolKey:"", localDecision:"", terminal:false};
      if (approvalId) this.approvalsById.set(approvalId, state);
    }
    if (event.type === "approval_request") {
      state.request = event;
      state.toolKey = this._approvalToolKey(event);
      if (state.toolKey) {
        this.approvalsByTool.set(state.toolKey, state);
        const toolPart = this.toolParts.get(state.toolKey);
        if (toolPart?.node) { toolPart.node.remove(); toolPart.node = null; }
      }
      if (!state.node) {
        const node = document.createElement("div");
        node.className = "msg system";
        node.dataset.eventId = event.event_id;
        (this._toolTurnBd(event) || container).appendChild(node);
        state.node = node;
        this.elements.set(event.event_id, node);
      }
    } else {
      state.resolution = event;
      if (!state.toolKey) state.toolKey = this._approvalToolKey(event);
      if (state.toolKey) this.approvalsByTool.set(state.toolKey, state);
    }
    this._renderApprovalState(state);
    _autoScroll();
  }
  markApprovalDecision(approvalId, decision) {
    const state = this.approvalsById.get(approvalId);
    if (!state) return;
    state.localDecision = decision;
    this._renderApprovalState(state);
  }
  _finishRun(runId) {
    settleThinking(document.getElementById("chatArea"));
    settleThinking(document.getElementById("chatAreaLower"));
    for (const [key, part] of this.toolParts) {
      if (!key.startsWith(runId + ":") || part.result) continue;
      const approval = this.approvalsByTool.get(key);
      if (approval) {
        approval.terminal = true;
        this._renderApprovalState(approval);
      } else if (part.node) {
        const call = part.call;
        const name = call?.payload?.name || "工具";
        const body = JSON.stringify(call?.payload?.input || {}, null, 2);
        part.node.querySelector(".collab-body").innerHTML = toolRowHtml(name, "stopped", runId, call?.payload?.tool_call_id, body, "运行已结束，未收到工具结果", true);
        stopToolTimer(part.node);
      }
    }
    this._finishActivity(runId);
  }
  _runStats(runId) {
    let succeeded = 0, failed = 0, running = 0;
    for (const [key, part] of this.toolParts) {
      if (!key.startsWith(runId + ":")) continue;
      if (!part.result) running += 1;
      else if (part.result.payload?.is_error) failed += 1;
      else succeeded += 1;
    }
    return {succeeded, failed, running};
  }
  _renderRunCompletion(event, container) {
    if (this.elements.has(event.event_id)) return;
    const budget = event.payload?.budget || {};
    const turns = Number(event.payload?.total_turns ?? budget.turns ?? 0);
    const tokens = Number(event.payload?.total_tokens ?? budget.total_tokens ?? 0);
    const elapsed = Number(budget.elapsed_seconds ?? 0);
    const run = event.workbench || (typeof workbenchStore !== "undefined" ? workbenchStore.getRun(event.run_id) : null);
    const semantic = run?.semantic || {};
    const outcome = semantic.outcome || {};
    const evidence = Array.isArray(semantic.evidence) ? semantic.evidence : [];
    const latestEvidence = evidence[evidence.length - 1] || null;
    const recoveryCount = Number(outcome.recovery_count || 0);
    const tools = this._runStats(event.run_id);
    const verification = event.payload?.verification || budget.verification || {};
    const stats = [];
    if (elapsed > 0) stats.push('<span>' + escapeHtml(formatDuration(elapsed)) + '</span>');
    if (latestEvidence?.counts) {
      const counts = latestEvidence.counts;
      const evidenceBits = [];
      if (Number(counts.passed || 0)) evidenceBits.push(Number(counts.passed) + ' passed');
      if (Number(counts.failed || 0)) evidenceBits.push(Number(counts.failed) + ' failed');
      if (evidenceBits.length) stats.push('<span class="verified">' + escapeHtml(evidenceBits.join(' · ')) + '</span>');
    }
    if (outcome.verified === true || (!outcome.status && verification.status === "passed")) stats.push('<span class="verified">✓ 已验证</span>');
    if (recoveryCount) stats.push('<span class="recovered">↻ 自动恢复 ' + recoveryCount + ' 次</span>');
    if (!outcome.status) {
      if (tools.succeeded) stats.push('<span>' + tools.succeeded + ' 个工具成功</span>');
      if (tools.failed) stats.push('<span class="failed">' + tools.failed + ' 个工具失败</span>');
    }
    const tasks = run?.tasks || [];
    const artifacts = run?.artifacts || [];
    const workerTasks = tasks.filter(task => task.task_kind !== "root");
    if (workerTasks.length) stats.push('<span>' + workerTasks.filter(task => task.status === "completed").length + '/' + workerTasks.length + ' 个子任务完成</span>');
    if (artifacts.length) stats.push('<span>' + artifacts.length + ' 个运行产物</span>');
    const ledger = budget.usage_ledger || {};
    const ledgerKeys = Object.keys(ledger).sort();
    const ledgerHtml = ledgerKeys.length
      ? '<div class="run-completion-ledger" hidden>' + ledgerKeys.map(key => {
          const entry = ledger[key] || {};
          const total = Number(entry.input_tokens || 0) + Number(entry.output_tokens || 0);
          return '<div class="rc-ledger-row"><span>' + escapeHtml(usageOwnerLabel(key)) + '</span><b>' + escapeHtml(formatTokens(total)) + '</b><small>' + Number(entry.input_tokens || 0).toLocaleString() + ' in · ' + Number(entry.output_tokens || 0).toLocaleString() + ' out</small></div>';
        }).join('') + '</div>'
      : '';
    const node = document.createElement("section");
    node.className = "run-completion";
    node.dataset.eventId = event.event_id;
    node.dataset.runId = event.run_id;
    node.setAttribute("role", "status");
    const completionTitle = outcome.summary || "任务完成";
    const evidenceAction = latestEvidence ? '<button type="button" data-completion-evidence="' + escapeHtml(latestEvidence.evidence_id || "") + '">查看验证</button>' : '';
    node.innerHTML = '<div class="run-completion-mark" aria-hidden="true">✓</div><div class="run-completion-main"><span class="run-completion-title">' + escapeHtml(completionTitle) + '</span><div class="run-completion-stats">' + stats.join("") + (ledgerKeys.length ? '<button type="button" class="rc-ledger-toggle" data-expanded="false">分项</button>' : '') + '</div>' + ledgerHtml + '<div class="run-completion-actions"><button type="button" data-completion-task>查看任务树</button>' + evidenceAction + (artifacts.length ? '<button type="button" data-completion-artifact="' + escapeHtml(artifacts[artifacts.length - 1].artifact_id || "") + '">打开最终产物</button>' : '') + '</div></div>';
    node.querySelector("[data-completion-task]")?.addEventListener("click", () => {
      workbenchStore.selectRun(event.run_id);
      if (window.innerWidth <= 1100) setWorkbenchPanel(true);
      if (window.ModusKanban && typeof window.ModusKanban.openRunDetail === "function") window.ModusKanban.openRunDetail(event.run_id);
    });
    node.querySelector("[data-completion-evidence]")?.addEventListener("click", () => {
      workbenchStore.selectRun(event.run_id);
      if (window.ModusWorkbenchWindows) window.ModusWorkbenchWindows.activate("review");
      if (window.innerWidth <= 1100) setWorkbenchPanel(true);
      if (window.ModusKanban && typeof window.ModusKanban.openRunDetail === "function") window.ModusKanban.openRunDetail(event.run_id);
    });
    node.querySelector("[data-completion-artifact]")?.addEventListener("click", button => requestArtifactContent(button.currentTarget.dataset.completionArtifact));
    node.querySelector(".rc-ledger-toggle")?.addEventListener("click", event => {
      const button = event.currentTarget;
      const expanded = button.dataset.expanded === "true";
      button.dataset.expanded = String(!expanded);
      button.textContent = expanded ? "分项" : "收起";
      const ledgerEl = node.querySelector(".run-completion-ledger");
      if (ledgerEl) ledgerEl.hidden = expanded;
    });
    container.appendChild(node);
    this.elements.set(event.event_id, node);
    _autoScroll();
  }
  _container(event) {
    // Direct-model tool work belongs in the main transcript. Multi-model and
    // subagent coordination keeps its dedicated collaboration channel.
    if (event.channel_id === "host_models" && event.mode === "default") return document.getElementById("chatArea");
    if (event.channel_id === "host_models") return document.getElementById("chatAreaLower");
    if (event.channel_id === "user_host") return document.getElementById("chatArea");
    return null;
  }
  // Derive a meaningful collapsible-group title from the first event seen for
  // a task.  Peri tasks carry a real title; MOA reference calls only carry a
  // target_label; aggregation carries a summary.  Fall back to the event type
  // label so a closed group is never just an anonymous "子任务".
  _subtaskTitle(event) {
    const payload = event.payload || {};
    if (payload.title) return payload.title;
    if (payload.scope) return payload.scope;
    if (payload.target_label) return payload.target_label;
    if (event.type === "host_dispatch") {
      const summary = String(payload.summary || payload.request_markdown || "").trim();
      if (summary) return summary.length > 40 ? summary.slice(0, 40) + "…" : summary;
    }
    if (event.type === "host_aggregation") {
      const summary = String(payload.summary || "").trim();
      if (summary) return summary.length > 40 ? summary.slice(0, 40) + "…" : summary;
    }
    return ({host_thinking:"主持人思考", reference_response:"参考回复", tool_call:"工具调用", tool_result:"工具结果"}[event.type]) || "子任务";
  }
  // Render sub-agent coordination as cards in the right-panel "子任务" window.
  // Each task gets one card (title + status + summary); clicking the card
  // expands its event stream inline.  Keyed by task_id; cards are reused and
  // updated across events for the same task.
  _subtaskContainer(event) {
    if (event.channel_id !== "host_models" || event.mode === "default") return null;
    if (!event.task_id) return null;
    if (event.type === "run_started" || event.type === "user_message"
        || event.type === "run_completed" || event.type === "host_response") return null;
    const list = document.getElementById("rpSubtasks");
    if (!list) return null;
    const key = "subtask_" + event.run_id + "_" + event.task_id;
    let card = this.subtaskContainers && this.subtaskContainers.get(key);
    if (card && document.getElementById(key)) {
      // Refresh the card meta (latest status) on subsequent events.
      const meta = card.querySelector(".subtask-meta");
      if (meta) meta.textContent = this._subtaskStatus(event);
      return card;
    }
    card = document.createElement("details");
    card.id = key;
    card.className = "subtask-card";
    card.dataset.runId = event.run_id;
    card.dataset.taskId = event.task_id;
    card.innerHTML =
      '<summary class="subtask-head"><span class="subtask-ava">◇</span>' +
      '<span class="subtask-title">' + escapeHtml(this._subtaskTitle(event)) + '</span>' +
      '<span class="subtask-meta">' + escapeHtml(this._subtaskStatus(event)) + '</span></summary><div class="subtask-body"></div>';
    // Cards are ordered by run (newest last within the window).
    const siblingCards = [...list.querySelectorAll(".subtask-card")];
    const later = siblingCards.find(item => {
      const keyRun = (item.dataset.runId || "");
      return keyRun > event.run_id;
    });
    list.insertBefore(card, later || null);
    if (!this.subtaskContainers) this.subtaskContainers = new Map();
    this.subtaskContainers.set(key, card);
    return card;
  }
  // Short human-readable status for a subtask card based on the latest event.
  _subtaskStatus(event) {
    if (!event || !event.type) return "";
    const status = event.status || "";
    if (event.type === "host_aggregation") return "聚合";
    if (event.type === "host_dispatch") return "征询";
    if (event.type === "subtask_assignment") return status === "started" ? "已分派" : "分派";
    if (event.type === "subagent_progress") return "进行中";
    if (event.type === "subagent_response" || event.type === "reference_response") return "已回复";
    if (event.type === "subagent_tool_call") return "调工具";
    if (event.type === "tool_call") return "调工具";
    if (event.type === "tool_result") return event.payload?.is_error ? "工具报错" : "工具完成";
    return status ? String(status) : "";
  }
  _presentation(event) {
    switch (event.type) {
      case "user_message": return {icon:"Y", label:"你", kind:"user", markdown:event.payload.markdown || ""};
      case "run_started": return {icon:"◌", label:"运行中", kind:"system", markdown:""};
      case "host_thinking": return {icon:"◌", label:event.actor.label || "主持人", kind:"host", html:true, markdown:thinkRowHtml(event.payload.text || "")};
      case "host_response": return {icon:"M", label:event.actor.label || "主持人", kind:"host", markdown:event.payload.markdown || ""};
      case "artifact": return {
        icon:"▣", label:event.payload.title || "运行产物", kind:"tool", html:true,
        markdown:'<details class="timeline-tool artifact-details" data-artifact-id="' + escapeHtml(event.payload.artifact_id || "") + '"><summary>▣ ' + escapeHtml(event.payload.title || "运行产物")
          + '</summary><div class="details-body"><div class="artifact-summary">' + escapeHtml(event.payload.summary || "已持久化") + '</div><button type="button" class="plain-small artifact-load">读取内容</button><pre class="artifact-content" hidden></pre>'
          + '<br><small>' + escapeHtml(event.payload.kind || "artifact") + ' · '
          + escapeHtml(formatTokens(event.payload.size_bytes || 0)) + ' bytes · ' + escapeHtml(event.payload.artifact_id || "")
          + '</small></div></details>'
      };
      case "host_dispatch": return {icon:"↗", label:"主持人 → " + (event.payload.target_label || event.actor.label), kind:"host", markdown:event.payload.request_markdown || ""};
      case "reference_started": return {icon:"◌", label:event.actor.label || "参考模型", kind:"reference_model streaming", markdown:"正在分析主持人的参考请求…"};
      case "reference_response": {
        const md = event.payload.markdown || "";
        const preview = md.length > 200 ? md.substring(0, 200) + "…" : md;
        return {icon:"✓", label:event.actor.label || "参考模型", kind:"reference_model", markdown:md, preview:preview};
      }
      case "subtask_assignment": return {icon:"↗", label:"主持人分派", kind:"host", markdown:"**" + escapeHtml(event.payload.title || "子任务") + "**\n" + escapeHtml(event.payload.scope || "")};
      case "subagent_progress": return {icon:"◌", label:event.actor.label || "子 Agent", kind:"subagent", markdown:event.payload.text || ""};
      case "subagent_tool_call": return {
        icon:"🔧", label:event.actor.label + " · " + (event.payload.name || "工具"), kind:"tool", html:true,
        markdown:toolRowHtml((event.actor.label || "") + " · " + (event.payload.name || "工具"), null, event.run_id, event.payload.tool_call_id, JSON.stringify(event.payload.input || {}, null, 2)),
      };
      case "subagent_tool_result": return {
        icon:event.payload.is_error ? "❌" : "✓", label:event.actor.label + " · " + (event.payload.name || "工具"), kind:"tool", html:true,
        markdown:toolRowHtml((event.actor.label || "") + " · " + (event.payload.name || "工具"), event.payload.is_error ? "err" : "ok", event.run_id, event.payload.tool_call_id, "", String(event.payload.result || ""), event.payload.is_error, event.payload.display_summary, event.payload.metadata, event.artifact_ids),
      };
      case "subagent_response": return {icon:"✓", label:event.actor.label || "子 Agent", kind:"subagent", markdown:event.payload.markdown || ""};
      case "host_review": return {icon:"◎", label:"主持人审阅", kind:"host", markdown:event.payload.markdown || ""};
      case "host_aggregation": return {icon:"◈", label:"主持人聚合", kind:"host", markdown:event.payload.summary || "正在整合参考信息…"};
      case "context_compacted": {
        const omitted = Number(event.payload.omitted_count || 0);
        const tail = Number(event.payload.tail_count || 0);
        return {
          icon:"◇", label:"上下文已整理", kind:"system", html:true,
          markdown:'<section class="context-compaction-note"><strong>已整理较早的对话上下文</strong><span>保留最近 '
            + escapeHtml(String(tail)) + ' 条，省略 ' + escapeHtml(String(omitted))
            + ' 条；完整会话记录仍保留在历史中。</span></section>',
        };
      }
      case "approval_request": return {
        icon:"⚠", label:"需要你的确认", kind:"system", html:true,
        markdown:'<section class="approval-card" data-approval-id="' + escapeHtml(event.payload.approval_id || "")
          + '" data-run-id="' + escapeHtml(event.payload.run_id || event.run_id || "") + '">'
          + '<strong>' + escapeHtml(event.payload.tool_name || "工具") + '</strong><span> · ' + escapeHtml(event.payload.danger_level || "medium") + '</span>'
          + '<p>' + escapeHtml(event.payload.description || "此操作可能产生副作用。") + '</p>'
          + '<pre><code>' + escapeHtml(JSON.stringify(event.payload.input || {}, null, 2)) + '</code></pre>'
          + '<div class="approval-actions"><button type="button" data-approval-decision="approve">允许执行</button><button type="button" data-approval-decision="deny">拒绝</button></div>'
          + '</section>'
      };
      case "run_error": {
        const reason = event.payload.stop_reason || event.payload.code || "failed";
        const labels = {cancelled:"已取消", max_turns:"达到轮次上限", token_limit:"达到 Token 上限", wall_time:"达到时间上限", engine_error:"模型错误", failed:"运行失败", verification_required:"需要验证", verification_retry_limit:"验证重试上限"};
        const verification = event.payload.verification || {};
        const attemptText = verification.attempts ? `（第 ${verification.attempts}/${verification.max_attempts || "?"} 次）` : "";
        const message = event.payload.message || (reason === "verification_required" ? "代码已修改，但尚未获得通过的验证证据。" : reason === "verification_retry_limit" ? "验证连续失败，已达到自动修复重试上限。" : labels[reason] || "未知错误");
        const canRetry = reason === "verification_required" || reason === "verification_retry_limit";
        const evidence = verification.last_evidence || {};
        const counts = evidence.counts || {};
        const evidenceBits = [];
        const paths = (verification.mutations || []).map(item => item.path).filter(Boolean);
        if (paths.length) evidenceBits.push('<span>文件：' + escapeHtml([...new Set(paths)].slice(0, 4).join("、")) + '</span>');
        if (counts.passed || counts.failed) evidenceBits.push('<span>测试：' + escapeHtml([counts.passed ? counts.passed + " passed" : "", counts.failed ? counts.failed + " failed" : ""].filter(Boolean).join(" · ")) + '</span>');
        if (evidence.status === "timed_out") evidenceBits.push('<span>最近验证：超时</span>');
        else if (evidence.status === "cancelled") evidenceBits.push('<span>最近验证：已取消</span>');
        else if (evidence.exit_code !== undefined && evidence.exit_code !== null) evidenceBits.push('<span>退出码：' + escapeHtml(String(evidence.exit_code)) + '</span>');
        const evidenceHtml = evidenceBits.length ? '<div class="run-error-evidence">' + evidenceBits.join("") + '</div>' : "";
        const detail = event.payload.detail;
        const detailHtml = detail
          ? '<details class="run-error-detail"><summary>查看错误详情</summary><pre><code>' + escapeHtml(detail) + '</code></pre></details>'
          : "";
        const markdown = canRetry
          ? '<section class="run-error-card" data-run-id="' + escapeHtml(event.run_id || "") + '"><strong>⚠ ' + escapeHtml(labels[reason] || "验证未完成") + ' ' + escapeHtml(attemptText) + '</strong><p>' + escapeHtml(message) + '</p>' + evidenceHtml + '<button type="button" data-verification-retry="' + escapeHtml(event.run_id || "") + '">继续修复并验证</button></section>'
          : message + detailHtml;
        // html=true whenever we may emit markup (retry card or error detail).
        return {icon:reason === "cancelled" ? "○" : "⚠", label:labels[reason] || "运行错误", kind:"system", html:canRetry || Boolean(detail), markdown};
      }
      default: return {icon:"•", label:event.actor.label || "事件", kind:"system", markdown:""};
    }
  }
  render(event) {
    let container = this._container(event);
    if (!container) return;
    // Sub-agent coordination events nest under their task's collapsible group.
    const subTaskContainer = this._subtaskContainer(event);
    if (subTaskContainer) {
      container = subTaskContainer.querySelector(".subtask-body");
    }
    container.querySelector(".empty-state")?.remove();
    const existing = this.elements.get(event.event_id);
    const view = this._presentation(event);
    this._prune(container);
    const turnBd = this._turnAnchor(event);
    const target = turnBd || container;

    // user_host events → render as chat bubbles (msg), not timeline cards
    if (event.channel_id === "user_host") {
      if (event.type === "approval_request" || event.type === "approval_resolved") {
        this._renderApproval(event, container);
        return;
      }
      if (event.type === "run_completed") {
        this._finishRun(event.run_id);
        this._renderRunCompletion(event, container);
        const turnNode = this.turnRuns.get(event.run_id || "");
        if (turnNode) {
          turnNode.dataset.replyState = "done";
          const footer = container.querySelector('.run-completion[data-run-id="' + event.run_id + '"]');
          if (footer) {
            footer.dataset.result = "ok";
            turnNode.querySelector(".turn-bd").appendChild(footer);
          }
          this._closeTurn();
        }
        return;
      }
      if (event.type === "run_started") {
        // A new run ends any live thinking preview from the previous one.
        settleThinking(container);
        this._absorbTrailingFlat(container);
        return;
      }
      if (event.type === "run_error") {
        this._finishRun(event.run_id);
      }
      // Host thinking → compact disclosure row with live preview
      if (event.type === "host_thinking") {
        const text = event.payload.text || "";
        if (!text.trim()) return; // Hermes self-gate: no content → no header
        if (existing) {
          const content = existing.querySelector(".thinking-content");
          if (content) {
            content.textContent = text;
          }
          return;
        }
        const node = document.createElement("div");
        node.className = "think-block thinking thinking-preview";
        node.dataset.eventId = event.event_id;
        node.dataset.thinkKey = event.run_id || "";
        if (event.task_id) node.dataset.taskId = event.task_id;
        if (event.sequence) node.dataset.anchorSeq = String(event.sequence);
        this._setReplyState(event.run_id, "streaming");
        node.innerHTML = '<div class="think-body">' + thinkRowHtml(text) + '</div>';
        this._insertBySeq(target, node, event);
        this.elements.set(event.event_id, node);
        startThinkTimer(node, event.run_id || "");
        _autoScroll();
        return;
      }
      // user_message → user bubble
      if (event.type === "user_message") {
        if (existing) return; // user messages don't update
        const node = document.createElement("div");
        node.className = "msg user msg-centered";
        node.dataset.messageRole = "user";
        node.dataset.eventId = event.event_id;
        if (event.task_id) node.dataset.taskId = event.task_id;
        if (event.sequence) node.dataset.anchorSeq = String(event.sequence);
        node.appendChild(userCardHtml(event.payload));
        this._insertBySeq(target, node, event);
        this.elements.set(event.event_id, node);
        addCopyHandlers(target);
        _autoScroll();
        return;
      }
      // host_response → assistant bubble (with streaming support).
      // Reply text streams into an open segment; a tool/approval row arriving
      // mid-reply seals that segment (in _renderToolPart) so the next deltas
      // open a new segment after the tool row — the reply visibly continues
      // below the tool, matching observation order.
      if (event.type === "host_response") {
        if (existing) {
          const seg = this._openReplySegment(target, event);
          seg.text += (event.payload.markdown || "").slice(seg.text.length);
          const prior = this._replyText.get(event.event_id) || "";
          this._replyText.set(event.event_id, prior + (event.payload.markdown || "").slice(prior.length));
          const block = seg.seg.querySelector(".block-text");
          if (block) block.innerHTML = renderTimelineMarkdown(seg.text, event.status === "streaming");
          addCopyHandlers(seg.seg);
          return;
        }
        // Answer begins → the thinking preview for this run is done.
        settleThinking(container);
        const node = document.createElement("div");
        node.className = "msg assistant msg-centered";
        node.dataset.messageRole = "assistant";
        node.dataset.eventId = event.event_id;
        if (event.task_id) node.dataset.taskId = event.task_id;
        if (event.sequence) node.dataset.anchorSeq = String(event.sequence);
        this._setReplyState(event.run_id, "streaming");
        node.innerHTML = '<div class="message-label">Modus</div><div class="block-text">'
          + renderTimelineMarkdown(view.markdown, event.status === "streaming")
          + '</div>';
        this._insertBySeq(target, node, event);
        this.elements.set(event.event_id, node);
        this._replySegs.set(event.event_id, {seg: node, text: (event.payload.markdown || "")});
        this._replyText.set(event.event_id, (event.payload.markdown || ""));
        this._runCursor.set(event.run_id || "", node);
        addCopyHandlers(target);
        _autoScroll();
        return;
      }
      // approval_request → approval card with buttons
      if (event.type === "approval_request") {
        this._renderApproval(event, container);
        return;
      }
      // Fallback for other user_host events → inline system message
      if (!existing) {
        const node = document.createElement("div");
        if (event.type === "run_error") {
          this._setReplyState(event.run_id, "done");
          // Verification failures keep the interactive retry card; all other
          // run_error events get the unified run-result footer.
          const canRetry = String(event.payload?.stop_reason || "") === "verification_required"
            || String(event.payload?.stop_reason || "") === "verification_retry_limit";
          if (canRetry && view.markdown) {
            node.className = "run-error-block";
            node.dataset.eventId = event.event_id;
            node.dataset.runId = event.run_id || "";
            node.innerHTML = view.markdown;
            target.appendChild(node);
            this.elements.set(event.event_id, node);
            addCopyHandlers(node);
            _autoScroll();
            return;
          }
          const cancelled = String(event.payload?.stop_reason || "") === "cancelled";
          node.className = "run-result";
          node.dataset.eventId = event.event_id;
          node.dataset.runId = event.run_id || "";
          node.dataset.result = cancelled ? "cancel" : "err";
          node.dataset.absorbable = "true";
          const mark = cancelled ? "○" : "✗";
          const title = cancelled ? "已取消" : "运行未完成";
          const sub = cancelled ? "运行被中断，未产生最终结果" : String(event.payload?.message || "运行异常终止");
          node.innerHTML = '<div class="run-result-mark" aria-hidden="true">' + mark + '</div>'
            + '<div class="run-result-main"><span class="run-result-title">' + title + '</span>'
            + '<span class="run-result-sub">' + escapeHtml(sub) + '</span></div>';
          target.appendChild(node);
          this.elements.set(event.event_id, node);
          _autoScroll();
          return;
        }
        node.className = "msg system";
        node.dataset.eventId = event.event_id;
        if (event.task_id) node.dataset.taskId = event.task_id;
        node.innerHTML = '<div class="block-text" style="font-size:10px;color:var(--text-tertiary);padding:2px 12px;">'
          + (view.html ? view.markdown : escapeHtml(view.markdown || ""))
          + '</div>';
        target.appendChild(node);
        this.elements.set(event.event_id, node);
        _autoScroll();
      }
      return;
    }

    // Tool call/result share one disclosure row while remaining separate
    // audited events. This prevents duplicate cards and timer mismatches.
    if (event.type === "tool_call" || event.type === "tool_result" || event.type === "subagent_tool_call" || event.type === "subagent_tool_result") {
      this._renderToolPart(event, container);
      return;
    }
    // ── host_models events → collab bubbles (not timeline cards) ──
    if (existing) {
      const md = view.markdown || "";
      const rendered = view.html ? view.markdown : renderTimelineMarkdown(view.markdown, event.status === "streaming");
      // If completed with long content, rebuild with preview/full split
      if (md.length > 300 && event.status !== "streaming" && !existing.querySelector(".collab-body-full")) {
        const previewHtml = renderTimelineMarkdown(md.substring(0, 200) + "…", false);
        // Preserve the header if present
        const header = existing.querySelector(".collab-header");
        const headerHtml = header ? header.outerHTML : "";
        existing.innerHTML = headerHtml
          + this._collabCollapseHtml(previewHtml, rendered);
        this._wireCollabToggle(existing);
      } else {
        // Update body content in existing layout
        const body = existing.querySelector(".collab-body") || existing.querySelector(".collab-body-full") || existing.querySelector(".collab-body-preview");
        if (body) {
          body.innerHTML = rendered;
        }
      }
      addCopyHandlers(existing);
      return;
    }
    const node = document.createElement("div");
    node.className = "collab-msg " + view.kind;
    node.dataset.eventId = event.event_id;
    if (event.task_id) node.dataset.taskId = event.task_id;

    // Compact action lines for host dispatch/aggregation
    if (event.type === "host_dispatch" || event.type === "host_aggregation") {
      let summary = event.payload.summary || event.payload.request_markdown || view.markdown || "";
      // For host_dispatch, prepend the target model label
      if (event.type === "host_dispatch" && event.payload.target_label) {
        summary = "→ " + event.payload.target_label + " " + summary;
      }
      node.innerHTML = '<span class="collab-action">' + view.icon + ' ' + escapeHtml(summary.substring(0, 120)) + '</span>';
    }
    // Reference model responses — collapsible with preview
    else if (event.type === "reference_response" || event.type === "reference_started") {
      const label = view.label || "参考模型";
      const md = view.markdown || "";
      const rendered = view.html ? view.markdown : renderTimelineMarkdown(view.markdown, event.status === "streaming");
      const header = '<div class="collab-header"><span class="collab-avatar">' + view.icon + '</span><span class="collab-name">' + escapeHtml(label) + '</span></div>';
      // Show preview + expand for content over ~300 chars
      if (md.length > 300 && event.status !== "streaming") {
        node.innerHTML = header + this._collabCollapseHtml(
          renderTimelineMarkdown(md.substring(0, 200) + "…", false), rendered);
      } else {
        node.innerHTML = header + '<div class="collab-body">' + rendered + '</div>';
      }
    }
    // System / errors
    else if (event.type === "run_error") {
      const detail = event.payload.detail;
      node.innerHTML = '<div class="collab-body error">⚠ ' + escapeHtml(event.payload.message || "未知错误")
        + (detail ? '<details class="run-error-detail"><summary>查看错误详情</summary><pre><code>' + escapeHtml(detail) + '</code></pre></details>' : '')
        + '</div>';
    }
    // Fallback
    else {
      node.innerHTML = '<div class="collab-body">' + (view.html ? view.markdown : renderTimelineMarkdown(view.markdown, false)) + '</div>';
    }

    const siblings = [...container.querySelectorAll("[data-run-id='" + CSS.escape(event.run_id) + "']")];
    const later = siblings.find(item => (this.store.event(item.dataset.eventId)?.sequence || Infinity) > event.sequence);
    container.insertBefore(node, later || null);
    this.elements.set(event.event_id, node);
    addCopyHandlers(node);
    // Expand/collapse toggle for reference responses
    this._wireCollabToggle(node);
    container.scrollTop = container.scrollHeight;
  }
  // Preview/full split for long collab content, with an expand/collapse toggle.
  _collabCollapseHtml(previewHtml, fullHtml) {
    return '<div class="collab-body-preview">' + previewHtml + '</div>'
      + '<div class="collab-body-full" style="display:none;">' + fullHtml + '</div>'
      + '<div class="collab-toggle" data-expanded="false">展开全部 ▾</div>';
  }
  _wireCollabToggle(root) {
    root.querySelector(".collab-toggle")?.addEventListener("click", function() {
      const expanded = this.dataset.expanded === "true";
      this.dataset.expanded = String(!expanded);
      this.textContent = expanded ? "展开全部 ▾" : "收起 ▴";
      const preview = root.querySelector(".collab-body-preview");
      const full = root.querySelector(".collab-body-full");
      if (preview) preview.style.display = expanded ? "" : "none";
      if (full) full.style.display = expanded ? "none" : "";
    });
  }
  _prune(container) {
    if (!container) return;
    const max = MAX_MOUNTED_NODES;
    const children = Array.from(container.children).filter(node => !node.classList.contains("timeline-expand-earlier"));
    const cold = this.coldNodes.get(container) || [];
    const live = children.length;
    if (live <= max) {
      if (cold.length) this._syncExpandAffordance(container, cold.length);
      return;
    }
    const overflow = live - max;
    for (let i = 0; i < overflow; i++) {
      const node = children[i];
      // Never detach a live streaming thinking preview or a pending approval.
      if (node.classList.contains("thinking-preview") || node.querySelector(".thinking-preview")) continue;
      if (node.dataset.approvalId || node.querySelector("[data-approval-id]")) continue;
      node.remove();
      cold.push(node);
    }
    this.coldNodes.set(container, cold);
    this._syncExpandAffordance(container, cold.length);
  }
  _syncExpandAffordance(container, count) {
    let button = container.querySelector(".timeline-expand-earlier");
    if (count <= 0) {
      if (button) button.remove();
      return;
    }
    if (!button) {
      button = document.createElement("button");
      button.type = "button";
      button.className = "timeline-expand-earlier";
      button.onclick = () => this._rehydrateEarlier(container);
      container.insertBefore(button, container.firstChild);
    }
    button.textContent = "▴ 展开更早 " + count + " 条";
  }
  _rehydrateEarlier(container) {
    const cold = this.coldNodes.get(container) || [];
    if (!cold.length) return;
    this.coldNodes.set(container, []);
    const anchor = container.querySelector(".timeline-expand-earlier");
    cold.forEach(node => container.insertBefore(node, anchor || null));
    this._syncExpandAffordance(container, 0);
    container.scrollTop = 0;
  }
  _pruneAll() {
    this._prune(document.getElementById("chatArea"));
    this._prune(document.getElementById("chatAreaLower"));
  }
}
const eventStore = new EventStore();
const timelineRenderer = new TimelineRenderer(eventStore);
// The KANBAN board owns the right panel now; the WorkbenchStore data layer is
// kept for projection/cursor semantics but renders into the board via
// kanban.js's prototype patch (containers are intentionally null).
const workbenchStore = new ModusWorkbench.WorkbenchStore({
  taskContainer: null,
  workspaceContainer: null,
  artifactContainer: null,
  runContainer: null,
  reviewContainer: null,
  onArtifact: artifactId => requestArtifactContent(artifactId),
  onRunSelected: runId => requestWorkbenchRun(runId),
  onRunReplay: runId => replayRun(runId),
  canReplay: () => !agentRunPending && !pendingVerificationRetry
    && !controlMutationPending && !replayingRunId,
});

// ─── Content-driven collaboration area ───
// The lower chat pane is shown only while it has rendered events, so an idle
// Peri/MOA session never leaves an empty strip between the main chat and the
// composer.  setMode() still grants MOA/Peri permission to show it; a Mutation
// observer hides it the moment its content is cleared.
const lowerChatArea = document.getElementById("chatAreaLower");
const chatDividerEl = document.getElementById("chatDivider");
function syncLowerVisibility() {
  if (!lowerChatArea) return;
  const hasContent = lowerChatArea.childElementCount > 0;
  const mode = currentMode || "default";
  const permitted = mode !== "default";
  const visible = hasContent && permitted;
  lowerChatArea.style.display = visible ? "flex" : "none";
  if (chatDividerEl) chatDividerEl.style.display = visible ? "" : "none";
}
if (lowerChatArea && typeof MutationObserver !== "undefined") {
  new MutationObserver(() => syncLowerVisibility()).observe(lowerChatArea, { childList: true, subtree: true });
}
// Re-evaluate when the mode changes too (setMode flips the permission).
if (typeof window.__modusModeChangeHook === "undefined") {
  window.__modusModeChangeHook = true;
  window.addEventListener("modus:mode-change", () => syncLowerVisibility());
}

function addCopyHandlers(container) {
  container.querySelectorAll(".cb-copy").forEach(btn => {
    btn.onclick = () => {
      try {
        const code = decodeURIComponent(btn.dataset.code);
        navigator.clipboard.writeText(code);
        btn.classList.add("copied");
        btn.innerHTML = COPIED_ICON_SVG;
        btn.title = "已复制";
        setTimeout(() => { btn.classList.remove("copied"); btn.innerHTML = COPY_ICON_SVG; btn.title = "复制"; }, 2000);
      } catch(e) { btn.innerHTML = "❌"; }
    };
  });
  container.querySelectorAll("[data-approval-decision]").forEach(btn => {
    btn.onclick = () => {
      const card = btn.closest("[data-approval-id]");
      const approvalId = card?.dataset.approvalId;
      const runId = card?.dataset.runId;
      if (!approvalId || !runId || !ws || ws.readyState !== WebSocket.OPEN) return;
      const decision = btn.dataset.approvalDecision;
      ws.send(JSON.stringify({type:"approval_response", run_id:runId, approval_id:approvalId, decision}));
      timelineRenderer.markApprovalDecision(approvalId, decision);
    };
  });
  container.querySelectorAll("[data-choice-card] .choice-btn").forEach(btn => {
    btn.onclick = () => submitChoice(btn);
  });
  container.querySelectorAll("[data-verification-retry]").forEach(btn => {
    const retryRunId = String(btn.dataset.verificationRetry || "");
    if (pendingVerificationRetry?.priorRunId === retryRunId) {
      setVerificationRetryButtonState(retryRunId, "pending");
    } else if (activeVerificationRetryPriorRunId === retryRunId) {
      setVerificationRetryButtonState(retryRunId, "running");
    } else if (verificationRetryConsumedRuns.has(retryRunId)) {
      setVerificationRetryButtonState(retryRunId, "settled");
    }
    btn.onclick = () => {
      const runId = btn.dataset.verificationRetry;
      beginVerificationRetry(runId);
    };
  });
  container.querySelectorAll(".artifact-load").forEach(btn => {
    btn.onclick = () => {
      const details = btn.closest("[data-artifact-id]");
      const artifactId = details?.dataset.artifactId;
      if (!artifactId || !ws || ws.readyState !== WebSocket.OPEN) return;
      btn.disabled = true; btn.textContent = "读取中…";
      if (!requestArtifactContent(artifactId)) {
        btn.disabled = false; btn.textContent = "重试读取";
      }
    };
  });
  // Tool rows that persisted an oversized full result open it in the viewer.
  container.querySelectorAll(".tool-result-artifact").forEach(btn => {
    btn.onclick = () => {
      const artifactId = btn.dataset.artifactId;
      if (!artifactId) return;
      if (!requestArtifactContent(artifactId)) btn.disabled = true;
    };
  });
  // Message hover action bar (copy whole message / refill-and-resend user turn).
  container.querySelectorAll(".msg").forEach(node => {
    if (node.querySelector(".msg-bar")) return;
    const bar = document.createElement("div");
    bar.className = "msg-bar";
    bar.innerHTML = '<button type="button" data-msg-copy title="复制整条">' + COPY_ICON_SVG + '</button>'
      + (node.classList.contains("user") ? '<button type="button" data-msg-resend title="回填重发">↻</button>' : '');
    node.appendChild(bar);
  });
  container.querySelectorAll("[data-msg-copy]").forEach(btn => {
    btn.onclick = () => {
      const node = btn.closest(".msg");
      const text = (node?.querySelector(".block-text")?.textContent || "").trim();
      if (!text) return;
      navigator.clipboard.writeText(text);
      btn.classList.add("copied");
      btn.innerHTML = COPIED_ICON_SVG;
      setTimeout(() => { btn.classList.remove("copied"); btn.innerHTML = COPY_ICON_SVG; }, 2000);
    };
  });
  container.querySelectorAll("[data-msg-resend]").forEach(btn => {
    btn.onclick = () => {
      const node = btn.closest(".msg");
      if (!node) return;
      const text = node.querySelector(".block-text")?.textContent || "";
      const input = document.getElementById("input");
      if (!input) return;
      input.value = text;
      input.focus();
    };
  });
  // User message cards: two-line preview → expand/edit/copy/outside-click close.
  container.querySelectorAll(".user-card").forEach(card => {
    if (typeof wireUserCardInteractions === "function") wireUserCardInteractions(card);
  });
}

function artifactMetadata(artifactId) {
  const id = String(artifactId || "");
  const run = typeof workbenchStore !== "undefined" ? workbenchStore.latestRun() : null;
  const artifact = (run?.artifacts || []).find(item => String(item.artifact_id || "") === id);
  if (artifact) return artifact;
  const details = [...document.querySelectorAll("[data-artifact-id]")]
    .find(node => String(node.dataset.artifactId || "") === id);
  return {
    artifact_id:id,
    title:details?.querySelector("summary")?.textContent?.replace(/^▣\s*/, "").trim() || "运行产物",
    kind:"artifact",
  };
}
function requestArtifactContent(artifactId, opts) {
  const id = String(artifactId || "");
  const requestedSessionId = String(currentDbId || "");
  if (!id) return false;
  const metadata = artifactMetadata(id);
  if (!ws || ws.readyState !== WebSocket.OPEN || !requestedSessionId) {
    if (typeof openArtifactViewer === "function") openArtifactViewer(metadata);
    if (typeof renderArtifactViewerError === "function") {
      renderArtifactViewerError(
        requestedSessionId ? "Desktop 连接已断开，请恢复连接后重试。" : "当前会话尚未保存，无法读取运行产物。",
        metadata,
      );
    }
    return false;
  }
  const requestId = nextTransientRequestId("artifact");
  const silent = Boolean(opts && opts.silent);
  // The viewer is a single focused surface. Opening or retrying an artifact
  // supersedes every older read intent so a late response cannot reappear.
  // A silent request (right-panel document window) skips the modal viewer but
  // still flows through the same request/settle machinery.
  pendingArtifactRequests.clear();
  pendingArtifactRequests.set(id, {
    requestId, sessionId:requestedSessionId, artifactId:id, silent,
  });
  if (!silent && typeof openArtifactViewer === "function") openArtifactViewer(metadata);
  ws.send(JSON.stringify({
    type:"artifact_get", artifact_id:id,
    request_id:requestId, session_id:requestedSessionId,
  }));
  return true;
}

function renderInlineArtifactContent(artifact, responseArtifactId="") {
  const artifactId = String(artifact?.artifact_id || responseArtifactId || "");
  document.querySelectorAll("[data-artifact-id]").forEach(details => {
    if (details.dataset.artifactId !== artifactId) return;
    const content = details.querySelector(".artifact-content");
    if (content) { content.textContent = String(artifact?.content ?? ""); content.hidden = false; }
    const button = details.querySelector(".artifact-load");
    if (button) { button.disabled = false; button.textContent = "重新读取"; }
    details.open = true;
  });
  // The document window re-renders once content arrives so plan/design/spec
  // artifacts fill in after the metadata-only announcement.
  const docKind = String(artifact?.kind || "").toLowerCase();
  if (["plan", "design", "spec"].includes(docKind) && typeof ModusWindows?.renderDocument === "function") {
    ModusWindows.renderDocument(artifact);
  }
}
