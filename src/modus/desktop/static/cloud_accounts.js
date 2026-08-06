// ═══ Modus 云账户前端：降级态 + 邮箱注册/登录占位 ═══
// 无真实后端时显示"未配置"降级；配置 MODUS_CLOUD_API 后表单可用。
(function (global) {
  "use strict";

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

  // Render the Modus cloud card content from a modus_account_status message.
  function renderCloudStatus(msg) {
    const host = el("modusCloudBox");
    if (!host) return;
    const cloud = msg.cloud || {};
    if (!cloud.configured) {
      host.innerHTML =
        '<div class="cloud-degraded">'
        + '<span class="cloud-icon">☁</span>'
        + '<strong>Modus 云账户尚未配置</strong>'
        + '<p>设置 <code>MODUS_CLOUD_API</code> 环境变量后，即可使用邮箱注册 / 登录云端账户，同步账号与用量。</p>'
        + '</div>';
      return;
    }
    // Configured: show email register/login forms (backend not yet wired).
    host.innerHTML =
      '<div class="cloud-forms">'
      + '<div class="settings-title">登录 Modus 账户</div>'
      + '<form class="auth-login-form" id="cloudLoginForm">'
      + '<input id="cloudLoginEmail" type="email" placeholder="邮箱" autocomplete="email">'
      + '<input id="cloudLoginPassword" type="password" placeholder="口令" autocomplete="current-password">'
      + '<button class="primary-small" type="submit">登录</button>'
      + '</form>'
      + '<div class="auth-create">'
      + '<div class="settings-title">注册新账户</div>'
      + '<input id="cloudRegisterEmail" type="email" placeholder="邮箱">'
      + '<input id="cloudRegisterPassword" type="password" placeholder="设置口令">'
      + '<button class="plain-small" type="button" id="cloudRegisterBtn">注册</button>'
      + '</div>'
      + '</div>';
    wireCloudForms();
  }

  function wireCloudForms() {
    const loginForm = el("cloudLoginForm");
    if (loginForm) loginForm.addEventListener("submit", ev => {
      ev.preventDefault();
      const email = el("cloudLoginEmail")?.value.trim();
      const pw = el("cloudLoginPassword")?.value;
      if (!email) return;
      sendWs({type: "modus_email_login", email, password: pw, request_id: "cl-" + Date.now()});
    });
    const regBtn = el("cloudRegisterBtn");
    if (regBtn) regBtn.addEventListener("click", () => {
      const email = el("cloudRegisterEmail")?.value.trim();
      const pw = el("cloudRegisterPassword")?.value;
      if (!email) return;
      sendWs({type: "modus_email_register", email, password: pw, request_id: "cr-" + Date.now()});
    });
  }

  // Append-only merge (auth.js may also define this).
  global.onModusAccountStatus = (function (prev) {
    return function (msg) {
      if (typeof prev === "function") prev(msg);
      renderCloudStatus(msg);
    };
  })(global.onModusAccountStatus);

  global.ModusCloud = { renderCloudStatus };
})(window);
