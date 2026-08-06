from pathlib import Path


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src/modus/desktop/static"


def test_artifact_viewer_has_accessible_states_and_actions() -> None:
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    viewer = page[page.index('id="artifactViewerOverlay"'):page.index("<!-- Custom Confirm Modal -->")]

    assert 'role="dialog"' in viewer
    assert 'aria-modal="true"' in viewer
    assert 'aria-labelledby="artifactViewerTitle"' in viewer
    assert 'aria-describedby="artifactViewerStatus"' in viewer
    assert 'role="status"' in viewer
    assert 'role="alert"' in viewer
    for element_id in (
        "artifactViewerTitle", "artifactViewerKind", "artifactViewerSize",
        "artifactViewerId", "artifactViewerLoading", "artifactViewerError",
        "artifactViewerContent", "artifactViewerCopyBtn",
        "artifactViewerDownloadBtn", "artifactViewerRetryBtn",
        "artifactViewerCloseBtn", "artifactViewerDoneBtn",
    ):
        assert f'id="{element_id}"' in viewer
    assert "storage_path" not in viewer
    assert "content_hash" not in viewer


def test_artifact_viewer_exposes_transport_neutral_safe_render_api() -> None:
    bindings = (STATIC / "bindings.js").read_text(encoding="utf-8")
    shell = bindings[
        bindings.index("// ═══ Artifact Viewer UI shell ═══"):
        bindings.index("// ═══ Event Bindings ═══")
    ]

    for function_name in (
        "getArtifactViewerState", "openArtifactViewer",
        "renderArtifactViewerLoading", "renderArtifactViewerContent",
        "renderArtifactViewerError", "closeArtifactViewer",
    ):
        assert f"function {function_name}" in shell
    assert 'document.getElementById("artifactViewerContent").textContent' in shell
    assert 'document.getElementById("artifactViewerErrorMessage").textContent' in shell
    assert 'new CustomEvent("modus:artifact-viewer-retry"' in shell
    assert "navigator.clipboard.writeText" in shell
    assert "URL.createObjectURL(new Blob" in shell
    assert 'event.key === "Escape"' in shell
    assert 'event.key !== "Tab"' in shell
    assert "WebSocket" not in shell
    assert "ws.send" not in shell
    assert "storage_path" not in shell
    assert "content_hash" not in shell


def test_artifact_viewer_is_a_responsive_drawer() -> None:
    css = (STATIC / "workbench.css").read_text(encoding="utf-8")

    assert ".artifact-viewer-overlay[hidden]" in css
    assert ".artifact-viewer{" in css
    assert ".artifact-viewer-content" in css
    assert "@media(max-width:640px)" in css
    assert "body.artifact-viewer-open" in css
    assert "@media(prefers-reduced-motion:reduce)" in css
