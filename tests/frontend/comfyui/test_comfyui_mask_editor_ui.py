"""Frontend smoke checks for the ComfyUI in-page mask editor."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_index_html_exposes_mask_editor_controls():
    html = _read("public/index.html")
    assert 'id="comfyui-mask-editor-open-btn"' in html
    assert 'id="comfyui-mask-editor-modal"' in html
    assert 'id="comfyui-mask-editor-source-canvas"' in html
    assert 'id="comfyui-mask-editor-mask-canvas"' in html
    assert 'id="comfyui-mask-editor-width"' in html
    assert 'id="comfyui-mask-editor-height"' in html
    assert 'id="comfyui-mask-editor-resize-btn"' in html
    assert 'id="comfyui-mask-editor-apply-btn"' in html


def test_mask_editor_draws_and_exports_to_mask_image_file():
    js = _read("public/js/36-comfyui.js")
    assert "function openComfyuiMaskEditor()" in js
    assert "function applyComfyuiMaskEditorSize()" in js
    assert "fillComfyuiMaskEditorBlankSource" in js
    assert "return true;" in js
    assert "function applyComfyuiMaskEditor()" in js
    assert "pointerdown" in js and "pointermove" in js
    assert '"destination-out"' in js
    assert 'output.toBlob' in js
    assert 'setComfyuiInputAssetFromFile("mask", file)' in js
    assert 'form.append("mask_image", comfyuiInputAssets.mask.file' in js


def test_template_mask_image_card_can_open_same_editor():
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    assert 'data-comfyui-template-mask-editor="1"' in workflow_js
    assert 'data-comfyui-template-mask-editor="1" disabled' not in workflow_js
    assert "openComfyuiMaskEditor()" in workflow_js
    assert 'binding.assetKey === "mask" ?' not in workflow_js


def test_mask_editor_is_touch_friendly_and_responsive():
    css = _read("public/styles.css")
    assert ".comfyui-mask-editor-modal" in css
    assert ".comfyui-mask-editor-stage" in css
    assert "touch-action: none" in css
    assert "@media (max-width: 860px)" in css


def test_generation_mode_is_inferred_from_source_and_mask_assets():
    js = _read("public/js/36-comfyui.js")
    html = _read("public/index.html")
    assert "const sourceCard = $(COMFYUI_INPUT_ASSET_META.source.cardId);" in js
    assert "comfyuiShouldShowSourceImageCard(mode) || comfyuiDiffusersSupportsImageInputMode()" in js
    assert 'if (upscaleField) upscaleField.style.display = comfyuiHasInputAsset("source") || comfyuiModeUsesUpscale(mode) ? "" : "none";' in js
    assert 'if (comfyuiHasInputAsset("mask")) return "inpaint";' in js
    assert 'return "img2img";' in js
    assert 'return ["img2img", "inpaint", "outpaint", "upscale", "i2v"].includes' in js
    assert 'return "文字生圖：只需要提示詞，不需要來源圖片。";' in js
    assert 'select id="comfyui-generation-mode" hidden aria-hidden="true" tabindex="-1"' in html
    assert 'id="comfyui-generation-mode-auto"' in html
