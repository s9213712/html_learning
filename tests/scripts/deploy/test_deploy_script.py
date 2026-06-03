import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_server_entrypoint_exposes_doctor_and_fails_loudly_on_missing_runtime():
    server_py = (ROOT / "server.py").read_text(encoding="utf-8")

    assert 'parser.add_argument("--doctor", action="store_true"' in server_py
    assert "def _doctor_report():" in server_py
    assert "def _print_doctor_report(report):" in server_py
    assert "missing runtime directory:" in server_py
    assert "ENTRYPOINT_DOCTOR_MODE" in server_py
    assert "run_doctor()" in server_py
    assert "doctor: fail" in server_py


def test_dev_launcher_copies_repo_to_tmp_and_bootstraps_dev_friendly_runtime():
    script = (ROOT / "test_for_develop.sh").read_text(encoding="utf-8")

    assert 'RUN_ROOT="${RUN_ROOT:-/tmp/hackme_web_dev_' in script
    assert 'tar -C "$SOURCE_ROOT"' in script
    assert 'local copy_items=(' in script
    assert '"server.py"' in script
    assert '"public"' in script
    assert '"routes"' in script
    assert '"services"' in script
    assert '"workflows"' in script
    assert 'reference repos/deploy examples/git' in script
    assert '--exclude=\'*/runtime\'' in script
    assert 'find "$COPY_ROOT/scripts" "$COPY_ROOT/tests" -type f -name \'*.md\' -delete' in script
    assert 'HTML_LEARNING_ROOT_PASSWORD="$ROOT_PASSWORD"' in script
    assert 'HTML_LEARNING_MANAGER_PASSWORD="$MANAGER_PASSWORD"' in script
    assert 'HTML_LEARNING_TEST_PASSWORD="$TEST_PASSWORD"' in script
    assert 'HTML_LEARNING_ARGON2_TIME_COST="${HTML_LEARNING_ARGON2_TIME_COST:-1}"' in script
    assert 'HTML_LEARNING_ARGON2_MEMORY_COST="${HTML_LEARNING_ARGON2_MEMORY_COST:-8192}"' in script
    assert 'HTML_LEARNING_ARGON2_PARALLELISM="${HTML_LEARNING_ARGON2_PARALLELISM:-1}"' in script
    assert 'HTML_LEARNING_DISABLE_DEFAULT_PASSWORD_POLICY=1' in script
    assert 'CAPACITY_DEFAULTS_FILE="${HACKME_DEV_CAPACITY_DEFAULTS_FILE:-$SOURCE_ROOT/.hackme_capacity_defaults.env}"' in script
    assert "maybe_run_capacity_probe_for_gunicorn_defaults" in script
    assert "--capacity-probe" in script
    assert "--no-capacity-probe" in script
    assert "--cloud-drive-root PATH" in script
    assert "--cloud-drive-max-size SIZE" in script
    assert "--allow-any-host" in script
    assert "HTML_LEARNING_DISABLE_TRUSTED_HOSTS=1" in script
    assert "CAPACITY_SETTINGS_FINALIZED=0" in script
    assert 'if [[ "$CAPACITY_SETTINGS_FINALIZED" != "1" ]]; then' in script
    assert 'CAPACITY_SETTINGS_FINALIZED=1' in script
    assert "missing files are copied into PATH" in script
    assert 'CLOUD_DRIVE_STORAGE_ROOT="${HACKME_DEV_CLOUD_DRIVE_STORAGE_ROOT:-}"' in script
    assert 'CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB="${HACKME_DEV_CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB:-}"' in script
    assert "migrate_legacy_runtime_storage_to_cloud_drive_root" in script
    assert "[dev-tmp] storage migration:" in script
    assert 'export HTML_LEARNING_STORAGE_DIR="$EFFECTIVE_STORAGE_ROOT"' in script
    assert 'export HACKME_DEV_CLOUD_DRIVE_STORAGE_ROOT="$CLOUD_DRIVE_STORAGE_ROOT"' in script
    assert '"cloud_drive_storage_root"' in script
    assert '"cloud_drive_global_capacity_limit_mb"' in script
    assert "predeploy_capacity_probe.py" in script
    capacity_probe = (ROOT / "scripts" / "testing" / "predeploy_capacity_probe.py").read_text(encoding="utf-8")
    assert "--ux-p95-ms" in capacity_probe
    assert "--keep-app-limits" in capacity_probe
    assert "HACKME_CAPACITY_PROBE_UNLIMITED" in capacity_probe
    assert "AccountLadder" in capacity_probe
    assert "RoundProgress" in capacity_probe
    assert "Could not locate repo root" in capacity_probe
    assert "contaminated_after_app_limit" in capacity_probe
    assert "root points chain verify" in capacity_probe
    assert "application_limit" in capacity_probe
    assert "server_instability" in capacity_probe
    assert "rc1_capacity_gate" in capacity_probe
    release_gate = (ROOT / "scripts" / "qa" / "points_chain_release_gate.py").read_text(encoding="utf-8")
    assert '"--no-sync-defaults"' in release_gate
    assert 'export HACKME_DEV_GUNICORN_MAX_REQUESTS="$GUNICORN_MAX_REQUESTS"' in script
    assert 'HACKME_RUNTIME_OUTPUT_CAPTURE=0 "$PYTHON_BIN" - <<\'PY\'' in script
    assert '"audit_chain_enabled": False' in script
    assert '"integrity_guard_enabled": False' in script
    assert '"production_single_ip_account_lock_enabled": False' in script
    assert '"production_single_account_ip_lock_enabled": False' in script
    assert '"ip_blocking_enabled": False' in script
    assert '"session_idle_timeout_minutes": 1440' in script
    assert '"server_timezone": os.environ.get("HACKME_DEV_SERVER_TIMEZONE") or os.environ.get("TZ") or "Asia/Taipei"' in script
    assert "allow_risk_grade_usage=1" in script
    assert '("trading.margin_liquidation_enabled", "true")' in script
    assert '("trading.bot_auto_scan_enabled", "true")' in script
    assert '("trading.bot_audit_enabled", "true")' in script
    assert '("trading.background_worker_dev_ready_enabled", "true" if os.environ.get("HACKME_DEV_TRADING_BACKGROUND_DEV_READY"' in script
    assert '("background_worker_dev_ready_enabled", "true" if os.environ.get("HACKME_DEV_TRADING_BACKGROUND_DEV_READY"' in script
    assert 'setsid "$PYTHON_BIN" server.py >"$LOG_CAPTURE" 2>&1 < /dev/null &' in script


