from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_inactivity_timeout_message_uses_configured_duration():
    core = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    auth = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")

    assert "const DEFAULT_INACTIVITY_LOGOUT_MS = 10 * 60 * 1000;" in core
    assert 'const IDLE_TIMEOUT_LOGOUT_STORAGE_KEY = "hackme_web.idle_timeout_logout_pending";' in core
    assert "formatInactivityTimeoutLabel" in core
    assert "已閒置 ${formatInactivityTimeoutLabel()}，系統將自動登出。" in core
    assert "await forceIdleTimeoutLogout();" in core
    assert "function showLoginScreen()" in core
    assert "function markIdleTimeoutLogoutPending()" in core
    assert "function hasIdleTimeoutLogoutPending()" in core
    assert "async function forceIdleTimeoutLogout()" in auth
    assert 'API + "/session/idle-timeout"' in auth
    assert '"X-Idle-Timeout-Logout": "1"' in auth
    assert "markIdleTimeoutLogoutPending();" in auth
    assert "clearIdleTimeoutLogoutPending();" in auth
    assert "showLoginScreen();" in auth
    assert "if (res.ok) clearIdleTimeoutLogoutPending();" in auth
    assert "閒置登出尚未獲伺服器確認" in auth
    assert "已超過 3 分鐘未操作" not in core
    assert 'label.textContent = currentUser && inactivityLogoutMs <= 0 ? "閒置登出：停用"' in core
    assert 's.session_idle_timeout_minutes ?? ""' in (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    assert 'session_idle_timeout_minutes: parseInt($("s-session-idle-timeout")?.value || "0", 10) || 0' in (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    assert "function setInactivitySuspendState(reason, active, labelText = \"系統工作進行中\")" in core
    assert "閒置登出：${currentInactivitySuspendLabel()}，暫停" in core
    assert '$("li-msg") || $("settings-msg")' not in core


def test_settings_success_banner_auto_clears_and_is_not_reused_by_idle_warning():
    admin = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")

    assert "let settingsStatusAutoClearTimer = null;" in admin
    assert "const autoClearMs = Number(options.autoClearMs || 0);" in admin
    assert "settingsStatusAutoClearTimer = setTimeout(() => {" in admin
    assert 'setSettingsStatus(\n      `${warnings.length || authUiRefreshFailed ? "設定已儲存，但仍有項目需確認" : "✅ 設定已儲存"}' in admin
    assert "{ autoClearMs: warnings.length || authUiRefreshFailed ? 0 : 4000 }" in admin


def test_pending_idle_timeout_blocks_auto_session_restore_on_refresh():
    bootstrap = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")

    assert "hasIdleTimeoutLogoutPending()" in bootstrap
    assert "await forceIdleTimeoutLogout();" in bootstrap
    assert "return;" in bootstrap


def test_comfyui_long_running_work_suspends_idle_logout():
    comfyui = ((ROOT / "public" / "js" / "36-comfyui.js").read_text(encoding="utf-8") + "\n" + (ROOT / "public" / "js" / "36-comfyui-workflows.js").read_text(encoding="utf-8"))

    assert 'function setComfyuiIdleSuspend(reason, active, label)' in comfyui
    assert 'setComfyuiIdleSuspend("comfyui_generate", !!busy, `${isComfyuiDiffusersMode() ? "Diffusers" : "ComfyUI"} 產圖中`);' in comfyui
    assert 'setComfyuiIdleSuspend("comfyui_start_local", true, "ComfyUI 啟動中");' in comfyui
    assert 'setComfyuiIdleSuspend("comfyui_start_local", false, "ComfyUI 啟動中");' in comfyui
    assert 'setComfyuiIdleSuspend("comfyui_model_download", true, "ComfyUI 模型下載中");' in comfyui
    assert 'setComfyuiIdleSuspend("comfyui_model_download", false, "ComfyUI 模型下載中");' in comfyui


def test_drive_transfers_suspend_idle_logout_and_guard_browser_upload_reload():
    drive = (ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8")

    assert "function syncDriveTransferIdleSuspend()" in drive
    assert 'setInactivitySuspendState("drive_transfer", active, "雲端硬碟傳輸中");' in drive
    assert "syncDriveTransferIdleSuspend();" in drive
    assert "function hasActiveDriveBrowserUpload()" in drive
    assert 'window.addEventListener("beforeunload", (event) => {' in drive
    assert '["upload", "folder_upload", "resumable_upload"].includes(item.kind)' in drive


def test_internal_test_login_token_is_hidden_outside_internal_test_mode():
    index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    core = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    auth = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")

    assert 'id="li-internal-test-token-field" style="display:none;"' in index
    assert '["test", "internal_test"].includes(siteConfig.server_mode)' in core
    assert "input.disabled = !showInternalTestToken;" in core
    assert "if (!showInternalTestToken) input.value = \"\";" in core
    assert "isInternalTestLoginMode() ? ($(\"li-internal-test-token\")?.value || \"\") : \"\"" in auth
    assert "if (devLoginToken) loginPayload.login_token = devLoginToken;" in auth


def test_login_recovery_uses_human_facing_verification_wording():
    index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    auth = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")
    bootstrap = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")

    assert "送出重設密碼審核" in index
    assert "寄送 Email 驗證碼" in index
    assert "重設密碼 token" not in index
    assert "Email 驗證 token" not in index
    assert "無法取得 CSRF token" not in auth
    assert "安全驗證狀態失效" in auth
    assert "function bindAuthRecoveryControls()" in auth
    assert '["reset-request-btn", requestPasswordReset]' in auth
    assert '["recovery-toggle", toggleRecoveryPanel]' in auth
    assert 'el.dataset.authRecoveryBound = "1";' in auth
    assert 'if (typeof bindAuthRecoveryControls === "function") bindAuthRecoveryControls();' in bootstrap


def test_public_auth_flows_force_refresh_public_csrf_tokens():
    auth = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")

    assert auth.count("fetchCsrfToken({ force: true });") >= 3
    assert "fetchCsrfToken({ force: false });" not in auth


def test_login_can_return_to_account_scoped_share_pages_only():
    auth = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")
    shared_file_js = (ROOT / "public" / "js" / "shared-file.js").read_text(encoding="utf-8")

    assert "function safeLoginReturnToPath()" in auth
    assert 'new URLSearchParams(window.location.search || "").get("return_to")' in auth
    assert 'raw.startsWith("//")' in auth
    assert 'raw.includes("\\\\")' in auth
    assert "target.origin !== window.location.origin" in auth
    assert 'target.pathname.startsWith("/shared/")' in auth
    assert "function redirectToLoginReturnToIfNeeded(loginJson = {})" in auth
    assert "if (loginJson?.must_change_password) return false;" in auth
    assert "window.location.assign(returnTo);" in auth
    assert "if (redirectToLoginReturnToIfNeeded(json)) return;" in auth
    assert 'link.href = `/?return_to=${encodeURIComponent(returnTo)}`;' in shared_file_js


def test_login_submit_is_guarded_against_double_requests():
    auth = (ROOT / "public" / "js" / "40-auth-users.js").read_text(encoding="utf-8")

    assert "let loginRequestBusy = false;" in auth
    assert "async function doLogin() {\n  if (loginRequestBusy) return;" in auth
    assert "loginRequestBusy = true;" in auth
    assert "loginRequestBusy = false;" in auth


def test_login_autofill_block_and_notification_mute_settings_are_wired():
    index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    core = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    admin = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")

    assert 'data-login-autofill-block="__LOGIN_AUTOFILL_BLOCK__"' in index
    assert 'autocomplete="__LOGIN_USER_AUTOCOMPLETE__"' in index
    assert 'autocomplete="__LOGIN_PASSWORD_AUTOCOMPLETE__"' in index
    assert 'id="s-login-autofill-block-enabled"' in index
    assert 'id="s-notification-muted-types"' in index

    assert "function loginAutofillBlockedForUi()" in core
    assert "function bindLoginAutofillGuards()" in core
    assert "function updateLoginAutofillPolicy()" in core
    assert "updateLoginAutofillPolicy();" in core
    assert "input.readOnly = enabled;" in core
    assert "data-lpignore" in core

    assert "login_autofill_block_enabled" in admin
    assert "notification_muted_types" in admin
