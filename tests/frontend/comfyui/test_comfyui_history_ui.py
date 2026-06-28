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
    assert "function comfyuiHistorySeedModeForApply(payload = {}, workflowPresetId = 0)" in js
    assert '["comfyui-seed-after-generate", historySeedMode],' in js
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
    assert 'data-comfyui-template-run-count="1"' in workflow_js
    assert 'vae: selectedVae && selectedVae !== COMFYUI_VAE_BUILTIN ? selectedVae : "",' in workflow_js
    assert '["comfyui-run-count", payload.run_count || 1],' in workflow_js
    assert '["comfyui-seed-after-generate", payload.seed_after_generate || "random"],' in workflow_js
    assert 'comfyuiTemplateSdxlSkipRefiner = skipRefiner;' in workflow_js
    assert 'Object.prototype.hasOwnProperty.call(payload || {}, "prompt")' in workflow_js
    assert 'label: `${currentRaw}（目前未列出，仍會送出）`' in workflow_js
    assert "return resolved || current;" in workflow_js
    assert 'if (targetId === "comfyui-cfg") return payload.cfg;' in workflow_js
    assert 'if (targetId === "comfyui-seed") return payload.seed;' in workflow_js
    assert 'if (targetId === "comfyui-width") return payload.width;' in workflow_js
    assert 'if (targetId === "comfyui-height") return payload.height;' in workflow_js
    assert 'params": params' in runtime_routes
    assert 'params = _runtime_workflow_snapshot_params(' in runtime_routes
    assert 'params["seed_after_generate"] = _runtime_workflow_rerun_seed_mode(params, request_body)' in runtime_routes
    assert '"workflow_preset_id": int(row["preset_id"]),' in runtime_routes
    assert '@app.route("/api/comfyui/resources", methods=["GET"])' in runtime_routes
    assert 'workflow_run_params["workflow_preset_id"] = int(preset_id)' in workflow_routes
    assert 'workflow_run_params["requested_width"] = requested_width' in workflow_routes
    assert 'workflow_run_params["requested_height"] = requested_height' in workflow_routes
    assert 'workflow_run_params["output_width"] = requested_width' in workflow_routes
    assert 'workflow_run_params["output_height"] = requested_height' in workflow_routes
    assert 'workflow_json, vae_changed = _apply_workflow_vae_override(workflow_json, selected_vae)' in workflow_routes


def test_qwen_edit_resize_prefers_requested_output_dimensions():
    routes = _read("routes/comfyui.py")

    assert '(params or {}).get("requested_width")' in routes
    assert '(params or {}).get("requested_height")' in routes
    assert 'or (params or {}).get("output_width")' in routes
    assert 'or (params or {}).get("output_height")' in routes
    assert 'or (params or {}).get("width")' in routes
    assert 'or (params or {}).get("height")' in routes


def test_load_last_settings_restores_template_mode_checkbox_and_dynamic_template_state():
    js = _read("public/js/36-comfyui.js")

    assert '"comfyui-generation-mode",' in js
    assert '"comfyui-template-select",' in js
    assert 'if (el.type === "checkbox") {' in js
    assert "draft[id] = !!el.checked;" in js
    assert 'el.checked = value === true || value === "true" || value === "1" || value === 1;' in js
    assert "function serializableComfyuiInputAssets()" in js
    assert "draft.input_assets = serializableComfyuiInputAssets();" in js
    assert "draft.template_field_overrides = comfyuiJsonClone(comfyuiTemplateFieldOverrides, {});" in js
    assert "draft.template_lora_overrides = comfyuiJsonClone(comfyuiTemplateLoraOverrides, {});" in js
    assert "function restoreComfyuiTemplateDraftState(draft = {})" in js
    assert "async function restoreComfyuiDraftForManualLoad()" in js
    assert 'await loadComfyuiWorkflowPresets({ silentTemplateReload: true });' in js
    assert 'await loadComfyuiSelectedTemplateDetail(templatePresetId, { silent: true, applyDefaults: false });' in js
    assert "restoreComfyuiTemplateDraftState(draft);" in js
    assert "await restoreComfyuiDraftInputAssets(draft);" in js
    assert "await restoreComfyuiDraftForManualLoad();" in js


def test_history_rerun_opens_generate_view_for_visible_progress():
    js = _read("public/js/36-comfyui.js")

    assert "async function rerunComfyuiHistory(historyId)" in js
    assert "這筆 ComfyUI 歷史缺少可重跑 ID，請重新整理歷史。" in js
    assert 'API + `/comfyui/history/${encodeURIComponent(targetId)}/rerun`' in js
    assert "setComfyuiMessage(\"正在建立 ComfyUI 重跑工作...\", true);" in js


def test_history_items_can_be_favorited_without_using_current_preview_state():
    js = _read("public/js/36-comfyui.js")

    assert "function comfyuiHistoryCanFavorite(item = {})" in js
    assert "function comfyuiHistoryImageCount(item = {}, payload = null)" in js
    assert "function comfyuiHistoryFavoriteParams(item = {})" in js
    assert "async function favoriteComfyuiHistoryImage(historyId)" in js
    assert 'data-comfyui-history-favorite="${sanitize(historyId)}"' in js
    assert 'button.getAttribute("data-comfyui-history-favorite")' in js
    assert 'API + "/comfyui/image-favorites"' in js
    assert 'source_type: item.history_source === "workflow" ? "workflow_history" : "history"' in js
    assert "image_ref: image.image_ref" in js
    assert "params," in js
    assert "comfyuiCurrentImage =" not in js[js.index("async function favoriteComfyuiHistoryImage(historyId)") : js.index("async function rerunComfyuiHistory")]
    assert "張數 ${imageCount}" in js


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


def test_comfyui_preview_resource_dashboard_is_wired():
    html = _read("public/index.html")
    js = _read("public/js/36-comfyui.js")
    core_js = _read("public/js/00-core.js")
    styles = _read("public/styles.css")
    runtime_routes = _read("routes/comfyui_sections/runtime_routes.py")

    assert 'id="comfyui-resource-dashboard"' in html
    assert 'apiFetch(API + "/comfyui/resources"' in js
    assert "function startComfyuiResourceDashboardPolling()" in js
    assert "function stopComfyuiResourceDashboardPolling()" in js
    assert 'if (!currentUser) {' in js
    assert 'if (!$("comfyui-resource-dashboard") || !currentUser) return;' in js
    assert 'if (currentUser && (activeView === "generate" || activeView === "hf"))' in js
    assert 'comfyuiResourceMetricMarkup({ label: "RAM", percent: null, available: false, detail: error })' in js
    assert 'comfyuiResourceMetricMarkup({ label: "GPU Load", percent: null, available: false, detail: "等待資源資料" })' in js
    assert 'displayValue: maxTemp === null ? "" : `${Math.round(maxTemp)}°C`' in js
    assert 'typeof startComfyuiResourceDashboardPolling === "function"' in core_js
    assert 'setTimeout(() => refreshComfyuiResourceDashboard(), 250);' in core_js
    assert ".comfyui-resource-dashboard" in styles
    assert '@app.route("/api/comfyui/resources", methods=["GET"])' in runtime_routes
