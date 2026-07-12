# Functional Smoke Script Usage

For the higher-level validation map, read [../11_QA_TESTING.md](../11_QA_TESTING.md)
first. This file documents the exact `run_functional_smoke.sh` behavior and options.
For the governance rules, validation naming, and phase input/output
catalog, read [QA_ARCHITECTURE.md](QA_ARCHITECTURE.md).

`scripts/security/pentest/run_functional_smoke.sh` 是本專案的功能回歸測試腳本。它和
`scripts/security/pentest/run_pentest.sh` 不同：pentest 腳本偏向外部安全掃描，functional
smoke 腳本會啟動一個隔離 runtime server，登入 root，實際操作主要功能，最後產生
Markdown 報告與原始回應紀錄。

It is broad functional smoke, not full production validation. 任何只針對最近
修補或單一風險的 pytest / script run 都只能稱為 focused regression 或 selected
tests，不能在文件、報告或 PR 文字中稱為 full validation。

這個腳本預設只打本機臨時 server，會把 database、log、chat、anchor、storage、
reports 全部導向 `/tmp` 底下的隔離資料夾，避免污染 repo 內的 runtime 資料。

## Quick Start

從 repo 根目錄執行：

```bash
scripts/security/pentest/run_functional_smoke.sh --qa-full
```

`--qa-full` 是預設行為，保留在命令中是為了讓意圖清楚：這條路線跑 broad
product QA，例如 community/chat/storage/video/ComfyUI/reports/moderation
工作流。上線前 production gate 只應使用 `--core-only`，聚焦 auth、admin、
security-center、用戶、PointsChain、trading、越權與必要 runtime 安全邊界。

指定 port：

```bash
scripts/security/pentest/run_functional_smoke.sh --port 50740
```

指定 runtime 目錄與報告目錄：

```bash
scripts/security/pentest/run_functional_smoke.sh \
  --runtime /tmp/hackme_web_functional_manual \
  --out /tmp/hackme_web_test_artifacts/reports/security
```

保留 runtime 目錄供人工檢查：

```bash
scripts/security/pentest/run_functional_smoke.sh --keep-runtime
```

`--runtime` 只接受 `/tmp/hackme_web_*` 下尚不存在的新目錄，避免覆寫或刪除
既有資料。`--keep-runtime` 會保留本輪隔離 runtime 的測試狀態供除錯；確認完後由
操作者自行移除。

## Options

| Option | Purpose |
|---|---|
| `--port N` | 臨時 server port。預設 `50734`。 |
| `--runtime DIR` | 隔離 runtime 根目錄。預設 `/tmp/hackme_web_functional_<RUN_ID>`。 |
| `--out DIR` | 報告根目錄。預設 `/tmp/hackme_web_test_artifacts/reports/security`，且不可位於 source checkout 或 disposable runtime 內。 |
| `--keep-runtime` | 測試後保留本輪隔離 runtime 供除錯。 |
| `--qa-full` | 跑完整 QA functional smoke；包含 community/chat/storage/video/ComfyUI/reports/moderation 等產品回歸。這是預設模式。 |
| `--core-only` | 只跑上線前必要核心功能與安全邊界；跳過 QA 類產品工作流。production gate 使用這個模式。 |
| `-h`, `--help` | 顯示腳本內建說明。 |

## Environment Overrides

| Variable | Default | Purpose |
|---|---:|---|
| `HOST` | `127.0.0.1` | 臨時 server bind host。 |
| `PORT` | `50734` | 臨時 server port。 |
| `SMOKE_SCHEME` | `https` | 測試連線協定；預設配合 server 自動產生的本地 TLS 憑證並使用 `curl -k`。 |
| `REPORT_ROOT` | `/tmp/hackme_web_test_artifacts/reports/security` | 報告根目錄。 |
| `HACKME_TEST_OUTPUT_ROOT` | `/tmp/hackme_web_test_artifacts` | 所有隔離 QA 產物的共同外部根目錄。 |
| `RUNTIME_ROOT` | `/tmp/hackme_web_functional_<RUN_ID>` | 隔離 runtime 根目錄。 |
| `ROOT_PASSWORD` | 每次隨機生成 | 測試 root 初始密碼；可覆寫以重現問題。 |
| `ROOT_CHANGED_PASSWORD` | 每次隨機生成 | 若 root 被要求改預設密碼，使用另一組隨機值；可覆寫以重現問題。 |
| `MANAGER_PASSWORD` | 每次隨機生成 | bootstrap manager 密碼；可覆寫以重現問題。 |
| `TEST_PASSWORD` | 每次隨機生成 | bootstrap test user 密碼；可覆寫以重現問題。 |
| `START_TIMEOUT` | `45` | 等待 server ready 的秒數。 |
| `RESET_OFFLINE_TIMEOUT` | `20` | reset server 後等待服務短暫離線的秒數；若 process 很快完成重啟且 `started_at` 已變更，也視為通過。 |
| `RESET_RECONNECT_TIMEOUT` | `180` | reset server 後等待服務重新連線且 `started_at` 變更的秒數。 |
| `GO_LIVE_CORE_ONLY` | `0` | 設為 `1` 等同 `--core-only`；保留給 production-gate wrapper 相容使用。 |

