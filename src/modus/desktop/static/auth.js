// ═══ Local auth: two-level card login (Local / Modus) + footer badge ═══
// Level 0: choose Local (offline accounts) or Modus (cloud, degraded).
// Level 1 (Local): pre-filled demo login + multi-account list/create/login/manage.
// Level 1 (Modus): cloud email login/register (degraded until backend exists).
// Default single-user installs still skip the login page entirely.
(function (global) {
  "use strict";

  let authState = { users: [], current_user: null, password_lock_available: false };

  function el(id) { return document.getElementById(id); }
  function escapeHtml(value) {
    const node = document.createElement("span");
    node.textContent = value === null || value === undefined ? "" : String(value);
    return node.innerHTML;
  }

  function sendWs(payload) {
    if (typeof ws !== "undefined" && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  }

  // ── Footer badge ──
  function renderBadge() {
    let badge = el("accountBadge");
    if (badge) badge.remove();
  }

  // ── Login overlay: two-level cards ──
  function ensureOverlay() {
    let view = el("authView");
    if (view) return view;
    view = document.createElement("div");
    view.id = "authView";
    view.className = "auth-view";
    view.innerHTML =
      '<div class="auth-card">'
      + '<div class="auth-brand"><span class="settings-logo">M</span><strong>Modus</strong><small>账户登录</small></div>'
      + '<div class="auth-error" id="authError" hidden></div>'

      // Level 0: choose account type
      + '<div class="auth-level" id="authLevel0">'
      + '<div class="auth-choice-grid">'
      + '<button class="auth-choice-card" type="button" id="authLocalCard">'
      + '<span class="auth-choice-icon">👤</span><strong>Local</strong>'
      + '<small>本机账户 · 演示账户 · 离线使用</small>'
      + '</button>'
      + '<button class="auth-choice-card" type="button" id="authModusCard">'
      + '<span class="auth-choice-icon">☁</span><strong>Modus</strong>'
      + '<small>云端账户 · 邮箱登录</small>'
      + '</button>'
      + '</div>'
      + '</div>'

      // Level 1: Local
      + '<div class="auth-level" id="authLevelLocal" hidden>'
      + '<button class="auth-back" type="button" id="authBackLocal">← 返回账户类型</button>'
      + '<div class="auth-demo" id="authDemoBox">'
      + '<div class="auth-demo-head">'
      + '<div class="settings-title">演示账户</div>'
      + '<span class="auth-demo-tag" id="authDemoTag">demo · 123456</span>'
      + '</div>'
      + '<div class="settings-copy">账号已就绪，一键进入即可体验</div>'
      + '<button class="primary-small auth-demo-enter" type="button" id="authDemoEnter"><span>🚀</span>使用演示账户登录</button>'
      + '</div>'
      + '<div class="auth-section-title">本地账户</div>'
      + '<div class="auth-users" id="authUsers"></div>'
      + '<div class="auth-actions">'
      + '<button class="auth-action-btn" type="button" id="authToggleLogin"><span>🔑</span>登录已有账户</button>'
      + '<button class="auth-action-btn" type="button" id="authToggleCreate"><span>✨</span>创建新账户</button>'
      + '</div>'
      + '<form class="auth-login-form auth-collapse" id="authLoginForm" hidden>'
      + '<input id="authUsername" type="text" placeholder="用户名" autocomplete="username">'
      + '<input id="authPassword" type="password" placeholder="口令" autocomplete="current-password">'
      + '<button class="primary-small" type="submit">登录</button>'
      + '</form>'
      + '<div class="auth-create auth-collapse" id="authCreateBox" hidden>'
      + '<input id="authNewUsername" type="text" placeholder="新用户名">'
      + '<input id="authNewPassword" type="password" placeholder="新口令（可留空）">'
      + '<button class="primary-small" type="button" id="authCreateBtn">创建并登录</button>'
      + '</div>'
      + '</div>'

      // Level 1: Modus
      + '<div class="auth-level" id="authLevelModus" hidden>'
      + '<button class="auth-back" type="button" id="authBackModus">← 返回账户类型</button>'
      + '<div id="modusCloudBox"><div class="repo-empty">连接中…</div></div>'
      + '</div>'
      + '</div>';
    document.body.appendChild(view);
    wireOverlay(view);
    return view;
  }

  function showLevel(name) {
    const l0 = el("authLevel0");
    const ll = el("authLevelLocal");
    const lm = el("authLevelModus");
    if (l0) l0.hidden = name !== "root";
    if (ll) ll.hidden = name !== "local";
    if (lm) lm.hidden = name !== "modus";
  }

  function openLoginForUser(user) {
    const view = ensureOverlay();
    view.hidden = false;
    view.classList.add("on");
    showLevel("local");
    const username = el("authUsername");
    const password = el("authPassword");
    const form = el("authLoginForm");
    const toggle = el("authToggleLogin");
    if (form) form.hidden = false;
    if (toggle) toggle.classList.add("active");
    if (username) username.value = String(user?.username || "");
    if (password) { password.value = ""; password.focus(); }
  }

  function wireOverlay(view) {
    // Level 0 cards
    const localCard = el("authLocalCard");
    if (localCard) localCard.addEventListener("click", () => {
      showLevel("local");
      sendWs({type: "auth_demo_account", request_id: "dm-" + Date.now()});
      sendWs({type: "auth_status", request_id: "as-" + Date.now()});
    });
    const modusCard = el("authModusCard");
    if (modusCard) modusCard.addEventListener("click", () => {
      showLevel("modus");
      sendWs({type: "modus_account_status", request_id: "cs-" + Date.now()});
    });
    const backLocal = el("authBackLocal");
    if (backLocal) backLocal.addEventListener("click", () => showLevel("root"));
    const backModus = el("authBackModus");
    if (backModus) backModus.addEventListener("click", () => showLevel("root"));

    // Demo card: one-click enter (credentials shown as a tag, not inputs).
    const demoEnter = el("authDemoEnter");
    if (demoEnter) demoEnter.addEventListener("click", () => {
      sendWs({type: "auth_login", username: "demo", password: "123456", request_id: "dm-" + Date.now()});
    });

    // Regular login / create — collapsible, revealed by toggle buttons.
    const toggleLogin = el("authToggleLogin");
    const toggleCreate = el("authToggleCreate");
    const loginForm = el("authLoginForm");
    const createBox = el("authCreateBox");
    if (toggleLogin && loginForm) toggleLogin.addEventListener("click", () => {
      const open = loginForm.hidden;
      loginForm.hidden = !open;
      toggleLogin.classList.toggle("active", open);
      if (open) {
        if (createBox) { createBox.hidden = true; }
        if (toggleCreate) toggleCreate.classList.remove("active");
        const u = el("authUsername");
        if (u) u.focus();
      }
    });
    if (toggleCreate && createBox) toggleCreate.addEventListener("click", () => {
      const open = createBox.hidden;
      createBox.hidden = !open;
      toggleCreate.classList.toggle("active", open);
      if (open) {
        if (loginForm) { loginForm.hidden = true; }
        if (toggleLogin) toggleLogin.classList.remove("active");
        const u = el("authNewUsername");
        if (u) u.focus();
      }
    });
    if (loginForm) loginForm.addEventListener("submit", ev => {
      ev.preventDefault();
      const u = el("authUsername").value.trim();
      const p = el("authPassword").value;
      if (!u) return;
      sendWs({type: "auth_login", username: u, password: p, request_id: "lg-" + Date.now()});
    });
    const createBtn = el("authCreateBtn");
    if (createBtn) createBtn.addEventListener("click", () => {
      const u = el("authNewUsername").value.trim();
      const p = el("authNewPassword").value;
      if (!u) return;
      sendWs({type: "user_create", username: u, password: p, request_id: "cr-" + Date.now()});
    });

    // Account list: login / rename / delete
    el("authUsers").addEventListener("click", ev => {
      const loginBtn = ev.target.closest("[data-auth-login]");
      if (loginBtn) {
        const user = authState.users.find(x => String(x.user_id) === loginBtn.dataset.authLogin);
        if (!user) return;
        if (user.has_password) {
          el("authUsername").value = user.username;
          el("authPassword").focus();
        } else {
          sendWs({type: "auth_switch_user", user_id: user.user_id, request_id: "sw-" + Date.now()});
        }
        return;
      }
      const renameBtn = ev.target.closest("[data-rename-user]");
      if (renameBtn) {
        const user = authState.users.find(x => String(x.user_id) === renameBtn.dataset.renameUser);
        if (!user) return;
        const name = window.prompt("修改账号名：", user.username);
        if (name && name.trim() && name.trim() !== user.username) {
          sendWs({type: "auth_rename_user", user_id: user.user_id, new_username: name.trim(), request_id: "rn-" + Date.now()});
        }
        return;
      }
      const deleteBtn = ev.target.closest("[data-delete-user]");
      if (deleteBtn) {
        const user = authState.users.find(x => String(x.user_id) === deleteBtn.dataset.deleteUser);
        if (user) openDeleteModal(user);
      }
    });

    // Delete confirmation modal
    const deleteCancel = el("authDeleteCancelBtn");
    if (deleteCancel) deleteCancel.addEventListener("click", () => closeDeleteModal());
    const deleteOk = el("authDeleteOkBtn");
    if (deleteOk) deleteOk.addEventListener("click", () => {
      const target = deleteOk.dataset.deleteUserId;
      const cascade = el("authDeleteCascade")?.checked || false;
      if (target) {
        sendWs({type: "auth_delete_user", user_id: target, delete_data: cascade, request_id: "dl-" + Date.now()});
      }
      closeDeleteModal();
    });
  }

  function openDeleteModal(user) {
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

  function closeDeleteModal() {
    const modal = el("authDeleteModal");
    if (modal) modal.classList.remove("on");
  }

  function renderOverlay() {
    const view = ensureOverlay();
    const usersEl = el("authUsers");
    if (!usersEl) return;
    // Reset the collapsed login/create panels so a re-shown login page never
    // carries a stale open state.
    const loginForm = el("authLoginForm");
    const createBox = el("authCreateBox");
    const toggleLogin = el("authToggleLogin");
    const toggleCreate = el("authToggleCreate");
    if (loginForm) loginForm.hidden = true;
    if (createBox) createBox.hidden = true;
    if (toggleLogin) toggleLogin.classList.remove("active");
    if (toggleCreate) toggleCreate.classList.remove("active");
    usersEl.innerHTML = authState.users
      // The demo account has its own one-click card above; skip it here so it
      // does not appear twice on the page.
      .filter(u => u.username !== "demo")
      .map(u =>
      '<div class="auth-user-row">'
      + '<button type="button" class="auth-user-btn" data-auth-login="' + escapeHtml(u.user_id) + '">'
      + '<span class="account-user-ava">' + (u.is_local_default ? "★" : "👤") + '</span>'
      + '<span>' + escapeHtml(u.username) + (u.has_password ? ' <em>已锁定</em>' : '') + '</span>'
      + '</button>'
      + '<span class="auth-user-actions">'
      + '<button type="button" class="plain-small" data-rename-user="' + escapeHtml(u.user_id) + '">改名</button>'
      + '<button type="button" class="plain-small auth-delete-btn" data-delete-user="' + escapeHtml(u.user_id) + '">删除</button>'
      + '</span>'
      + '</div>'
    ).join("");
    view.hidden = false;
    view.classList.add("on");
  }

  function showError(msg) {
    const e = el("authError");
    if (!e) return;
    e.textContent = msg;
    e.hidden = false;
  }

  function maybeShowLogin() {
    const locked = authState.users.filter(u => u.has_password);
    if (locked.length && !(authState.current_user && authState.current_user.has_password)) {
      renderOverlay();
    }
  }

  // ── Message handlers (append-only merge, chain preserved) ──
  global.onAuthStatus = (function (prev) {
    return function (msg) {
      if (typeof prev === "function") prev(msg);
      authState = msg;
      renderBadge();
      maybeShowLogin();
    };
  })(global.onAuthStatus);
  global.onAuthChanged = (function (prev) {
    return function (msg) {
      if (typeof prev === "function") prev(msg);
      if (msg && msg.account_reset) {
        localStorage.removeItem("modus_last_db_id");
        if (typeof abandonPendingRunSubmission === "function") {
          abandonPendingRunSubmission("account_switch");
        }
        if (typeof cancelPendingSessionResume === "function") {
          cancelPendingSessionResume("account_switch");
        }
        if (typeof resetTransientRequests === "function") {
          resetTransientRequests("account_switch");
        }
        sessionId = msg.runtime_session_id || sessionId;
        currentDbId = ""; renderedSessionId = "";
        currentModelId = ""; currentModeConfig = {};
        currentReasoningEffort = null;
        transcriptCursors = {};
        Object.keys(transcriptCursorsBySession || {}).forEach((key) => {
          delete transcriptCursorsBySession[key];
        });
        loadSessionMessages("", []);
        applyCurrentWorkspace(null);
        window.ModusWorkspaceManager?.reset?.();
        renderSessionRun(null);
        setMode("default");
        refreshSessionCatalog();
        sendWs({type:"model_repository_get"});
        sendWs({type:"skills_list"});
        sendWs({type:"extensions_list"});
        sendWs({type:"mcp_servers_list"});
      }
      const view = el("authView");
      if (view) { view.hidden = true; view.classList.remove("on"); }
      sendWs({type: "auth_status", request_id: "as-" + Date.now()});
      if (typeof window.ModusAccount !== "undefined" && window.ModusAccount.refresh) window.ModusAccount.refresh();
    };
  })(global.onAuthChanged);
  global.onUserCreated = (function (prev) {
    return function (msg) {
      if (typeof prev === "function") prev(msg);
      sendWs({type: "auth_status", request_id: "as-" + Date.now()});
      if (msg.user && !msg.user.has_password) {
        sendWs({type: "auth_switch_user", user_id: msg.user.user_id, request_id: "sw-" + Date.now()});
      }
    };
  })(global.onUserCreated);
  global.onAuthDemo = global.onAuthDemo || function (msg) {
    const u = msg.user || {};
    const tag = el("authDemoTag");
    if (tag) {
      const cred = u.username || "demo";
      tag.textContent = (u.username ? u.username : "demo") + " · " + (u.password || "123456");
    }
  };
  global.onUserRenamed = global.onUserRenamed || function () {
    sendWs({type: "auth_status", request_id: "as-" + Date.now()});
  };
  global.onUserDeleted = global.onUserDeleted || function () {
    sendWs({type: "auth_status", request_id: "as-" + Date.now()});
  };

  // Request auth state once the socket is open (retry until it is).
  function boot() {
    if (typeof ws !== "undefined" && ws && ws.readyState === WebSocket.OPEN) {
      sendWs({type: "auth_status", request_id: "as-boot-" + Date.now()});
    } else {
      setTimeout(boot, 300);
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  global.ModusAuth = { state: () => authState, refresh: boot, openLoginForUser };
})(window);
