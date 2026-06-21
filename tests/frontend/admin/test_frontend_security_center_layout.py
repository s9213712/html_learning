from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_moderation_violation_integrity_scan_is_bounded():
    moderation = (ROOT / "routes" / "moderation.py").read_text(encoding="utf-8")

    assert "users_limit = 500" in moderation
    assert "integrity_limit = 1000" in moderation
    assert "SELECT id FROM users WHERE username<>'root' ORDER BY id ASC LIMIT ?" in moderation
    assert "SELECT id FROM users WHERE username<>'root' ORDER BY id ASC\").fetchall()" not in moderation


def test_security_center_logs_have_non_overlapping_layout():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    assert 'class="security-log-grid"' in index_html
    assert 'id="security-audit-entries" class="security-log-box"' in index_html
    assert 'id="security-server-log" class="security-log-box security-log-pre"' in index_html
    assert 'id="security-server-output" class="security-log-box security-log-pre"' in index_html
    assert 'class="security-log-row"' in admin_js
    assert ".security-log-grid" in css
    assert "grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));" in css
    assert ".security-log-box" in css
    assert "overflow-wrap: anywhere;" in css
    assert ".security-log-row" in css
    assert "grid-template-columns: auto auto minmax(0, .9fr) minmax(0, .7fr) minmax(0, 1.6fr);" in css


def test_server_and_system_management_tabs_are_split_by_domain():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")

    assert 'id="tab-server-overview"' in index_html
    assert 'id="tab-server-server-mode"' in index_html
    assert 'id="security-settings-slot"' in index_html
    assert 'id="tab-system-health"' in index_html
    assert 'id="tab-system-server-mode"' not in index_html
    assert 'id="tab-system-features"' in index_html
    assert 'id="tab-system-appearance"' in index_html
    assert 'id="tab-system-core"' in index_html
    assert 'id="tab-system-capacity"' in index_html
    assert 'id="tab-system-env"' in index_html
    assert 'id="tab-system-launch-check" style="display:none;"' in index_html
    assert 'id="server-mode-launch-check-slot" style="display:none;margin-top:.75rem;"' in index_html
    assert 'id="tab-system-overview"' not in index_html
    assert 'id="tab-system-settings"' not in index_html
    assert 'id="tab-server-launch-check"' not in index_html
    assert 'id="sec-settings-listen"' in index_html
    assert 'id="sec-settings-backpressure"' in index_html
    assert index_html.index('id="sec-settings-listen"') < index_html.index('id="sec-settings-backpressure"')
    assert '<section class="drive-collapsible-panel settings-static-panel" id="sec-settings-backpressure">' in index_html
    assert 'id="sec-settings-backpressure"><summary>' not in index_html
    assert '{ label: "總覽", action: "server:overview" }' in core_js
    assert '{ label: "伺服器模式", action: "server:server-mode" }' in core_js
    assert 'action: "system:server-mode"' not in core_js
    assert '{ label: "請求容量", action: "system:capacity" }' in core_js
    assert 'switchServerTab(currentServerTab || "overview")' in admin_js
    assert 'switchSystemTab(currentSystemTab || "health")' in admin_js
    assert 'const shouldShow = target === "production";' in admin_js
    assert '["sec-server-launch-check", "server-mode-launch-check-slot"]' in admin_js
    assert '["sec-settings-backpressure", "system-capacity-slot"]' in admin_js
    assert '["sec-server-launch-check", "system-launch-check-slot"]' not in admin_js
    assert '["sec-server-settings", "system-server-mode-slot"]' not in admin_js
    assert 'tabSystemCapacity.addEventListener("click", () => switchSystemTab("capacity"))' in bootstrap_js
    assert 'tabServerServerMode.addEventListener("click", () => switchServerTab("server-mode"))' in bootstrap_js
    assert 'tabSystemServerMode' not in bootstrap_js
    assert 'tabServerLaunchCheck' not in bootstrap_js
    assert 'case "member-settings":\n      return currentUser === "root";' in admin_js
    assert "const balanced = gap === 0;" in admin_js
    assert "對帳提示待查" in admin_js


