# Dev Launcher Remote Update Study

Date: 2026-06-03 18:05 CST
Branch: 04.BLOCKCHAIN_RC1
Remote head studied: eecf06b Streamline dev launch prompts

## Scope

Cloned the latest remote branch into `/tmp/hackme_web_remote_study_20260603_eecf06b` and reviewed the new dev launcher update before applying it to the main checkout.

## Update Summary

- `test_for_develop.sh`
  - Adds explicit env/CLI detection so remote-download, Transmission, runtime layout, and maintenance prompts can be skipped when already configured.
  - Adds a Transmission setup mode prompt: automatic helper setup, manual existing daemon setup, or skip.
  - Persists more Transmission settings into `restart_develop_server.sh`.
  - Adds `--bt-download-staging-dir` / `--transmission-download-dir` / `--transmission-staging-dir`.
  - Treats runtime backup path as an output archive or existing output directory, not as the runtime root.
- `scripts/storage/setup_transmission_backend.sh`
  - Adds `--no-install`.
  - Can install missing `transmission-daemon` / `acl` on apt-based systems unless disabled.
  - Starts the service once to initialize a missing settings file.
- `.gitignore`
  - Ignores generated `restart_develop_server.sh`.

## Finding

- Stale test contract: `tests/scripts/deploy/test_deploy_script.py` still expected the dev launcher to hard-code `trading.background_worker_dev_ready_enabled=true`.
- Current launcher intentionally writes this setting from `HACKME_DEV_TRADING_BACKGROUND_DEV_READY`, so the test now checks for the conditional wiring instead of a fixed true value.

## Verification

- `bash -n test_for_develop.sh scripts/storage/setup_transmission_backend.sh`
- `./test_for_develop.sh --help`
- `scripts/storage/setup_transmission_backend.sh --help`
- `git diff --check 5968e6f..HEAD`
- `./test_for_develop.sh --cli --dry-run ... --setup-transmission-backend ...`
- `./test_for_develop.sh --cli --dry-run ... --no-setup-transmission-backend --bt-download-staging-dir ...`
- env-driven `./test_for_develop.sh --cli --dry-run ...`
- `PYTHONPYCACHEPREFIX=/tmp/hackme_web_eecf06b_compile_pyc python3 -m compileall -q scripts tests`
- `pytest -q --collect-only tests/scripts tests/storage/test_remote_downloads.py tests/storage/test_cloud_drive_attachments.py tests/frontend/video/test_frontend_videos.py`
- `pytest -q tests/scripts/deploy/test_deploy_script.py tests/scripts/deploy/test_predeploy_capacity_probe.py tests/scripts/testing/test_playwright_acceptance_pipeline.py tests/scripts/security/test_functional_smoke_script.py tests/scripts/security/test_pentest_script.py tests/scripts/testing/test_system_stress_probe.py tests/storage/test_remote_downloads.py tests/storage/test_cloud_drive_attachments.py tests/frontend/video/test_frontend_videos.py`

## Result

The cloned remote update is understood and the stale script test was updated. Targeted script/storage/video checks pass.
