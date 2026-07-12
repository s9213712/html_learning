import argparse
from pathlib import Path

import pytest

from scripts.testing.playwright_deep_site_check import (
    env_int,
    is_recoverable_network_cascade,
    require_external_server_credentials,
)
from scripts.testing.playwright_platform_health_check import ignored_browser_error


ROOT = Path(__file__).resolve().parents[3]


def test_deep_playwright_external_server_requires_environment_credentials(monkeypatch):
    names = (
        "PLAYWRIGHT_ROOT_PASSWORD",
        "PLAYWRIGHT_MANAGER_PASSWORD",
        "PLAYWRIGHT_TEST_PASSWORD",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    parser = argparse.ArgumentParser()

    with pytest.raises(SystemExit) as exc:
        require_external_server_credentials(parser, "https://127.0.0.1:5000")  # ci-safety: fixture-only

    assert exc.value.code == 2

    for name in names:
        monkeypatch.setenv(name, f"{name}-secret")
    require_external_server_credentials(parser, "https://127.0.0.1:5000")  # ci-safety: fixture-only


def test_deep_playwright_env_int_keeps_invalid_values_optional(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_TEST_INTEGER", "31")
    assert env_int("PLAYWRIGHT_TEST_INTEGER") == 31
    monkeypatch.setenv("PLAYWRIGHT_TEST_INTEGER", "invalid")
    assert env_int("PLAYWRIGHT_TEST_INTEGER") is None


def test_playwright_acceptance_runner_uses_isolated_runtime_and_expected_checks():
    script = ROOT / "scripts" / "testing" / "run_playwright_acceptance.sh"
    text = script.read_text(encoding="utf-8")

    assert "playwright_comfyui_workflow_builder_check.py" in text
    assert "playwright_platform_health_check.py" in text
    assert "playwright_deep_site_check.py" in text
    assert "/tmp/hackme_web_playwright_acceptance_" in text
    assert "--runtime-root \"${RUNTIME_BASE}/platform_attempt_${attempt}\"" in text
    assert "--runtime-root \"${RUNTIME_BASE}/deep_attempt_${attempt}\"" in text
    assert "RUN_DEEP_PLAYWRIGHT" in text
    assert "HTML_LEARNING_PORT=5000" not in text
    assert "AUTO_RUNTIME_BASE=1" in text
    assert '"${AUTO_RUNTIME_BASE}" == "1"' in text
    assert "caller-selected runtime base must not already exist" in text
    assert "PLAYWRIGHT_RUNTIME_BASE must resolve below /tmp" in text


def test_playwright_qa_workflow_template_installs_browser_and_runs_runner():
    workflow = ROOT / "scripts" / "testing" / "playwright-qa.workflow.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "python -m playwright install --with-deps chromium" in text
    assert "bash scripts/testing/run_playwright_acceptance.sh" in text
    assert "workflow_dispatch:" in text
    assert "schedule:" in text
    assert "github.event.inputs.run_deep_playwright" in text
    assert "03b.Comfyui" in text


def test_playwright_qa_workflow_is_installed_in_github_actions():
    workflow = ROOT / ".github" / "workflows" / "playwright-qa.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "name: playwright-qa" in text
    assert "python -m playwright install --with-deps chromium" in text
    assert "bash scripts/testing/run_playwright_acceptance.sh" in text
    assert "workflow_dispatch:" in text
    assert "schedule:" in text
    assert "github.event.inputs.run_deep_playwright" in text
    assert "03b.Comfyui" in text


def test_platform_health_filters_expected_offline_browser_http_failures():
    assert ignored_browser_error("503 https://127.0.0.1:40341/api/admin/trading/report")
    assert ignored_browser_error("503 https://127.0.0.1:40341/api/trading/btc-signal?market=BTC%2FUSDT")
    assert ignored_browser_error("503 https://127.0.0.1:40341/api/trading/grid-bots")
    assert ignored_browser_error("503 https://127.0.0.1:40341/api/root/trading/sitewide/pools")
    assert ignored_browser_error("503 https://127.0.0.1:40341/api/videos/1/realtime-proxy")
    assert ignored_browser_error("Failed to load resource: the server responded with a status of 503")
    assert not ignored_browser_error("503 https://127.0.0.1:40341/api/videos/1")
    assert not ignored_browser_error("500 https://127.0.0.1:40341/api/storage/files")


def test_comfyui_workflow_editor_exports_comfyui_and_project_json_formats():
    html = (ROOT / "public" / "comfyui-workflow-editor.html").read_text(encoding="utf-8")
    js = (ROOT / "public" / "js" / "comfyui-workflow-editor.js").read_text(encoding="utf-8")

    assert 'id="downloadJsonBtn"' in html
    assert 'id="downloadApiJsonBtn"' in html
    assert 'id="downloadPresetJsonBtn"' in html
    assert "下載 ComfyUI Workflow" in html
    assert "下載本站 Preset" in html
    assert "function downloadJson()" in js
    assert "function exportComfyUiWorkflowGraph()" in js
    assert "function exportComfyUiApiPrompt()" in js
    assert "function uiWidgetValues(node)" in js
    assert '"randomize"' in js
    assert "function downloadApiJson()" in js
    assert "function downloadPresetJson()" in js
    assert "workflowExportFileName()" in js
    assert "workflowExportFileName(\".api-prompt\")" in js
    assert "workflowExportFileName(\".hackme-preset\")" in js
    assert "ComfyUI 不能直接載入" in js
    assert 'downloadJsonBtn")?.addEventListener("click", downloadJson)' in js
    assert 'downloadApiJsonBtn")?.addEventListener("click", downloadApiJson)' in js
    assert 'downloadPresetJsonBtn")?.addEventListener("click", downloadPresetJson)' in js


def test_deep_playwright_shared_video_uses_unlock_share_session():
    script = (ROOT / "scripts" / "testing" / "playwright_deep_site_check.py").read_text(encoding="utf-8")

    assert "share_session_id" in script
    assert 'share_session_query = f"?share_session={share_session_id}"' in script
    assert 'f"/api/videos/shared/{token}/playback{share_session_query}"' in script


def test_deep_playwright_comfyui_host_port_counts_as_live_config():
    script = (ROOT / "scripts" / "testing" / "playwright_deep_site_check.py").read_text(encoding="utf-8")

    assert "or self.local_api_host.strip()" in script
    assert "or self.local_api_port" in script
    assert "or args.comfyui_api_host" in script
    assert "or args.comfyui_api_port" in script
    assert "elif cfg.local_base_dir or cfg.local_start_script or cfg.local_api_host or cfg.local_api_port:" in script


def test_deep_playwright_video_flow_opens_detail_before_like_selector():
    script = (ROOT / "scripts" / "testing" / "playwright_deep_site_check.py").read_text(encoding="utf-8")

    assert 'videos = fetch_json_get_retry(page, "/api/videos")' in script
    assert 'manage = fetch_json_get_retry(page, "/api/videos/manage")' in script
    assert 'latest_id = int(latest.get("id") or 0)' in script
    assert 'page.click("#video-back-btn")' in script
    assert "page.wait_for_selector(f'[data-video-open=\"{latest_id}\"]', state=\"visible\"" in script
    assert "page.locator(f'[data-video-open=\"{latest_id}\"]').first.click" in script
    assert "page.wait_for_selector(f'[data-video-like=\"{latest_id}\"]'" in script
    assert 'parser.add_argument("--only-video-share"' in script


def test_deep_playwright_only_downgrades_correlated_recovered_network_cascades():
    script = (ROOT / "scripts" / "testing" / "playwright_deep_site_check.py").read_text(encoding="utf-8")

    assert is_recoverable_network_cascade("requestfailed", "net::ERR_NETWORK_CHANGED GET https://127.0.0.1/app.js")
    assert is_recoverable_network_cascade("pageerror", "toggleChatCreatePanel is not defined")
    assert not is_recoverable_network_cascade("pageerror", "TypeError: cannot read properties of null")
    assert "navigation_has_network_change" in script
    assert 'collector.finish_navigation(page, recovered=True)' in script
    assert '"browser_warnings": browser_warnings' in script
    assert "def goto_with_network_retry" in script
    assert 'goto_with_network_retry(anon_page, base_url + "/comfyui-workflow-editor.html")' in script
    assert "controlled server backpressure" in script
    assert 'headers.get("x-hackme-backpressure-rejected") == "1"' in script
    assert "click_wait_recovered" in script
    assert 'health = fetch_json_get_retry(page, "/api/admin/health")' in script


def test_deep_playwright_sets_up_admin_state_before_loading_full_app():
    script = (ROOT / "scripts" / "testing" / "playwright_deep_site_check.py").read_text(encoding="utf-8")

    assert "def load_authenticated_app(page, base_url: str)" in script
    assert "login(page, base_url, load_app=False)" in script
    assert "root_auth_headers.update(build_direct_auth_headers(page, base_url))" in script
    assert "enable_required_features(page, base_url, load_app=False, auth_headers=root_auth_headers)" in script
    assert "apply_optional_comfyui_settings(rec, page, optional_comfyui, base_url, root_auth_headers)" in script
    assert 'rec.guard("load_authenticated_app", lambda: load_authenticated_app(page, base_url))' in script


def test_deep_playwright_reuses_isolated_server_and_checks_all_mobile_module_tabs():
    script = (ROOT / "scripts" / "testing" / "playwright_deep_site_check.py").read_text(encoding="utf-8")

    assert 'parser.add_argument("--base-url"' in script
    assert "urlopen_json(base_url + \"/api/version\"" in script
    assert "server = start_server(runtime_root, port) if not args.base_url else None" in script
    assert '{"width": 360, "height": 800}' in script
    assert '{"width": 390, "height": 844}' in script
    assert '{"width": 768, "height": 1024}' in script
    assert "control outside viewport" in script
    assert "drain_frontend_failures(page)" in script
    assert "main_frontend_failure_buffer" in script
    assert '"#module-main-tabs > .tab[id^=\'tab-module-\']"' in script
    assert 'role_label="member"' in script
    assert '"module_tabs_member_mobile_390x844"' in script
    assert "def check_account_context_isolation_journey(" in script
    assert '"account_context_isolation_journey"' in script
    assert '"account_context_cross_user_isolation"' in script
    assert 'error_collector.register(member_mobile_page, "module-tabs-member-mobile-390x844")' in script
    assert "record_browser_error" not in script
    assert "record_browser_warning" not in script
    assert '"ai_agent_root_launch_preflight_dry_run"' in script
    assert 'bulk_dir = f"/QA/bulk-{stamp}"' in script
    assert 'plain_name = f"plain-note-{stamp}.txt"' in script
    assert 'f"ai-agent-user-live-qa-{user_file_stamp}.txt"' in script


def test_deep_playwright_browser_fetches_are_bounded_and_traceable():
    script = (ROOT / "scripts" / "testing" / "playwright_deep_site_check.py").read_text(encoding="utf-8")

    assert "timeout_ms: int = 30000" in script
    assert "new AbortController()" in script
    assert 'print(f"[INFO] api_surface: {endpoint}", flush=True)' in script
    assert "fetch_error" in script
    assert "result.status === 503 && result.backpressureRejected" in script
    assert "x-hackme-backpressure-rejected" in script
    assert "backpressure_attempts" in script


def test_deep_playwright_api_surface_uses_direct_session_http():
    script = (ROOT / "scripts" / "testing" / "playwright_deep_site_check.py").read_text(encoding="utf-8")

    assert "def build_direct_auth_headers(page, base_url: str)" in script
    assert "def check_api_surface(rec: Recorder, page, base_url: str, auth_headers: dict[str, str])" in script
    assert 'fetch_json_direct(page, base_url, "GET", endpoint, timeout_seconds=25, auth_headers=auth_headers)' in script
    assert "check_api_surface(rec, page, base_url, root_auth_headers)" in script


def test_deep_playwright_accepts_current_realtime_proxy_payload():
    script = (ROOT / "scripts" / "testing" / "playwright_deep_site_check.py").read_text(encoding="utf-8")

    assert "def realtime_or_direct_stream_url" in script
    assert 'body.get("realtime_proxy_url")' in script
    assert 'realtime_proxy.get("url")' in script
    assert 'playback_mode in {"direct", "realtime", "realtime_proxy"}' in script
    assert 'shared_playback_mode in {"direct", "realtime", "realtime_proxy"}' in script


def test_platform_health_auth_wait_and_screenshots_are_ci_tolerant():
    deep_script = (ROOT / "scripts" / "testing" / "playwright_deep_site_check.py").read_text(encoding="utf-8")
    platform_script = (ROOT / "scripts" / "testing" / "playwright_platform_health_check.py").read_text(encoding="utf-8")

    assert "def wait_for_auth_app(page, *, timeout: int = 30000)" in deep_script
    assert "skipped non-blocking screenshot capture" in platform_script
    assert "wait_for_auth_app(page)" in platform_script


def test_platform_health_checks_mobile_root_operations_tabs_and_timings():
    platform_script = (ROOT / "scripts" / "testing" / "playwright_platform_health_check.py").read_text(encoding="utf-8")

    assert "def check_mobile_root_operations" in platform_script
    assert "switch_system_tab(page, tab)" in platform_script
    assert '"health", "sec-server-health"' in platform_script
    assert '"capacity", "sec-settings-backpressure"' in platform_script
    assert '"env", "sec-server-env"' in platform_script
    assert "active_root_operations_overflow" in platform_script
    assert "server-health-frontend-observability" in platform_script
    assert "__hackmeRootAdminTimings" in platform_script
    assert "phase15_mobile_root_operations" in platform_script


def test_documented_playwright_health_entrypoints_exist_and_delegate():
    testing_dir = ROOT / "scripts" / "testing"
    full_site = (testing_dir / "playwright_full_site_check.py").read_text(encoding="utf-8")
    visual = (testing_dir / "playwright_visual_health_check.py").read_text(encoding="utf-8")
    mobile = (testing_dir / "playwright_mobile_viewports.py").read_text(encoding="utf-8")

    assert "playwright_deep_site_check.py" in full_site
    assert "--max-chess-human-moves" in full_site
    assert "playwright_comfyui_workflow_builder_check.py" in visual
    assert "playwright_platform_health_check.py" in visual
    assert "playwright_platform_health_check.py" in mobile
