# Dev Launcher Transmission Credential Summary QA

- Date: 2026-06-03 18:28 Asia/Taipei
- Branch: `04.BLOCKCHAIN_RC1`
- Scope: `test_for_develop.sh` startup usability for Transmission-backed BT/magnet testing.

## Findings

1. The launcher configured Transmission and wrote RPC credentials to the helper log, but the user-facing startup summary only printed the RPC URL and staging directory. Users had to inspect the log or know the credentials from prompts.
2. While verifying the fix with a custom `/tmp` run root, `--shutdown --port` skipped the copied `python3 server.py` process as `non-dev` because `is_dev_server_pid` only recognized default `/tmp/hackme_web_dev_*` copy paths.

## Fixes

- Print Transmission RPC URL, Web UI URL, username, and password in the setup completion output and final launcher summary.
- Print the same website account and Transmission summary before `--foreground` exec.
- Recognize custom `/tmp/*/hackme_web` run roots and their `runtime/server.pid` files during shutdown.

## Verification

- `bash -n test_for_develop.sh scripts/storage/setup_transmission_backend.sh`
- `pytest -q tests/scripts/deploy/test_deploy_script.py`
- Started an isolated Flask dev server on `55211` with `--bt-backend transmission --no-setup-transmission-backend`; startup output included:
  - `transmission_rpc`
  - `transmission_web`
  - `transmission_user`
  - `transmission_password`
- Ran `./test_for_develop.sh --cli --shutdown --port 55211 --run-root /tmp/hackme_web_transmission_summary_probe2`; shutdown recognized and stopped the copied dev pid.
