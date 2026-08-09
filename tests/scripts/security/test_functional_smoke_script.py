import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "security" / "pentest" / "run_functional_smoke.sh"


def test_functional_smoke_refuses_runtime_outside_tmp(tmp_path):
    runtime = Path("/tmp") / f"not_hackme_web_functional_{tmp_path.name}"
    result = subprocess.run(
        [str(SCRIPT), "--runtime", str(runtime), "--out", "/tmp/hackme_web_test_artifacts/security"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )

    assert result.returncode == 2
    assert "runtime must resolve below /tmp/hackme_web_*" in result.stdout


def test_functional_smoke_refuses_existing_or_symlink_runtime(tmp_path):
    runtime = Path("/tmp") / f"hackme_web_functional_symlink_{tmp_path.name}"
    runtime.symlink_to(ROOT, target_is_directory=True)
    try:
        result = subprocess.run(
            [str(SCRIPT), "--runtime", str(runtime), "--out", str(tmp_path / "reports")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    finally:
        runtime.unlink(missing_ok=True)

    assert result.returncode == 2
    assert "runtime must not already exist" in result.stdout


def test_functional_smoke_sanitizes_report_credentials():
    script = SCRIPT.read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "security" / "FUNCTIONAL_SMOKE.md").read_text(encoding="utf-8")

    assert 'if [[ -e "$OUT_DIR" || -L "$OUT_DIR" ]]' in script
    assert 'COOKIE_JAR="$RUNTIME_ROOT/.functional_smoke_cookies.txt"' in script
    assert "generate_smoke_password()" in script
    assert 'ROOT_PASSWORD="${ROOT_PASSWORD:-$(generate_smoke_password)}"' in script
    assert "sanitize_report_secrets()" in script
    assert 'rm -f "$COOKIE_JAR"' in script
    assert 'root.rglob("*.json")' in script
    assert '"<REDACTED>" if sensitive_key(key)' in script
    assert '"share_url"' in script
    assert '"fragment_key"' in script
    assert "report secret sanitization failed" in script
    assert 'args+=(--data-binary @-)' in script
    assert "printf '%s' \"$body\" | curl" in script
    assert "證據包不得保留可重放憑證" in docs
    assert "cookie jar 只會建立在 disposable runtime" in docs


def test_functional_smoke_waits_for_reset_restart_reconnect():
    script = (ROOT / "scripts" / "security" / "pentest" / "run_functional_smoke.sh").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "security" / "FUNCTIONAL_SMOKE.md").read_text(encoding="utf-8")

    assert 'RESET_OFFLINE_TIMEOUT="${RESET_OFFLINE_TIMEOUT:-20}"' in script
    assert 'RESET_RECONNECT_TIMEOUT="${RESET_RECONNECT_TIMEOUT:-180}"' in script
    assert 'START_TIMEOUT="${START_TIMEOUT:-180}"' in script
    assert "wait_for_restart_reconnect()" in script
    assert 'server_started_at "$RAW_DIR/reset_before_version.json"' in script
    assert 'request "server reset runtime state"' in script
    assert 'wait_for_restart_reconnect "server reset restart reconnect"' in script
    assert 'data.get("started_at", "")' in script
    assert "server did not go offline within" in script
    assert "offline phase" in script
    assert "server did not reconnect with a new started_at within" in script
    assert "RESET_OFFLINE_TIMEOUT" in docs
    assert "RESET_RECONNECT_TIMEOUT" in docs
    assert "20 秒內觀察連線失敗" in docs
    assert "started_at" in docs
    assert "3 分鐘內重新連線" in docs
    assert 'HACKME_RUNTIME_DIR="$RUNTIME_ROOT"' in script
    assert "ensure_start_port || return 1" in script
    assert "refresh_base_url()" in script


def test_functional_smoke_uses_valid_bounded_custom_profile_name():
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'SMOKE_PROFILE_NAME="smoke_${BASHPID}"' in script
    assert '\\\"name\\\":\\\"${SMOKE_PROFILE_NAME}\\\"' in script
    assert '\\\"mode\\\":\\\"${SMOKE_PROFILE_NAME}\\\"' in script
    assert "smoke_profile_${RUN_ID,,}" not in script


def test_functional_smoke_has_explicit_qa_full_and_core_only_modes():
    script = (ROOT / "scripts" / "security" / "pentest" / "run_functional_smoke.sh").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "security" / "FUNCTIONAL_SMOKE.md").read_text(encoding="utf-8")
    qa_docs = (ROOT / "docs" / "11_QA_TESTING.md").read_text(encoding="utf-8")
    index = (ROOT / "scripts" / "INDEX.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "security" / "QA_ARCHITECTURE.md").read_text(encoding="utf-8")

    assert "[--qa-full|--core-only]" in script
    assert "--qa-full" in script
    assert "--core-only" in script
    assert "FUNCTIONAL_SCOPE=\"go_live_core\"" in script
    assert "Scope: go-live core only; broad QA product workflows are skipped" in script
    assert "Scope: QA full functional smoke" in script
    assert "QA full functional smoke" in script
    assert "production-gate core coverage and skips broad product QA workflows" in script
    assert "`--qa-full` 是預設行為" in docs
    assert "`--core-only`" in docs
    assert "ComfyUI/reports/moderation" in docs
    assert "scripts/security/pentest/run_functional_smoke.sh --qa-full --port 50741" in qa_docs
    assert "scripts/testing/pytest_in_tmp.sh -q \\" in qa_docs
    assert "tests/frontend/games" in qa_docs
    assert "run_functional_smoke.sh --core-only" in index
    assert "run_functional_smoke.sh --qa-full" in index
    assert "QA-only product workflows" in architecture


def test_functional_smoke_covers_latest_trading_and_announcement_paths():
    script = (ROOT / "scripts" / "security" / "pentest" / "run_functional_smoke.sh").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "security" / "FUNCTIONAL_SMOKE.md").read_text(encoding="utf-8")

    assert 'request "points admin credit smoke user disabled" "POST" "/api/admin/points/adjust" "410"' in script
    assert '"points admin credit disabled policy"' in script
    assert 'request "points chain seal async job" "POST" "/api/root/points/chain/seal" "202"' in script
    assert '"points chain seal async guidance"' in script
    assert 'request "points chain verify async job" "GET" "/api/root/points/chain/verify" "202"' in script
    assert 'request "points economy stats async job" "GET" "/api/admin/points/economy/stats" "202"' in script
    assert 'bool(data.get("job_uuid") or data.get("job_id"))' in script
    assert 'request "points chain backup manual disabled" "POST" "/api/root/points/chain/backups" "410" \'{}\'' in script
    assert '"points chain backup disabled policy"' in script
    assert 'request "trading market buy blocked in custom profile" "POST" "/api/trading/orders" "400"' in script
    assert '"trading custom profile block guidance"' in script
    assert 'request "security center switch to test mode" "POST" "/api/admin/server-mode" "200" \'{"mode":"test","confirm":"SWITCH_TO_TEST","notes":"functional smoke trading diagnostics"}\'' in script
    assert 'enable_smoke_feature_flags_after_mode_switch "admin after test mode"' in script
    assert 'request "trading live price" "GET" "/api/trading/live-price?market=ETH/USDT" "200"' in script
    assert 'request "trading reference prices" "GET" "/api/trading/reference-prices?market=ETH/USDT&interval=15m&limit=24" "200"' in script
    assert 'request "trading grid preview" "POST" "/api/trading/grid/preview" "200"' in script
    assert 'request "admin rotate maintenance bypass token" "POST" "/api/admin/access-controls/maintenance-bypass-token" "200" \'{"confirm":"ROTATE","ttl_minutes":30}\'' in script
    assert 'MAINTENANCE_BYPASS_TOKEN="$(json_expr \'data["token"]\'' in script
    assert 'request "security center switch to internal_test mode" "POST" "/api/admin/server-mode" "200" \'{"mode":"internal_test","confirm":"SWITCH_TO_INTERNAL_TEST","notes":"functional smoke routed trading write validation"}\'' in script
    assert "datetime.now() + timedelta(hours=6)" in script
    assert "Using utcnow() here creates a token" in script
    assert 'request "root create tester token for smoke trading" "POST" "/api/root/tester-token/create" "200"' in script
    assert 'login_smoke_user "auth login smoke user internal_test" "$TESTER_TOKEN"' in script
    assert 'request_with_tester_token "trading tester internal_test limit order accepted after live warmup" "POST" "/api/trading/orders" "200"' in script
    assert 'internal_test routed trading write succeeds after live quote warmup' in script
    assert '"trading tester internal_test routed order payload"' in script
    assert '"isinstance": isinstance' in script
    assert 'isinstance(data.get("order"), dict)' in script
    assert 'request "security center switch back to test mode after internal_test" "POST" "/api/admin/server-mode" "200"' in script
    assert 'request "trading root price fusion status" "GET" "/api/root/trading/price-fusion-status?market_symbol=ETH/USDT" "200"' in script
    assert 'request "trading root bot audit dashboard" "GET" "/api/root/trading/bot-audit/dashboard?limit=10" "200"' in script
    assert 'request "trading root bot audit manual run" "POST" "/api/root/trading/bot-audit/run" "200"' in script
    assert '"community create announcement guidance"' in script
    assert 'request "community announcements list after create" "GET" "/api/community/announcements" "200"' in script
    assert 'request "community edit announcement" "PUT" "/api/community/announcements/${ANNOUNCEMENT_ID}" "200"' in script
    assert 'request "comfyui civitai search missing api key" "POST" "/api/root/comfyui/civitai/search" "400"' in script
    assert 'request "comfyui workflow presets list" "GET" "/api/comfyui/workflows" "200"' in script
    assert 'request "comfyui workflow import unsafe path rejected" "POST" "/api/comfyui/workflows/import" "400"' in script
    assert 'request "comfyui template preview adds embedding child" "POST" "/api/comfyui/templates/preview" "200"' in script
    assert '"comfyui template embeddings text child"' in script
    assert '"text:embeddings"' in script
    assert '"embedding_shortcuts"' in script
    assert 'python3 scripts/admin/root_recovery.py --help' in script
    assert '"price_type" in data and "source" in data and "confidence" in data and "stale" in data and "degraded" in data and "provider_count" in data' in script
    assert '"reference_price_context" in data and "risk_grade_price_context" in data' in script
    assert '"connected" in data and "fallback" in data and "last_update_at" in data and "exclusion_reason" in data and "transport_state" in data' in script
    assert '"transport_state" in data.get("status", {}) and "connected" in data.get("status", {}) and "fallback" in data.get("status", {}) and "stale" in data.get("status", {}) and "confidence" in data.get("status", {}) and "provider_count" in data.get("status", {}) and "last_update_at" in data.get("status", {}) and "exclusion_reason" in data.get("status", {})' in script
    assert "trading extras" in docs
    assert "announcement create/edit" in docs
    assert "custom profile trading block plus test-mode diagnostics and internal_test routed order payload after live warm-up" in docs
    assert "browser-only mode 需帶 maintenance bypass token" in docs
    assert "本地時間、無時區" in docs
    assert "seal/verify/one-click anomaly handler/economy stats async job payloads" in docs
    assert "Civitai search API key guard" in docs
    assert "workflow preset list/import guards" in docs
    assert "template preview text panel `text:embeddings` / `embedding_shortcuts` child" in docs
    assert "offline root recovery CLI availability" in docs


