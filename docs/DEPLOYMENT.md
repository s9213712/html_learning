# Deployment And Operations Scripts

This file is the script-level reference. New deployers should start with
[01_DEPLOY_QUICKSTART.md](01_DEPLOY_QUICKSTART.md) and
[02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md), then come back here when
they need exact command modes and script flags. For the canonical dependency
matrix, see [SYSTEM_DEPENDENCIES.md](SYSTEM_DEPENDENCIES.md).

## Direct Deployment

正式部署不要把 Flask development server 直接暴露給使用者。單機正式部署請優先使用
`deploy/nginx/` 與 `deploy/systemd/` 內的 Nginx + systemd + bounded Gunicorn 範本。

手動直接啟動 `server.py` 只適合本機檢查或緊急維修：

```bash
python3 server.py --doctor
python3 server.py
```

`--doctor` 會先檢查 runtime 目錄是否存在且可寫；若環境缺失，會直接報錯，不會靜默幫你補建。
如果你只是要本機開發，不要走這條，改用 repo root `./test_for_develop.sh`。

## Nginx / systemd Templates

可配置範本：

- `deploy/nginx/hackme_web.conf.example`
- `deploy/systemd/hackme-web.service.example`
- `deploy/systemd/hackme-web.env.example`
- `deploy/systemd/hackme-web.tmpfiles.example`

完整套用順序與「不要這樣做」清單放在 [../deploy/README.md](../deploy/README.md)。

部署重點：

- Nginx 對外，Gunicorn 只綁 `127.0.0.1:8000`。
- `/etc/hackme_web/hackme-web.env` 保存 production secrets，不進 git。
- mutable runtime 放 `/var/lib/hackme_web/runtime` 與 `/var/log/hackme_web`。
- web service 只跑 HTTP request；重型背景工作需獨立 worker service。
- `HTML_LEARNING_TRUSTED_HOSTS` 必須列出正式 domain；Nginx 需轉送原始
  `Host`，讓 app 能拒絕不受信任 Host。
- 維護旁路 token 只用 `X-Maintenance-Bypass-Token` header；query string
  token 不被接受。
- multipart guard 預設為 `HTML_LEARNING_MAX_FORM_MEMORY_KB=512` 與
  `HTML_LEARNING_MAX_FORM_PARTS=1000`，用來限制小表單記憶體與 parts 數量。

### Environment variables

`deploy/systemd/hackme-web.env.example` 是正式部署 env 的集中範本；部署者應把它安裝成
`/etc/hackme_web/hackme-web.env` 並替換 secrets。常用分類如下。

