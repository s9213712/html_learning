from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_comfyui_idle_ui_reset_is_bound_on_bootstrap():
    comfyui_js = _read("public/js/36-comfyui.js")
    bootstrap_js = _read("public/js/90-bootstrap.js")
    assert "function resetComfyuiIdleUi()" in comfyui_js
    assert "stopComfyuiProgress();" in comfyui_js
    assert 'preview.innerHTML = `<div class="drive-empty">${sanitize(comfyuiPreviewEmptyText())}</div>`;' in comfyui_js
    assert 'if (typeof resetComfyuiIdleUi === "function") resetComfyuiIdleUi();' in bootstrap_js


def test_comfyui_generate_retries_stale_unavailable_state():
    comfyui_js = _read("public/js/36-comfyui.js")
    assert "generate.disabled = !!busy;" in comfyui_js
    assert "generate.disabled = !!busy || unavailable;" not in comfyui_js
    assert 'setComfyuiMessage("正在重新檢查 ComfyUI 連線...", true);' in comfyui_js
    assert "const available = await refreshComfyuiStatus({ switchAway: false });" in comfyui_js
    assert 'setComfyuiMessage("正在載入 ComfyUI 模型清單...", true);' in comfyui_js


def test_comfyui_stop_button_stays_visible_while_local_runtime_is_starting():
    comfyui_js = _read("public/js/36-comfyui.js")
    assert "let comfyuiLocalRuntimeActive = false;" in comfyui_js
    assert "const showLocalRuntimeStop = isRoot && localMode && (comfyuiServerAvailable === true || comfyuiLocalRuntimeActive);" in comfyui_js
    assert 'start.style.display = localMode && comfyuiServerAvailable !== true && !comfyuiLocalRuntimeActive ? "" : "none";' in comfyui_js
    assert 'stop.style.display = showLocalRuntimeStop ? "" : "none";' in comfyui_js
    assert 'comfyuiLocalRuntimeActive = comfyuiEffectiveConnectionMode() === "local" && (available || starting || !!json.local_runtime);' in comfyui_js
    assert "comfyuiLocalRuntimeActive = true;" in comfyui_js
    assert "comfyuiLocalRuntimeActive = false;" in comfyui_js


def test_comfyui_static_asset_cache_busted_for_idle_retry_fix():
    index_html = _read("public/index.html")
    assert "/js/36-comfyui.js?v=20260614-comfyui-progress-temp-embedding-commas" in index_html
