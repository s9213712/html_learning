from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_template_schema_declares_embeddings_as_text_child():
    schema_py = _read("services/comfyui/template/ui_schema.py")
    assert '"id": "text:embeddings"' in schema_py
    assert '"input_type": "embedding_shortcuts"' in schema_py
    assert '"parent_category": "text"' in schema_py


def test_selected_template_renders_embedding_shortcuts_under_text_panel():
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    assert 'field?.input_type === "embedding_shortcuts"' in workflow_js
    assert "renderComfyuiTemplateEmbeddingShortcuts(field)" in workflow_js
    assert "data-comfyui-template-embedding" in workflow_js
    assert "data-comfyui-template-embedding-targets" in workflow_js
    assert "insertComfyuiTemplateEmbeddingToken" in workflow_js
    assert "removeComfyuiEmbeddingTokenFromInput" in workflow_js
    assert "comfyuiTemplateLastFocusedTextFieldId" in workflow_js
    assert "function comfyuiTemplateEmbeddingInsertionTarget" in workflow_js
    assert "comfyuiTemplateLooksNegativeTextTarget" not in workflow_js
    assert "點一下插入，再點一次會從提示詞移除。" in workflow_js
    assert "renderSelectedComfyuiTemplate({ preserveOpenPanels: true });" in workflow_js
    assert "data-comfyui-template-panel-id" in workflow_js


def test_workflow_registry_list_surfaces_manifest_summary_badge():
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    assert "manifest_summary" in workflow_js
    assert "Manifest ${sanitize(String(manifest.panel_count || 0))} panels" in workflow_js


def test_import_preview_modal_renders_embedding_shortcuts_for_text_fields():
    comfyui_js = _read("public/js/36-comfyui.js")
    assert 'field.input_type === "embedding_shortcuts"' in comfyui_js
    assert "data-comfyui-template-importer-embedding" in comfyui_js
    assert "function insertTemplateModalEmbeddingToken(name)" in comfyui_js
    assert "function removeComfyuiEmbeddingTokenFromInput(input, name)" in comfyui_js
    assert "function comfyuiEmbeddingTokenVariants(name)" in comfyui_js
    assert "插入 / 移除" in comfyui_js
    assert 'el.dataset.category === "TEXT"' in comfyui_js