| 分類 | 環境變數 | 說明 |
| --- | --- | --- |
| TLS / proxy | `FORCE_HTTPS`, `SESSION_COOKIE_SECURE`, `USE_XFF`, `TRUSTED_PROXY_IPS`, `GUNICORN_FORWARDED_ALLOW_IPS` | Nginx TLS termination 與 proxy header 設定。 |
| Host guard | `HTML_LEARNING_TRUSTED_HOSTS` | 正式 domain 白名單；公開部署不可留空。 |
| Multipart guard | `HTML_LEARNING_MAX_FORM_MEMORY_KB`, `HTML_LEARNING_MAX_FORM_PARTS` | 小表單記憶體與 parts 數量限制。 |
| Secrets | `SESSION_SECRET`, `CSRF_SECRET_KEY`, `ROOT_INTEGRITY_SIGNING_KEY`, `TURNSTILE_SECRET_KEY` | 必須替換；不要提交。 |
| Runtime dirs | `HACKME_RUNTIME_DIR`, `HTML_LEARNING_DB_DIR`, `HTML_LEARNING_LOG_DIR`, `HTML_LEARNING_STORAGE_DIR`, `HTML_LEARNING_REPORTS_DIR` 等 | mutable runtime / DB / logs / storage 位置。 |
| Bootstrap | `HTML_LEARNING_ROOT_PASSWORD`, `HTML_LEARNING_MANAGER_PASSWORD` | first boot 用；root 完成 bootstrap 後應 rotate/remove。 |
| Gunicorn fallback bind | `HTML_LEARNING_HOST`, `HTML_LEARNING_PORT` | Flask fallback 與服務內部 bind 口徑；production Gunicorn 仍由 systemd service `ExecStart` 決定。 |
| Backpressure | `HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY`, `HTML_LEARNING_BACKPRESSURE_NORMAL_LIMIT`, `HTML_LEARNING_BACKPRESSURE_HEAVY_LIMIT`, `HTML_LEARNING_BACKPRESSURE_ROOT_LIMIT`, `HTML_LEARNING_BACKPRESSURE_FAST_LANE_RESERVED`, `HTML_LEARNING_BACKPRESSURE_ROOT_PRIORITY_ENABLED` | app-level server busy / fast lane 初始值。 |
| Remote download capacity | `HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL`, `HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER` | Direct link / BT / magnet 外部下載 worker 的全站與單用戶併發預設；老硬體建議 `1/1`。 |
| BT backend | `HACKME_BT_BACKEND` | `auto`, `transmission`, or `aria2`；fresh DB 預設值，root 前台保存後以 root 設定為準。 |
| Transmission RPC | `HACKME_TRANSMISSION_RPC_URL`, `HACKME_TRANSMISSION_RPC_USERNAME`, `HACKME_TRANSMISSION_RPC_PASSWORD` | BT/magnet 使用 Transmission RPC 時的連線設定；詳見 [13_REMOTE_DOWNLOAD_TRANSMISSION.md](13_REMOTE_DOWNLOAD_TRANSMISSION.md)。 |
| Startup | `HTML_LEARNING_BOOTSTRAP_POINTS_CHAIN` | 可選 first boot 行為。 |

Remote download / BT backend env 是 fresh DB / first boot 預設。已存在站點若 root 前台已保存
`BT/magnet 下載後端` 或 Transmission RPC 設定，runtime 會以 root system settings 為準；需要跨部署
預先套用時，請同時更新 env 範本與 root 前台預設策略。

Generated runtime files remain local and must not be committed:

- `.env`
- `runtime/.fkey`
- `runtime/.filekey`
- `runtime/.csrfkey`
- `runtime/.chain_seed`
- `runtime/.integrity_key`
- `runtime/integrity_manifest.json`
- `runtime/cert.pem`
- `runtime/key.pem`
- `runtime/database/`, `runtime/logs/`, `runtime/storage/`, `runtime/chats/`,
  `runtime/anchors/`, and `runtime/reports/`

## Capability Checks

`python3 server.py --doctor` 主要檢查的是 runtime 目錄是否完整可用；影音與第三方能力則仍需部署者自行確認：

- `ffmpeg` / `ffprobe` 是否存在
  - 影響影音平台的 HLS 衍生檔與 metadata probe
- `CIVITAI_API_KEY` 是否存在
  - 影響 root-only Civitai 搜尋 / 下載
- `scripts/admin/root_recovery.py`
  - root 忘記密碼時的正式 offline 補救入口；必須明確設定
    `HACKME_RUNTIME_DIR`、`--runtime-dir` 或 `--db-path`

這些能力提示屬於**可選擴充檢查**，不會阻擋一般部署。
更完整的 system binary / external service 清單請看
[SYSTEM_DEPENDENCIES.md](SYSTEM_DEPENDENCIES.md)。

## Pre-Deploy Capacity Probe

部署前可先在同一台機器上探勘 Gunicorn workers / threads 的安全範圍：

```bash
python3 scripts/testing/predeploy_capacity_probe.py
```

