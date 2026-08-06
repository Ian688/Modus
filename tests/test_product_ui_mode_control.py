from pathlib import Path

from _bundle import css_bundle, js_bundle, page_html


PAGE = Path(__file__).parents[1] / "src/modus/desktop/static/index.html"


def test_default_chat_has_no_user_visible_named_baseline_or_fixed_mode_explainer() -> None:
    page = page_html()

    assert 'id="modeExplain"' not in page
    assert "MODE_PROFILES" not in page
    assert 'id="modeBaseline"' not in page
    assert 'data-tab="default"' not in page
    assert 'id="composerSelect"' in page
    assert 'id="composerMenu"' in page


def test_composer_combines_default_model_selection_and_enhanced_modes() -> None:
    page = js_bundle()
    html = page_html()

    assert 'data-model-id=' in page
    assert "data-mode=" in page
    assert '"moa"' in page
    assert '"peri"' in page
    assert "function chooseDefault" in page
    assert "function chooseMode" in page
    assert 'type:"session_set_model"' in page
    assert 'type:"model_select_default"' in page
    assert 'type:"session_set_mode"' in page
    assert 'id="composerReasoning"' in html
    assert 'id="composerReasoningMenu"' in html
    assert "function chooseReasoning" in page
    assert 'type:"session_set_reasoning"' in page


def test_model_repository_ui_is_provider_grouped_and_never_reads_key_back() -> None:
    page = js_bundle()
    html = page_html()

    assert 'id="repoModelList"' in html
    assert "provider-group" in page
    assert "credential_hint" in page
    assert 'id="repoKey" type="password"' in html
    assert 'data-tab="skills"' in html
    assert 'id="skillList"' in html
    assert 'type:"skills_list"' in page
    assert "m.api_key" not in page
    assert 'type:"model_create"' in page
    assert 'type:"model_update"' in page
    assert 'type:"model_delete"' in page
    assert 'id="repoContextWindow"' in html
    assert 'id="repoReasoningEfforts"' in html
    assert 'id="repoTestBtn"' in html
    assert 'type: "model_test_connection"' in page
    assert 'pendingModelTestRequestId = nextTransientRequestId("model-test")' in page
    assert "payload.request_id = pendingModelTestRequestId" in page
    assert "msg.request_id !== pendingModelTestRequestId" in page
    assert "function resetModelTestState()" in page
    assert "modelCapabilitySummary" in page
    assert 'type:"model_discover"' in page
    assert 'type:"model_create_discovered"' in page
    assert 'id="repoDiscovery"' in html
    assert "capability_sources" in page


def test_mode_settings_send_real_backend_role_configuration() -> None:
    page = js_bundle()
    html = page_html()

    for field in (
        "moaHostTemp", "moaHostContext", "moaHostReasoning",
        "moaRef1Temp", "moaRef1Context", "moaRef1Reasoning",
        "periHostTemp", "periHostContext", "periHostReasoning",
        "periSub1Temp", "periSub1Context", "periSub1Reasoning",
    ):
        assert f'id="{field}"' in html
    assert "function rolePayload" in page
    assert "function saveModeModelConfiguration(mode, roles)" in page
    assert 'type:"mode_models_set",mode,roles' in page
    assert 'saveModeModelConfiguration("moa"' in page
    assert 'saveModeModelConfiguration("peri"' in page
    assert "moa_temperatures" not in page
    assert 'id="periReadinessBtn"' in html
    assert 'type:"peri_git_readiness"' in page


def test_ui_does_not_expose_internal_worldview_or_llm_to_llm_labels() -> None:
    page = page_html()

    assert "🌌 世界观" not in page
    assert "主 LLM → 子 LLM" not in page
    assert "子 LLM 通信区" not in page
    assert "当前聚焦" in page
    assert "协作记录" in page


def test_moa_settings_do_not_offer_unimplemented_vote_or_trigger_controls() -> None:
    page = page_html()

    assert 'id="moaStrategy"' not in page
    assert 'id="moaTrigger"' not in page
    assert "参考意见" in page


def test_mobile_layout_keeps_sessions_and_settings_reachable() -> None:
    page = js_bundle()
    html = page_html()
    css = css_bundle()

    assert 'id="mobileSessionsBtn"' in html
    assert 'aria-label="打开会话列表"' in html
    assert 'id="mobileSettingsBtn"' in html
    assert 'aria-label="打开设置"' in html
    assert 'id="mobileSidebarScrim"' in html
    assert "function setMobileSidebar" in page
    assert "closeMobileSidebar();" in page
    assert "body.mobile-sidebar-open .sidebar" in css
