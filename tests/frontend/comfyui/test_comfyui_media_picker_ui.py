"""Frontend smoke checks for ComfyUI media modes and reusable image picker."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_index_html_exposes_reusable_image_picker_and_media_modes():
    html = _read("public/index.html")
    assert 'id="comfyui-source-image-picker-btn"' in html
    assert 'id="comfyui-mask-image-picker-btn"' in html
    assert 'id="comfyui-control-image-picker-btn"' in html
    assert 'id="comfyui-image-picker-modal"' in html
    assert 'id="comfyui-preview-card-title"' in html
    assert 'id="comfyui-preview-card-sub"' in html
    for mode in ("t2v", "i2v", "v2v", "t2s", "t2sv"):
        assert f'value="{mode}"' in html


def test_comfyui_js_imports_history_and_drive_images_for_inputs():
    js = _read("public/js/36-comfyui.js")
    assert 'apiFetch(API + "/comfyui/input-image-candidates"' in js
    assert 'apiFetch(API + "/comfyui/import-drive-image"' in js
    assert 'apiFetch(API + "/comfyui/import-history-image"' in js
    assert 'apiFetch(API + "/comfyui/import-uploaded-video"' in js
    assert "cloudFileId" in js
    assert "function renderComfyuiGeneratedMedia(mediaItems" in js
    assert "function comfyuiNormalizeOutputKind(kind)" in js
    assert "function updateComfyuiPreviewCardForOutputKinds(kinds = null)" in js
    assert "function comfyuiGeneratedMediaMarkup(mediaItems = [])" in js


def test_comfyui_generation_results_lazy_load_output_previews():
    js = _read("public/js/36-comfyui.js")
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    assert "async function hydrateComfyuiGeneratedImages" in js
    assert 'apiFetch(API + "/comfyui/image-preview"' in js
    assert "async function hydrateComfyuiGeneratedMedia" in js
    assert 'apiFetch(API + "/comfyui/media-preview"' in js
    assert "function comfyuiDataUrlToBlobUrl(dataUrl, cacheKey = \"\")" in js
    assert "new Blob(chunks, { type: mimeType })" in js
    assert "preview_url: dataUrl ? comfyuiDataUrlToBlobUrl(dataUrl, cacheKey) : \"\"" in js
    assert "preview_url: item.preview_url || comfyuiDataUrlToBlobUrl(item.data_url, existingKey)" in js
    assert "const src = item.preview_url || item.data_url || \"\";" in js
    assert '<video controls preload="metadata" playsinline><source src="${sanitize(src)}"' in js
    assert 'type="${sanitize(item.mime_type || "video/mp4")}"' in js
    assert "const runImages = await hydrateComfyuiGeneratedImages(rawRunImages);" in js
    assert "const runMedia = await hydrateComfyuiGeneratedMedia(Array.isArray(json.media) ? json.media : [], jobId);" in js
    assert "const images = await hydrateComfyuiGeneratedImages(rawImages);" in workflow_js
    assert "const media = await hydrateComfyuiGeneratedMedia(Array.isArray(result.media) ? result.media : [], jobId);" in workflow_js
    assert "updateComfyuiResultButtons(!!images.length);" in workflow_js
    assert 'throw new Error("ComfyUI 未回傳圖片");' in js
    assert 'if (!comfyuiCurrentImage?.data_url) throw new Error("ComfyUI 未回傳圖片");' not in js
    assert "function openComfyuiGeneratedImage" in js
    assert "function closeComfyuiGeneratedImageLightbox" in js
    assert 'overlay.id = "comfyui-image-lightbox";' in js
    assert 'window.open(image.data_url' not in js
    assert 'class="comfyui-output-gallery"' in js
    assert 'data-comfyui-open-image' in js


def test_workflow_template_run_sends_image_field_assignments():
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    assert "function collectComfyuiTemplateImageAssignments(detail)" in workflow_js
    assert "image_field_assignments: imageAssignmentState.assignments" in workflow_js
    assert 'data-comfyui-template-image-picker' in workflow_js
    assert 'data-comfyui-template-video' in workflow_js
    assert 'field?.class_type === "LoadVideo" && field?.input_name === "file"' in workflow_js
    assert "COMFYUI_TEMPLATE_MEDIA_BINDING_KINDS" in workflow_js
    assert "function comfyuiTemplateElementValue" in workflow_js
    assert 'field?.input_type === "checkbox"' in workflow_js


def test_template_locked_model_requirements_keep_customer_edit_action():
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    assert "let comfyuiTemplateEditableModelFields = {};" in workflow_js
    assert "function comfyuiTemplateCanEditLockedModelField" in workflow_js
    assert 'data-comfyui-template-model-edit' in workflow_js
    assert 'data-comfyui-template-model-reset' in workflow_js
    assert 'editableLockedModel: true' in workflow_js
    assert 'comfyuiTemplateSelectCurrentValue' in workflow_js
    assert '目前遠端未列出' in workflow_js
    assert '若此欄原本是 refiner，也可用同一個 checkpoint 跳過 refiner 專用模型' in workflow_js
    assert 'classType === "CLIPVisionLoader"' in workflow_js
    assert "CLIP Vision 模型" in workflow_js


def test_image_picker_styles_are_responsive():
    css = _read("public/styles.css")
    assert ".comfyui-image-picker-modal" in css
    assert ".comfyui-image-picker-list" in css
    assert ".comfyui-generated-media" in css
    assert ".comfyui-generated-media-card img" in css
    assert ".comfyui-output-gallery" in css
    assert ".comfyui-output-main" in css
    assert ".comfyui-image-lightbox" in css
    assert ".comfyui-lightbox-nav" in css
    assert ".comfyui-template-checkbox" in css
    assert ".comfyui-layout {\n      display: grid;" in css
    assert "isolation: isolate;" in css
    assert ".comfyui-panel,\n    .comfyui-preview-card" in css
    assert ".comfyui-input-asset-card > .drive-file-actions" in css
    assert ".comfyui-template-field-card > .drive-file-actions" in css
    assert "@media (max-width: 860px)" in css
