"""Frontend smoke checks for Hugging Face Diffusers repo preflight."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_diffusers_generation_page_accepts_repo_and_variant_selection():
    html = _read("public/index.html")
    assert 'id="comfyui-diffusers-model-repo"' in html
    assert 'id="comfyui-diffusers-inspect-btn"' in html
    assert 'id="comfyui-diffusers-model-variant"' in html
    assert 'id="comfyui-diffusers-gguf-profile"' in html
    assert 'id="comfyui-diffusers-gguf-variant"' in html
    assert 'id="comfyui-diffusers-gguf-base-repo"' in html
    assert 'id="comfyui-diffusers-gguf-profile-hint"' in html
    assert 'id="comfyui-installed-gguf-list"' in html
    assert 'id="comfyui-diffusers-repo-status"' in html
    assert 'id="s-comfyui-allow-in-process-diffusers"' in html
    assert 'id="s-comfyui-diffusers-device-map"' in html
    assert 'id="s-comfyui-diffusers-low-cpu-mem-usage"' in html
    assert 'id="s-comfyui-diffusers-keep-downloaded-models"' in html
    assert "Use this model" in html
    assert "有 Diffusers 的 repo" in html


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
    assert "prompt_style_hint" in js
    assert "comfyuiInstalledGgufModels" in js
    assert "renderComfyuiInstalledGgufModels" in js
    assert "installed_gguf_models" in js
    assert "GGUF 只允許官方已驗證 profile" in js
    assert "updateComfyuiDiffusersGgufOptions" in js
    assert "尚未開始下載" in js
    assert "避免重複下載" in js


def test_diffusers_cache_busts_preflight_ui_assets():
    html = _read("public/index.html")
    assert "/js/36-comfyui.js?v=20260604-hf-settings-fronttab" in html
    assert "/js/36-comfyui-workflows.js?v=20260604-remote-workflow-shared-default" in html


def test_hf_settings_tab_is_exposed_in_comfyui_frontend_settings():
    html = _read("public/index.html")
    admin_js = _read("public/js/50-admin.js")

    assert 'id="comfyui-settings-slot"' in html
    assert 'data-comfyui-view="settings" hidden>AI 後端設定</button>' in html
    assert "ComfyUI / GGUF 與 HF 是兩組獨立設定" in html
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
    assert 'if (sourceCard) sourceCard.style.display = comfyuiShouldShowSourceImageCard(mode) ? "" : "none";' in js


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
