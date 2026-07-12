from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_restart_button_waits_for_offline_then_online():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )

    assert 'id="restart-server-status"' in index_html
    assert 'id="restart-server-btn"' in index_html
    assert 'type="button" id="restart-server-btn"' in index_html
    assert 'async function restartServer(event)' in admin_js
    assert 'event.preventDefault' in admin_js
    assert "安全驗證狀態失效" in admin_js
    assert "waitForRestartOffline(25000, operation)" in admin_js
    assert "waitForRestartOnline(previousStartedAt, 180000, operation)" in admin_js
    assert "25 秒內沒有偵測到伺服器離線" in admin_js
    assert "3 分鐘內未重新連線" in admin_js
    assert "location.reload()" in admin_js


def test_server_status_indicator_requires_consecutive_failures_or_slow_checks():
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")

    assert "const SERVER_CONNECTION_UNSTABLE_FAILURE_COUNT = 2;" in core_js
    assert "const SERVER_CONNECTION_OFFLINE_FAILURE_COUNT = 3;" in core_js
    assert "const SERVER_CONNECTION_UNSTABLE_LATENCY_MS = 2500;" in core_js
    assert "const SERVER_CONNECTION_UNSTABLE_SLOW_STREAK = 2;" in core_js
    assert 'setServerConnectionState("unstable", `連線偏慢 ${latency}ms`);' in core_js
    assert "if (serverConnectionSlowStreak >= SERVER_CONNECTION_UNSTABLE_SLOW_STREAK)" in core_js
    assert "if (serverConnectionFailures >= SERVER_CONNECTION_OFFLINE_FAILURE_COUNT)" in core_js
    assert "else if (serverConnectionFailures >= SERVER_CONNECTION_UNSTABLE_FAILURE_COUNT)" in core_js
    assert 'const isHealthy = state === "online";' in core_js
    assert "text.hidden = isHealthy;" in core_js
    assert 'text.textContent = isHealthy ? "" : label;' in core_js
    assert 'dot.setAttribute("aria-label", label);' in core_js
