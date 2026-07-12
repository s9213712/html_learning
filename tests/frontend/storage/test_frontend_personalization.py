from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_personal_appearance_editor_and_routes_are_wired():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    auth_js = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")
    public_py = (ROOT / "routes" / "public.py").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )

    assert "/js/40-auth-users.js?v=" in index_html
    assert 'id="edit-user-appearance-section"' in index_html
    assert 'style="display:none;"' in index_html
    assert 'id="edit-user-appearance-preset"' in index_html
    assert 'id="theme-quick-toggle"' in index_html
    assert 'id="edit-user-theme-mode"' in index_html
    assert 'id="s-site-theme-mode"' in index_html
    assert '<option value="daylight">Daylight</option>' in index_html
    assert '<option value="ocean">Ocean Console</option>' in index_html
    assert '<option value="terminal">Terminal</option>' in index_html
    assert 'id="edit-user-appearance-reset"' in index_html
    assert '按視窗底部的「儲存」後才會寫入帳號' in auth_js
    assert 'id="edit-user-appearance-status"' in index_html
    assert 'id="edit-user-site-radius-px"' in index_html
    assert 'id="edit-user-site-font-scale"' in index_html
    assert 'id="edit-user-site-content-width"' in index_html
    assert 'id="edit-user-site-font-family"' in index_html
    assert 'id="edit-user-site-background-style"' in index_html
    assert 'id="edit-user-site-panel-style"' in index_html
    assert 'id="edit-user-site-sidebar-width"' in index_html
    assert 'id="s-site-radius-px"' in index_html
    assert 'id="s-site-font-scale"' in index_html
    assert 'id="s-site-content-width"' in index_html
    assert 'id="s-site-font-family"' in index_html
    assert 'id="s-site-background-style"' in index_html
    assert 'id="s-site-panel-style"' in index_html
    assert 'id="s-site-sidebar-width"' in index_html
    assert "允許使用者覆寫個人外觀" in index_html
    assert "feature_personalization_enabled" in admin_js
    assert "let globalSiteConfig = {};" in core_js
    assert "let userSiteAppearanceConfig = {};" in core_js
    assert '"site_theme_mode",' in core_js
    assert 'const SITE_THEME_MODE_PALETTES = {' in core_js
    assert 'function getUserAppearanceConfig()' in core_js
    assert 'function getEffectiveSiteThemeMode()' in core_js
    assert 'document.body.dataset.themeMode = themeMode;' in core_js
    assert 'const SITE_FONT_FAMILY_MAP = {' in core_js
    assert 'const SITE_SIDEBAR_WIDTH_MAP = {' in core_js
    assert 'function clearUserAppearanceConfig()' in core_js
    assert 'applySiteConfig(json.appearance_settings, { scope: "user" })' in core_js
    assert 'const USER_APPEARANCE_PRESETS = {' in auth_js
    assert 'site_theme_mode: "light"' in auth_js
    assert 'function toggleUserThemeModeQuickly()' in auth_js
    assert 'function applyUserThemeModeSelection()' in auth_js
    assert 'const USER_APPEARANCE_THEME_PALETTES = {' in auth_js
    assert 'USER_APPEARANCE_THEME_NEUTRAL_KEYS.forEach((key) => delete nextAppearance[key]);' in auth_js
    assert 'function userAppearanceFeatureEnabled()' in auth_js
    assert 'function setUserAppearanceEditorDisabled(disabled)' in auth_js
    assert 'if (resetBtn) resetBtn.style.display = "none";' in auth_js
    assert 'if (resetBtn) resetBtn.disabled = !enabled;' in auth_js
    assert 'function saveUserAppearanceSettings(operation = authUsersOperationContext(editingUserId))' in auth_js
    assert 'API + "/me/appearance"' in auth_js
    assert 'function updateUserAppearanceEditorVisibility()' in auth_js
    assert '@app.route("/api/me/appearance", methods=["GET", "PUT", "DELETE"])' in public_py
    assert 'require_csrf_safe = deps["require_csrf_safe"]' in public_py
    assert 'get_profile_appearance(conn, ctx["id"])' in public_py
    assert '"require_csrf_safe": require_csrf_safe,' in (ROOT / "server.py").read_text(encoding="utf-8")
    assert 'if ($("s-site-radius-px")) $("s-site-radius-px").value = String(s.site_radius_px || 12);' in admin_js
    assert 'if ($("s-site-theme-mode")) $("s-site-theme-mode").value = s.site_theme_mode || "dark";' in admin_js
    assert 'if ($("s-site-font-family")) $("s-site-font-family").value = s.site_font_family || "system";' in admin_js
