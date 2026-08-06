// ═══ Account center: balance, usage chart, recharge, users, password lock ═══
// Native canvas/SVG chart (zero build).  Colors read from --accent so the
// chart re-tints when the theme (glass/linear/deep) switches.
(function (global) {
  "use strict";

  let lastSummary = null;
  let lastAuthStatus = null;
  let lastProviderUsage = null;

  function el(id) { return document.getElementById(id); }
  function money(cents) {
    const n = Number(cents || 0);
    return (n / 100).toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + " 元";
  }
  function tokens(n) {
    const v = Number(n || 0);
    if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "k";
    return String(v);
  }
  function escapeHtml(value) {
    const node = document.createElement("span");
    node.textContent = value === null || value === undefined ? "" : String(value);
    return node.innerHTML;
  }

  function renderBalance() {
    const b = el("accountBalance");
    const lt = el("accountLifetime");
    const rc = el("accountRecharge");
    if (!b || !lastSummary) return;
    b.textContent = money(lastSummary.balance_cents);
    lt.textContent = "累计用量 " + money(lastSummary.lifetime_cents);
    rc.textContent = money(lastSummary.total_recharge_cents);
  }

  function renderChart() {
    const host = el("usageChart");
    if (!host) return;
    const daily = (lastSummary && lastSummary.daily) || [];
    if (!daily.length) {
      host.innerHTML = '<div class="repo-empty">暂无用量数据</div>';
      return;
    }
    const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#1a1a1a";
    const maxCost = Math.max(1, ...daily.map(d => Number(d.cost_cents || 0)));
    const svg = [];
    svg.push('<svg viewBox="0 0 300 80" preserveAspectRatio="none" class="account-chart-svg" aria-hidden="true">');
    daily.forEach((d, i) => {
      const h = Math.max(2, Math.round((Number(d.cost_cents || 0) / maxCost) * 60));
      const x = 8 + i * (284 / Math.max(1, daily.length - 1 || 1));
      svg.push('<rect x="' + (i * 22 + 6) + '" y="' + (70 - h) + '" width="14" height="' + h + '" rx="3" fill="' + accent + '" opacity="0.85"/>');
    });
    svg.push('</svg>');
    svg.push('<div class="account-chart-labels">');
    svg.push(daily.map((d, i) =>
      '<span title="' + escapeHtml(d.day) + ' · ' + money(d.cost_cents) + ' · ' + tokens(d.input_tokens + d.output_tokens) + ' tokens">'
      + escapeHtml(String(d.day).slice(5)) + '</span>'
    ).join(""));
    svg.push('</div>');
    host.innerHTML = svg.join("");
  }

  function renderModelUsage() {
    const host = el("modelUsage");
    if (!host) return;
    const models = (lastSummary && lastSummary.models) || [];
    if (!models.length) {
      host.innerHTML = '<div class="repo-empty">暂无模型用量</div>';
      return;
    }
    host.innerHTML = models.map(m =>
      '<div class="model-usage-row">'
      + '<span class="model-usage-name">' + escapeHtml(m.model_id) + '</span>'
      + '<span class="model-usage-bar"><i style="width:' + Math.min(100, Math.round(Number(m.cost_cents || 0) / Math.max(1, models[0].cost_cents) * 100)) + '%"></i></span>'
      + '<span class="model-usage-meta">' + tokens(m.input_tokens + m.output_tokens) + ' tok · ' + m.runs + ' 次 · ' + money(m.cost_cents) + '</span>'
      + '</div>'
    ).join("");
  }

  function renderUsers() {
    const host = el("accountUsers");
    if (!host) return;
    const users = (lastAuthStatus && lastAuthStatus.users) || [];
    const currentId = (lastAuthStatus && lastAuthStatus.current_user && lastAuthStatus.current_user.user_id) || "";
    if (!users.length) {
      host.innerHTML = '<div class="repo-empty">暂无用户</div>';
      return;
    }
    host.innerHTML = users.map(u =>
      '<div class="account-user-row" data-user="' + escapeHtml(u.user_id) + '">'
      + '<span class="account-user-ava">' + (u.is_local_default ? "★" : "👤") + '</span>'
      + '<span class="account-user-name">' + escapeHtml(u.username)
      + (u.user_id === currentId ? ' <em class="account-current">当前</em>' : '')
      + (u.has_password ? ' <em class="account-locked">已锁定</em>' : '')
      + '</span>'
      + '<span class="account-user-actions">'
      + (u.user_id !== currentId
        ? '<button class="plain-small" data-switch-user="' + escapeHtml(u.user_id) + '">切换</button>'
        : '<span class="settings-copy">当前</span>')
      + '<button class="plain-small" data-rename-user="' + escapeHtml(u.user_id) + '">改名</button>'
      + '<button class="plain-small auth-delete-btn" data-delete-user="' + escapeHtml(u.user_id) + '">删除</button>'
      + '</span>'
      + '</div>'
    ).join("");
    host.querySelectorAll("[data-switch-user]").forEach(btn => {
      btn.addEventListener("click", () => {
        const user = users.find(x => String(x.user_id) === btn.dataset.switchUser);
        if (user?.has_password && window.ModusAuth?.openLoginForUser) {
          window.ModusAuth.openLoginForUser(user);
          return;
        }
        sendWs({type: "auth_switch_user", user_id: btn.dataset.switchUser, request_id: "sw-" + Date.now()});
      });
    });
    host.querySelectorAll("[data-rename-user]").forEach(btn => {
      btn.addEventListener("click", () => {
        const user = users.find(x => String(x.user_id) === btn.dataset.renameUser);
        if (!user) return;
        const name = window.prompt("修改账号名：", user.username);
        if (name && name.trim() && name.trim() !== user.username) {
          sendWs({type: "auth_rename_user", user_id: user.user_id, new_username: name.trim(), request_id: "rn-" + Date.now()});
        }
      });
    });
    host.querySelectorAll("[data-delete-user]").forEach(btn => {
      btn.addEventListener("click", () => {
        const user = users.find(x => String(x.user_id) === btn.dataset.deleteUser);
        if (!user) return;
        openAuthDeleteModal(user);
      });
    });
  }

  function openAuthDeleteModal(user) {
    const modal = el("authDeleteModal");
    if (!modal) return;
    const msg = el("authDeleteMsg");
    if (msg) {
      msg.textContent = user.is_local_default
        ? "该账户为本机默认账户，其下已有数据将归属本地默认用户。确认删除？"
        : "确定删除账户 \"" + user.username + "\"？";
    }
    const ok = el("authDeleteOkBtn");
    if (ok) ok.dataset.deleteUserId = user.user_id;
    const cascade = el("authDeleteCascade");
    if (cascade) cascade.checked = false;
    modal.classList.add("on");
  }

  function renderAll() {
    renderBalance();
    renderChart();
    renderModelUsage();
    renderUsers();
    renderProviderUsage();
  }

  // ── Provider usage / balance cards ──
  function renderProviderUsage() {
    const host = el("providerUsageList");
    if (!host) return;
    const models = lastProviderUsage || [];
    if (!models.length) {
      host.innerHTML = '<div class="repo-empty">点击"刷新查询"拉取各模型余量</div>';
      return;
    }
    host.innerHTML = models.map(m => {
      let body = '';
      if (m.status === 'queried' && m.balance && m.balance.cards) {
        body = '<div class="pu-balance">'
          + m.balance.cards.map(c =>
              '<span class="pu-balance-item"><b>' + escapeHtml(c.total_balance) + '</b>'
              + '<small>' + escapeHtml(c.currency) + (c.topped_up_balance && c.granted_balance ? ' · 充 ' + escapeHtml(c.topped_up_balance) + ' · 赠 ' + escapeHtml(c.granted_balance) : '') + '</small></span>'
            ).join("")
          + '</div>';
      } else if (m.status === 'queried') {
        const parts = [];
        if (m.cost && m.cost.total) parts.push('成本 ' + escapeHtml(String(m.cost.total.amount || m.cost.total.value || 0)) + ' ' + escapeHtml(String(m.cost.total.currency || '')));
        if (m.usage && m.usage.total_tokens) parts.push(escapeHtml(formatTokens(m.usage.total_tokens)) + ' tokens');
        if (m.rate_limits) parts.push('限流配置可查');
        body = parts.length ? '<div class="pu-summary">' + parts.join(' · ') + '</div>' : '<div class="pu-summary">用量数据已获取</div>';
      } else {
        body = '<div class="pu-unavailable">' + escapeHtml(m.message || m.status) + '</div>';
      }
      const caps = (m.capabilities || []).map(c => '<em class="pu-cap">' + escapeHtml(c) + '</em>').join("");
      return '<div class="provider-usage-card" data-status="' + escapeHtml(m.status) + '">'
        + '<div class="pu-head"><span class="pu-name">' + escapeHtml(m.name || m.model_id) + '</span>'
        + '<span class="pu-provider">' + escapeHtml(m.provider || '') + '</span>'
        + (m.status === 'queried' ? '<span class="pu-ok">✓</span>' : m.status === 'error' ? '<span class="pu-err">!</span>' : '<span class="pu-warn">…</span>')
        + caps + '</div>'
        + body
        + '</div>';
    }).join("");
  }

  function refreshProviderUsage() {
    sendWs({type: "provider_usage", request_id: "pu-" + Date.now()});
  }

  function sendWs(payload) {
    if (typeof ws !== "undefined" && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  }

  function refresh() {
    sendWs({type: "auth_status", request_id: "as-" + Date.now()});
    sendWs({type: "usage_summary", request_id: "us-" + Date.now()});
  }

  // ── Message handlers wired from websocket.js ──
  // Merge into existing handlers (auth.js may already define onAuthStatus)
  // instead of overwriting, so the login badge and account panel both update.
  global.onAuthStatus = (function (prev) {
    return function (msg) {
      if (typeof prev === "function") prev(msg);
      lastAuthStatus = msg;
      const sub = el("accountSubtitle");
      if (sub && msg.current_user) {
        sub.textContent = "当前用户：" + msg.current_user.username;
      }
      if (el("accountPassword")) el("accountPassword").placeholder =
        (msg.current_user && msg.current_user.has_password) ? "输入新口令以修改" : "设置口令（留空清除）";
      renderUsers();
    };
  })(global.onAuthStatus);
  global.onAuthChanged = (function (prev) {
    return function (msg) {
      if (typeof prev === "function") prev(msg);
      refresh();
    };
  })(global.onAuthChanged);
  global.onUserCreated = (function (prev) {
    return function (msg) {
      if (typeof prev === "function") prev(msg);
      refresh();
    };
  })(global.onUserCreated);
  global.onUsageSummary = function (msg) {
    lastSummary = msg.summary;
    renderAll();
  };
  global.onUsageSummaryUpdated = function (msg) {
    lastSummary = msg.summary;
    renderAll();
  };
  global.onRechargeDone = function () { refresh(); };
  global.onProviderUsage = function (msg) {
    lastProviderUsage = msg.models || [];
    renderProviderUsage();
  };
  global.onUserRenamed = (function (prev) {
    return function (msg) {
      if (typeof prev === "function") prev(msg);
      refresh();
    };
  })(global.onUserRenamed);
  global.onUserDeleted = (function (prev) {
    return function (msg) {
      if (typeof prev === "function") prev(msg);
      refresh();
    };
  })(global.onUserDeleted);

  // ── Button wiring ──
  function wire() {
    const rechargeBtn = el("rechargeBtn");
    if (rechargeBtn) rechargeBtn.addEventListener("click", () => {
      const amountInput = el("rechargeAmount");
      const yuan = Number(amountInput && amountInput.value || 0);
      if (!yuan || yuan <= 0) { amountInput && amountInput.focus(); return; }
      sendWs({type: "recharge", amount_cents: Math.round(yuan * 100), note: "手动充值", request_id: "rc-" + Date.now()});
      if (amountInput) amountInput.value = "";
    });
    const pwBtn = el("accountSetPasswordBtn");
    if (pwBtn) pwBtn.addEventListener("click", () => {
      const pw = el("accountPassword");
      sendWs({type: "auth_set_password", password: pw ? pw.value : "", request_id: "pw-" + Date.now()});
      if (pw) pw.value = "";
    });
    const puBtn = el("providerUsageRefreshBtn");
    if (puBtn) puBtn.addEventListener("click", refreshProviderUsage);
    // Re-render the chart when the theme changes in Settings.
    window.addEventListener("modus-theme-change", () => { if (lastSummary) renderChart(); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();

  global.ModusAccount = { refresh, renderAll };
})(window);