def test_system_environment_processes_and_integrity_are_rendered_below_resources():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    security_routes = (ROOT / "routes" / "system_admin_sections" / "security_routes.py").read_text(encoding="utf-8")
    backpressure_py = (ROOT / "services" / "server" / "backpressure.py").read_text(encoding="utf-8")
    server_py = (ROOT / "server.py").read_text(encoding="utf-8")

    assert 'id="server-env-process-list"' in index_html
    assert 'renderServerEnvProcessList' in admin_js
    assert '"程序 PID"' not in admin_js
    assert '"environment": _environment_identity_payload()' in security_routes
    assert '"platform": platform.platform()' in security_routes
    assert '"python_version": sys.version.split()[0]' in security_routes
    assert 'env: json.environment || lastServerEnvironment || {}' in admin_js
    assert 'HLS decode / ffmpeg' in security_routes
    assert 'aria2 download' in security_routes
    assert 'trading engine worker' in security_routes
    assert '<th>主程式 / 詳細命令</th>' in admin_js
    assert 'item.command || item.args || item.process_name' in admin_js
    assert '"command": command[:command_limit]' in security_routes
    assert '"command_truncated": len(command) > command_limit' in security_routes
    assert 'per-process bytes unavailable' not in security_routes
    assert 'item.network' not in admin_js
    assert '"integrity_check": integrity_check or {}' in security_routes
    assert '"audit_hash_check": audit_hash_check or {}' in security_routes
    assert '"processes": _related_processes_snapshot(base_dir=base_dir)' in security_routes
    assert "server_busy_rejected" in backpressure_py
    assert "hard_5xx_response" in backpressure_py
    assert "anomaly_audit" in backpressure_py
    assert "BACKPRESSURE_ANOMALY" in server_py


def test_saving_settings_preserves_current_admin_surface():
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    save_body = admin_js.split("async function saveSettings()", 1)[1].split("async function loadServerEnv()", 1)[0]

    assert "setAuthState({" not in save_body
    assert "const activeModule = currentModuleTab;" in save_body
    assert "const activeServerTab = currentServerTab;" in save_body
    assert "const activeSettingsSection = currentSettingsSection;" in save_body
    assert "switchModuleTab(activeModule);" in save_body


def test_prelaunch_tests_include_stress_progress_and_logs():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")

    assert 'id="security-stress-start-btn"' in index_html
    assert 'id="security-privilege-start-btn"' in index_html
    assert 'id="security-pentest-log"' in index_html
    assert 'id="security-privilege-log"' in index_html
    assert 'id="security-functional-log"' in index_html
    assert 'id="security-stress-log"' in index_html
    assert 'id="security-pentest-progress-fill"' in index_html
    assert 'id="security-privilege-progress-fill"' in index_html
    assert 'id="security-functional-progress-fill"' in index_html
    assert 'id="security-stress-progress-fill"' in index_html
    assert 'id="security-stress-requests"' in index_html
    assert 'id="security-stress-concurrency"' in index_html
    assert "startSecurityPrivilegeTest" in admin_js
    assert "startSecurityStressTest" in admin_js
    assert 'API + "/root/security-tests/privilege"' in admin_js
    assert 'API + "/root/security-tests/stress"' in admin_js
    assert "drive-progress-fill" in admin_js
    assert "job.log_tail" in admin_js
    assert "renderSecurityTestPanel" in admin_js
    assert 'securityTestMsg("越權測試啟動中..."' in admin_js
    assert "securityStressStart" in bootstrap_js
    assert 'securityTestMsg("滲透測試啟動中..."' in admin_js
    assert 'securityTestMsg("全功能測試啟動中..."' in admin_js
    assert 'securityTestMsg("壓力測試啟動中..."' in admin_js
    assert 'msg show ${ok ? "ok" : "err"}' in admin_js
    assert 'securityPentestStart.addEventListener("click", startSecurityPentest)' in bootstrap_js
    assert 'securityPrivilegeStart.addEventListener("click", startSecurityPrivilegeTest)' in bootstrap_js
    assert 'securityFunctionalStart.addEventListener("click", startSecurityFunctionalSmoke)' in bootstrap_js
    assert 'securityStressStart.addEventListener("click", startSecurityStressTest)' in bootstrap_js


