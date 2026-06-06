# test_for_develop.sh Command Catalog

This catalog keeps operational details out of `test_for_develop.sh --help`.
Use the script help for option discovery and this file for behavior, safety
boundaries, and recovery commands.


## Full Option Directory

This section mirrors the functional surface of `test_for_develop.sh`. Keep
long explanations here instead of expanding the shell script help text.

### Invocation Mode

| Option | Purpose |
| --- | --- |
| `--cli` | Non-interactive mode. The script never prompts and uses CLI/env values only. |
| `-h`, `--help` | Print short help and point to this catalog. |
| `--dry-run` | Print resolved configuration and exit before copying, installing, or starting. |

Without `--cli`, the script asks for workspace, host, port, server runner,
feature mode, security posture, server mode, dependency handling, foreground
mode, BTC_trade autostart, account passwords, and extra accounts.

### Host, Port, And Trust

| Option | Purpose |
| --- | --- |
| `--host HOST` | Bind host. Default: `0.0.0.0`. |
| `--port PORT` | Bind port. Default: `5000`. Interactive mode prompts if occupied. |
| `--trusted-hosts LIST` | Comma-separated host allowlist exported as `HTML_LEARNING_TRUSTED_HOSTS`. |
| `--public-host HOST`, `--public-ip HOST` | Add an external/NAT host/IP to trusted hosts and print it as a test URL. |
| `--allow-any-host`, `--disable-trusted-hosts` | Dev-only escape hatch disabling Flask trusted-host checks. Do not use for production. |
| `--enforce-trusted-hosts` | Re-enable trusted-host checks after a previous disabling option/env value. |
| `--port-conflict ACTION` | `ask`, `kill`, `fallback`, or `fail`. `kill` attempts to stop the occupying dev process and falls back if the port remains busy. |
| `--stop`, `--shutdown` | Stop a previous dev server process group/child tree for the selected port and exit. |

Generated restart shortcuts are written under the active runtime root by default
as `restart_develop_server.sh`. Set `HACKME_DEV_RESTART_SCRIPT_FILE` only when a
specific alternate shortcut path is needed.

### Feature Selection

| Option | Purpose |
| --- | --- |
| `--feature-mode MODE` | `all`, `defaults`, `bundles`, or `custom`. Default: `all`. |
| `--feature-bundles LIST`, `--feature-packages LIST`, `--feature-presets LIST` | Comma-separated package names such as `ops-minimum`, `safe-community`, `creator-media`, `exchange-ops`, `ai`. |
| `--features LIST`, `--enable-features LIST` | Comma-separated `feature_*` keys or package names for custom mode. Required/recommended dependencies are expanded. |

Use feature bundles for coarse product surfaces. Use explicit `--features` when
you need a narrow matrix.

### Dev Tokens And Accounts

| Option | Purpose |
| --- | --- |
| `--token-features LIST` | Restrict generated test/internal-test dev tokens to selected features. Empty or `0` means no extra token-level feature restriction. |
| `--internal-test-token-features LIST` | Alias of `--token-features`. |
| `--token-ttl-minutes N`, `--internal-test-token-ttl-minutes N` | TTL for generated test/internal-test tokens. Default: `1440`. |
| `--token-user USERNAME` | Account bound to generated test/internal-test tokens. Default: `test`. |
| `--token-password VALUE` | Password for the token user. Blank keeps existing users unchanged and auto-generates only when the user does not exist. |
| `--token-role ROLE` | Role for a newly created/updated token account: `user`, `manager`, `super_admin`. Default: `user`. |
| `--add-account SPEC` | Add a dev account as `username:password[:role]`; repeatable. |
| `--accounts LIST` | Comma-separated list of `--add-account` specs. |
| `--root-password VALUE` | Root password. Default: `root`. |
| `--manager-password VALUE` | Manager password. Default: `admin`. |
| `--test-password VALUE` | Test user password. Default: `test`. |

### Security And Runtime Mode

