// ─── User message card ───
// Centered, no avatar. Clicking the card edits the original message in place;
// attachments (project/folder/file/image/url) stay inside the same card below
// the text and are carried forward when the edited message is submitted.
function userCardHtml(payload) {
  const text = String(payload?.markdown || "").trim();
  const attachments = Array.isArray(payload?.attachments) ? payload.attachments : [];
  const hasText = Boolean(text);
  if (!hasText && !attachments.length) {
    return document.createDocumentFragment();
  }
  const card = document.createElement("div");
  card.className = "user-card";
  card.dataset.expanded = "false";
  card._userAttachments = attachments.map(item => ({...item}));
  card.innerHTML = (hasText
    ? '<div class="user-text-preview">' + escapeHtml(text) + '</div>'
      + '<textarea class="user-text-edit" rows="3" hidden>' + escapeHtml(text) + '</textarea>'
      + '<div class="user-card-actions" hidden>'
      + '<button type="button" data-user-edit-send title="发送修改后的消息">发送</button>'
      + '</div>'
      + '<button type="button" class="user-card-copy" title="复制" data-user-copy="1">'
      + COPY_ICON_SVG + '</button>'
    : '')
    + (attachments.length
      ? '<div class="user-attach">' + attachments.map(attachCardHtml).join("") + '</div>'
      : '');
  const frag = document.createDocumentFragment();
  frag.appendChild(card);
  return frag;
}

// One attachment card by kind. Images render as a thumbnail (no name), files
// carry an extension badge + name, folders/projects an icon + name, URLs a
// favicon + title with a click hint. `_attImageCache` restores a thumbnail
// across reloads when the payload carries a data URL.
const _attImageCache = new Map();

function attachCardHtml(item) {
  const rawKind = item && item.kind;
  const rawLabel = String((item && item.label) || (item && item.value) || "");
  const label = rawLabel.replace(/^["']+|["']+$/g, "");
  const value = String((item && item.value) || label).replace(/^["']+|["']+$/g, "");
  const pathName = value.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "";
  const looksLikeDirectory = (value.startsWith("/") || /^[A-Za-z]:[\\/]/.test(value))
    && !/\.[A-Za-z0-9]{1,10}$/.test(pathName);
  const kind = rawKind === "project" || rawKind === "folder" || looksLikeDirectory
    ? "folder" : rawKind;
  if (kind === "image") {
    const src = item && item.thumb;
    if (src) _attImageCache.set(value || label, src);
    const cached = src || _attImageCache.get(value || label);
    return '<div class="attach-card attach-img" data-kind="image" title="' + escapeHtml(label) + '">'
      + (cached ? '<img src="' + escapeHtml(cached) + '" alt="" loading="lazy">' : '')
      + '</div>';
  }
  const icon = kind === "folder" || kind === "project" ? "📁"
    : kind === "url" ? "◉"
    : /\.([a-z0-9]{1,8})$/i.test(label) ? label.match(/\.([a-z0-9]{1,8})$/i)[1].toUpperCase()
    : "▤";
  if (kind === "url") {
    const href = value;
    const meta = (item && item.meta) || {};
    const title = meta.title || label;
    const favicon = meta.favicon || "";
    return '<a class="attach-card attach-url" data-kind="url" href="' + escapeHtml(href)
      + '" target="_blank" rel="noopener" title="' + escapeHtml(href) + '">'
      + (favicon ? '<span class="attach-ico attach-favicon"><img src="' + escapeHtml(favicon) + '" alt="" loading="lazy"></span>'
        : '<span class="attach-ico">' + icon + '</span>')
      + '<span class="attach-name">' + escapeHtml(title) + '</span>'
      + '<span class="attach-open" aria-hidden="true">↗</span></a>';
  }
  const displayLabel = kind === "folder"
    ? (label.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || label)
    : label;
  return '<div class="attach-card" data-kind="' + escapeHtml(kind || "file") + '">'
    + '<span class="attach-ico">' + icon + '</span>'
    + '<span class="attach-name" title="' + escapeHtml(label) + '">' + escapeHtml(displayLabel) + '</span></div>';
}

// Wire the user-card interactions: hover → show copy; click → expand + edit;
// outside click → collapse. Delegated so it works for live + reloaded cards.
function wireUserCardInteractions(root) {
  const card = root.closest ? root.closest(".user-card") : null;
  if (!card) return;
  const preview = card.querySelector(".user-text-preview");
  const editor = card.querySelector(".user-text-edit");
  const actions = card.querySelector(".user-card-actions");
  const copyBtn = card.querySelector(".user-card-copy");
  const msg = card.closest(".msg");
  if (!preview || !editor) return;

  if (copyBtn) {
    copyBtn.onclick = event => {
      event.stopPropagation();
      const text = preview.textContent || "";
      navigator.clipboard && navigator.clipboard.writeText(text.trim());
    };
  }

  const expand = () => {
    if (card.dataset.expanded === "true") return;
    card.dataset.expanded = "true";
    preview.hidden = true;
    editor.value = (preview.textContent || "").trim();
    editor.hidden = false;
    actions.hidden = false;
    if (copyBtn) copyBtn.hidden = true;
    editor.focus();
  };
  const collapse = () => {
    if (card.dataset.expanded !== "true") return;
    card.dataset.expanded = "false";
    editor.hidden = true;
    actions.hidden = true;
    preview.hidden = false;
    if (copyBtn) copyBtn.hidden = false;
  };

  card.addEventListener("click", event => {
    if (event.target.closest("a,button")) return;
    expand();
  });
  const sendBtn = actions && actions.querySelector("[data-user-edit-send]");
  if (sendBtn) {
    sendBtn.onclick = event => {
      event.stopPropagation();
      const t = editor.value.trim();
      if (!t) return;
      if (typeof sendUserEditedMessage === "function") {
        sendUserEditedMessage(t, msg && msg.dataset.eventId, card._userAttachments || []);
      }
      collapse();
    };
  }
  editor.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      sendBtn && sendBtn.click();
    }
  });
  document.addEventListener("click", (e) => {
    if (card.dataset.expanded === "true" && !card.contains(e.target)) collapse();
  });
  // Expose collapse for cleanup.
  if (msg) msg._collapseUserCard = collapse;
}
