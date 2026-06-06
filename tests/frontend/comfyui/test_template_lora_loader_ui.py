from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_selected_template_lora_loader_renders_interactive_controls():
    workflow_js = _read("public/js/36-comfyui-workflows.js")

    assert 'field?.class_type === "LoraLoader" || field?.class_type === "LoraLoaderModelOnly"' in workflow_js
    assert 'field?.input_name === "lora_name") return { kind: "lora", nodeId: field.node_id }' in workflow_js
    assert 'return { kind: "lora", nodeId: field.node_id }' in workflow_js
    assert 'data-comfyui-template-lora-node' in workflow_js
    assert "let comfyuiTemplateLoraOverrides = {};" in workflow_js
    assert "upsertComfyuiTemplateLora(nodeId, select.value)" in workflow_js
    assert "applyComfyuiPromptTerms(detail.trained_words || [])" in workflow_js
    assert "comfyuiLoraCompatibilityHint(cleanName)" in workflow_js
    assert "若模型不相容，ComfyUI 可能產圖失敗或效果異常" in workflow_js
    assert "detail.supported !== true" not in workflow_js
    assert "addOption(option.value || \"\", option.textContent || option.label || option.value || \"\", false);" in workflow_js
    assert "const triggerText = insertedTerms.length" in workflow_js
    assert "並自動補上 trigger words" in workflow_js


def test_lora_warning_messages_are_deduplicated_and_names_stay_clean():
    comfyui_js = _read("public/js/36-comfyui.js")
    workflow_js = _read("public/js/36-comfyui-workflows.js")

    assert "function normalizeComfyuiLoraName(name)" in comfyui_js
    assert "replace(/（提醒：[^）]*）$/u" in comfyui_js
    assert "function uniqueComfyuiLoraCompatibilityHints(loras = [])" in comfyui_js
    assert "uniqueComfyuiLoraCompatibilityHints(comfyuiSelectedLoras)" in comfyui_js
    assert "seen.has(hint)" in comfyui_js
    assert "normalizeComfyuiLoraName(item?.name)" in comfyui_js
    assert "normalizeComfyuiLoraName(value)" in workflow_js
    assert "normalizeComfyuiLoraName(rawCurrent)" in workflow_js


def test_selected_template_lora_loader_exposes_weight_controls_and_run_inputs():
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    route_py = _read("routes/comfyui_sections/workflow_routes.py")

    assert 'field?.class_type === "LoraLoader" || field?.class_type === "LoraLoaderModelOnly"' in workflow_js
    assert 'field?.input_name === "strength_model" || field?.input_name === "strength_clip"' in workflow_js
    assert 'data-comfyui-template-lora-strength' in workflow_js
    assert "updateComfyuiTemplateLoraStrength(nodeId, field, input.value)" in workflow_js
    assert "function collectComfyuiTemplateUserInputs(detail)" in workflow_js
    assert "user_inputs: requestUserInputs" in workflow_js
    assert 'return selected?.name || field?.current_value || "";' not in workflow_js
    assert 'return selected?.name || "";' in workflow_js
    assert "模板預設 LoRA" in workflow_js
    assert "def _apply_legacy_workflow_user_inputs(workflow_json, user_inputs):" in route_py
    assert "key not in inputs or isinstance(inputs.get(key), list)" in route_py