def test_functional_smoke_login_helpers_require_real_session_cookie():
    script = (ROOT / "scripts" / "security" / "pentest" / "run_functional_smoke.sh").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "security" / "FUNCTIONAL_SMOKE.md").read_text(encoding="utf-8")

    assert "session_token_from_cookie()" in script
    assert 'fail "auth session refresh" "missing session_token cookie after root login"' in script
    assert 'fail "auth session refresh smoke user" "missing session_token cookie after smoke user login"' in script
    assert "port_is_available()" in script
    assert "pick_free_local_port()" in script
    assert 'local connect_host="${CLIENT_HOST:-$HOST}"' in script
    assert 'BASE_URL="${SMOKE_SCHEME}://${connect_host}:${PORT}"' in script
    assert "refresh_base_url()" in script
    assert 'if [[ -z "$PORT" ]]; then' in script
    assert 'auto-pick failed; choose --port explicitly or run outside a socket-restricted sandbox' in script
    assert 'pass "server startup port selection" "port $previous_port already in use; switched to free port $PORT"' in script
    assert 'fail "server startup port selection" "port $PORT already in use; choose another --port or PORT value"' in script
    assert "socket probe" in docs


def test_functional_smoke_covers_video_share_flow_and_user_facing_error_paths():
    script = (ROOT / "scripts" / "security" / "pentest" / "run_functional_smoke.sh").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "security" / "FUNCTIONAL_SMOKE.md").read_text(encoding="utf-8")

    assert '"feature_videos_enabled": true' in script
    assert 'multipart_request "video upload missing file" "/api/videos/upload" "400"' in script
    assert 'multipart_request "video upload rejects non media" "/api/videos/upload" "400"' in script
    assert 'multipart_request "video upload and publish shared video" "/api/videos/upload" "200"' in script
    assert 'request "video shared page" "GET" "${SMOKE_VIDEO_SHARE_URL}" "200"' in script
    assert 'request "video shared page script asset" "GET" "/js/shared-video.js" "200"' in script
    assert 'request "video shared detail" "GET" "/api/videos/shared/${SMOKE_VIDEO_SHARE_TOKEN}" "200"' in script
    assert 'request "video shared playback" "GET" "/api/videos/shared/${SMOKE_VIDEO_SHARE_TOKEN}/playback" "200"' in script
    assert 'request "video share revoke" "DELETE" "/api/videos/${SMOKE_VIDEO_ID}/share-link" "200" \'{}\'' in script
    assert 'request "video shared detail after revoke" "GET" "/api/videos/shared/${SMOKE_VIDEO_SHARE_TOKEN}" "404"' in script
    assert 'multipart_request "storage e2ee media upload" "/api/storage/files" "200"' in script
    assert '"privacy_mode=e2ee"' in script
    assert 'request "video publish shared e2ee" "POST" "/api/videos/publish" "200"' in script
    assert 'E2EE_STORAGE_FILE_ID="$(json_expr \'data["file"]["file_id"]\'' in script
    assert 'request "video e2ee shared detail locked" "GET" "/api/videos/shared/${E2EE_SHARE_TOKEN}" "401"' in script
    assert 'request "video e2ee shared unlock wrong password" "POST" "/api/videos/shared/${E2EE_SHARE_TOKEN}/unlock" "403"' in script
    assert 'request "video e2ee shared unlock" "POST" "/api/videos/shared/${E2EE_SHARE_TOKEN}/unlock" "200"' in script
    assert 'request "video e2ee shared playback" "GET" "/api/videos/shared/${E2EE_SHARE_TOKEN}/playback" "200"' in script
    assert 'request "video e2ee shared key payload" "GET" "/api/videos/shared/${E2EE_SHARE_TOKEN}/e2ee-key" "200"' in script
    assert 'str(data.get("mode") or "").startswith("e2ee")' in script
    assert 'bool(data["video"].get("share_requires_fragment_key"))' in script
    assert 'data["e2ee_share"].get("privacy_mode") == "e2ee"' in script
    assert 'request "video e2ee share revoke" "DELETE" "/api/videos/${E2EE_VIDEO_ID}/share-link" "200" \'{}\'' in script
    assert 'request "video e2ee shared detail after revoke" "GET" "/api/videos/shared/${E2EE_SHARE_TOKEN}" "404"' in script
    assert 'assert_body_contains \\' in script
    assert '"讀取中..."' in script
    assert '"/js/shared-video.js"' in script
    assert '"AbortController"' in script
    assert '"分享影音載入失敗"' in script
    assert '"video upload missing file guidance"' in script
    assert '"video upload non-media guidance"' in script
    assert '"video shared revoke guidance"' in script
    assert "Video platform: upload/publish, shared page load, password unlock, E2EE bootstrap, anonymous shared playback, revoke flow" in docs
    assert "known regressions: copy-share fallback and shared page loading timeout guard" in docs
    assert "strict E2EE share flow now verifies password gate" in docs
    assert "remote downloader rejections expose a user-facing message" in docs
    assert "HACKME_RUNTIME_DIR" in docs
