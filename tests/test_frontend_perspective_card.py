"""Frontend contract: the KANBAN board renders semantic run data by column."""
from _bundle import js_bundle, page_html


def test_board_shell_and_containers_exist():
    page = js_bundle()
    html = page_html()

    # kanban.js exposes the board adapter API.
    assert "ModusKanban" in page
    assert "function columnOfRun(run)" in page
    assert "function cardHtml(run)" in page
    assert "function boardHtml(runs, selectedRunId)" in page
    assert "function renderBoard(store)" in page
    assert "function mountKanban(container)" in page
    assert "function setActiveColumn(columnName)" in page
    # The board containers live in the right panel.
    assert 'id="kbBoard"' in html
    assert 'id="kbColumns"' in html
    assert 'id="kbEmptyState"' in html
    assert 'id="kbRunSelect"' in html
    assert 'id="kbDrawer"' in html
    # Five flow columns are declared.
    assert "待处理" in html or "todo" in page
    assert "已完成" in html or "completed" in page


def test_column_derivation_is_pure_and_phase_driven():
    page = js_bundle()

    # The five columns cover terminal + in-flight runs.
    assert "const COLUMNS = [" in page
    assert "analyzing" in page
    assert "executing" in page
    assert "verifying" in page
    # A completed run lands in the completed column; in-flight runs derive
    # from the latest activity phase, not from the coarse state field.
    assert 'if (state === "completed") return "completed"' in page
    assert "lastActivityPhase(semantic)" in page
    assert "COLUMN_OF_PHASE" in page


def test_card_aggregates_activities_and_badges():
    page = js_bundle()

    # Machine-readable category counts (no string-matching of localized labels).
    assert "countByCategory(activities)" in page
    assert "activity.category" in page
    assert "kb-badge" in page
    assert "outcome.verified === true" in page
    assert 'requires_user_action === true' in page
    # Card face composes read/write/command counts + worker count.
    assert "读 " in page
    assert "写 " in page
    assert "命令 " in page
    assert "worker" in page


def test_board_patches_workbench_store_prototype():
    page = js_bundle()

    # The board follows the store by patching its render + apply.
    assert "ModusWorkbench.WorkbenchStore" in page
    assert "Store.prototype.render" in page
    assert "Store.prototype.applyAuthoritativeRun" in page
    assert "renderBoard(this)" in page


def test_perspective_card_keeps_contract_redlines():
    page = js_bundle()

    # No forbidden globals, no raw path/credential leakage.
    assert "_fallbackEvent" not in page
    assert "storage_path" not in page
    assert "content_hash" not in page
    # Single renderer / single addSystemMsg still enforced elsewhere; kanban
    # must not define its own handleMsg.
    assert "function handleMsg(msg)" in page
    assert page.count("function handleMsg(msg)") == 1