def test_audit_chain_repair_and_points_chain_recovery_buttons_live_in_correct_areas():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    economy_js = (ROOT / "public" / "js" / "55-economy.js").read_text(encoding="utf-8")
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    audit_section = index_html.split('id="sec-server-audit"', 1)[1].split('id="sec-server-security"', 1)[0]
    economy_recovery_section = index_html.split('id="economy-recovery-card"', 1)[1].split('id="economy-account-query-card"', 1)[0]

    assert 'id="audit-chain-repair-btn"' in audit_section
    assert 'id="economy-recovery-auto-handle-btn"' not in audit_section
    assert 'id="economy-recovery-auto-handle-btn"' in economy_recovery_section
    assert "檢查異常處理方案" in economy_recovery_section
    assert 'id="economy-recovery-action-status"' in economy_recovery_section
    assert "economyRecoveryActionMsg" in economy_js
    assert "rows.filter((row) => row !== null && row !== undefined)" in economy_js
    assert "safe.active_provisional_freezes.filter((item) => item && typeof item === \"object\")" in economy_js
    assert 'auditChainRepair.addEventListener("click", repairIntegrityChains)' in bootstrap_js
    assert 'integrityBulkApprove.addEventListener("click", () => reviewSelectedIntegrityFindings("approve"))' in bootstrap_js
    assert 'igBulkApprove.addEventListener("click", () => reviewSelectedIntegrityFindings("approve"))' not in admin_js


def test_custom_security_profile_uses_form_controls_not_raw_json():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    profile_section = index_html.split("新增自定義設定檔", 1)[1].split('id="security-profile-save-btn"', 1)[0]
    save_body = admin_js.split("async function saveSecurityProfile()", 1)[1].split("function healthStatusColor", 1)[0]

    assert 'id="security-profile-settings-json"' not in profile_section
    assert 'id="security-profile-thresholds-json"' not in profile_section
    assert "settings JSON" not in profile_section
    assert "thresholds JSON" not in profile_section
    assert 'id="security-profile-ip-blocking-enabled"' in profile_section
    assert 'id="security-profile-security-pending-chat-reports-threshold"' in profile_section
    assert 'collectSecurityProfileDraft("security-profile")' in save_body
    assert "JSON.parse" not in save_body


def test_security_control_and_threshold_saves_show_visible_status():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    controls_body = admin_js.split("async function saveSecurityCenterControls()", 1)[1].split("async function saveSecurityThresholds()", 1)[0]
    thresholds_body = admin_js.split("async function saveSecurityThresholds()", 1)[1].split("async function applySecurityMode()", 1)[0]

    assert 'id="security-save-status"' in index_html
    assert "function setSecuritySaveStatus" in admin_js
    assert 'setSecuritySaveStatus("正在儲存安全開關..."' in controls_body
    assert 'setSecuritySaveStatus("正在儲存安全閾值..."' in thresholds_body
    assert "security-controls-msg" in controls_body
    assert "security-thresholds-msg" in thresholds_body
    assert "catch (err)" in controls_body
    assert "catch (err)" in thresholds_body
    assert "btn.disabled = true" in controls_body
    assert "btn.disabled = true" in thresholds_body


