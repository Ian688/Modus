(function (global) {
  "use strict";

  const STATUS_LABELS = {
    pending: "等待", queued: "等待", running: "执行中", blocked: "受阻",
    revision: "修订中", reviewing: "审阅中", completed: "完成",
    failed: "失败", cancelled: "取消", interrupted: "中断",
  };
  const STATUS_ICONS = {
    pending: "·", queued: "·", running: "◌", blocked: "Ⅱ", revision: "↻",
    reviewing: "◎", completed: "✓", failed: "!", cancelled: "○", interrupted: "○",
  };

  function html(value) {
    const node = document.createElement("span");
    node.textContent = value === null || value === undefined ? "" : String(value);
    return node.innerHTML;
  }

  function modeLabel(mode) {
    return mode === "peri" ? "Peri 共识协作" : mode === "moa" ? "MOA 参考协作" : "Agent 运行";
  }

  function stateLabel(state) {
    return {running:"执行中",completed:"完成",failed:"失败",cancelled:"取消",interrupted:"中断"}[state] || state || "未知";
  }

  function timeLabel(value) {
    const raw = Number(value || 0);
    if (!raw) return "";
    const date = new Date(raw < 10_000_000_000 ? raw * 1000 : raw);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("zh-CN", {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false});
  }

  function roleLabel(role) {
    if (role === "host") return "Host";
    if (/^worker_\d+$/.test(role)) return "Worker " + role.split("_")[1];
    if (/^reference_\d+$/.test(role)) return "参考 " + role.split("_")[1];
    return role;
  }

  function modelSnapshotLabel(model, fallback="") {
    if (!model || typeof model !== "object") return String(fallback || "");
    return String(model.name || model.model || model.model_id || fallback || "");
  }

  class WorkbenchStore {
    constructor(options) {
      this.taskContainer = options.taskContainer || null;
      this.workspaceContainer = options.workspaceContainer || null;
      this.artifactContainer = options.artifactContainer || null;
      this.runContainer = options.runContainer || null;
      this.reviewContainer = options.reviewContainer || null;
      this.runs = new Map();
      this.workspace = null;
      this.clock = 0;
      this.selectedRunId = null;
      this.selectionPinned = false;
      this.sessionId = null;
      this.onArtifact = options.onArtifact || null;
      this.onRunSelected = options.onRunSelected || null;
      this.onRunReplay = options.onRunReplay || null;
      this.canReplay = options.canReplay || (() => true);
      this.render();
    }

    reset() {
      this.runs = new Map();
      this.workspace = null;
      this.clock = 0;
      this.selectedRunId = null;
      this.selectionPinned = false;
      this.sessionId = null;
      this.render();
    }

    load(snapshot) {
      if (!snapshot || typeof snapshot !== "object") return;
      if (Object.prototype.hasOwnProperty.call(snapshot, "workspace")) {
        this.workspace = snapshot.workspace || null;
      }
      const nextSessionId = String(snapshot.session_id || "");
      const sameSession = this.sessionId === null || this.sessionId === nextSessionId;
      const priorSelection = sameSession ? this.selectedRunId : null;
      if (!sameSession) this.selectionPinned = false;
      this.sessionId = nextSessionId;
      this.runs = new Map();
      this.selectedRunId = null;
      (snapshot.runs || []).forEach(run => this._putRun(run));
      const keptSelection = Boolean(priorSelection && this.runs.has(priorSelection));
      this.selectedRunId = keptSelection ? priorSelection : (this._newestRun()?.run_id || null);
      if (!keptSelection) this.selectionPinned = false;
      this.render();
    }

    observe(event) {
      if (!event || !event.run_id) return;
      if (event.workbench && typeof event.workbench === "object") {
        this._putRun(event.workbench);
      }
      this.render();
    }

    _putRun(run) {
      const runId = String(run.run_id || "");
      if (!runId) return;
      const isNew = !this.runs.has(runId);
      const prior = this.runs.get(runId) || {};
      if (!isNew && this._compareProjectionCursor(run.projection_cursor, prior.projection_cursor) < 0) {
        return;
      }
      this.runs.set(runId, {
        ...prior, ...run,
        tasks: Array.isArray(run.tasks) ? run.tasks.map(task => ({...task})) : (prior.tasks || []),
        artifacts: Array.isArray(run.artifacts) ? run.artifacts.map(item => ({...item})) : (prior.artifacts || []),
        updatedOrder: ++this.clock,
      });
      if (String(run.state || "") === "running" && (isNew || !this.selectionPinned)) this.selectedRunId = runId;
    }

    _compareProjectionCursor(left, right) {
      const normalized = cursor => ({
        ledgerRevision: Math.max(0, Number(cursor?.ledger_revision || 0)),
        sequence: Math.max(0, Number(cursor?.sequence || 0)),
        // Legacy packets used `revision` for the event replacement revision.
        eventRevision: Math.max(0, Number(cursor?.event_revision ?? cursor?.revision ?? 0)),
      });
      const leftCursor = normalized(left);
      const rightCursor = normalized(right);
      if (leftCursor.ledgerRevision !== rightCursor.ledgerRevision) {
        return leftCursor.ledgerRevision - rightCursor.ledgerRevision;
      }
      const leftSequence = leftCursor.sequence;
      const rightSequence = rightCursor.sequence;
      if (leftSequence !== rightSequence) return leftSequence - rightSequence;
      return leftCursor.eventRevision - rightCursor.eventRevision;
    }

    applyAuthoritativeRun(run) {
      this._putRun(run);
      this.render();
    }

    latestRun() {
      if (this.selectedRunId && this.runs.has(this.selectedRunId)) return this.runs.get(this.selectedRunId);
      return this._newestRun();
    }

    _newestRun() {
      return [...this.runs.values()].sort((a, b) => Number(b.started_at || b.updated_at || b.updatedOrder || 0) - Number(a.started_at || a.updated_at || a.updatedOrder || 0))[0] || null;
    }

    getRun(runId) {
      return this.runs.get(String(runId || "")) || null;
    }

    selectRun(runId) {
      const id = String(runId || "");
      if (!this.runs.has(id)) return;
      this.selectedRunId = id;
      this.selectionPinned = true;
      this.render();
      if (this.onRunSelected) this.onRunSelected(id);
    }

    _runHistoryHtml(selected) {
      const runs = [...this.runs.values()].sort((a, b) => Number(b.started_at || b.updated_at || b.updatedOrder || 0) - Number(a.started_at || a.updated_at || a.updatedOrder || 0));
      if (!runs.length) return '<div class="rp-empty">尚无运行</div>';
      const selectedId = String(selected?.run_id || "");
      return '<div class="wb-run-history">' + runs.map((run, index) => {
        const tasks = run.tasks || [];
        const completed = tasks.filter(task => task.status === "completed").length;
        const review = run.review || {};
        const evidence = this._reviewPresentation(review).historyLabel;
        return '<div class="wb-run-row">'
          + '<button class="wb-run" type="button" data-run-select="' + html(run.run_id) + '" data-status="' + html(run.state || "running") + '" aria-pressed="' + String(String(run.run_id) === selectedId) + '"><i></i><span class="wb-run-index">' + (runs.length - index) + '</span><span class="wb-run-copy"><strong>' + html(modeLabel(run.mode)) + '</strong><small>' + html(timeLabel(run.started_at) || run.run_id) + ' · ' + completed + '/' + tasks.length + ' 任务 · ' + html(evidence) + '</small></span><em>' + html(stateLabel(run.state)) + '</em></button>'
          + '<button class="wb-run-replay" type="button" title="回放该次运行的事件" aria-label="回放该次运行的事件" data-run-replay="' + html(run.run_id) + '"' + (this.canReplay() ? '' : ' disabled') + '>↶</button>'
          + '</div>';
      }).join("") + '</div>';
    }

    _runDetailHtml(run) {
      if (!run) return "";
      const budget = run.budget || {};
      const duration = run.ended_at && run.started_at ? Math.max(0, Number(run.ended_at) - Number(run.started_at)) : Number(budget.elapsed_seconds || 0);
      const bits = [
        timeLabel(run.started_at),
        duration ? (duration < 60 ? duration.toFixed(1) + " 秒" : Math.floor(duration / 60) + " 分 " + Math.round(duration % 60) + " 秒") : "",
        budget.turns ? budget.turns + " 轮" : "",
        budget.total_tokens ? Number(budget.total_tokens).toLocaleString("zh-CN") + " tokens" : "",
      ].filter(Boolean);
      const ledger = budget.usage_ledger || {};
      const ledgerKeys = Object.keys(ledger).sort();
      const ledgerHtml = ledgerKeys.length
        ? '<div class="wb-run-ledger">' + ledgerKeys.map(key => {
            const entry = ledger[key] || {};
            const total = Number(entry.input_tokens || 0) + Number(entry.output_tokens || 0);
            return '<span>' + html(usageOwnerLabel(key)) + '</span><b>' + html(total.toLocaleString("zh-CN")) + '</b><small>' + Number(entry.input_tokens || 0).toLocaleString("zh-CN") + ' in · ' + Number(entry.output_tokens || 0).toLocaleString("zh-CN") + ' out</small>';
          }).join("") + '</div>'
        : "";
      return '<div class="wb-run-detail"><div><span>' + html(stateLabel(run.state)) + '</span><strong>' + html(modeLabel(run.mode)) + '</strong></div>' + (bits.length ? '<small>' + bits.map(html).join(" · ") + '</small>' : '') + ledgerHtml + this._runConfigHtml(run.config_snapshot) + (run.stop_reason && run.stop_reason !== "completed" ? '<em>停止原因：' + html(run.stop_reason) + '</em>' : '') + '</div>';
    }

    _runConfigHtml(snapshot) {
      if (!snapshot || typeof snapshot !== "object" || !Object.keys(snapshot).length) return "";
      const roles = snapshot.roles && typeof snapshot.roles === "object" ? snapshot.roles : {};
      const roleOrder = ["host", "reference_1", "reference_2", "worker_1", "worker_2"];
      const roleKeys = [...roleOrder.filter(role => roles[role]), ...Object.keys(roles).filter(role => !roleOrder.includes(role)).sort()];
      const modelRows = roleKeys.map(role => {
        const value = modelSnapshotLabel(roles[role], role === "host" ? snapshot.host_model_id : "");
        return value ? {label:roleLabel(role), value} : null;
      }).filter(Boolean);
      if (!modelRows.some(item => item.label === "Host")) {
        const host = modelSnapshotLabel(null, snapshot.host_model_id);
        if (host) modelRows.unshift({label:"Host", value:host});
      }
      const budget = snapshot.budget && typeof snapshot.budget === "object" ? snapshot.budget : {};
      const verification = snapshot.verification && typeof snapshot.verification === "object" ? snapshot.verification : {};
      const configRows = [...modelRows];
      const effort = String(snapshot.reasoning_effort || roles.host?.reasoning_effort || "");
      if (effort) configRows.push({label:"思考", value:effort});
      const limits = [];
      if (Number(budget.max_turns) > 0) limits.push(Number(budget.max_turns).toLocaleString("zh-CN") + " 轮");
      if (Number(budget.max_tokens) > 0) limits.push(Number(budget.max_tokens).toLocaleString("zh-CN") + " tokens");
      if (Number(budget.max_wall_seconds) > 0) limits.push(Number(budget.max_wall_seconds).toLocaleString("zh-CN") + " 秒");
      if (limits.length) configRows.push({label:"上限", value:limits.join(" · ")});
      if (verification.required === true) {
        const attempts = Number(verification.max_attempts);
        configRows.push({label:"验证", value:"必须" + (attempts > 0 ? " · 最多 " + attempts.toLocaleString("zh-CN") + " 次" : "")});
      } else if (verification.required === false && Object.prototype.hasOwnProperty.call(verification, "required")) {
        configRows.push({label:"验证", value:"按需"});
      }
      if (!configRows.length) return "";
      return '<div class="wb-run-config"><span>启动配置</span><dl>'
        + configRows.map(item => '<div><dt>' + html(item.label) + '</dt><dd>' + html(item.value) + '</dd></div>').join("")
        + '</dl></div>';
    }

    _children(run, parentId) {
      return (run.tasks || [])
        .filter(task => String(task.parent_task_id || "") === String(parentId || ""))
        .sort((a, b) => Number(a.ordinal || 0) - Number(b.ordinal || 0));
    }

    _taskHtml(run, task, depth) {
      const status = String(task.status || "pending");
      const children = this._children(run, task.task_id);
      const owner = task.actor_label || task.assigned_model_id || (task.task_kind === "root" ? "主持人" : "");
      const description = task.description || task.success_criteria || "";
      const taskArtifacts = (run.artifacts || []).filter(item => item.task_id === task.task_id);
      const taskFiles = ((run.review || {}).files || []).filter(item => (item.task_ids || []).includes(task.task_id));
      const resultCount = taskArtifacts.length;
      const changeText = taskFiles.length ? ' · ' + taskFiles.length + ' 个改动文件' : '';
      const actions = (taskArtifacts.length || taskFiles.length)
        ? '<div class="wb-task-actions">'
          + (taskArtifacts.length ? '<button type="button" data-task-artifact="' + html(taskArtifacts[0].artifact_id) + '">▣ 打开产物' + (taskArtifacts.length > 1 ? ' · ' + taskArtifacts.length : '') + '</button>' : '')
          + (taskFiles.length ? '<button type="button" data-task-review="' + html(taskFiles[0].path) + '">📝 查看改动' + (taskFiles.length > 1 ? ' · ' + taskFiles.length : '') + '</button>' : '')
          + '</div>' : '';
      return '<div class="wb-task-wrap" style="--wb-depth:' + depth + '">'
        + '<button class="wb-task" type="button" data-task-select="' + html(task.task_id) + '" data-status="' + html(status) + '">'
        + '<span class="wb-task-icon">' + (STATUS_ICONS[status] || "·") + '</span>'
        + '<span class="wb-task-copy"><strong>' + html(task.title || "任务") + '</strong>'
        + (description ? '<small>' + html(description) + '</small>' : '')
        + '<span class="wb-task-meta">' + html(owner) + (resultCount ? ' · ' + resultCount + ' 个产物' : '') + changeText + '</span></span>'
        + '<span class="wb-task-state">' + html(STATUS_LABELS[status] || status) + '</span></button>'
        + actions
        + children.map(child => this._taskHtml(run, child, depth + 1)).join("") + '</div>';
    }

    _reviewPresentation(review) {
      const files = Array.isArray(review?.files) ? review.files : [];
      const fileCount = Math.max(Number(review?.file_count || 0), files.length);
      const verifications = Array.isArray(review?.verifications) ? review.verifications : [];
      const latest = review?.latest_verification || verifications[verifications.length - 1] || null;
      const verificationStatus = String(latest?.status || "");
      const status = String(review?.status || "clean");
      if (!fileCount) {
        if (verificationStatus === "passed" || status === "verified") {
          return {label:"验证通过", historyLabel:"验证通过", fileCount:0};
        }
        if (verificationStatus || status === "failed") {
          return {label:"验证失败", historyLabel:"验证失败", fileCount:0};
        }
        return {label:"无文件改动", historyLabel:"无文件改动", fileCount:0};
      }
      const label = {
        verified:"文件已验证", failed:"验证失败", unverified:"文件待验证",
        changed:"有文件改动", clean:"无文件改动",
      }[status] || "审阅中";
      return {label, historyLabel:label, fileCount};
    }

    _reviewHtml(review) {
      if (!review) return '<div class="rp-empty">本次运行尚无验证或文件改动</div>';
      const files = Array.isArray(review.files) ? review.files.slice(-8).reverse() : [];
      const verifications = Array.isArray(review.verifications) ? review.verifications.slice(-3).reverse() : [];
      const presentation = this._reviewPresentation(review);
      if (!presentation.fileCount && !verifications.length) {
        return '<div class="rp-empty">本次运行无文件改动</div>';
      }
      const fileHtml = files.map(file => {
        const operation = file.change_type === "create" ? "新建" : file.operation === "write" ? "写入" : "编辑";
        const diff = String(file.diff || "");
        return '<article class="wb-review-file" data-review-path="' + html(file.path) + '">'
          + '<div class="wb-review-file-head"><span class="wb-review-op">' + html(operation) + '</span><strong title="' + html(file.path) + '">' + html(file.path) + '</strong><em>+' + Number(file.additions || 0) + ' −' + Number(file.deletions || 0) + '</em></div>'
          + (diff ? '<details><summary>查看 Diff' + (file.diff_truncated ? ' · 已截断' : '') + '</summary><pre><code>' + html(diff) + '</code></pre></details>' : '<small>没有可展示的文本 Diff</small>')
          + '</article>';
      }).join("");
      const verificationHtml = verifications.map(item => {
        const counts = item.counts || {};
        const parts = [counts.passed ? counts.passed + " passed" : "", counts.failed ? counts.failed + " failed" : "", counts.skipped ? counts.skipped + " skipped" : ""].filter(Boolean);
        const label = parts.join(" · ") || item.status || "unknown";
        return '<div class="wb-verification" data-status="' + html(item.status || "failed") + '"><span>' + (item.status === "passed" ? "✓" : "!") + '</span><div><strong>' + html(label) + '</strong><small>' + html(item.command || "验证命令") + (item.duration_seconds !== null && item.duration_seconds !== undefined ? ' · ' + Number(item.duration_seconds).toFixed(2) + 's' : '') + '</small></div></div>';
      }).join("");
      return '<section class="wb-review" data-status="' + html(review.status || "changed") + '">'
        + '<div class="wb-review-summary"><span><i></i>' + html(presentation.label) + '</span><strong>' + (presentation.fileCount ? presentation.fileCount + ' 个文件' : '无文件改动') + '</strong>' + (presentation.fileCount ? '<em>+' + Number(review.additions || 0) + ' −' + Number(review.deletions || 0) + '</em>' : '') + '</div>'
        + (fileHtml ? '<div class="wb-review-files">' + fileHtml + '</div>' : '')
        + (verificationHtml ? '<div class="wb-verifications"><div class="wb-review-caption">验证证据</div>' + verificationHtml + '</div>' : '<div class="wb-review-notice">尚未记录结构化验证证据</div>')
        + '</section>';
    }

    render() {
      const run = this.latestRun();
      if (this.workspaceContainer) {
        const workspace = this.workspace;
        this.workspaceContainer.innerHTML = workspace
          ? '<div class="wb-workspace"><span class="wb-workspace-mark">W</span><div><strong>' + html(workspace.name || "工作区") + '</strong><small title="' + html(workspace.root || "") + '">' + html(workspace.root || "") + '</small></div></div>'
          : '<div class="rp-empty">等待工作区身份</div>';
      }
      if (this.runContainer) {
        this.runContainer.innerHTML = this._runHistoryHtml(run);
        this.runContainer.querySelectorAll("[data-run-select]").forEach(button => {
          button.addEventListener("click", () => this.selectRun(button.dataset.runSelect));
        });
        this.runContainer.querySelectorAll("[data-run-replay]").forEach(button => {
          button.addEventListener("click", () => {
            if (this.onRunReplay) this.onRunReplay(button.dataset.runReplay);
          });
        });
      }
      if (this.taskContainer) {
        if (!run || !(run.tasks || []).length) {
          this.taskContainer.innerHTML = (run ? this._runDetailHtml(run) : "") + '<div class="rp-empty">当前会话没有任务记录</div>';
        } else {
          const roots = this._children(run, null);
          const entries = roots.length ? roots : run.tasks.filter(task => !task.parent_task_id);
          this.taskContainer.innerHTML = this._runDetailHtml(run) + '<div class="wb-task-tree">' + entries.map(task => this._taskHtml(run, task, 0)).join("") + '</div>';
          this.taskContainer.querySelectorAll("[data-task-select]").forEach(button => {
            button.addEventListener("click", () => this.focusTask(button.dataset.taskSelect));
          });
          this.taskContainer.querySelectorAll("[data-task-artifact]").forEach(button => {
            button.addEventListener("click", () => {
              if (this.onArtifact) this.onArtifact(button.dataset.taskArtifact);
            });
          });
          this.taskContainer.querySelectorAll("[data-task-review]").forEach(button => {
            button.addEventListener("click", () => this.focusReviewFile(button.dataset.taskReview));
          });
        }
      }
      if (this.artifactContainer) {
        const artifacts = run ? (run.artifacts || []).slice(-8).reverse() : [];
        this.artifactContainer.innerHTML = artifacts.length
          ? '<div class="wb-artifacts">' + artifacts.map(item => '<button class="wb-artifact" type="button" data-artifact-open="' + html(item.artifact_id) + '"><span>▣</span><span><strong>' + html(item.title || "运行产物") + '</strong><small>' + html(item.summary || item.kind || "") + '</small></span><em>打开</em></button>').join("") + '</div>'
          : '<div class="rp-empty">运行产物会显示在这里</div>';
        this.artifactContainer.querySelectorAll("[data-artifact-open]").forEach(button => {
          button.addEventListener("click", () => {
            if (this.onArtifact) this.onArtifact(button.dataset.artifactOpen);
          });
        });
      }
      if (this.reviewContainer) {
        if (!run) this.reviewContainer.innerHTML = '<div class="rp-empty">运行后显示文件改动与验证证据</div>';
        else this.reviewContainer.innerHTML = this._reviewHtml(run.review);
      }
    }

    focusTask(taskId) {
      const target = document.querySelector('[data-task-id="' + CSS.escape(String(taskId || "")) + '"]');
      if (target) {
        target.scrollIntoView({behavior: "smooth", block: "center"});
        target.classList.add("review-target-highlight");
        setTimeout(() => target.classList.remove("review-target-highlight"), 1800);
        return;
      }
      const run = this.latestRun();
      const artifact = (run?.artifacts || []).find(item => item.task_id === taskId);
      if (artifact && this.onArtifact) {
        this.onArtifact(artifact.artifact_id);
        return;
      }
      const reviewFile = ((run?.review || {}).files || []).find(item => (item.task_ids || []).includes(taskId));
      if (reviewFile) this.focusReviewFile(reviewFile.path);
    }

    focusReviewFile(path) {
      const entries = this.reviewContainer?.querySelectorAll(".wb-review-file") || [];
      for (const entry of entries) {
        if (entry.dataset.reviewPath !== path) continue;
        const details = entry.querySelector("details");
        if (details) details.open = true;
        entry.scrollIntoView({behavior:"smooth", block:"center"});
        entry.classList.add("review-target-highlight");
        setTimeout(() => entry.classList.remove("review-target-highlight"), 1800);
        break;
      }
    }
  }

  global.ModusWorkbench = {WorkbenchStore};
})(window);
