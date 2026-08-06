from _bundle import css_bundle, js_bundle


def test_timeline_renderer_has_incremental_code_fence_rendering() -> None:
    page = js_bundle()

    # A typed streaming ContentBlock receives accumulated markdown. It shows an
    # open code fence as a real code card before the closing fence arrives.
    assert "function renderTimelineMarkdown(markdown, streaming)" in page
    assert 'class="code-block timeline-stream-code"' in page
    assert 'renderTimelineMarkdown(view.markdown, event.status === "streaming")' in page


def test_timeline_tool_events_prioritize_result_summary_over_raw_log() -> None:
    page = js_bundle()

    assert 'class="timeline-tool"' in page
    assert "function summarizeToolResult(name, result, isError)" in page
    assert "function toolResultViewHtml(name, result, isError)" in page
    assert "displaySummary" in page
    assert "metadata?.operation" in page
    assert 'class="tool-result-summary"' in page
    assert 'class="tool-result-output-details"' in page
    assert "查看输出" in page
    assert "查看执行结果" not in page
    assert "tool_call" in page and "tool_result" in page


def test_settled_thinking_leaves_the_conversation_without_a_duplicate_summary() -> None:
    page = js_bundle()

    assert "function settleThinking(container)" in page
    assert "row.remove()" in page
    assert "分析摘要" not in page
    assert "查看完整分析" not in page
    assert 'class="thinking-content" hidden' in page


def test_tool_operations_are_grouped_into_one_bounded_activity_disclosure() -> None:
    page = js_bundle()

    assert 'node.className = "run-activity"' in page
    assert 'class="run-activity-items"' in page
    assert 'rows.length >= 3' in page
    assert 'activity.node.open = false' in page
    assert "查看输出" in page


def test_assistant_markdown_styles_target_the_current_block_renderer() -> None:
    css = css_bundle()

    assert ".msg.assistant .block-text ul" in css
    assert '.msg.assistant .block-text li::before{content:""' in css
    assert ".msg.assistant .block-text code.il-code" in css


def test_pipe_tables_render_as_one_semantic_scrollable_table() -> None:
    page = js_bundle()
    css = css_bundle()
    assert 'class="markdown-table"' in page
    assert '<thead><tr>' in page
    assert '<th scope="col">' in page
    assert "display:block;width:max-content" in css


def test_default_tool_work_stays_visible_in_the_main_transcript() -> None:
    page = js_bundle()

    assert 'event.channel_id === "host_models" && event.mode === "default"' in page
    assert 'return document.getElementById("chatArea")' in page
    assert "raw.length > 900 || lineCount > 12" in page


def test_parity_features_are_present_before_legacy_renderer_removal() -> None:
    page = js_bundle()

    assert "renderTimelineMarkdown" in page
    assert "timeline-tool" in page
    assert "Legacy renderer removal is deferred" not in page


def test_long_timelines_prune_settled_nodes_and_offer_expand_earlier() -> None:
    page = js_bundle()

    # Virtualization: beyond a mounted-node cap the renderer detaches settled
    # nodes into a cold region (data stays in the event store) and offers an
    # explicit affordance to rehydrate them, instead of unbounded DOM growth.
    assert "MAX_MOUNTED_NODES = 400" in page
    assert "this.coldNodes = new Map()" in page
    assert "_prune(container)" in page
    assert "thinking-preview" in page and "Never detach a live streaming" in page
    assert "timeline-expand-earlier" in page
    assert "_rehydrateEarlier(container)" in page
    assert "_pruneAll()" in page
    assert "this._prune(container)" in page
