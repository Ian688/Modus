/* Browser element annotation (Phase A2).
 *
 * Injected into the preview iframe (same-origin via /api/preview) by the parent
 * window when the user clicks "批注".  Makes every visible element selectable,
 * lets the user pick multiple elements and write an annotation on each, then
 * posts the {selector, text, annotation} list back to the parent so it can be
 * sent to the LLM.
 *
 * Contract with the parent window (moduswindows.js):
 *   parent → iframe (postMessage, targetOrigin="*"):
 *     {cmd:"annotate.on"}   enable annotation mode (hover highlight)
 *     {cmd:"annotate.pick"} re-enter pick mode after a draft
 *     {cmd:"annotate.off"}  disable, clear selection
 *     {cmd:"annotate.clear"} clear selection, stay on
 *   iframe → parent (postMessage, targetOrigin="*"):
 *     {type:"modus-annotate:ready"}
 *     {type:"modus-annotate:submit", url, items:[{selector,tag,id,text,annotation,rect}]}
 *
 * The CSS selector is the cross-surface contract: the Agent reproduces the same
 * element in its headless browser with browser_click(selector)/browser_extract(selector).
 */

(function () {
  "use strict";
  if (window.__MODUS_ANNOTATE_INJECTED__) return;
  window.__MODUS_ANNOTATE_INJECTED__ = true;

  var STATE = { mode: "off", selected: [], hoverEl: null, nextIndex: 1 };

  // ── selector builder (pure, exported for tests) ──
  function buildSelector(el, depth) {
    depth = depth || 12;
    var parts = [];
    while (el && el.tagName && el.tagName.toLowerCase() !== "html" && depth-- > 0) {
      var part = el.tagName.toLowerCase();
      if (el.id) {
        part += "#" + el.id;
      } else {
        var cls = Array.prototype.filter.call(el.classList || [], function (c) {
          return /^[a-z][\w-]*$/i.test(c);
        }).slice(0, 2);
        if (cls.length) part += "." + cls.join(".");
      }
      var parent = el.parentElement;
      if (parent) {
        var sibs = Array.prototype.filter.call(parent.children, function (c) {
          return c.tagName === el.tagName;
        });
        if (sibs.length > 1) {
          part += ":nth-of-type(" + (sibs.indexOf(el) + 1) + ")";
        }
      }
      parts.unshift(part);
      el = parent;
    }
    return parts.join(" > ");
  }

  // ── injected style: outline + fixed bubble, never disturbs layout ──
  var style = document.createElement("style");
  style.textContent =
    ".modus-annotate *{cursor:crosshair !important}" +
    ".modus-annotate .modus-annotate-target{outline:2px solid #f59e0b !important;outline-offset:2px;box-shadow:0 0 0 4px rgba(245,158,11,.18)}" +
    ".modus-annotate .modus-annotate-picked{outline:2px solid #3b82f6 !important;outline-offset:2px;box-shadow:0 0 0 4px rgba(59,130,246,.22)}" +
    ".modus-annotate .modus-annotate-pin{position:fixed;z-index:2147483647;min-width:18px;height:18px;padding:0 4px;border-radius:999px;background:#3b82f6;color:#fff;font:600 11px/18px sans-serif;text-align:center;pointer-events:none}" +
    ".modus-annotate .modus-annotate-bubble{position:fixed;z-index:2147483647;width:260px;background:#fff;border:1px solid #d1d5db;border-radius:8px;box-shadow:0 8px 30px rgba(0,0,0,.18);font:13px/1.5 -apple-system,sans-serif;color:#111}" +
    ".modus-annotate .modus-annotate-bubble textarea{width:100%;box-sizing:border-box;min-height:56px;border:1px solid #d1d5db;border-radius:6px;padding:6px;font:13px/1.4 sans-serif;resize:vertical}" +
    ".modus-annotate .modus-annotate-bubble .modus-annotate-actions{display:flex;gap:6px;justify-content:flex-end;margin-top:6px}" +
    ".modus-annotate .modus-annotate-bubble button{font:600 12px sans-serif;border:1px solid #d1d5db;border-radius:6px;padding:4px 10px;background:#f9fafb;cursor:pointer}" +
    ".modus-annotate .modus-annotate-bubble button.modus-annotate-send{background:#3b82f6;color:#fff;border-color:#3b82f6}";
  document.head.appendChild(style);

  // ── helpers ──
  function isNoise(el) {
    if (!el || !el.tagName) return true;
    var t = el.tagName.toLowerCase();
    return t === "script" || t === "style" || t === "link" || t === "meta" ||
      t === "html" || t === "body" || el.classList.contains("modus-annotate-bubble") ||
      el.classList.contains("modus-annotate-pin");
  }

  function refreshPins() {
    document.querySelectorAll(".modus-annotate-pin").forEach(function (p) { p.remove(); });
    STATE.selected.forEach(function (item) {
      var el = document.querySelector(item.selector);
      if (!el) return;
      var r = el.getBoundingClientRect();
      var pin = document.createElement("div");
      pin.className = "modus-annotate-pin";
      pin.textContent = String(item.index);
      pin.style.left = (r.left + r.width - 4) + "px";
      pin.style.top = (r.top - 9) + "px";
      document.documentElement.appendChild(pin);
    });
  }

  function showBubble(x, y, item) {
    var old = document.querySelector(".modus-annotate-bubble");
    if (old) old.remove();
    var b = document.createElement("div");
    b.className = "modus-annotate-bubble";
    b.style.left = Math.min(x, window.innerWidth - 270) + "px";
    b.style.top = Math.max(8, y - 10) + "px";
    var ta = document.createElement("textarea");
    ta.value = item.annotation || "";
    ta.placeholder = "点评这个元素（可留空）";
    var actions = document.createElement("div");
    actions.className = "modus-annotate-actions";
    var more = document.createElement("button");
    more.textContent = "+ 添加另一个元素";
    more.onclick = function () { b.remove(); STATE.mode = "pick"; };
    var send = document.createElement("button");
    send.className = "modus-annotate-send";
    send.textContent = "发送给 Agent";
    send.onclick = function () {
      item.annotation = ta.value.trim();
      submitToParent();
    };
    var cancel = document.createElement("button");
    cancel.textContent = "取消";
    cancel.onclick = function () { b.remove(); STATE.mode = "off"; };
    actions.appendChild(more);
    actions.appendChild(cancel);
    actions.appendChild(send);
    b.appendChild(ta);
    b.appendChild(actions);
    document.documentElement.appendChild(b);
    ta.focus();
  }

  function pickElement(el) {
    if (isNoise(el)) return;
    var existing = STATE.selected.find(function (it) { return it.selector === buildSelector(el); });
    if (existing) { existing.annotation = ""; showBubble(0, 0, existing); return; }
    var item = {
      selector: buildSelector(el),
      tag: el.tagName.toLowerCase(),
      id: el.id || "",
      text: (el.innerText || el.textContent || "").trim().slice(0, 200),
      annotation: "",
      rect: (function (r) { return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) }; })(el.getBoundingClientRect()),
      index: STATE.nextIndex++,
    };
    captureElementShot(el, item);
    STATE.selected.push(item);
    if (STATE.selected.length > 20) STATE.selected.shift();
    showBubble(0, 0, item);
    refreshPins();
  }

  // Capture a local screenshot of one element via foreignObject → canvas →
  // dataURL.  Same-origin only (the preview proxy guarantees it), so the
  // foreignObject serializes the element's DOM without tainting the canvas.
  // Best-effort: fonts/images from cross-origin origins can taint the canvas
  // and make toDataURL throw — then we just omit the image.
  function captureElementShot(el, item) {
    try {
      var rect = el.getBoundingClientRect();
      if (rect.width < 8 || rect.height < 8) return;  // too small to be useful
      var width = Math.min(Math.ceil(rect.width), 600);
      var height = Math.min(Math.ceil(rect.height), 600);
      var clone = el.cloneNode(true);
      // Scale the clone so the captured region matches the visible rect.
      var scale = 1;
      var foreign = document.createElementNS("http://www.w3.org/2000/svg", "foreignObject");
      foreign.setAttribute("width", String(width));
      foreign.setAttribute("height", String(height));
      foreign.setAttribute("style", "overflow:visible");
      foreign.appendChild(clone);
      var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
      svg.setAttribute("width", String(width));
      svg.setAttribute("height", String(height));
      svg.appendChild(foreign);
      var xml = new XMLSerializer().serializeToString(svg);
      var dataUrl = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(xml);
      var img = new Image();
      img.onload = function () {
        var canvas = document.createElement("canvas");
        canvas.width = width; canvas.height = height;
        var ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, width, height);
        try {
          var png = canvas.toDataURL("image/png");
          if (png && png.length < 1500000) item.image = png;
        } catch (_) { /* tainted canvas — omit image */ }
        refreshPins();
      };
      img.onerror = function () { /* omit image */ };
      img.src = dataUrl;
    } catch (_) { /* omit image */ }
  }

  function submitToParent() {
    refreshPins();
    parent.postMessage({
      source: "modus-annotate",
      type: "modus-annotate:submit",
      url: location.href,
      items: STATE.selected.map(function (it) {
        return { selector: it.selector, tag: it.tag, id: it.id, text: it.text,
                 annotation: it.annotation, rect: it.rect,
                 image: it.image || null };
      }),
    }, "*");
    STATE.mode = "off";
  }

  // ── event listeners (delegated; active only in pick mode) ──
  document.addEventListener("mouseover", function (e) {
    if (STATE.mode !== "pick") return;
    var el = e.target;
    if (isNoise(el)) return;
    if (STATE.hoverEl) STATE.hoverEl.classList.remove("modus-annotate-target");
    STATE.hoverEl = el;
    el.classList.add("modus-annotate-target");
  });
  document.addEventListener("mouseout", function () {
    if (STATE.hoverEl) { STATE.hoverEl.classList.remove("modus-annotate-target"); STATE.hoverEl = null; }
  });
  document.addEventListener("click", function (e) {
    if (STATE.mode !== "pick") return;
    // The annotation bubble and its buttons are OUR UI: let their clicks pass
    // through to the buttons' own handlers instead of picking an element.
    if (e.target.closest && e.target.closest(".modus-annotate-bubble, .modus-annotate-pin")) return;
    e.preventDefault();
    e.stopPropagation();
    var el = e.target;
    pickElement(el);
  }, true);
  window.addEventListener("resize", refreshPins);
  window.addEventListener("scroll", refreshPins, true);

  // ── parent commands ──
  window.addEventListener("message", function (e) {
    var msg = e.data;
    if (!msg || typeof msg !== "object" || !msg.cmd) return;
    if (msg.cmd === "annotate.on") {
      STATE.mode = "pick";
      document.documentElement.classList.add("modus-annotate");
      refreshPins();
      parent.postMessage({ source: "modus-annotate", type: "modus-annotate:ready" }, "*");
    } else if (msg.cmd === "annotate.pick") {
      STATE.mode = "pick";
    } else if (msg.cmd === "annotate.off") {
      STATE.mode = "off";
      document.documentElement.classList.remove("modus-annotate");
      document.querySelectorAll(".modus-annotate-bubble,.modus-annotate-pin").forEach(function (n) { n.remove(); });
      STATE.selected = [];
      STATE.nextIndex = 1;
    } else if (msg.cmd === "annotate.clear") {
      STATE.selected = [];
      STATE.nextIndex = 1;
      refreshPins();
    }
  });

  // Expose for tests / parent introspection.
  window.__MODUS_ANNOTATE__ = { state: STATE, buildSelector: buildSelector };
})();