| Option | Purpose |
| --- | --- |
| `--security VALUE` | `on`/`off`. Default is dev-friendly `off`. |
| `--security-enabled`, `--enable-security` | Alias for `--security on`. |
| `--no-security`, `--disable-security` | Alias for `--security off`. |
| `--session-idle-timeout-minutes N` | Override frontend idle logout countdown. `0` disables. |
| `--idle-timeout-minutes N` | Alias of `--session-idle-timeout-minutes`. |
| `--logout-countdown-minutes N` | Alias of `--session-idle-timeout-minutes`. |
| `--server-mode MODE` | `dev_ready`, `internal_test`, `test`, `preprod`, `production`, `superweak`, `maintenance`, or `incident_lockdown`. |

For server-mode / production-gate validation, `HTML_LEARNING_GIT_REPO_DIR` must
point to a real git repo with readable `.git`. The `/tmp` copy is source-only
and intentionally excludes git metadata.

### Trading And Background Work

| Option | Purpose |
| --- | --- |
| `--btc-trade-autostart` | Start BTC_trade in the background after boot. |
| `--no-btc-trade-autostart` | Do not start BTC_trade in the background. |
| `--backtest-probe-on-startup` | Run first-boot trading backtest capacity probe in the temporary runtime. |
| `--trading-background-dev-ready`, `--enable-trading-background-dev-ready` | In `dev_ready`, allow mutating trading background jobs: price refresh, matching, bot scan, liquidation, interest accrual. |
| `--no-trading-background-dev-ready`, `--disable-trading-background-dev-ready` | Keep `dev_ready` trading background jobs disabled except sitewide metrics refresh. Default. |

### Server Runner And Gunicorn

| Option | Purpose |
| --- | --- |
| `--server-runner RUNNER` | `flask` or `gunicorn`. Default: `gunicorn`. |
| `--gunicorn-workers N` | Worker count. `auto` uses capacity report/probe when available. |
| `--gunicorn-threads N` | Threads per worker. `auto` uses capacity report/probe when available. |
| `--gunicorn-timeout N` | Worker timeout seconds. Default: `20`; dev-ready ComfyUI/HF startup uses a `900` second floor unless this option or `HACKME_DEV_GUNICORN_TIMEOUT` is set. |
| `--gunicorn-graceful-timeout N` | Graceful shutdown timeout seconds. |
| `--gunicorn-keep-alive N` | Keep-alive timeout seconds. |
| `--gunicorn-backlog N` | Listen backlog. Default: `64`. |
| `--gunicorn-max-requests N` | Worker recycle threshold. Default: `10000`; `0` disables recycling. |
| `--gunicorn-max-requests-jitter N` | Random jitter added to max-requests recycling. |

### Capacity And HLS Slot Probes

| Option | Purpose |
| --- | --- |
| `--capacity-probe`, `--retest-capacity`, `--refresh-capacity` | Run/refresh local capacity probe before launch. |
| `--capacity-probe-tier TIER`, `--capacity-tier TIER` | `auto`, `sbc`, `legacy`, `laptop`, `midrange`, or `highend`. |
| `--capacity-probe-light`, `--light-capacity-probe` | Alias for `--capacity-probe --capacity-probe-tier legacy`. |
| `--no-capacity-probe` | Skip probing when `auto` has no local result and use conservative fallback. |
| `--capacity-defaults-file PATH` | Defaults env file. Default: `.hackme_capacity_defaults.env`. |
| `--capacity-report-file PATH` | JSON capacity report read before env defaults. Default: `.hackme_capacity_report.json`. |
| `--hls-slot-probe`, `--hls-capacity-probe` | Run quick Premium HLS slot sizing probe before launch. |
| `--no-hls-slot-probe` | Skip HLS slot sizing probe. |

### BT / Remote Download / Transmission

