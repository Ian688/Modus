(function (global) {
  "use strict";

  // Right-panel compatibility adapter.  The panel is now a single KANBAN board
  // (see kanban.js); this module keeps the legacy API that older callers and
  // contract tests relied on, mapping window names onto board actions:
  //
  //   workspace -> empty state (workspace manager)
  //   execution / tasks / subtasks / runs / review / activity / focus
  //              -> open the selected run's detail drawer
  //   document   -> open the document pane in the drawer
  //   browser    -> open the preview pane in the drawer
  //
  // The board itself never switches tabs; it highlights the active column and
  // slides the detail drawer over the columns.

  const DEFAULT_WINDOW = "execution";

  function groupSubtabOf(name) {
    return Object.prototype.hasOwnProperty.call({
      focus: "workspace",
      activity: "workspace",
      tasks: "execution",
      subtasks: "execution",
      runs: "execution",
      document: "artifacts",
      review: "artifacts",
    }, name) ? name : null;
  }

  function activate(name) {
    const requested = (name && String(name)) || DEFAULT_WINDOW;
    if (global.ModusKanban && typeof global.ModusKanban.handleLegacyRoute === "function") {
      global.ModusKanban.handleLegacyRoute(requested);
    }
  }

  function setSubtab(name) {
    // No sub-tabs remain; keep the legacy name normalized for callers that
    // still pass it (contextbar's open() calls setSubtab("overview")).
    return name && ["overview", "focus", "activity", "tasks", "subtasks", "runs", "artifacts", "document", "review"].includes(name) ? name : "overview";
  }

  function init() {
    // Nothing to bind: the board owns the right panel.  Keep the pin button
    // semantics (📌 locks the router) intact for older callers.
    const pin = document.getElementById("wbPinBtn");
    if (pin) {
      pin.addEventListener("click", () => {
        const locked = global.ModusWindowRouter ? !global.ModusWindowRouter.isLocked() : false;
        if (global.ModusWindowRouter) global.ModusWindowRouter.setLocked(locked);
        pin.classList.toggle("active", locked);
        pin.setAttribute("aria-pressed", String(locked));
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  global.ModusWorkbenchWindows = { activate, init, setSubtab };
})(window);
