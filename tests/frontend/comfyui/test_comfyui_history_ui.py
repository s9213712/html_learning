"""Frontend checks for ComfyUI history apply/rerun actions."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_history_apply_returns_to_generate_form_and_reports_errors():
    js = _read("public/js/36-comfyui.js")

    assert "function comfyuiHistoryItemId(item)" in js
    assert "item?.id ?? item?.history_id ?? item?.historyId" in js
    assert "function setComfyuiHistoryActionMessage" in js
    assert 'setComfyuiView("generate");' in js
    assert "ComfyUI 歷史套回表單失敗" in js
    assert "找不到這筆 ComfyUI 歷史紀錄，請重新整理歷史。" in js


def test_history_apply_restores_full_generation_payload():
    js = _read("public/js/36-comfyui.js")
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    runtime_routes = _read("routes/comfyui_sections/runtime_routes.py")
    workflow_routes = _read("routes/comfyui_sections/workflow_routes.py")

    assert "function comfyuiHistoryPayload(item = {})" in js
    assert 'const params = item?.params && typeof item.params === "object" ? item.params : {};' in js
    assert "workflow_preset_id" in js
    assert "function comfyuiHistoryInputAssets(item = {}, payload = null)" in js
    assert "function ensureComfyuiHistoryWorkflowSelectOption(presetId" in js
    assert 'option.dataset.historyValue = "1";' in js
    assert '["comfyui-diffusers-model-repo", payload.diffusers_model_repo || ""],' in js
    assert 'function setComfyuiFieldValue(id, value, { preserveMissingOption = false } = {})' in js
    assert 'option.dataset.historyValue = "1";' in js
    assert '["comfyui-diffusers-model-variant", payload.diffusers_model_variant || "", true],' in js
    assert '["comfyui-batch-size", payload.ui_batch_size || payload.batch_size || 1],' in js
    assert '["comfyui-run-count", payload.run_count || 1],' in js
    assert '["comfyui-seed-after-generate", payload.seed_after_generate || (workflowPresetId > 0 ? "random" : "fixed")],' in js
    assert '.filter(([id]) => !(workflowPresetId > 0 && id === "comfyui-model-select"))' in js
    assert 'await applyComfyuiHistoryAssets(comfyuiHistoryInputAssets(item, payload));' in js
    assert 'const targetView = payload.diffusers_model_repo ? "hf" : "generate";' in js
    assert 'setComfyuiView(targetView);' in js
    assert 'checked = targetView !== "hf" && controlEnabled' in js
    assert 'setComfyuiFieldValue("comfyui-controlnet-model", controlnet.model_name || "", { preserveMissingOption: true });' in js
    assert 'setComfyuiFieldValue("comfyui-controlnet-preprocessor", controlnet.preprocessor || "", { preserveMissingOption: true });' in js
    assert 'field?.class_type === "KSamplerAdvanced" && field?.input_name === "noise_seed"' in workflow_js
    assert "function comfyuiTemplateEffectiveFieldValue(field = {})" in workflow_js
    assert "seed_after_generate: seedAfterGenerate" in workflow_js
    assert "run_count: runCount" in workflow_js
    assert '["comfyui-run-count", payload.run_count || 1],' in workflow_js
    assert '["comfyui-seed-after-generate", payload.seed_after_generate || "random"],' in workflow_js
    assert 'comfyuiTemplateSdxlSkipRefiner = skipRefiner;' in workflow_js
    assert 'if (targetId === "comfyui-cfg") return payload.cfg;' in workflow_js
    assert 'if (targetId === "comfyui-seed") return payload.seed;' in workflow_js
    assert 'if (targetId === "comfyui-width") return payload.width;' in workflow_js
    assert 'if (targetId === "comfyui-height") return payload.height;' in workflow_js
    assert 'params": params' in runtime_routes
    assert '"workflow_preset_id": int(row["preset_id"]),' in runtime_routes
    assert 'workflow_run_params["workflow_preset_id"] = int(preset_id)' in workflow_routes


def test_history_rerun_opens_generate_view_for_visible_progress():
    js = _read("public/js/36-comfyui.js")

    assert "async function rerunComfyuiHistory(historyId)" in js
    assert "這筆 ComfyUI 歷史缺少可重跑 ID，請重新整理歷史。" in js
    assert 'API + `/comfyui/history/${encodeURIComponent(targetId)}/rerun`' in js
    assert "setComfyuiMessage(\"正在建立 ComfyUI 重跑工作...\", true);" in js


def test_history_delete_removes_legacy_and_workflow_runs():
    js = _read("public/js/36-comfyui.js")
    runtime_routes = _read("routes/comfyui_sections/runtime_routes.py")

    assert 'data-comfyui-history-delete="${sanitize(historyId)}"' in js
    assert "async function deleteComfyuiHistory(historyId)" in js
    assert "這筆 ComfyUI 歷史缺少可刪除 ID，請重新整理歷史。" in js
    assert 'API + `/comfyui/workflow-runs/${encodeURIComponent(workflowRunId)}`' in js
    assert 'API + `/comfyui/history/${encodeURIComponent(legacyId)}`' in js
    assert 'method: "DELETE"' in js
    assert '@app.route("/api/comfyui/history/<int:history_id>", methods=["DELETE"])' in runtime_routes
    assert '@app.route("/api/comfyui/workflow-runs/<int:run_id>", methods=["DELETE"])' in runtime_routes