腳本會透過 `test_for_develop.sh` 啟動隔離的 `/tmp` 測試站台，建立臨時帳號並跑聊天、
討論區、雲端硬碟、相簿、遊戲、PointsChain、治理、申覆、疑義交易、交易所三大交易方式、
三大交易機器人、借貸清算、掛單撮合與交易背景任務的混合前後端流量。測試結束預設會 kill
測試 Gunicorn 並刪除臨時 runtime、DB、uploads 與 fixture；JSON 報告保留在 `/tmp`，不污染
後續部署。

負載型態用 `--load-profile` 選：

- `normal`：預設值，模擬一般會員與 root 維運背景工作；只有這個 profile 的成功結果會同步
  `.hackme_capacity_defaults.env`。
- `malicious`：在 normal 上增加 SQL/XSS/錯誤 CSRF/越權讀取/錯誤治理與交易 payload 等攻擊與
  例外請求，用來測防禦與錯誤處理，不會同步部署預設值。
- `heavy`：在 normal 上增加重複預覽/下載、分段上傳、線上文字更新、交易回測、snapshot 與
  PointsChain backup-disabled/recovery-boundary 探針，用來測 I/O、CPU、背景任務與帳本不可覆寫規則，
  不會同步部署預設值。
- `full`：同時啟用 malicious 與 heavy。也可用 `--load-kinds normal,malicious,heavy` 自訂組合。

成功完成探勘時，腳本會同步更新 repo root 的 `.hackme_capacity_defaults.env`。之後
`test_for_develop.sh` 的 Gunicorn `auto` 會優先使用這份本機實測結果；檔案不存在時才會
自動跑一次探勘。若要重新測試，用 `./test_for_develop.sh --capacity-probe` 或直接重跑
`predeploy_capacity_probe.py`；若只想產生報告、不更新本機預設值，加 `--no-sync-defaults`。

容量探勘預設使用 HTTP keep-alive，較接近瀏覽器和正式反向代理後方的行為；不要用
`Connection: close` 的結果判定機器極限。需要特別測試短連線/相容性時再加
`--close-connections`。短測試也會把 Gunicorn `max-requests` 預設設為 `10000`，避免 worker
回收剛好發生在探勘期間，造成 `RemoteDisconnected` 這類假性容量錯誤。

探勘伺服器是獨立 `/tmp` 環境，預設會關閉登入/IP/使用者/上傳等濫用與流量限制，避免
防刷規則先於機器極限觸發；若要測正式防濫用策略本身，加 `--keep-app-limits`。伺服器
backpressure 預設保留，因為它是防止程序被打爆的保護線；若要測裸跑崩潰行為，加
`--disable-backpressure`。

常用範例：

```bash
python3 scripts/testing/predeploy_capacity_probe.py \
  --profiles 1x6,2x6,3x6,4x6 \
  --target-p95-ms 1500
```

`--account-counts` 預設為 `auto`；腳本會從 `--start-accounts` 開始按 `--growth-factor`
往上探，體驗開始劣化後改用 `--fine-growth-factor` 細找伺服器穩定線。為避免使用者誤跑
過久，預設有 `--max-rounds 8` 與 `--max-accounts 256` 的安全煞車；若要在高階機器上找
硬崩線，明確加 `--max-rounds 0 --max-accounts 0 --continue-after-failure`。

長時間探勘會持續輸出進度：目前帳號、每帳號操作清單、各帳號卡在哪個功能、完成比例、
CPU/RAM、以及即時最慢功能延遲。若輸出太多，可用 `--progress-active-limit N` 限制每次
顯示的活躍帳號數，或用 `--no-progress` 關閉。

報告裡的 `recommendation.suggested_test_for_develop_args` 和 `suggested_env` 可作為本機或
staging 的初始設定。若報告出現 `multi_worker_cpu_observed: true`，代表測試期間至少兩個
Gunicorn worker 同時有 CPU 活動；`active_worker_peak` 越接近 worker 數，越能證明該設定
真的有利用多核心。

容量報告也會輸出 `limits`，同時標示兩種門檻：

