"""Frontend smoke checks for Hugging Face Diffusers repo preflight."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def _hf_repo_block(html):
    start = html.index('id="comfyui-diffusers-repo-field"')
    end = html.index('id="comfyui-model-select"')
    return html[start:end]


def _hf_settings_block(html):
    start = html.index('id="comfyui-diffusers-settings"')
    end = html.index('id="comfyui-local-settings"')
    return html[start:end]


def test_diffusers_generation_page_accepts_repo_and_variant_selection():
    html = _read("public/index.html")
    hf_block = _hf_repo_block(html)
    hf_settings = _hf_settings_block(html)
    assert 'id="comfyui-diffusers-model-repo"' in html
    assert 'id="comfyui-diffusers-model-repo" placeholder="dhead/waiIllustriousSDXL_v150 或 Heartsync/NSFW-Uncensored"' in html
    assert 'id="comfyui-diffusers-inspect-btn"' in html
    assert 'id="comfyui-diffusers-common-repo"' in html
    assert 'id="comfyui-diffusers-add-common-repo"' in html
    assert 'id="comfyui-diffusers-remove-common-repo"' in html
    assert 'id="comfyui-diffusers-model-variant"' in html
    assert 'id="comfyui-diffusers-repo-status"' in html
    assert 'id="comfyui-diffusers-hf-token"' not in html
    assert 'id="comfyui-diffusers-hf-token-save-btn"' not in html
    assert 'id="comfyui-diffusers-hf-token-state"' not in html
    assert 'id="s-comfyui-huggingface-api-token"' in html
    assert 'id="comfyui-huggingface-api-token-state"' in html
    assert "Hugging Face Token 快速設定" in html
    assert "hf_transfer/Xet 加速下載" in html
    assert 'id="s-comfyui-allow-in-process-diffusers"' in html
    assert 'id="s-comfyui-diffusers-device-map"' in html
    assert 'id="s-comfyui-diffusers-low-cpu-mem-usage"' in html
    assert 'id="s-comfyui-diffusers-keep-downloaded-models"' in html
    assert "Use this model" in html
    assert "有 Diffusers 的 repo" in html
    assert ".safetensors</code> 是權重檔名，不是 repo" in html
    assert "GGUF" not in hf_block
    assert ".gguf" not in hf_block
    assert 'id="comfyui-diffusers-gguf-options"' not in html
    assert 'id="comfyui-diffusers-gguf-profile"' not in html
    assert 'id="comfyui-diffusers-gguf-variant"' not in html
    assert 'id="comfyui-diffusers-gguf-base-repo"' not in html
    assert 'id="comfyui-installed-gguf-list"' in html
    assert 'id="comfyui-installed-gguf-list" class="comfyui-installed-gguf-list" hidden' in html
    assert 'id="comfyui-civitai-settings"' not in hf_settings
    assert 'id="s-comfyui-civitai-api-key"' not in hf_settings
    assert 'id="s-comfyui-account-api-key"' not in hf_settings
    assert 'id="s-comfyui-max-batch-size"' not in hf_settings
    assert 'id="s-comfyui-default-width"' not in hf_settings


def test_diffusers_repo_examples_do_not_use_weight_filenames():
    html = _read("public/index.html")
    repo_input = re.search(r'<input[^>]+id="comfyui-diffusers-model-repo"[^>]+>', html).group(0)
    settings_input = re.search(r'<input[^>]+id="s-comfyui-diffusers-model-repo"[^>]+>', html).group(0)
    assert ".safetensors" not in repo_input
    assert ".gguf" not in repo_input
    assert ".safetensors" not in settings_input
    assert ".gguf" not in settings_input
    assert "JANKU" not in html
    assert "dhead/waiIllustriousSDXL_v150" in repo_input
    assert "Heartsync/NSFW-Uncensored" in repo_input
    assert "dhead/waiIllustriousSDXL_v150" in settings_input
    assert "Heartsync/NSFW-Uncensored" in settings_input


def test_comfyui_background_refresh_failures_are_visible():
    js = _read("public/js/36-comfyui.js")

    assert "loadComfyuiHistory().catch(() => {})" not in js
    assert "loadComfyuiWorkflowPresets().catch(() => {})" not in js
    assert "Workflow preset 讀取失敗" in js
    assert "ComfyUI 歷史讀取失敗" in js
    assert "ComfyUI 歷史重新整理失敗" in js


def test_diffusers_js_preflights_huggingface_repo_before_generation():
    js = _read("public/js/36-comfyui.js")
    assert 'apiFetch(API + "/comfyui/diffusers/inspect?" + query.toString()' in js
    assert "new URLSearchParams" in js
    assert "function inspectComfyuiDiffusersRepo" in js
    assert "comfyuiDiffusersInspectCache" in js
    assert "comfyuiDiffusersInspectInflight" in js
    assert "COMFYUI_DIFFUSERS_INSPECT_CACHE_MS" in js
    assert "getCachedComfyuiDiffusersInspection" in js
    assert "diffusers_model_variant" in js
    assert "diffusers_gguf_file" in js
    assert "diffusers_gguf_profile" in js
    assert "fillComfyuiGgufProfiles" in js
    assert "renderComfyuiGgufProfileHint" in js
    assert "ensureComfyuiGgufProfilesLoaded" in js
    assert "COMFYUI_DIFFUSERS_BUILTIN_COMMON_REPOS" in js
    assert "dhead/wai-nsfw-illustrious-sdxl-v140-sdxl" in js
    assert "Heartsync/NSFW-Uncensored" in js
    assert "stabilityai/sd-turbo" in js
    assert "stabilityai/sdxl-turbo" in js
    assert "cagliostrolab/animagine-xl-4.0" in js
    assert "stablediffusionapi/animapencil-xl-v3" in js
    assert "black-forest-labs/FLUX.1-schnell" in js
    assert "Qwen/Qwen-Image" in js
    assert "Qwen/Qwen-Image-Edit" in js
    assert "Tongyi-MAI/Z-Image-Turbo" in js
    assert "SD Turbo（txt2img / I2I）" in js
    assert "SDXL Turbo（txt2img / I2I）" in js
    assert "Animagine XL 4.0（Anime SDXL / I2I）" in js
    assert "FLUX.1 schnell（txt2img / I2I，需 HF 授權）" in js
    assert "Qwen Image（txt2img / I2I）" in js
    assert "Z-Image Turbo（txt2img / I2I）" in js
    assert "function addCurrentComfyuiDiffusersCommonRepo()" in js
    assert "function removeSelectedComfyuiDiffusersCommonRepo()" in js
    assert "function setComfyuiDiffusersRepo(repo, { inspect = true } = {})" in js
    assert "comfyui:diffusers-common-repos" in js
    assert 'API + "/comfyui/installed-gguf" + comfyuiRequestQuery()' in js
    assert "function loadComfyuiDiffusersTokenState" not in js
    assert "function saveComfyuiDiffusersTokenShortcut" not in js
    assert "prompt_style_hint" in js
    assert "comfyuiInstalledGgufModels" in js
    assert "renderComfyuiInstalledGgufModels" in js
    assert "shouldShowComfyuiInstalledGgufModels" in js
    assert "COMFYUI_HF_MODEL_FILE_EXT_RE" in js
    assert "isComfyuiHuggingFaceRepoLike" in js
    assert "sanitizeComfyuiDiffusersRepoField" in js
    assert "comfyuiHuggingFaceRepoInputLooksLikeModelFile" in js
    assert '"text-to-image": "txt2img"' in js
    assert '"text2text-generation": "t2t"' in js
    assert '"image-to-text": "i2t"' in js
    assert '"image-text-to-text": "i2t"' in js
    assert "文字轉文字" in js
    assert "圖片轉文字" in js
    assert 'if (COMFYUI_HF_MODEL_FILE_EXT_RE.test(tail)) return "";' in js
    assert "repoInput.value = candidateRepo;" in js
    assert "repoInput.value = modelSelect.value;" not in js
    assert '"origin_sdxl_gguf_txt2img"' in js
    assert "installed_gguf_models" in js
    assert "updateComfyuiDiffusersGgufOptions" in js
    assert "尚未開始下載" in js
    assert "避免重複下載" in js
    assert "可用模式：" in js
    assert "本站可執行：" in js
    assert "此 HF pipeline 目前只能辨識" in js
    assert "allOptions.filter((option) => option?.kind !== \"gguf\")" in js


def test_civitai_frontend_entry_is_explicit_and_not_hf_settings_family():
    html = _read("public/index.html")
    admin_js = _read("public/js/50-admin.js")
    comfyui_js = _read("public/js/36-comfyui.js")

    assert 'data-comfyui-view="models">Civitai / 模型管理</button>' in html
    assert 'data-comfyui-view="models" hidden' not in html
    assert 'id="comfyui-open-civitai-panel-btn"' not in html
    assert 'id="comfyui-root-model-access-note"' in html
    assert 'id="comfyui-civitai-settings" data-comfyui-settings-family-panel="comfyui"' in html
    assert 'id="s-comfyui-paid-api-nodes-enabled"' in html
    assert 'id="s-comfyui-account-api-key"' in html
    assert 'if (civitaiBox) civitaiBox.style.display = settingsFamily === "comfyui" ? "" : "none";' in admin_js
    assert 'if (civitaiInput) civitaiInput.disabled = settingsFamily !== "comfyui";' in admin_js
    assert "function openComfyuiCivitaiPanel()" in comfyui_js
    assert "comfyui-open-civitai-panel-btn" not in comfyui_js
    assert "if (modelsTab) modelsTab.hidden = false;" in comfyui_js
    assert 'if (accessNote) accessNote.style.display = showLocalModels ? "none" : "";' in comfyui_js
    assert "updateComfyuiRootPanelVisibility();" in comfyui_js


def test_diffusers_text_only_repo_hides_image_cards_and_omits_image_payloads():
    js = _read("public/js/36-comfyui.js")
    assert "function comfyuiDiffusersTextOnlyMode()" in js
    assert "function comfyuiDiffusersInspectionRunnableModes()" in js
    assert "runnable_modes" in js
    assert 'modes.every((mode) => mode === "txt2img")' in js
    assert "clearComfyuiDiffusersImageAssetsForTextOnly()" in js
    assert "已判斷為文字生圖，已隱藏來源圖片/遮罩卡片" in js
    assert 'if (comfyuiDiffusersTextOnlyMode()) return "txt2img";' in js
    assert "return !comfyuiDiffusersTextOnlyMode();" in js
    assert "if (sourceCard) sourceCard.style.display = comfyuiShouldShowSourceImageCard(mode) || comfyuiDiffusersSupportsImageInputMode() ? \"\" : \"none\";" in js
    assert "repo 支援圖生圖，上傳來源圖後自動切換" in js
    assert "若要圖生圖，請上傳來源圖片" in js
    assert 'if (maskCard) maskCard.style.display = diffusersTextOnly ? "none"' in js
    assert 'if (controlCard) controlCard.style.display = diffusersTextOnly ? "none"' in js
    assert "const includeImageAssets = !(diffusersMode && comfyuiDiffusersTextOnlyMode());" in js
    assert "const allowImageAssets = !(isComfyuiDiffusersMode() && comfyuiDiffusersTextOnlyMode());" in js
    assert "這個 HF repo 已判斷為文字生圖，不需要來源圖片或遮罩。" in js


def test_diffusers_cache_busts_preflight_ui_assets():
    html = _read("public/index.html")
    assert "/js/36-comfyui.js?v=20260606-civitai-history-restore" in html
    assert "/js/36-comfyui-workflows.js?v=20260605-history-gguf-rehydrate" in html




def test_hf_and_comfyui_are_separate_frontend_generation_tabs():
    html = _read("public/index.html")
    hf_block = _hf_repo_block(html)
    hf_settings = _hf_settings_block(html)
    comfyui_js = _read("public/js/36-comfyui.js")
    runtime_routes = _read("routes/comfyui_sections/runtime_routes.py")

    assert 'data-comfyui-view="generate">ComfyUI / GGUF</button>' in html
    assert 'data-comfyui-view="hf">HF / Diffusers</button>' in html
    assert 'id="comfyui-backend-title"' in html
    assert 'let comfyuiActiveBackendFamily = "comfyui";' in comfyui_js
    assert 'activeView === "hf" ? "hf" : "comfyui"' in comfyui_js
    assert 'activeView === "hf" && panelView === "generate"' in comfyui_js
    assert 'diffusers://frontend' in comfyui_js
    assert 'query.set("backend_url", backendUrl);' in comfyui_js
    assert 'return backendUrl ? { backend_url: backendUrl } : {};' in comfyui_js
    assert 'backend_url=request.args.get("backend_url")' in runtime_routes
    assert 'backend_url=request_data.get("backend_url") or request_data.get("comfyui_backend_url")' in runtime_routes
    assert '"backend_url": request.args.get("backend_url") or request.args.get("comfyui_backend_url")' in runtime_routes
    admin_helpers = _read("routes/comfyui_sections/admin_helpers.py")
    assert 'binding = _comfyui_binding(actor, backend_url=data.get("backend_url") or data.get("comfyui_backend_url"))' in runtime_routes
    assert 'parsed.netloc == "frontend"' in admin_helpers
    assert 'return diffusers_backend_url("")' in admin_helpers
    assert 'function comfyuiDiffusersStatusText()' in comfyui_js
    assert '不使用 ComfyUI server 模型清單' in comfyui_js
    assert 'updateComfyuiStatusForActiveBackend();' in comfyui_js
    assert 'isComfyuiDiffusersMode() ? comfyuiDiffusersStatusText() : comfyuiModelsLastStatusText' in comfyui_js
    assert 'if (!isComfyuiDiffusersMode()) comfyuiModelsLastStatusText = activeStatusText;' in comfyui_js
    assert 'diffusers_gguf_file: "",' in comfyui_js
    assert 'diffusers_gguf_profile: "",' in comfyui_js
    assert 'HF / Diffusers 只支援 Hugging Face 模型頁' in html
    assert "GGUF" not in hf_block
    assert "GGUF" not in hf_settings
    assert 'comfyui-diffusers-hf-token-save-btn' not in html
    assert 'id="s-comfyui-huggingface-api-token"' in html
    assert 'updateComfyuiDiffusersGgufOptions();' in comfyui_js


def test_comfyui_history_lists_and_reruns_workflow_runs():
    comfyui_js = _read("public/js/36-comfyui.js")
    runtime_routes = _read("routes/comfyui_sections/runtime_routes.py")

    assert '"id": f"workflow-{int(row[\'id\'])}",' in runtime_routes
    assert '"history_source": "workflow"' in runtime_routes
    assert "name='comfyui_workflow_runs'" in runtime_routes
    assert 'JOIN comfyui_workflow_presets p ON p.id = r.preset_id' in runtime_routes
    assert 'WHERE r.actor_user_id=?' in runtime_routes
    assert 'r.workflow_json, r.output_refs_json' in runtime_routes
    assert '"workflow_json": workflow_json' in runtime_routes
    assert 'if callable(_ensure_comfyui_workflow_schema):' in runtime_routes
    assert '"ensure_comfyui_workflow_schema": _ensure_comfyui_workflow_schema' in _read("routes/comfyui.py")
    assert '"create_workflow_run": _create_workflow_run' in _read("routes/comfyui.py")
    assert '"run_comfyui_workflow_preset_job": _run_comfyui_workflow_preset_job' in _read("routes/comfyui.py")
    assert 'items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)' in runtime_routes
    assert 'text.startsWith("workflow-")' in comfyui_js
    assert 'historyItem?.history_source === "workflow"' in comfyui_js
    assert 'await loadComfyuiSelectedTemplateDetail(workflowPresetId, { silent: true, applyDefaults: false });' in comfyui_js
    workflow_js = _read("public/js/36-comfyui-workflows.js")
    assert "function applyComfyuiTemplateHistorySnapshotToForm" in workflow_js
    assert "comfyuiGgufProfileVariantForSnapshot" in workflow_js
    assert 'applyComfyuiTemplateHistorySnapshotToForm(item.workflow_json || {}, payload);' in comfyui_js
    assert 'API + `/comfyui/workflow-runs/${encodeURIComponent(workflowRunId)}/rerun`' in comfyui_js
    assert "function comfyuiHistoryPreviewMarkup" in comfyui_js
    assert "hydrateComfyuiHistoryPreviews().catch(() => {});" in comfyui_js
    assert "data-comfyui-history-preview" in comfyui_js
    assert 'can_rerun = int(row["actor_user_id"] or 0) == actor_id' in runtime_routes
    assert 'or int(row["is_official"] or 0) == 1' not in runtime_routes


def test_gguf_workflow_history_rerun_repairs_legacy_checkpoint_snapshot():
    runtime_routes = _read("routes/comfyui_sections/runtime_routes.py")
    gguf_workflow = _read("services/comfyui/template/gguf_workflow.py")

    assert "needs_gguf_workflow_snapshot_repair(workflow_json)" in runtime_routes
    assert "infer_gguf_workflow_spec_from_snapshot(workflow_json, params)" in runtime_routes
    assert "p.workflow_json AS preset_workflow_json, p.system_bundle_id" in runtime_routes
    assert "def needs_gguf_workflow_snapshot_repair" in gguf_workflow
    assert 'class_type == "CheckpointLoaderSimple" and ckpt_name.endswith(".gguf")' in gguf_workflow
    assert "def infer_gguf_workflow_spec_from_snapshot" in gguf_workflow

def test_hf_settings_tab_is_exposed_in_comfyui_frontend_settings():
    html = _read("public/index.html")
    admin_js = _read("public/js/50-admin.js")

    assert 'id="comfyui-settings-slot"' in html
    assert 'data-comfyui-view="settings" hidden>AI 後端設定</button>' in html
    assert "ComfyUI 與 HF 是兩組獨立設定" in html
    assert '["sec-settings-comfyui", "comfyui-settings-slot"]' in admin_js
    assert 'sectionId === "sec-settings-comfyui"' in admin_js
    assert 'section.open = true;' in admin_js
    assert 'data-comfyui-settings-family="hf"' in html


def test_diffusers_generation_progress_surfaces_huggingface_download_bytes():
    html = _read("public/index.html")
    js = _read("public/js/36-comfyui.js")
    assert 'phase === "downloading"' in js
    assert "下載 Hugging Face 模型" in js
    assert "下載 Diffusers model" in js
    assert "Diffusers 暫無新進度" in js
    assert 'String(progress.backend_kind || "").toLowerCase() === "diffusers"' in js
    assert "baseDetail = `${baseDetail}（${percent}%）`;" in js
    assert "python_log_tail" in js
    assert "comfyui-progress-python-log" in html
    assert "Diffusers Python log" in html
    assert "showPythonLog" in js
    assert "Diffusers Python log 尚未輸出" in js
    assert "ComfyUI 後端沒有提供 Python log" in js
    assert "遠端/本地 ComfyUI 沒有回傳即時 logs" in js
    assert "/queue、/system_stats" in js
    assert "comfyuiBuildJobFailureMessage" in js
    assert "progress.bytes_written" in js
    assert "progress.total_bytes" in js
    assert "progress.current_file" in js
    assert "progress.speed_bytes_per_sec" in js
    assert "progress.step" in js
    assert "formatDriveBytes(writtenBytes)" in js
    assert "不設最長等待上限" in js


def test_comfyui_model_loading_dedupes_and_manual_refresh_busts_cache():
    js = _read("public/js/36-comfyui.js")
    bootstrap_js = _read("public/js/90-bootstrap.js")

    assert "comfyuiModelsLoadPromise" in js
    assert "COMFYUI_MODELS_CACHE_MS" in js
    assert "clearComfyuiModelsCache" in js
    assert "if (!forceRefresh && comfyuiModelsLoadPromise) return comfyuiModelsLoadPromise;" in js
    assert "loadComfyuiModels({ forceRefresh: true })" in bootstrap_js


def test_diffusers_txt2img_hides_source_image_card_until_needed():
    js = _read("public/js/36-comfyui.js")

    assert "function comfyuiShouldShowSourceImageCard" in js
    assert "if (!isComfyuiDiffusersMode()) return true;" in js
    assert 'return comfyuiModeUsesSourceImage(mode) || comfyuiHasInputAsset("source") || comfyuiHasInputAsset("mask");' in js
    assert 'if (sourceCard) sourceCard.style.display = comfyuiShouldShowSourceImageCard(mode) || comfyuiDiffusersSupportsImageInputMode() ? "" : "none";' in js


def test_diffusers_in_process_runtime_confirmation_is_in_quick_settings():
    quick_js = _read("public/js/01-root-quick-settings.js")
    admin_js = _read("public/js/50-admin.js")
    html = _read("public/index.html")

    assert "s-comfyui-allow-in-process-diffusers" in quick_js
    assert "接受主程序 Diffusers 資源風險" in quick_js
    assert "comfyui_allow_in_process_diffusers" in admin_js
    assert "comfyui_diffusers_device_map" in admin_js
    assert "comfyui_diffusers_low_cpu_mem_usage" in admin_js
    assert "comfyui_diffusers_cuda_fallback_to_cpu" in admin_js
    assert "comfyui_diffusers_keep_downloaded_models" in admin_js
    assert "comfyui_diffusers_disable_xet" in admin_js
    assert "只有勾選主程序資源風險確認後才允許直接推論" in admin_js
    assert "Diffusers device_map" in quick_js
    assert "GPU 失敗改用 CPU" in quick_js
    assert "低 RAM 載入" in quick_js
    assert "保留已下載模型快取" in quick_js
    assert "/js/01-root-quick-settings.js?v=" in html
    assert "/js/50-admin.js?v=" in html


def test_local_comfyui_main_py_performance_controls_are_root_configurable():
    html = _read("public/index.html")
    admin_js = _read("public/js/50-admin.js")
    quick_js = _read("public/js/01-root-quick-settings.js")

    for field_id in [
        "s-comfyui-local-vram-mode",
        "s-comfyui-local-precision",
        "s-comfyui-local-unet-dtype",
        "s-comfyui-local-vae-dtype",
        "s-comfyui-local-text-encoder-dtype",
        "s-comfyui-local-cpu-vae",
        "s-comfyui-local-attention-mode",
        "s-comfyui-local-upcast-attention",
        "s-comfyui-local-cuda-malloc",
        "s-comfyui-local-disable-smart-memory",
        "s-comfyui-local-deterministic",
        "s-comfyui-local-async-offload",
        "s-comfyui-local-cache-mode",
        "s-comfyui-local-cache-lru",
        "s-comfyui-local-reserve-vram-gb",
    ]:
        assert f'id="{field_id}"' in html
        assert field_id in admin_js
    assert "本地 ComfyUI main.py 性能參數" in html
    assert "遠端 API 無法改變對方啟動旗標" in html
    assert "comfyui_local_vram_mode" in admin_js
    assert "comfyui_local_cpu_vae" in admin_js
    assert "本地 VRAM 模式" in quick_js
    assert "本地 CPU VAE" in quick_js


def test_embedding_quick_insert_hides_when_no_embeddings_are_available():
    html = _read("public/index.html")
    comfyui_js = _read("public/js/36-comfyui.js")
    workflows_js = _read("public/js/36-comfyui-workflows.js")

    assert 'id="comfyui-embedding-shortcuts-field" style="display:none;"' in html
    assert 'const field = $("comfyui-embedding-shortcuts-field") || box.closest(".field");' in comfyui_js
    assert 'field.style.display = comfyuiAvailableEmbeddings.length ? "" : "none";' in comfyui_js
    assert 'box.innerHTML = "";' in comfyui_js
    assert "if (!values.length) return;" in comfyui_js
    assert 'if (!values.length) return "";' in workflows_js
