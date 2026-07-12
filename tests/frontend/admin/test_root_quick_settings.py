from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_root_quick_settings_expose_service_fee_pricing_for_feature_pages():
    js = _read("public/js/01-root-quick-settings.js")
    html = _read("public/index.html")
    css = _read("public/styles.css")
    priced_tabs = {
        "profile": ["username_change", "profile_decoration"],
        "shares": ["cloud_storage_1gb_7d", "cloud_storage_1gb_30d", "video_publish_basic"],
        "announcements": ["post_cost_standard", "post_pin_24h"],
        "community": ["post_cost_standard", "post_pin_24h"],
        "appeals": ["violation_fine"],
        "drive": ["cloud_storage_1gb_7d", "cloud_storage_1gb_30d"],
        "albums": ["cloud_storage_1gb_7d", "cloud_storage_1gb_30d"],
        "videos": ["video_publish_basic", "video_boost_24h"],
        "games": ["game_entry_standard", "game_virtual_item_common"],
        "comfyui": ["comfyui_txt2img_basic", "comfyui_txt2img_highres", "comfyui_batch_10"],
        '"ai-agent"': ["ai_agent_task_basic"],
        "accounts": ["username_change", "profile_decoration", "violation_fine"],
    }
    unpriced_tabs = ["chat", "jobs", "experiments", "economy", "trading", "server"]

    assert "ROOT_SERVICE_FEE_QUICK_PRESETS" in js
    assert "window.HACKME_SERVICE_FEE_PRICING_PRESETS" in js
    for tab, keys in priced_tabs.items():
        expected = f'{tab}: {{'
        assert expected in js
        assert f'pricingKeys: {keys!r}'.replace("'", '"') in js
    for tab in unpriced_tabs:
        start = js.index(f"  {tab}: {{")
        end = js.index("\n  },", start)
        assert "pricingKeys" not in js[start:end]
    assert "每次消耗點數" in js
    assert "雲端容量 1GB / 7 天" in js
    assert "duration_days: 7" in js
    assert "雲端容量 1GB / 30 天" in js
    assert "duration_days: 30" in js
    assert "saveRootModulePricing(config)" in js
    assert "/root/economy/catalog" in js
    assert "root-module-pricing-panel" in js
    assert "root-module-pricing-panel" in css
    assert "/js/01-root-quick-settings.js?v=__ASSET_VERSION__" in html
    assert "服務費小帳本" not in js
    assert "pc0 站內帳本即時" in js


def test_admin_billing_catalog_reuses_shared_quick_pricing_presets():
    quick_js = _read("public/js/01-root-quick-settings.js")
    admin_js = _read("public/js/53-admin-storage-economy.js")

    assert "window.HACKME_SERVICE_FEE_PRICING_PRESETS" in admin_js
    assert "? window.HACKME_SERVICE_FEE_PRICING_PRESETS" in admin_js
    assert "comfyui_txt2img_basic" in quick_js
    assert "ROOT_SERVICE_FEE_PRICING_PRESETS.map" in admin_js
    assert "服務費小帳本" not in quick_js
    assert "服務費小帳本" not in admin_js
    assert "pc0 站內帳本即時" in quick_js


def test_admin_health_playwright_ci_background_failure_is_visible():
    admin_js = _read("public/js/50-admin.js")

    assert "loadPlaywrightCiHealth().catch(() => {})" not in admin_js
    assert 'label: "Playwright CI"' in admin_js
    assert 'value: "unavailable"' in admin_js
    assert 'err?.message || "CI 狀態讀取失敗"' in admin_js


def test_experiments_quick_toggle_controls_feature_visibility():
    core_js = _read("public/js/00-core.js")
    quick_js = _read("public/js/01-root-quick-settings.js")

    assert 'experiments: "feature_experiments_enabled"' in core_js
    assert "if (!moduleFeatureEnabledForUi(moduleKey)) return false;" in core_js
    assert 'if (currentUser === "root") return true;' in core_js
    experiments_start = quick_js.index("  experiments: {")
    experiments_end = quick_js.index("\n  },", experiments_start)
    experiments_block = quick_js[experiments_start:experiments_end]
    assert 'id: "s-feature-experiments-enabled"' in experiments_block
    assert 'label: "開放實驗區"' in experiments_block


def test_ai_agent_quick_settings_expose_operation_mode_near_top():
    quick_js = _read("public/js/01-root-quick-settings.js")

    start = quick_js.index('  "ai-agent": {')
    end = quick_js.index("\n  },", start)
    block = quick_js[start:end]

    assert 'id: "s-feature-ai-agent-enabled"' in block
    assert 'id: "s-ai-agent-operation-mode"' in block
    assert 'readonly/assist/write/audit' in block
    assert block.index('id: "s-ai-agent-operation-mode"') < block.index('id: "s-module-ai-agent-min-role"')


def test_ai_agent_quick_settings_refreshes_agent_status_after_save():
    quick_js = _read("public/js/01-root-quick-settings.js")

    assert 'const moduleTab = overlay?.dataset.moduleTab || currentModuleTab;' in quick_js
    assert 'if (moduleTab === "ai-agent" && typeof loadAiAgentStatus === "function")' in quick_js
    assert 'await loadAiAgentStatus({ force: true });' in quick_js
    assert 'loadAiAgentReadOnly({ scope: "all", limit: 20, silent: true, force: true })' in quick_js
    assert 'loadAiAgentAuditStatus({ silent: true })' in quick_js


def test_ai_agent_quick_settings_provider_presets_update_connection_fields():
    quick_js = _read("public/js/01-root-quick-settings.js")

    assert "AI_AGENT_PROVIDER_QUICK_PRESETS" in quick_js
    assert 'apiBaseUrl: "http://127.0.0.1:8642/v1"' in quick_js
    assert 'model: ""' in quick_js
    assert 'allowedModels: ""' in quick_js
    assert 'apiBaseUrl: "http://127.0.0.1:11434/v1"' in quick_js
    assert 'gpt-oss:120b-cloud' not in quick_js
    assert 'minimax-m2.7:cloud' not in quick_js
    assert 'applyAiAgentProviderQuickPreset(provider.value)' in quick_js
    assert 'setRootModuleFieldValue("s-ai-agent-api-base-url", preset.apiBaseUrl)' in quick_js
    assert 'setRootModuleFieldValue("s-ai-agent-model", preset.model)' in quick_js
    assert 'setRootModuleFieldValue("s-ai-agent-allowed-models", preset.allowedModels)' in quick_js