- `limits.experience.degradation_starts_at`：使用者體驗開始不佳，預設以 `p95 >= 2000ms`
  或 `p99 >= 4000ms` 判定，可用 `--ux-p95-ms`、`--ux-p99-ms` 調整。
- `limits.server_instability.first_observed_at`：伺服器或應用穩定性失效，例如連線錯誤、
  `503 server_busy`、非預期 `5xx`，或延遲超過 `--hard-p95-ms` / `--hard-max-ms`。`429`
  會另外列在 `limits.application_limit`，代表防濫用/限流先被觸發，不等同伺服器崩潰。

## Functional Smoke

Run:

```bash
scripts/security/pentest/run_functional_smoke.sh
```

It starts an isolated temporary server, tests core features, verifies
snapshot/restore/reset behavior, checks TLS file generation, and writes reports
under `runtime/reports/security`.

## Functional Permission Pentest

Run:

```bash
scripts/security/pentest/run_pentest.sh --target http://127.0.0.1:5000 --only functional-permissions
```

It logs in as root, manager, normal user, and anonymous clients to verify
allowed actions, blocked actions, high-risk confirmation/CSRF guards, JSON error
format, and no 500/502/503 regressions.

## Stress Test

Run a lightweight local traffic estimate:

```bash
scripts/security/pentest/stress_test.py --target http://127.0.0.1:5000 --requests 500 --concurrency 50
```

Run a short duration-based flood simulation against a loopback server you own:

```bash
python3 scripts/security/pentest/stress_test.py \
  --target http://127.0.0.1:5000 \
  --mode duration \
  --duration-seconds 20 \
  --max-requests 4000 \
  --concurrency 80 \
  --burst-size 10 \
  --burst-interval-ms 200
```

The script reports approximate requests per second, status distribution, and
latency percentiles. It is not a replacement for production-grade load testing,
but it gives a repeatable baseline for the current host. It refuses public
targets by default; use `--i-own-this-target` only for staging or systems you are
explicitly authorized to test. Concurrency is capped at `100`, and duration mode
still honors a safety `--max-requests` cap.

## Pre-push Validation

Run the project-level validation before publishing:

```bash
python3 scripts/prepush/pre_push_checks.py
```

The helper compiles Python under `server.py`, `routes/`, `services/`,
`security/`, `scripts/`, and `tests/`, checks that the Release ID appears in the
required docs, rejects tracked runtime artifacts and local workstation paths,
runs config/CI safety checks, runs `git diff --check`, runs the plaintext secret
scanner, runs `gitleaks` and `node --check` when those tools are installed, and
runs a focused pytest set. The default mode is fast and does not start the
server. Use `--full` when you need the isolated `/tmp` server smoke, API
behavior, snapshot/restore, Server Mode, PointsChain, and log-chain checks.

`--ci` is a non-interactive/sanitized execution mode; it does not automatically
enable heavyweight checks. Optional cleanup flags list their deletion plan first
and require confirmation unless `--yes` is used:

```bash
python3 scripts/prepush/pre_push_checks.py --clean --clean-temp --yes
```

- `--clean`: remove safe repository caches such as `__pycache__`,
  `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.coverage`, `htmlcov`,
  `dist`, `build`, `*.pyc`, and `*.pyo`. It never removes user/runtime data,
  reports, key files, `.gitkeep`, or tracked files unless they are explicit
  cache artifacts.
- `--clean-temp`: remove old `/tmp/html_learning_prepush_*` and
  `/tmp/html_learning_secrets_*` directories, keeping the newest two by
  default.
- `--keep-temp`: keep this run's isolated runtime even in `--ci` success.
- `--yes`: skip cleanup confirmation.

Install the hook with:

```bash
bash hooks/install-hooks.sh
```

The hook bumps `APP_RELEASE_ID`, amends the current commit, runs
`scripts/prepush/pre_push_checks.py --ci`, and blocks the push on failure.