| Option | Purpose |
| --- | --- |
| `--bt-backend BACKEND`, `--bt-download-backend BACKEND` | BT/magnet backend: `auto`, `transmission`, or `aria2`. |
| `--transmission-rpc-url URL` | Transmission RPC endpoint. |
| `--transmission-rpc-username USER` | Optional Transmission RPC username. |
| `--transmission-rpc-password PASS` | Optional Transmission RPC password. |
| `--setup-transmission-backend`, `--configure-transmission-backend` | Run `scripts/storage/setup_transmission_backend.sh` before app launch. Requires sudo/root. |
| `--no-setup-transmission-backend` | Do not configure daemon; use existing Transmission settings. |
| `--transmission-setup-script PATH` | Override setup helper path. |
| `--transmission-settings-file PATH` | Transmission `settings.json` passed to setup. Default: `/etc/transmission-daemon/settings.json`. |
| `--transmission-service NAME` | systemd service passed to setup. Default: `transmission-daemon`. |
| `--transmission-rpc-bind-address ADDR` | RPC bind address passed to setup helper. Helper default: `0.0.0.0`. |
| `--transmission-rpc-whitelist LIST` | RPC IP whitelist passed to setup helper. Helper default: `*.*.*.*`. |
| `--transmission-rpc-whitelist-enabled VALUE` | Enable whitelist: true/false. Helper default: `false`. |
| `--transmission-rpc-authentication-required VALUE` | Require RPC/Web login: true/false. |
| `--transmission-disable-rpc-auth`, `--disable-transmission-rpc-auth`, `--transmission-no-rpc-auth` | Dev-only: configure Transmission without login. Use only on isolated networks. |
| `--transmission-allow-any-rpc-ip`, `--allow-any-transmission-rpc-ip` | Configure daemon RPC on `0.0.0.0` and allow any source IP. Auth still applies unless disabled. |
| `--bt-download-staging-dir PATH`, `--transmission-staging-dir PATH` | Staging directory scanned/imported by hackme_web. |
| `--transmission-download-dir PATH` | Alias for `--bt-download-staging-dir`. |
| `--remote-download-global N`, `--remote-download-max-concurrent-global N` | Global remote download concurrency default. |
| `--remote-download-per-user N`, `--remote-download-max-concurrent-per-user N` | Per-user remote download concurrency default. |

### Cloud Drive And Upload Limits

| Option | Purpose |
| --- | --- |
| `--cloud-drive-root PATH` | Alias for `--cloud-drive-storage-root`. |
| `--cloud-drive-storage-root PATH` | Actual cloud-drive file storage root instead of `runtime/storage`. Must be absolute, non-public, non-project-root. |
| `--cloud-drive-max-mb MB` | Alias for global cloud-drive capacity limit. |
| `--cloud-drive-global-capacity-limit-mb MB` | Total cloud-drive occupancy cap in MB. `-1` keeps disk-backed 95% default. |
| `--cloud-drive-max-size SIZE`, `--cloud-drive-capacity SIZE`, `--cloud-drive-max-usage SIZE` | Same capacity cap with units, e.g. `1024M`, `10G`, `1.5TB`. Bare numbers mean MB. |
| `--max-content-mb MB`, `--html-learning-max-content-mb MB` | Override `HTML_LEARNING_MAX_CONTENT_MB`. |
| `--upload-request-max-mb MB` | Alias for `--max-content-mb`. |

### Runtime Layout And Source Copy

| Option | Purpose |
| --- | --- |
| `--run-root PATH` | Fixed `/tmp` run root instead of auto-generating one. |
| `--runtime-root PATH` | Runtime directory instead of layout default. |
| `--runtime-dir PATH`, `--runtime-directory PATH` | Alias for `--runtime-root`. |
| `--in-place`, `--no-copy` | Launch from current repo; runtime still uses run-root. |
| `--runtime-in-source` | Launch from current repo and write `runtime/` there. |
| `--source-runtime` | Alias for `--runtime-in-source`. |
| `--deploy-in-place` | Alias for `--runtime-in-source`; local deployment layout. |
| `--tmp-runtime` | With `--in-place`, keep runtime under `--run-root`. |
| `--copy` | Force default `/tmp` copied source workspace. |

### Dependencies And Process Mode

| Option | Purpose |
| --- | --- |
| `--skip-install` | Reuse runtime venv/current Python environment. |
| `--requirements-file PATH` | Install selected requirements file. Common choices: `requirements-minimal.txt`, `requirements-dev.txt`, `requirements-games.txt`, `requirements-comfyui.txt`, `requirements-hf.txt`, `requirements.txt`. |
| `--foreground` | Run in foreground instead of nohup/background mode. |

### AI Backend Dependencies

ComfyUI / GGUF and HF / Diffusers are configured as separate operator surfaces.
ComfyUI / GGUF selects the execution backend (`remote` API or `local` startup
script). HF / Diffusers keeps Hugging Face repo, token, cache, dtype, device,
and in-process runtime risk settings. Opening or saving HF fields should not
turn off the ComfyUI / GGUF remote/local backend.

