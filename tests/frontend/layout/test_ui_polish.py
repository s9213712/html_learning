from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[3]


def test_global_ui_polish_feedback_is_wired():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    styles_css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    root_quick_settings_js = (ROOT / "public" / "js" / "01-root-quick-settings.js").read_text(encoding="utf-8")
    admin_js = (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")

    assert 'id="toast-host"' in index_html
    assert 'role="status"' in index_html
    assert 'aria-relevant="additions text"' in index_html
    assert "/js/01-root-quick-settings.js?v=" in index_html

    assert "function showAppToast" in core_js
    assert "function announceInlineMessage" in core_js
    assert "function installUiInteractionFeedback" in core_js
    assert "function animateActiveModule" in core_js
    assert "if (!options.skipToast) announceInlineMessage(message, ok);" in core_js
    assert "installUiInteractionFeedback();" in core_js
    assert 'closest?.(".btn, .tab, .icon-action-btn, .game-catalog-card' in core_js
    assert "function ensureRootModuleSettingsButtons()" in root_quick_settings_js
    assert "function openRootModuleSettings" in root_quick_settings_js
    assert "function saveRootModuleSettings" in root_quick_settings_js
    assert 'window.syncRootModuleSettingsButtons = syncRootModuleSettingsButtons;' in root_quick_settings_js
    assert 'document.addEventListener("hackme:module-changed", syncRootModuleSettingsButtons);' in root_quick_settings_js
    assert "root-trading-borrowing-enabled" in root_quick_settings_js
    assert "s-comfyui-connection-mode" in root_quick_settings_js
    assert "s-feature-privacy-uploads-enabled" in root_quick_settings_js
    assert 'animateActiveModule(normTab);' in admin_js
    assert "syncRootModuleSettingsButtons();" in admin_js

    assert "@keyframes ui-module-enter" in styles_css
    assert "@keyframes ui-press-ripple" in styles_css
    assert "@keyframes ui-shimmer" in styles_css
    assert ".module-section.ui-module-enter > .admin-tools" in styles_css
    assert ".root-module-settings-btn" in styles_css
    assert ".root-module-settings-btn.show" in styles_css
    assert ".root-module-settings-modal" in styles_css
    assert ".toast-host" in styles_css
    assert ".toast.show" in styles_css
    assert ".toast-ok::before" in styles_css
    assert ".toast-err::before" in styles_css
    assert ".btn.loading" in styles_css
    assert ".field:focus-within label" in styles_css
    assert "prefers-reduced-motion: reduce" in styles_css
    assert "#module-drive .drive-breadcrumb button" in styles_css
    assert "overflow-wrap: anywhere;" in styles_css


def test_status_messages_do_not_create_duplicate_button_feedback():
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    js_files = list((ROOT / "public" / "js").rglob("*.js"))
    all_js = "\n".join(path.read_text(encoding="utf-8") for path in js_files)

    flash_match = re.search(
        r"function\s+flash\s*\([^)]*\)\s*\{(?P<body>.*?)\n\}\n\nfunction\s+uiPrefersReducedMotion",
        core_js,
        re.S,
    )
    assert flash_match, "flash() helper should remain next to uiPrefersReducedMotion()"
    flash_body = flash_match.group("body")
    assert "showActionFeedback" not in flash_body
    assert "announceInlineMessage" not in flash_body
    assert 'el.setAttribute("role", ok ? "status" : "alert");' in flash_body
    assert "showActionFeedback(document.activeElement" not in all_js
    assert "showActionFeedback(tradingActiveActionButton" not in all_js
    assert "announceInlineMessage(text, ok);" not in all_js

    assert "function showCopyLinkFeedback" in core_js
    assert 'showCopyLinkFeedback(button, "已完成複製", true)' in all_js


def test_api_fetch_retries_only_explicit_pre_handler_backpressure():
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    drive_js = (ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8")

    assert 'response.headers?.get?.("X-Hackme-Backpressure-Rejected") === "1"' in core_js
    assert 'payload?.error === "server_busy"' in core_js
    assert "const maxAttempts = isReplayableFetchBody(opts.body) ? 3 : 1;" in core_js
    assert "serverBusyRetryDelayMs(response, responsePayload)" in core_js
    assert 'response.headers?.get?.("X-Hackme-Backpressure") || body?.error' not in core_js
    assert 'result?.backpressure_rejected === "1" && result?.json?.error === "server_busy"' in drive_js


def test_background_frontend_failures_are_bounded_redacted_and_observable():
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    critical_files = {
        "comfyui": (ROOT / "public" / "js" / "36-comfyui.js").read_text(encoding="utf-8"),
        "workflows": (ROOT / "public" / "js" / "36-comfyui-workflows.js").read_text(encoding="utf-8"),
        "trading": (ROOT / "public" / "js" / "56-trading-bots.js").read_text(encoding="utf-8"),
        "root": (ROOT / "public" / "js" / "01-root-quick-settings.js").read_text(encoding="utf-8"),
        "fps": (ROOT / "public" / "js" / "38-fps-arena.js").read_text(encoding="utf-8"),
        "stickman": (ROOT / "public" / "js" / "games" / "stickman-shooter.js").read_text(encoding="utf-8"),
        "drive": (ROOT / "public" / "js" / "35-drive.js").read_text(encoding="utf-8"),
        "games": (ROOT / "public" / "js" / "38-games.js").read_text(encoding="utf-8"),
        "agent": (ROOT / "public" / "js" / "37-ai-agent.js").read_text(encoding="utf-8"),
        "admin": (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8"),
    }

    assert "const FRONTEND_FAILURE_BUFFER_LIMIT = 100;" in core_js
    assert "function reportFrontendFailure" in core_js
    assert "redactFrontendFailureText" in core_js
    assert "hackme:frontend-failure" in core_js
    assert "window.__hackmeFrontendFailures" in core_js
    assert "token|key|password|secret|signature" in core_js
    assert "interruptRequest.catch(() => {})" not in critical_files["comfyui"]
    assert "}).catch(() => {});" not in critical_files["workflows"]
    assert "loadEconomyDashboard().catch(() => {})" not in critical_files["trading"]
    assert "refreshComfyuiStatus({ switchAway: false }).catch(() => {})" not in critical_files["root"]
    assert "start?.(multiplayerRoom.id).catch(() => {})" not in critical_files["fps"]
    assert "start?.(multiplayerRoom.id).catch(() => {})" not in critical_files["stickman"]
    assert "try { await restoreResumableUploadSessions(); } catch (_) {}" not in critical_files["drive"]
    assert "await loadChessRootDashboard().catch(() => {});" not in critical_files["games"]
    assert 'reportFrontendFailure("game-multiplayer-invite-poll", err)' in critical_files["games"]
    assert 'reportFrontendFailure("game-multiplayer-invite-refresh", refreshErr)' in critical_files["games"]
    assert '}).catch(() => undefined);' not in critical_files["agent"].split("function clearAiAgentConversation", 1)[1].split("function handleAiAgentAccountContextChanged", 1)[0]
    assert "async function clearAiAgentConversation()" in critical_files["agent"]
    assert 'reportFrontendFailure("ai-agent-conversation-load", err)' in critical_files["agent"]
    assert 'reportFrontendFailure("ai-agent-conversation-persist", new Error(message))' in critical_files["agent"]
    assert 'reportFrontendFailure("ai-agent-write-tools-load", err)' in critical_files["agent"]
    assert 'reportFrontendFailure("ai-agent-readonly-load", err)' in critical_files["agent"]
    assert 'reportFrontendFailure("ai-agent-models-load", err)' in critical_files["agent"]
    assert 'reportFrontendFailure("root-backpressure-traffic-poll", err)' in critical_files["admin"]
    assert 'reportFrontendFailure("admin-settings-auth-ui-refresh", err)' in critical_files["admin"]


def test_privileged_surfaces_are_hidden_in_initial_markup_and_revealed_by_role():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    comfyui_js = (ROOT / "public" / "js" / "36-comfyui.js").read_text(encoding="utf-8")
    games_js = (ROOT / "public" / "js" / "38-games.js").read_text(encoding="utf-8")
    chess_js = (ROOT / "public" / "js" / "games" / "chess.js").read_text(encoding="utf-8")
    root_quick_settings_js = (ROOT / "public" / "js" / "01-root-quick-settings.js").read_text(encoding="utf-8")

    assert 'id="tab-module-server" style="display:none;"' in index_html
    assert 'id="tab-module-accounts" style="display:none;"' in index_html
    assert 'id="tab-module-jobs" style="display:none;"' in index_html
    assert 'data-comfyui-view="models">Civitai / 模型管理</button>' in index_html
    assert 'data-comfyui-view="models" hidden' not in index_html
    assert 'id="comfyui-root-model-access-note" style="display:none;"' in index_html
    assert 'id="comfyui-root-model-panel" style="display:none;"' in index_html
    assert 'id="game-root-chess-panel" style="display:none;margin-top:1rem;"' in index_html
    assert 'id="game-award-btn" type="button" style="display:none;"' in index_html
    assert 'id="economy-root-card" style="display:none;margin-top:.75rem;"' in index_html
    assert 'id="trading-root-sitewide-card" style="display:none;margin-top:.75rem;"' in index_html
    assert 'id="economy-admin-card"' not in index_html
    assert 'id="trading-root-card" style="display:none;margin-bottom:.85rem;"' in index_html

    assert 'if (tabModuleServer) tabModuleServer.style.display = currentUser === "root" ? "" : "none";' in core_js
    assert 'if (tabModuleAccounts) tabModuleAccounts.style.display = canAccessModule("accounts") ? "" : "none";' in core_js
    assert 'adminWrap.classList.add("show");' in core_js
    assert 'adminWrap.classList.remove("show");' in core_js
    assert 'if (addPanel) addPanel.style.display = canManageUsers ? "block" : "none";' in core_js
    assert "function canManageComfyuiLocalModels" in comfyui_js
    assert "modelsTab.hidden = false" in comfyui_js
    assert 'awardBtn.style.display = currentUser === "root" && key === "chess" ? "" : "none";' in games_js
    assert 'panel.style.display = gameRootChessPanelVisible() ? "" : "none";' in chess_js
    assert 'const rootMode = currentUser === "root";' in root_quick_settings_js
    assert "button.hidden = !visible;" in root_quick_settings_js
