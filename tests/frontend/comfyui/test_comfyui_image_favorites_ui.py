"""Frontend wiring checks for ComfyUI image favorites."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_image_favorites_tab_and_generated_favorite_action_are_wired():
    html = _read("public/index.html")
    js = _read("public/js/36-comfyui.js")
    css = _read("public/styles.css")
    routes = _read("routes/comfyui_sections/image_routes.py")
    schema = _read("bootstrap.schema.sql")

    assert 'data-comfyui-view="favorites">圖片收藏</button>' in html
    assert 'id="comfyui-view-favorites" data-comfyui-view-panel="favorites"' in html
    assert 'id="comfyui-favorite-btn"' in html
    assert 'id="comfyui-favorite-civitai-open-btn"' in html
    assert 'id="comfyui-favorite-upload-open-btn"' in html
    assert 'id="comfyui-favorite-modal"' in html
    assert 'id="comfyui-favorite-modal" role="dialog" aria-modal="true" aria-labelledby="comfyui-favorite-modal-title" data-global-modal-close="none"' in html
    assert 'id="comfyui-favorite-civitai-url"' in html
    assert 'id="comfyui-favorite-upload-file"' in html
    assert "/js/36-comfyui.js?v=20260607-image-favorites-history-favorite-dashboard" in html

    assert '"favorites"' in js
    assert "let comfyuiImageFavorites = [];" in js
    assert "async function loadComfyuiImageFavorites()" in js
    assert "async function importComfyuiFavoriteFromCivitai()" in js
    assert "async function favoriteComfyuiGeneratedImage()" in js
    assert "async function saveUploadedComfyuiFavorite()" in js
    assert "async function applyComfyuiFavoriteToForm" in js
    assert "function openComfyuiFavoritePreview" in js
    assert "function comfyuiFavoriteRequirementsMarkup" in js
    assert "function comfyuiFavoriteRequirementExists" in js
    assert "function setComfyuiSamplingFieldsFromValues" in js
    assert "function comfyuiResolvedSamplerValue" in js
    assert '["comfyui-sampler", params.sampler_name || "", true]' not in js
    assert '["comfyui-scheduler", params.scheduler || "", true]' not in js
    assert 'setComfyuiSamplingFieldsFromValues(params.sampler_name || "euler", params.scheduler || "normal");' in js
    assert "data-model-exists" in js
    assert 'data-model-exists="${exists ? "1" : "0"}' in js
    assert '${exists ? "✓" : "×"}' in js
    assert "comfyuiAvailableCheckpoints" in js
    assert "comfyuiAvailableLoras" in js
    assert "comfyuiAvailableEmbeddings" in js
    assert "params.embeddings" in js
    assert "params.source_model_name" in js
    assert '["model", "checkpoint", "base_model"].includes(kindKey)' in js
    assert "function comfyuiFavoritePromptSectionMarkup" in js
    assert "data-comfyui-favorite-copy" in js
    assert "function currentComfyuiPromptTypeForInsertion()" in js
    assert "function bindComfyuiPromptCursorTracking()" in js
    assert "let comfyuiLastFocusedPromptType" in js
    assert "function isNegativeComfyuiEmbedding" not in js
    assert "lastFocusedImporterTextInput" in js
    assert 'modal.dataset.globalModalClose = "none"' in js
    assert 'data-global-modal-close="none"' in js
    assert "workflow_system_bundle_id" in js
    assert 'API + "/comfyui/image-favorites/import-civitai"' in js
    assert 'API + "/comfyui/image-favorites"' in js

    assert ".comfyui-favorites-grid" in css
    assert ".comfyui-favorite-card" in css
    assert ".comfyui-favorite-preview-modal" in css
    assert ".comfyui-favorite-requirement-item" in css
    assert ".comfyui-favorite-preview-section-head" in css
    assert ".comfyui-favorite-copy-btn" in css
    assert ".comfyui-favorite-preview-card > .global-modal-close-row" in css
    assert ".comfyui-favorite-modal > .global-modal-close-row" in css
    assert ".comfyui-favorite-requirement-status" in css
    assert ".comfyui-favorite-requirement-item.exists" in css
    assert ".comfyui-favorite-requirement-item.missing" in css
    assert "place-items: start center" in css
    assert "max-height: 72vh" in css
    assert "overflow: visible" in css
    assert "background: #111522" in css
    assert "border-top: 1px solid rgba(255,255,255,.1)" in css
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
    assert "grid-template-columns: 1fr" in css
    assert "overflow-y: auto" in css

    assert '@app.route("/api/comfyui/image-favorites", methods=["GET"])' in routes
    assert '@app.route("/api/comfyui/image-favorites", methods=["POST"])' in routes
    assert '@app.route("/api/comfyui/image-favorites/import-civitai", methods=["POST"])' in routes
    assert '@app.route("/api/comfyui/image-favorites/<int:favorite_id>/preview", methods=["GET"])' in routes
    assert "CREATE TABLE IF NOT EXISTS comfyui_image_favorites" in schema
