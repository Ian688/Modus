// ─── Workspace manager ───
// Project folders are runtime infrastructure, so their lifecycle lives in the
// Workbench rather than beside the message composer. Records are account-local;
// forgetting one never deletes files from disk.
(function (global) {
  "use strict";

  const pathForm = document.getElementById("workspacePathForm");
  const pathInput = document.getElementById("workspacePathInput");
  const listEl = document.getElementById("workspaceMemoryList");
  const statusEl = document.getElementById("workspaceManagerStatus");
  if (!listEl) return;

  let current = null;
  let openRequestId = "";

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, char => ({
      "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;",
    })[char]);
  }

  function connected() {
    return typeof ws !== "undefined" && ws && ws.readyState === WebSocket.OPEN;
  }

  function status(text, tone="") {
    if (!statusEl) return;
    statusEl.textContent = String(text || "");
    statusEl.dataset.tone = tone;
  }

  function send(packet) {
    if (!connected()) {
      status("连接尚未就绪，请稍后再试", "error");
      return false;
    }
    ws.send(JSON.stringify(packet));
    return true;
  }

  function refresh() {
    render();
  }

  function setCurrent(workspace) {
    current = workspace?.workspace_id && workspace?.root ? {...workspace} : null;
    render();
  }

  function render() {
    if (!current) {
      listEl.innerHTML = '<article class="wb-workspace-memory wb-workspace-empty">'
        + '<span class="wb-workspace-empty-copy"><strong>未设置工作区</strong><small>默认本地处理；发送文件内容前会确认</small></span>'
        + '<button type="button" class="wb-workspace-more" data-workspace-menu-toggle="empty" aria-label="工作区管理" aria-haspopup="menu" aria-expanded="false">•••</button>'
        + '<div class="wb-workspace-menu" role="menu" data-workspace-menu="empty" hidden>'
        + '<button type="button" role="menuitem" data-workspace-add-browse>设置工作区</button>'
        + '</div></article>';
      bindMenus();
      return;
    }
    listEl.innerHTML = '<article class="wb-workspace-memory" data-active="true">'
      + '<div class="wb-workspace-current">'
      + '<span class="wb-workspace-mark">W</span><span class="wb-workspace-copy"><strong>' + esc(current.name || "工作区") + '</strong>'
      + '<small title="' + esc(current.root || "") + '">' + esc(current.root || "") + '</small></span></div>'
      + '<button type="button" class="wb-workspace-more" data-workspace-menu-toggle="' + esc(current.workspace_id) + '" aria-label="工作区管理" aria-haspopup="menu" aria-expanded="false">•••</button>'
      + '<div class="wb-workspace-menu" role="menu" data-workspace-menu="' + esc(current.workspace_id) + '" hidden>'
      + '<button type="button" role="menuitem" data-workspace-add-browse>修改工作区</button>'
      + '<button type="button" role="menuitem" class="danger" data-workspace-forget="' + esc(current.workspace_id) + '">移除工作区</button>'
      + '</div></article>';

    bindMenus();
    listEl.querySelectorAll("[data-workspace-forget]").forEach(button => {
      button.addEventListener("click", () => {
        closeMenus();
        const label = current?.name || "这个工作区";
        if (!global.confirm('移除当前会话的“' + label + '”工作区？\n源文件不会被删除。')) return;
        status("正在移除工作区…");
        send({
          type:"workspace_forget", workspace_id:button.dataset.workspaceForget,
          request_id:"workspace-forget-" + Date.now(),
        });
      });
    });
  }

  function bindMenus() {
    listEl.querySelectorAll("[data-workspace-menu-toggle]").forEach(button => {
      button.addEventListener("click", event => {
        event.stopPropagation();
        const id = button.dataset.workspaceMenuToggle;
        const menu = listEl.querySelector('[data-workspace-menu="' + CSS.escape(id) + '"]');
        const willOpen = Boolean(menu?.hidden);
        closeMenus();
        if (!menu || !willOpen) return;
        menu.hidden = false;
        button.setAttribute("aria-expanded", "true");
        menu.querySelector("button:not(:disabled)")?.focus();
      });
    });
    listEl.querySelectorAll("[data-workspace-add-browse]").forEach(button => {
      button.addEventListener("click", () => {
        closeMenus();
        status("");
        pickDirectory();
      });
    });
  }

  function closeMenus() {
    listEl.querySelectorAll("[data-workspace-menu]").forEach(menu => { menu.hidden = true; });
    listEl.querySelectorAll("[data-workspace-menu-toggle]").forEach(button => {
      button.setAttribute("aria-expanded", "false");
    });
  }

  function openPath(path) {
    const value = String(path || "").trim();
    if (!value) return;
    openRequestId = "workspace-open-" + Date.now();
    status("正在添加工作区…");
    send({type:"workspace_open", path:value, request_id:openRequestId});
  }

  function pickDirectory() {
    if (openRequestId) return;
    openRequestId = "workspace-pick-" + Date.now();
    status("正在打开本地文件夹选择器…");
    if (!send({type:"workspace_pick", request_id:openRequestId})) openRequestId = "";
  }

  function open(options={}) {
    global.ModusWorkbenchWindows?.activate?.("workspace");
    global.ModusWorkbenchWindows?.setSubtab?.("overview");
    if (global.innerWidth <= 1100 && typeof setWorkbenchPanel === "function") {
      setWorkbenchPanel(true);
    }
    refresh();
    if (options.focus) setTimeout(() => listEl.querySelector("[data-workspace-menu-toggle]")?.focus(), 50);
  }

  pathForm?.addEventListener("submit", event => {
    event.preventDefault();
    openPath(pathInput.value);
  });
  document.addEventListener("click", event => {
    if (!listEl.contains(event.target)) closeMenus();
  });
  listEl.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    const openButton = listEl.querySelector('[data-workspace-menu-toggle][aria-expanded="true"]');
    closeMenus();
    openButton?.focus();
  });

  global.ModusWorkspaceManager = {
    open, refresh, setCurrent,
    reset() { current = null; render(); status(""); },
    handleWorkspaceList(message) {
      return true;
    },
    handleWorkspaceOpened(message) {
      if (openRequestId && message.request_id !== openRequestId) return false;
      openRequestId = "";
      const workspace = message.workspace;
      if (!workspace?.workspace_id) return true;
      status("工作区已添加，正在启用…", "success");
      send({type:"session_set_workspace", session_id:String(currentDbId || ""), workspace_id:workspace.workspace_id});
      return true;
    },
    handlePickCancelled(message) {
      if (message.request_id !== openRequestId) return false;
      openRequestId = "";
      status("");
      return true;
    },
    handleError(message) {
      if (!["workspace_pick", "workspace_open"].includes(message.operation)) return false;
      if (openRequestId && message.request_id !== openRequestId) return false;
      openRequestId = "";
      status(message.message || "未能设置工作区", "error");
      if (message.operation === "workspace_pick") {
        pathForm.hidden = false;
        pathInput.focus();
      }
      return true;
    },
    handleForgotten() { status("工作区已移除，源文件未受影响", "success"); refresh(); },
  };

  setCurrent(typeof currentWorkspace !== "undefined" ? currentWorkspace : null);
  if (connected()) refresh();
})(window);