## Runtime Isolation

腳本會使用環境變數把 server runtime 路徑全部改到隔離目錄：

| Runtime Data | Redirected Env |
|---|---|
| runtime root / certs / local TLS / other runtime-relative files | `HACKME_RUNTIME_DIR` |
| SQLite database | `HTML_LEARNING_DB_DIR` |
| server log | `HTML_LEARNING_LOG_DIR` |
| chat sidecar | `HTML_LEARNING_CHAT_DIR` |
| audit anchors | `HTML_LEARNING_ANCHOR_DIR` |
| cloud drive/storage files | `HTML_LEARNING_STORAGE_DIR` |
| runtime reports | `HTML_LEARNING_REPORTS_DIR` |

這代表測試不應修改 tracked 的 `bootstrap.schema.sql`，也不應把 runtime
狀態寫回 repo 根目錄；logs、chats、anchors、storage 與 reports 等執行期資料
只能存在於本輪新建的隔離 runtime 或 checkout 外部報告目錄。腳本會明確設定
`HACKME_RUNTIME_DIR`，並拒絕 source checkout runtime。

另外，若預設 port `50734` 已被其他本機 server 佔用，腳本會改挑一個空閒 port，
並在報告中明確記錄 `server startup port selection`；若你是用 `--port` 或 `PORT`
明確指定 port，則遇到衝突會直接 fail，避免誤打到舊 server 而產生假陽性結果。
若執行環境本身禁止 socket probe，腳本也會直接 fail，避免把空白 port 當成成功，
這時請改用明確 `--port`，或在允許 local bind 的環境下執行。

## Functional Coverage

目前腳本會覆蓋：

`--core-only` 只跑上線前核心區：public/auth/admin/security-center、用戶建立、
account sessions、PointsChain、trading、maintenance bypass / tester token 與必要
runtime hardening guidance。QA 類產品工作流保留在
`--qa-full`：community/chat/DM、storage 檔案整理、ComfyUI、video/E2EE share、
bug report、moderation、appeals、reputation，以及 snapshot restore/reset 的
廣泛殘留資料檢查。

| Area | Checked Behaviors |
|---|---|
| runtime safety | 啟動前 filesystem snapshot、隔離 runtime、結束清理或還原 runtime。 |
| public API | index、site config、version、password strength、captcha challenge、offline root recovery CLI availability。 |
| local TLS | `runtime/cert.pem` / `runtime/key.pem` 首次啟動自動生成，reset 後重啟也會重新生成。 |
| auth | CSRF token、root login、預設密碼強制修改、session identity。 |
| admin | health、readiness、anomaly、DB integrity、audit chain、environment、settings、feature flags、access controls、member rules、platform stats、audit log。 |
| security center | summary、server log、security controls、threshold update、自定義 profile、server mode switch。 |
| snapshot/restore/reset | 建立 snapshot、restore 後只保留 baseline 發文、reset 後 baseline 發文也消失，並驗證 server 真的重啟或 `started_at` 已更新。 |
| accounts | 建立 smoke user、列出 users、account sessions。 |
| community/forum | announcement create/edit、category、board、board approval、thread、reply、lock、sticky、curate。 |
| chat/DM | chat room、chat message、DM thread、DM message。 |
| storage/cloud drive | quota/list、root storage capacity audit、root storage user list、storage upgrade catalog、cloud-drive upload、status、preview、download、delete。 |
| video platform | upload/publish、shared page load、anonymous shared playback、revoke flow、missing file / non-media upload error guidance。 |
| PointsChain | wallet、catalog/rules、private-chain permission model 下的 disabled admin adjustment / wallet / ledger、seal/verify/one-click anomaly handler/economy stats async job payloads、disabled ledger backup endpoints、recovery status。 |
| trading extras | 先驗證 custom profile 下交易寫入會 fail closed，再切到 `test` 模式驗證 read-only diagnostics，之後旋轉 maintenance bypass token、切到 `internal_test` 驗證 tester token 寫路徑在 live warm-up 後可回傳有效 order payload；同時覆蓋 `live-price` metadata / transport state、fee-aware `grid/preview`、root `price-fusion-status` transport state、root `bot-audit` dashboard / manual run。 |
| ComfyUI integration | model/status wiring、optional backend availability、workflow preset list/import guards、template preview text panel `text:embeddings` / `embedding_shortcuts` child、share/discard error paths、root Civitai search API key guard。 |
| reports/moderation | bug reports、reports、notifications、appeals、moderation actions/proposals、violations、message reports、mod notes、reputation endpoints。 |
| hardening | unknown path `OPTIONS` 不應宣告 PUT/DELETE/PATCH 等危險方法；remote downloader rejections expose a user-facing message；known regressions: copy-share fallback and shared page loading timeout guard；custom profile trading block plus test-mode diagnostics and internal_test routed order payload after live warm-up；browser-only mode 需帶 maintenance bypass token 才能讓 operator script 繼續驗證。 |