def test_dev_launcher_dry_run_resolves_cloud_drive_storage_options(tmp_path):
    storage_root = tmp_path / "drive-store"

    result = subprocess.run(
        [
            str(ROOT / "test_for_develop.sh"),
            "--cli",
            "--dry-run",
            "--server-runner",
            "flask",
            "--no-capacity-probe",
            "--cloud-drive-root",
            str(storage_root),
            "--cloud-drive-max-size",
            "1.5G",
            "--allow-any-host",
        ],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"cloud_drive_root:    {storage_root}" in result.stdout
    assert "cloud_drive_max_mb:  1536" in result.stdout
    assert "trusted_hosts:       disabled (dev only)" in result.stdout


def test_legacy_root_wrappers_are_removed():
    assert not (ROOT / "one_click_setup.sh").exists()
    assert not (ROOT / "on_live_reports_make.sh").exists()
    assert not (ROOT / "scripts" / "dev" / "run_tmp_server_py.sh").exists()
    assert not (ROOT / "scripts" / "dev" / "run_tmp_one_click.sh").exists()


def test_ci_workflows_call_current_script_paths():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    secrets = (ROOT / ".github" / "workflows" / "security-secrets-scan.yml").read_text(encoding="utf-8")

    assert "python scripts/prepush/pre_push_checks.py --ci" in ci
    assert "scripts/pre_push_checks.py" not in ci
    assert "python scripts/security/gate/scan_plaintext_secrets.py" in secrets
    assert "scripts/security/scan_plaintext_secrets.py" not in secrets


def test_gitignore_only_ignores_repo_root_runtime_directory():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "/runtime/" in gitignore
    assert ".hackme_capacity_defaults.env" in gitignore
    assert "\nruntime/\n" not in gitignore
    assert "\nstorage/\n" not in gitignore