Dependency selection follows that split:

```text
requirements-comfyui.txt  # external/local ComfyUI integration layer
requirements-hf.txt       # heavy local Hugging Face / Diffusers runtime
```

If the HF test-connection button reports missing Diffusers Python packages,
rerun the launcher and select the HF dependency bundle:

```bash
./test_for_develop.sh --requirements-file requirements-hf.txt
```

For standalone HF/GGUF probes outside the app runtime, install the probe helper
requirements instead:

```bash
python3 -m pip install -r scripts/comfyui/generation_probe_requirements.txt
```

### Maintenance Commands

| Option | Purpose |
| --- | --- |
| `--backup [PATH]`, `--backup-file PATH`, `--backup-archive PATH` | Create a runtime-state backup archive and exit. |
| `--restore PATH` | Restore runtime state from a `--backup` archive and exit. |
| `--reset` | Reset runtime state, preserve server-side key material, and create an orphan recovery bundle before clearing DB/catalog state. |
| `--delete` | Delete the selected runtime root, preserving internal storage by moving it aside. |

## Common Start / Stop

Start a local HTTPS dev server on an available port:

```bash
./test_for_develop.sh --port 50785
```

Start the current repo in-place on port 5000:

```bash
./test_for_develop.sh --cli --in-place --host 0.0.0.0 --port 5000 --port-conflict kill
```

Stop a server started by the launcher:

```bash
./test_for_develop.sh --shutdown --port 5000
```

## Runtime Selection

Use a specific runtime root:

```bash
./test_for_develop.sh --runtime-root /home/s92137/USB
```

Use a cloud-drive storage root outside the runtime root:

```bash
./test_for_develop.sh --runtime-root /home/s92137/USB --cloud-drive-storage-root /mnt/storage/hackme
```

The cloud-drive storage root is where user files live. It is intentionally
treated differently from transient runtime state.

## Backup

Create a runtime-state backup archive and exit:

```bash
./test_for_develop.sh --backup /tmp/hackme_runtime_backup.tar.gz --runtime-root /home/s92137/USB
```

If the backup path is a directory, the script writes a timestamped archive
inside that directory. The archive path must be outside the runtime root.

`--backup` excludes:

```text
storage/
venv/
pycache/
logs/
server.pid
*.sock
*.lock
tmp/
temp/
```

It is a runtime-state backup, not a complete cloud-drive file backup.

## Restore

Restore a backup archive and exit:

```bash
./test_for_develop.sh --restore /tmp/hackme_runtime_backup.tar.gz --runtime-root /home/s92137/USB
```

`--restore` refuses unsafe archive paths, requires the active runtime to be
stopped, and moves any existing runtime to:

```text
<RUNTIME_ROOT>.pre-restore-<timestamp>
```

The archive format does not restore `storage/`.

## Reset

Reset selected runtime state and exit:

```bash
./test_for_develop.sh --reset --runtime-root /home/s92137/USB
```

`--reset` refuses to run while that runtime is active. Stop the server first.

`--reset` clears:

```text
database/
chats/
anchors/
reports/
logs/
pycache/
tmp/
temp/
dev_tokens.json
server.pid
*.sock
*.lock
```

`--reset` preserves server-side key material in the runtime root, including:

```text
.filekey
.fkey
.csrfkey
.integrity_key
.chain_seed
.server_mode_log_hmac_key
cert.pem
key.pem
integrity_manifest.json
```

### Reset Storage Behavior

Before clearing DB/catalog state, `--reset` creates a recovery bundle under the
storage root:

```text
storage/.reset_orphan_recovery/reset_<timestamp>/
```

The bundle contains:

```text
database/                                # pre-reset DB/catalog metadata
runtime_secrets/                         # copied key/secret material
scripts/admin/decrypt_server_files.py    # decrypt/export helper copy
orphaned_storage/                        # pre-reset storage contents moved here
README_SERVER_ENCRYPTED_RECOVERY.txt
export_server_encrypted_plaintext.sh
restore_database_catalog_from_bundle.sh
recovery_action.lock                     # created when one recovery path starts
runtime_root.txt
storage_root.txt
```