Video platform: upload/publish, shared page load, password unlock, E2EE bootstrap, anonymous shared playback, revoke flow.
strict E2EE share flow now verifies password gate, wrong-password guidance,
post-unlock playback bootstrap, browser-side key payload, and revoke fail-closed behavior.

`internal_test` 的 tester token smoke 會用「本地時間、無時區」的 ISO 8601 到期字串，
因為 root UI 送的是 `datetime-local`；若改成 naive UTC，token 可能會在 UTC+offset
環境下看起來建立成功、實際登入時卻被當成已過期。

## Snapshot / Restore / Reset Verification

腳本的 snapshot 測試流程是刻意設計成可檢查狀態是否真的回復：

1. root 登入成功後先建立 baseline forum post。
2. 建立 app snapshot checkpoint。
3. 執行後續功能測試，產生 residual category/thread 等測試資料。
4. Restore 到 checkpoint。
5. 驗證 baseline post 還存在。
6. 驗證 residual 測試資料已消失。
7. 執行 reset server。
8. 等待 server 在合理時間內短暫離線，預設 20 秒內觀察連線失敗；若重啟太快沒有可觀察離線窗口，但 `/api/version.started_at` 已變更，也視為 reset restart 成功。
9. 若已觀察到離線，等待 server 在 3 分鐘內重新連線，並確認 `/api/version.started_at` 已變更。
10. 重新登入 root。
11. 驗證 baseline post 也消失。

如果 restore 後看到一堆測試殘留文章，代表 restore 失敗或測試腳本找到真正的功能
bug。若 reset 後沒有短暫離線且 `started_at` 也沒有變更，代表可能沒有真的重啟；若 3 分鐘內沒有重新連線，
代表自動重啟失敗；若 baseline post 仍存在，代表 reset server 的 runtime 清理不完整。reset 後 production-like 預設只開管理功能，社群等頁面可能回 `503 feature disabled`，此時只要資料已清除即視為正確。

## Report Output

每次執行會建立：

```text
/tmp/hackme_web_test_artifacts/reports/security/functional_<RUN_ID>/
├── 00_FUNCTIONAL_SMOKE.md
├── results.tsv
├── server.out
├── pre_start_runtime_snapshot.tar.gz
├── upload.txt
└── raw/
```

重要檔案：

| File | Purpose |
|---|---|
| `00_FUNCTIONAL_SMOKE.md` | 人類可讀的總結報告，包含通過、失敗、跳過與覆蓋範圍。 |
| `results.tsv` | 每個檢查項目的機器可讀結果。 |
| `server.out` | 臨時 server stdout/stderr。 |
| `pre_start_runtime_snapshot.tar.gz` | 新建隔離 runtime 啟動前的證據快照；不會用來覆寫呼叫者既有目錄。 |
| `raw/` | 每個 API request 的原始 JSON/body 回應。 |

`/tmp/hackme_web_test_artifacts/reports/security/` 預設不追蹤；只有需要提交的修復報告才應另行
整理成可追蹤文件。

cookie jar 只會建立在 disposable runtime，不會放入報告目錄；腳本結束時會刪除它，
並遞迴遮罩 raw JSON 內的 password、session/CSRF、maintenance bypass、tester token、
share/signed URL、檔案解密 key、API key 與私鑰類欄位；證據包不得保留可重放憑證。

## Exit Codes

| Code | Meaning |
|---:|---|
| `0` | 所有必要檢查通過；可能仍有 skip。 |
| `1` | 至少一項檢查失敗。 |
| `2` | CLI 參數錯誤。 |

## Requirements

腳本需要：

- Bash
- `curl`
- `tar`
- `python3`
- 專案 Python dependencies：最小啟動用 `requirements-minimal.txt`；執行本文件的
  開發 / QA 流程請再安裝 `requirements-dev.txt`。若要一次沿用舊流程，可安裝聚合檔
  `requirements.txt`。

外部 pentest 工具如 `nmap`、`nuclei`、`sqlmap` 不需要安裝；那些只屬於
`scripts/security/pentest/run_pentest.sh`。

## Troubleshooting

| Symptom | Check |
|---|---|
| `server startup` fail | 查看該次報告目錄的 `server.out`。通常是 port 被占用、Python dependency 缺失，或 server 啟動例外。 |
| `auth login root` fail | 確認 `ROOT_PASSWORD` 與 bootstrap root 密碼一致；隔離 runtime 首次啟動通常會使用腳本預設密碼。 |
| CSRF 相關失敗 | 查看 `raw/00_csrf_token.json`、login 後 cookie jar 與 `server.out`。 |
| cloud drive upload 失敗 | 查看 `raw/cloud_drive_upload.json` 與 `server.out`，再確認 scanner/security policy 是否阻擋。 |
| restore/reset 驗證失敗 | 優先視為功能 bug，檢查 `raw/restore_*`、`raw/reset_*`、`server.out`，不要先假設是腳本誤判。 |
