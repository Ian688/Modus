"""Frontend contract: tool rows link to persisted full-result artifacts."""
from _bundle import js_bundle


def test_tool_row_renders_full_result_artifact_link():
    page = js_bundle()

    assert 'class="timeline-tool-artifacts"' in page
    assert "tool-result-artifact" in page
    assert 'data-artifact-id="' in page
    assert "查看完整结果产物" in page


def test_tool_row_artifact_button_is_wired_to_viewer():
    page = js_bundle()

    # The button handler calls requestArtifactContent (which opens the viewer).
    assert ".tool-result-artifact" in page
    assert "requestArtifactContent(artifactId)" in page
    assert "btn.dataset.artifactId" in page


def test_progressive_disclosure_is_preserved_beside_artifact_link():
    page = js_bundle()

    # The existing bounded preview/summary renderer still exists.
    assert "function toolResultViewHtml(name, result, isError)" in page
    assert 'class="tool-result-output-details"' in page
    assert "查看输出" in page