def test_server_update_ui_is_health_card_preview_only():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")

    health_section = index_html.split('id="sec-server-health"', 1)[1].split('id="sec-server-integrity"', 1)[0]
    server_mode_section = index_html.split('id="sec-server-settings"', 1)[1].split('<form id="settings-form"', 1)[0]

    assert "版本檢查" in health_section
    assert "GitHub 更新中心" not in index_html
    assert 'id="server-update-branch-select"' in index_html
    assert 'id="server-update-preview-btn"' in index_html
    assert 'id="server-update-apply-btn"' not in index_html
    assert 'id="server-update-diff"' in index_html
    assert "APPLY_UNVERIFIED_UPDATE" not in index_html
    assert 'id="server-update-branch-select"' not in server_mode_section
    assert "loadServerUpdateStatus" in admin_js
    assert "previewServerUpdate" in admin_js
    assert "applyServerUpdate" not in admin_js
    assert 'API + "/root/server-update/preview"' in admin_js
    assert 'API + "/root/server-update/apply"' not in admin_js
    assert "此頁不提供線上套用" in admin_js
    assert "serverUpdateRefresh.addEventListener" in bootstrap_js
    assert "serverUpdatePreview.addEventListener" in bootstrap_js
    assert "serverUpdateApply" not in bootstrap_js


def test_launch_check_treats_production_profile_settings_as_auto_applied_not_manual_blockers():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    launch_body = admin_js.split("function launchCheckConditionList(sc, requirements) {", 1)[1].split("function jumpToAnchor", 1)[0]

    assert "production profile 的 HTTPS / audit chain / Integrity Guard / browser-only 等安全設定會在 mode switch 成功時自動套用" in index_html
    assert "不是</strong>你必須先手動打開的上線前檢查項目" in index_html
    assert 'id="launch-check-upload-panel"' in index_html
    assert 'id="launch-check-upload-file"' in index_html
    assert 'id="launch-check-upload-json"' in index_html
    assert 'id="launch-check-upload-submit-btn"' in index_html
    assert "raw_report" in index_html
    assert "hmac_sha256" in index_html
    assert 'id="launch-check-doc-panel"' in index_html
    assert 'id="launch-check-doc-content"' in index_html
    assert "openLaunchCheckDoc" in admin_js
    assert 'API}/root/launch-check/doc?path=' in admin_js or 'API + "/root/launch-check/doc?path=' in admin_js
    assert "submitLaunchCheckReportUpload" in admin_js
    assert 'API + "/root/production-report/upload"' in admin_js
    assert "伺服器會重算 hash 並驗簽" in admin_js
    assert 'data-launch-upload="' in admin_js
    assert 'launchCheckUploadSubmit.addEventListener("click", () => submitLaunchCheckReportUpload())' in bootstrap_js
    assert 'launchCheckUploadFile.addEventListener("change", async () => {' in bootstrap_js
    assert "productionAutoSummary" in launch_body
    assert "defaultOutput" in admin_js
    assert "預設放置" in admin_js
    assert "runtime/reports/security/server_mode_v2_clean_smoke_<timestamp>.json|.md" in admin_js
    assert "runtime/reports/security/functional_permission_pentest_<timestamp>.json|.md" in admin_js
    assert "runtime/reports/security/production_gate/integrity_guard_report.json" in admin_js
    assert "python3 scripts/security/pentest/functional_permission_pentest.py" in admin_js
    assert "上線前檢查可在非 production 執行；真正切換由 GO_LIVE 完成" in launch_body
    assert "這些安全設定會在切換到 production 時自動套用，不是上線前檢查的手動前置條件" in launch_body
    assert "production 必須開 Integrity Guard" not in launch_body
    assert "請先切到 dev_ready 再開上線檢查" not in launch_body


def test_launch_check_release_bundle_and_artifact_controls_are_available():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    )

    assert 'id="launch-check-bundle-btn"' in index_html
    assert 'id="launch-check-artifacts-btn"' in index_html
    assert 'id="launch-check-release-panel"' in index_html
    assert "/root/production-release/bundle" in admin_js
    assert "/root/qa-artifacts/index" in admin_js
    assert "QA runs" in admin_js
    assert "qa_runs" in admin_js
    assert "createLaunchCheckReleaseBundle" in admin_js
    assert "refreshLaunchCheckQaArtifacts" in admin_js


