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
    assert "/js/01-root-quick-settings.js?v=20260603-drive-bulk-actions" in html
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
