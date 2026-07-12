"""§8.3 / §9 frontend modal regression — string-grep checks for the
ComfyUI template importer UI wired into 36-comfyui.js + index.html."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_index_html_carries_template_import_button():
    index_html = _read("public/index.html")
    assert 'id="comfyui-template-import-btn"' in index_html, "import button must be present in the comfyui module HTML"
    assert "匯入 workflow" in index_html, "Chinese button label expected"


def test_36_comfyui_js_defines_template_importer_namespace():
    js = _read("public/js/36-comfyui.js")
    assert "ComfyUITemplateImporter" in js, "namespace declaration missing"
    assert "openImportModal" in js
    assert "closeImportModal" in js


def test_36_comfyui_js_calls_preview_endpoint():
    js = _read("public/js/36-comfyui.js")
    assert '"/api/comfyui/templates/preview"' in js, "/preview endpoint should be the upload target"
    assert 'formData.append("workflow", file)' in js, "workflow file part must be named 'workflow'"
    assert '"X-CSRF-Token"' in js, "CSRF header required on state-changing fetch"


def test_36_comfyui_js_calls_import_endpoint_with_preview_token():
    js = _read("public/js/36-comfyui.js")
    assert '"/api/comfyui/templates/import"' in js
    assert 'preview_token' in js, "import body must carry preview_token field"


def test_36_comfyui_js_renders_capability_badge():
    js = _read("public/js/36-comfyui.js")
    assert 'overall === "SUPPORTED"' in js
    assert 'overall === "PARTIALLY_SUPPORTED"' in js
    # UNSUPPORTED disables the import button per §8.3
    assert 'importBtn.disabled = overall === "UNSUPPORTED"' in js


def test_36_comfyui_js_renders_panels_in_order():
    """§9.2: panels iterated in the order the backend returns them."""
    js = _read("public/js/36-comfyui.js")
    assert "uiSchema.panels" in js
    assert "panel.collapsed_default" in js, "should honor backend-supplied collapsed default"


def test_36_comfyui_js_blocks_import_until_token_obtained():
    js = _read("public/js/36-comfyui.js")
    assert "尚未取得 preview_token" in js or "preview_token" in js
    # Active imports are disabled against double submission, then restored to
    # the capability-derived state once the request settles.
    assert 'if (importBtn) importBtn.disabled = true;' in js
    assert 'importBtn.disabled = currentCapability?.overall === "UNSUPPORTED";' in js
    assert 'currentToken = null' in js


def test_36_comfyui_js_button_binding_idempotent():
    """The button click handler should bind exactly once per element."""
    js = _read("public/js/36-comfyui.js")
    assert "cuiTemplateBound" in js, "expected an idempotency marker on the bound button"


def test_36_comfyui_js_escapes_blocker_strings_for_html():
    """Blockers come from the backend (server-controlled) but still go through
    an escape helper to defend against XSS via custom workflow upload paths."""
    js = _read("public/js/36-comfyui.js")
    assert "escapeHtmlSafe" in js or "innerHTML" not in js or 'replace(/</g,' in js
