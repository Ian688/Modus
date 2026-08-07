/* Kanban adapter + board renderer: the right panel is a flow board, not tabs.

   Every view here consumes the authoritative ``semantic-run.v1`` projection
   (goal, outcome, phases, activities, evidence, metrics) which default / MOA /
   Peri all produce with the same shape.  Nothing reads raw events and nothing
   branches on mode — the board is the mode-independent "perspective lens" over
   all Agent work in a session.
*/
(function (global) {
  "use strict";

  const PHASE_LABELS = {
    analyzing: "分析",
    executing: "执行",
    verifying: "验证",
    reviewing: "审阅",
    approving: "待审批",
    delivering: "产出",
    completed: "完成",
    failed: "失败",
  };
  const CATEGORY_LABELS = {
    read: "读",
    write: "写",
    command: "命令",
    verify: "验证",
    message: "消息",
    tool: "工具",
    system: "系统",
  };
  const STATUS_MARK = {
    ok: "✓",
    error: "✗",
    active: "◌",
    cancelled: "–",
  };
  // Column order + labels for the 5-column board.
  const COLUMNS = [
    { key: "todo", label: "待处理" },
    { key: "analyzing", label: "分析中" },
    { key: "executing", label: "执行中" },
    { key: "verifying", label: "验证中" },
    { key: "completed", label: "已完成" },
  ];
  const COLUMN_OF_PHASE = {
    analyzing: "analyzing",
    executing: "executing",
    verifying: "verifying",
    approving: "analyzing",
    reviewing: "executing",
    delivering: "executing",
    completed: "completed",
    failed: "completed",
  };
  // Legacy window names the router may still target -> board action.
  const LEGACY_ROUTES = {
    tasks: "detail",
    subtasks: "detail",
    runs: "detail",
    review: "detail",
    activity: "detail",
    focus: "detail",
    document: "document",
    browser: "preview",
    workspace: "empty",
  };

  let activeStore = null;
  let boardContainer = null;
  let openDrawerRunId = null;
  let activeColumn = null;

  function html(value) {
    const node = document.createElement("span");
    node.textContent = value === null || value === undefined ? "" : String(value);
    return node.innerHTML;
  }

  // ─── Pure column derivation ───
  function lastActivityPhase(semantic) {
    const activities = (semantic && semantic.activities) || [];
    if (!activities.length) return null;
    let latest = null;
    activities.forEach(function (activity) {
      if (!latest || Number(activity.sequence) > Number(latest.sequence)) latest = activity;
    });
    return String(latest.phase || "");
  }

  function columnOfRun(run) {
    if (!run) return "todo";
    const state = String(run.state || "");
    if (state === "completed") return "completed";
    if (state === "failed" || state === "cancelled" || state === "interrupted") return "completed";
    const semantic = run.semantic || {};
    const phase = lastActivityPhase(semantic);
    const column = phase ? (COLUMN_OF_PHASE[phase] || "analyzing") : "analyzing";
    return column;
  }

  // ─── Counting helpers ───
  function countByCategory(activities) {
    const counts = {};
    (activities || []).forEach(function (activity) {
      const key = activity.category || "tool";
      counts[key] = (counts[key] || 0) + 1;
    });
    return counts;
  }

  // ─── Card face ───
  function cardHtml(run) {
    const semantic = (run && run.semantic) || {};
    const goal = semantic.goal || {};
    const outcome = semantic.outcome || {};
    const counts = countByCategory(semantic.activities);
    const metrics = semantic.metrics || {};
    const bits = [];
    if (counts.read) bits.push("读 " + counts.read);
    if (counts.write) bits.push("写 " + counts.write);
    if (counts.command) bits.push("命令 " + counts.command);
    const summary = bits.length ? bits.join(" · ") : "";
    const badges = [];
    if (outcome.verified === true) badges.push('<span class="kb-badge kb-badge-ok">✓ 已验证</span>');
    if (outcome.status === "failed" || outcome.status === "incomplete") badges.push('<span class="kb-badge kb-badge-err">✗ 失败</span>');
    if (outcome.requires_user_action === true || outcome.attention === "action_required") {
      badges.push('<span class="kb-badge kb-badge-approval">⚠ 待审批</span>');
    }
    const workerCount = (run.tasks || []).filter(function (task) { return String(task.task_kind || "") !== "root"; }).length;
    const metaBits = [];
    if (workerCount) metaBits.push(workerCount + " worker");
    if (metrics.duration_seconds) metaBits.push(Number(metrics.duration_seconds).toFixed(1) + "s");
    if (metrics.tokens) metaBits.push(Number(metrics.tokens).toLocaleString("zh-CN") + " tok");
    const meta = metaBits.length ? '<span class="kb-card-meta">' + metaBits.map(html).join(" · ") + "</span>" : "";
    const mode = run.mode ? '<span class="kb-card-mode">' + html(String(run.mode)) + "</span>" : "";
    const statusClass = String(outcome.status || run.state || "running");
    return '<button type="button" class="kb-card" data-run-id="' + html(String(run.run_id || "")) + '" data-status="' + html(statusClass) + '">'
      + '<div class="kb-card-head">' + mode + (badges.length ? '<span class="kb-card-badges">' + badges.join("") + "</span>" : "") + "</div>"
      + '<div class="kb-card-goal">' + html(String(goal.summary || "任务")) + "</div>"
      + (summary ? '<div class="kb-card-summary">' + summary.split(" · ").map(html).join(" · ") + "</div>" : "")
      + meta
      + "</button>";
  }

  // ─── Board ───
  function columnAttentionCount(cards) {
    // Cards that need a human decision or are blocked, per column.
    return (cards || []).reduce(function (count, run) {
      const outcome = (run.semantic && run.semantic.outcome) || {};
      const attention = String(outcome.attention || "");
      if (attention === "blocked" || attention === "action_required") return count + 1;
      return count;
    }, 0);
  }

  function boardHtml(runs, selectedRunId) {
    const byColumn = {};
    COLUMNS.forEach(function (col) { byColumn[col.key] = []; });
    (runs || []).forEach(function (run) {
      const column = columnOfRun(run);
      (byColumn[column] || byColumn.analyzing).push(run);
    });
    return COLUMNS.map(function (col) {
      const cards = byColumn[col.key] || [];
      const cardHtmlList = cards.map(function (run) {
        const sel = run.run_id && run.run_id === selectedRunId ? ' data-selected="true"' : "";
        return '<div class="kb-card-wrap"' + sel + ">" + cardHtml(run) + "</div>";
      }).join("");
      const attention = columnAttentionCount(cards);
      const attentionMark = attention
        ? '<i class="kb-col-attention" data-kb-attention="' + col.key + '" title="' + attention + ' 项需要关注">⚠ ' + attention + "</i>"
        : "";
      return '<section class="kb-column" data-kb-column="' + col.key + '">'
        + '<div class="kb-col-head"><span>' + html(col.label) + '</span><em data-kb-count="' + col.key + '">' + cards.length + "</em>"
        + attentionMark
        + "</div>"
        + '<div class="kb-col-body">' + (cardHtmlList || '<div class="kb-col-empty">—</div>') + "</div>"
        + "</section>";
    }).join("");
  }

  function renderBoard(store) {
    if (!boardContainer) return;
    const runs = Array.from((store && store.runs ? store.runs.values() : []) || []);
    const emptyEl = document.getElementById("kbEmptyState");
    const boardEl = document.getElementById("kbBoard");
    const columnsEl = document.getElementById("kbColumns");
    if (!runs.length) {
      if (boardEl) boardEl.hidden = true;
      if (emptyEl) emptyEl.hidden = false;
      return;
    }
    if (emptyEl) emptyEl.hidden = true;
    if (boardEl) boardEl.hidden = false;
    if (columnsEl) columnsEl.innerHTML = boardHtml(runs, store && store.selectedRunId);
    if (columnsEl) {
      columnsEl.querySelectorAll(".kb-card").forEach(function (button) {
        button.addEventListener("click", function () {
          const runId = button.dataset.runId;
          if (store && runId && store.selectRun) store.selectRun(runId);
          openRunDetail(runId);
        });
      });
    }
    renderRunSelect(store, runs);
    if (openDrawerRunId) renderDrawer(store, openDrawerRunId);
  }

  function renderRunSelect(store, runs) {
    const selectEl = document.getElementById("kbRunSelect");
    if (!selectEl) return;
    const selected = store && store.selectedRunId;
    const options = (runs || []).map(function (run) {
      const goal = (run.semantic && run.semantic.goal) || {};
      const label = String(goal.summary || run.mode || run.run_id || "运行");
      return '<option value="' + html(String(run.run_id || "")) + '"' + (run.run_id === selected ? " selected" : "") + ">" + html(label) + "</option>";
    }).join("");
    selectEl.innerHTML = '<option value="">全部运行</option>' + options;
    selectEl.onchange = function () {
      const value = selectEl.value;
      if (store) {
        if (value) { store.selectRun(value); openRunDetail(value); }
        else { store.selectRun(null); closeDrawer(); }
      }
    };
  }

  // ─── Column highlight ───
  function setActiveColumn(columnName) {
    activeColumn = columnName || null;
    document.querySelectorAll("[data-kb-column]").forEach(function (col) {
      col.classList.toggle("kb-col-active", col.dataset.kbColumn === activeColumn);
    });
  }

  // ─── Drawer (detail slide-over) ───
  function drawerHtml(store, run) {
    if (!run) return "";
    const semantic = run.semantic || {};
    const header = typeof store._runDetailHtml === "function" ? store._runDetailHtml(run) : "";
    const tasks = (run.tasks || []).length
      ? (function () {
          const roots = typeof store._children === "function" ? store._children(run, null) : [];
          const entries = roots.length ? roots : run.tasks.filter(function (task) { return !task.parent_task_id; });
          return '<div class="kb-drawer-section"><div class="kb-drawer-hd">任务</div><div class="wb-task-tree">'
            + entries.map(function (task) { return store._taskHtml(run, task, 0); }).join("")
            + "</div></div>";
        })()
      : "";
    const review = run.review && typeof store._reviewHtml === "function"
      ? '<div class="kb-drawer-section"><div class="kb-drawer-hd">验证与改动</div>' + store._reviewHtml(run.review) + "</div>"
      : "";
    const activities = (semantic.activities || []).slice(-20).map(activityRow).join("");
    const feed = activities
      ? '<div class="kb-drawer-section"><div class="kb-drawer-hd">活动</div><ol class="kb-activity-list">' + activities + "</ol></div>"
      : "";
    // The preview iframe lives statically in #kbPreviewSection (index.html);
    // loadPreview (moduswindows.js) points it at /api/preview?url=….
    const artifacts = (run.artifacts || []).length
      ? '<div class="kb-drawer-section"><div class="kb-drawer-hd">产物</div><div class="wb-artifacts">'
        + run.artifacts.slice(-8).reverse().map(function (item) {
            return '<button class="wb-artifact" type="button" data-artifact-open="' + html(String(item.artifact_id || "")) + '"><span>▣</span><span><strong>' + html(String(item.title || "运行产物")) + '</strong><small>' + html(String(item.summary || item.kind || "")) + "</small></span><em>打开</em></button>";
          }).join("")
        + "</div></div>"
      : "";
    return '<div class="kb-drawer-inner">'
      + '<div class="kb-drawer-close" data-kb-close>×</div>'
      + header + tasks + review + feed + artifacts
      + "</div>";
  }

  function renderDrawer(store, runId) {
    const drawer = document.getElementById("kbDrawer");
    if (!drawer) return;
    const run = store && store.getRun ? store.getRun(runId) : null;
    if (!run) { drawer.hidden = true; openDrawerRunId = null; return; }
    openDrawerRunId = runId;
    drawer.hidden = false;
    drawer.innerHTML = drawerHtml(store, run);
    drawer.querySelectorAll(".wb-artifact").forEach(function (button) {
      button.addEventListener("click", function () {
        const id = button.dataset.artifactOpen;
        if (id && typeof global.requestArtifactContent === "function") global.requestArtifactContent(id);
      });
    });
    drawer.querySelectorAll("[data-task-select]").forEach(function (button) {
      button.addEventListener("click", function () { if (store.focusTask) store.focusTask(button.dataset.taskSelect); });
    });
    drawer.querySelectorAll("[data-task-artifact]").forEach(function (button) {
      button.addEventListener("click", function () {
        const id = button.dataset.taskArtifact;
        if (id && typeof global.requestArtifactContent === "function") global.requestArtifactContent(id);
      });
    });
    drawer.querySelectorAll("[data-task-review]").forEach(function (button) {
      button.addEventListener("click", function () { if (store.focusReviewFile) store.focusReviewFile(button.dataset.taskReview); });
    });
    drawer.querySelectorAll("[data-kb-close]").forEach(function (button) {
      button.addEventListener("click", closeDrawer);
    });
  }

  function openRunDetail(runId) {
    if (activeStore) renderDrawer(activeStore, runId);
  }
  function closeDrawer() {
    openDrawerRunId = null;
    const drawer = document.getElementById("kbDrawer");
    if (drawer) drawer.hidden = true;
  }

  // ─── Legacy perspective card (kept for backward-compatible contract) ───
  function perspectiveHtml(run) {
    const semantic = (run && run.semantic) || {};
    const goal = semantic.goal || {};
    const outcome = semantic.outcome || {};
    const activities = semantic.activities || [];
    const metrics = semantic.metrics || {};
    const counts = countByCategory(activities);
    const goalLine = goal.summary ? '<div class="kb-goal">' + html(String(goal.summary)) + "</div>" : "";
    const outcomeLine = outcome.status
      ? '<div class="kb-outcome" data-outcome="' + html(String(outcome.status)) + '">' + html(String(outcome.summary || outcome.status)) + "</div>"
      : "";
    const bits = [];
    if (counts.read) bits.push("读 " + counts.read + " 项");
    if (counts.write) bits.push("写 " + counts.write + " 项");
    if (counts.command) bits.push("命令 " + counts.command + " 次");
    if (metrics.tokens) bits.push(Number(metrics.tokens).toLocaleString("zh-CN") + " tokens");
    if (metrics.duration_seconds) bits.push(Number(metrics.duration_seconds).toFixed(1) + " 秒");
    const summaryLine = bits.length ? '<div class="kb-summary">' + bits.map(html).join(" · ") + "</div>" : "";
    const rows = activities.slice(-20).map(activityRow).join("");
    const feed = rows ? '<details class="kb-feed"><summary>活动记录（' + activities.length + '）</summary><ol class="kb-activity-list">' + rows + "</ol></details>" : "";
    return '<section class="kb-perspective" data-run-id="' + html(String(run.run_id || "")) + '">'
      + "<div class=\"kb-head\"><strong>运行透视</strong><small>" + html(String(run.mode || "")) + "</small></div>"
      + goalLine + outcomeLine + summaryLine + feed + "</section>";
  }

  // ─── Activity row (reused by drawer) ───
  function activityRow(activity) {    const mark = STATUS_MARK[activity.status] || "•";
    const cat = CATEGORY_LABELS[activity.category] || "";
    const catTag = cat ? '<span class="kb-activity-cat">' + cat + "</span>" : "";
    const actor = activity.actor || "";
    const detail = activity.detail
      ? '<span class="kb-activity-detail">' + html(String(activity.detail)) + "</span>"
      : "";
    return '<li class="kb-activity" data-status="' + html(String(activity.status || "")) + '">'
      + '<span class="kb-activity-mark">' + mark + "</span>"
      + catTag
      + "<span>" + html(String(activity.action || "操作")) + "</span>"
      + (actor && actor !== "host" ? "<small>" + html(String(actor)) + "</small>" : "")
      + detail
      + "</li>";
  }

  // ─── Legacy route handling for the adapter ───
  function handleLegacyRoute(name) {
    const action = LEGACY_ROUTES[name] || "detail";
    if (action === "document") {
      // Document rendering moved into the drawer; open the selected run if any.
      if (activeStore && activeStore.selectedRunId) openRunDetail(activeStore.selectedRunId);
    } else if (action === "preview") {
      if (activeStore && activeStore.selectedRunId) openRunDetail(activeStore.selectedRunId);
    } else if (action === "empty") {
      closeDrawer();
    } else {
      if (activeStore && activeStore.selectedRunId) openRunDetail(activeStore.selectedRunId);
    }
  }

  // ─── Mount: patch the WorkbenchStore prototype once ───
  function mountKanban(container) {
    boardContainer = container;
    const Store = global.ModusWorkbench && global.ModusWorkbench.WorkbenchStore;
    if (!Store) return;
    const originalRender = Store.prototype.render;
    Store.prototype.render = function () {
      const result = originalRender.apply(this, arguments);
      activeStore = this;
      renderBoard(this);
      return result;
    };
    const originalApply = Store.prototype.applyAuthoritativeRun;
    if (originalApply) {
      Store.prototype.applyAuthoritativeRun = function (run) {
        const result = originalApply.apply(this, arguments);
        activeStore = this;
        renderBoard(this);
        return result;
      };
    }
    renderBoard(activeStore);
  }

  function refreshBoard() {
    if (activeStore) renderBoard(activeStore);
  }

  global.ModusKanban = {
    columnOfRun: columnOfRun,
    cardHtml: cardHtml,
    boardHtml: boardHtml,
    renderBoard: renderBoard,
    setActiveColumn: setActiveColumn,
    openRunDetail: openRunDetail,
    closeDrawer: closeDrawer,
    handleLegacyRoute: handleLegacyRoute,
    mountKanban: mountKanban,
    refreshBoard: refreshBoard,
    perspectiveHtml: perspectiveHtml,
  };
})(window);