def test_launch_check_surfaces_failing_backend_endpoint_names():
    admin_js = (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    launch_body = admin_js.split("async function loadLaunchCheck()", 1)[1].split("async function loadServerModeLogs()", 1)[0]

    assert "const failures = [];" in launch_body
    assert 'failures.push(`requirements: ${reqJson.msg || `HTTP ${reqRes.status}`}`);' in launch_body
    assert 'failures.push(`security-center: ${scJson.msg || `HTTP ${scRes.status}`}`);' in launch_body
    assert 'throw new Error(failures.join("；"));' in launch_body


def test_admin_audit_and_violations_load_failures_surface_visible_errors():
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")

    assert 'id="violation-user-select"' in index_html
    assert "renderViolationUserSelect" in admin_js
    assert "summary_only=1" in admin_js
    assert "請先選擇帳號，才會載入個別違規原因。" in admin_js
    assert 'statusEl.textContent = json.msg || "審計記錄讀取失敗"' in admin_js
    assert 'container.innerHTML = `<p style=\'color:var(--red);text-align:center;padding:1rem;\'>${sanitize(json.msg || "審計記錄讀取失敗")}</p>`' in admin_js
    assert 'const message = json.msg || "違規記錄讀取失敗";' in admin_js
    assert 'entriesEl.innerHTML = `<p style=\'color:var(--red);text-align:center;padding:1rem;\'>${sanitize(message)}</p>`' in admin_js


def test_admin_appeals_and_reports_load_failures_surface_visible_errors():
    appeals_js = (ROOT / "public" / "js" / "30-appeals.js").read_text(encoding="utf-8")

    assert 'const message = json.msg || "申覆清單讀取失敗";' in appeals_js
    assert 'list.innerHTML = `<p style=\'color:var(--red);text-align:center;padding:1rem;\'>${sanitize(message)}</p>`;' in appeals_js
    assert 'const message = json.msg || "訊息檢舉清單讀取失敗";' in appeals_js
    assert 'list.innerHTML = `<p style=\'color:var(--red);text-align:center;padding:1rem;\'>${sanitize(message)}</p>`;' in appeals_js


def test_settings_area_uses_collapsible_groups_to_reduce_clutter():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    server_settings = index_html.split('id="sec-server-settings"', 1)[1].split('id="sec-server-env"', 1)[0]
    security_center = index_html.split('id="sec-server-security"', 1)[1].split('id="sec-server-health"', 1)[0]

    assert server_settings.count('class="drive-collapsible-panel settings-collapse') >= 12
    assert security_center.count('class="drive-collapsible-panel settings-collapse') >= 5
    assert "Snapshot / Restore / Reset" in server_settings
    assert "危險區" in server_settings
    assert "上線前測試" in security_center
    assert "審計與伺服器輸出" in security_center
    assert ".settings-collapse" in css
    assert ".settings-collapse.danger-collapse" in css


def test_root_server_time_timezone_controls_are_wired():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    server_settings = index_html.split('id="sec-settings-system"', 1)[1].split('id="sec-settings-billing"', 1)[0]

    assert 'id="s-server-timezone"' in server_settings
    assert 'id="server-time-check-btn"' in server_settings
    assert 'id="server-time-status"' in server_settings
    assert 'value="Asia/Taipei"' in server_settings
    assert 'server_timezone: ($("s-server-timezone")?.value || "UTC").trim() || "UTC"' in admin_js
    assert 'renderServerTimeStatus(json.server_time)' in admin_js
    assert 'apiFetch(API + "/version"' in admin_js
    assert 'serverTimeCheckBtn.addEventListener("click", refreshServerTimeStatus)' in bootstrap_js


def test_system_environment_has_resource_dashboard():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

    env_section = index_html.split('id="sec-server-env"', 1)[1].split('</main>', 1)[0]
    assert 'class="system-resource-board"' in env_section
    assert 'id="system-resource-gauges"' in env_section
    assert 'id="system-resource-sampled-at"' in env_section
    assert 'id="server-env-transfer-details"' in env_section
    assert 'id="server-env-db-details"' in env_section
    assert 'class="server-env-summary-grid"' in env_section
    assert 'id="s-system-resource-board-refresh-seconds"' in index_html
    assert 'id="s-job-center-refresh-seconds"' in index_html
    assert 'id="s-trading-dashboard-refresh-seconds"' in index_html
    assert 'id="s-comfyui-job-poll-seconds"' in index_html
    assert 'id="s-notification-poll-seconds"' in index_html
    assert 'id="s-game-invite-poll-active-seconds"' in index_html
    assert 'id="s-server-connection-monitor-seconds"' in index_html
    assert 'id="s-drive-dashboard-lazy-refresh-seconds"' in index_html
    assert "function renderSystemResourceBoard" in admin_js
    assert "function startSystemResourcePoll" in admin_js
    assert "function stopSystemResourcePoll" in admin_js
    assert "function installRootManagementVisibilityGuard" in admin_js
    assert 'document.addEventListener("visibilitychange"' in admin_js
    assert "stopRootManagementPolls" in admin_js
    assert "resumeRootManagementPolls" in admin_js
    assert "canRunRootManagementPoll(isSystemEnvActive)" in admin_js
    assert "canRunRootManagementPoll(isSystemOverviewActive)" in admin_js
    assert "scheduleRootManagementIdleTask(loadPlatformStats" in admin_js
    assert 'API + "/admin/environment/resources"' in admin_js
    assert "system_resource_board_refresh_seconds" in admin_js
    assert "server_backpressure_traffic_refresh_seconds" in admin_js
    assert "edge guard" in admin_js
    assert "traffic-chart-edge" in css
    assert "totals.edge_guard" in admin_js
    assert "trading_live_price_refresh_seconds" in admin_js
    assert "comfyui_job_poll_seconds" in admin_js
    assert "notification_poll_seconds" in admin_js
    assert "game_invite_poll_hidden_seconds" in admin_js
    assert "server_connection_monitor_seconds" in admin_js
    assert "drive_dashboard_lazy_refresh_seconds" in admin_js
    assert "systemResourceGaugeMarkup" in admin_js
    assert "renderServerEnvOperationalStats" in admin_js
    assert "upload_bytes_per_second" in admin_js
    assert "cumulative_download_bytes" in admin_js
    assert "database_usage" in admin_js
    assert "json.resource_usage" in admin_js
    summary_renderer = admin_js.split("function renderServerEnvSummary", 1)[1].split(
        "function renderServerEnvTransferDetails", 1
    )[0]
    assert "上傳速度" not in summary_renderer
    assert "下載速度" not in summary_renderer
    assert "累計上傳" not in summary_renderer
    assert "累計下載" not in summary_renderer
    assert "DB 合計" not in summary_renderer
    assert 'envRefresh.addEventListener("click", loadServerEnv)' in bootstrap_js
    assert ".system-resource-arc" in css
    assert ".server-env-detail-grid" in css
    assert ".server-env-table" in css
    assert "conic-gradient(from 270deg at 50% 100%" in css
    assert "@keyframes system-resource-arc-pulse" in css


def test_admin_settings_and_server_output_failures_are_not_silent():
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
    notifications_js = (ROOT / "public" / "js" / "32-notifications.js").read_text(encoding="utf-8")

    assert 'setSettingsStatus(json.msg || "系統設定讀取失敗", false);' in admin_js
    assert 'securityTestMsg(json.msg || "伺服器即時輸出讀取失敗", false);' in admin_js
    assert 'list.innerHTML = `<p style="color:#ffb74d;">${sanitize(json.msg || "通知讀取失敗，請稍後重試。")}</p>`;' in notifications_js