The existing pre-reset storage contents are moved into `orphaned_storage/`, so
the post-reset storage root starts clean except for `.reset_orphan_recovery/`.
This prevents old ciphertext files from polluting the fresh runtime.

### Export Server-Encrypted Orphans

For `server_encrypted` files, `.filekey` plus ciphertext is the cryptographic
minimum for decryption. For a complete export with filenames and file selection,
use the pre-reset DB/catalog metadata in the recovery bundle.

Use the bundle wrapper so the recovery action is locked as soon as the action starts:

```bash
/home/s92137/USB/storage/.reset_orphan_recovery/reset_<timestamp>/export_server_encrypted_plaintext.sh \
  /tmp/hackme_server_encrypted_plaintext_export
```

When plaintext export starts, `recovery_action.lock` prevents DB/catalog restore from the same bundle. The encrypted files, DB metadata, and bundled scripts remain in the recovery bundle.

Strict E2EE files cannot be decrypted with `.filekey`; they require the user's
E2EE passphrase/key material. After choosing plaintext export, keep the bundle and run the included script when the user passphrase is available:

```bash
PYTHONPATH=/home/s92137/hackme_web \
  /home/s92137/USB/venv/bin/python3 \
  /home/s92137/USB/storage/.reset_orphan_recovery/reset_<timestamp>/scripts/admin/decrypt_server_files.py \
  --db /home/s92137/USB/storage/.reset_orphan_recovery/reset_<timestamp>/database/database.db \
  --storage-root /home/s92137/USB/storage/.reset_orphan_recovery/reset_<timestamp>/orphaned_storage \
  --privacy-mode e2ee \
  --prompt-e2ee-passphrase \
  --output-dir /tmp/hackme_e2ee_plaintext_export \
  --confirm-plaintext-output
```

### Import Catalog And Files Back

To undo the orphaning and make the old storage files visible to the app again,
stop the server and run the bundle helper:

```bash
/home/s92137/USB/storage/.reset_orphan_recovery/reset_<timestamp>/restore_database_catalog_from_bundle.sh
```

The helper:

- refuses if `recovery_action.lock` already records plaintext export
- refuses to run if a server process appears active
- stages the bundled `database/` first, before modifying the current runtime DB
- backs up the current runtime `database/` to `database.before-orphan-catalog-restore-<timestamp>`
- copies/stages the bundle DB instead of moving or deleting the bundle copy
- preserves original file owners where possible; file catalog rows whose owner user no longer exists are reassigned to root
- writes `recovery_action.lock` before moving storage or replacing DB
- moves current post-reset storage contents into `post_reset_storage_backup_<timestamp>` inside the bundle
- moves `orphaned_storage/` contents back into the storage root
- restores pre-reset `database/` metadata only after storage movement and DB staging succeed

When catalog restore starts, `recovery_action.lock` prevents plaintext export from the same bundle. This is intentional: root must not both obtain plaintext export and then restore the same encrypted catalog back into service from one reset bundle.

If a catalog row references a user that no longer exists in the pre-reset database, the restore helper reassigns that file/catalog owner to root instead of dropping the file. Existing owners are preserved when they still exist.

Verify the app and cloud-drive catalog before deleting the backup folders.

## Delete

Delete the selected runtime root and exit:

```bash
./test_for_develop.sh --delete --runtime-root /home/s92137/USB
```

`--delete` refuses to run while that runtime is active. If storage is inside the
runtime root, it is moved to a sibling path first:

```text
<RUNTIME_ROOT>.storage-preserved-<timestamp>
```

External cloud-drive storage roots are never deleted.

## Transmission / BT Downloads

Use an existing Transmission daemon:

```bash
./test_for_develop.sh --bt-backend transmission \
  --transmission-rpc-url http://127.0.0.1:9091/transmission/rpc \
  --transmission-rpc-username hackme_web \
  --transmission-rpc-password '<password>' \
  --no-setup-transmission-backend
```

Run setup only when you intend to configure the daemon and can provide sudo/root
permission:

```bash
./test_for_develop.sh --setup-transmission-backend
```

## Dry Run

Print resolved configuration without starting or modifying runtime state:

```bash
./test_for_develop.sh --cli --dry-run --runtime-root /home/s92137/USB
```
