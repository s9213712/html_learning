# 11 QA Testing

一句話說明：這份文件把部署者、root、QA 與開發者最常需要的驗證路線收斂成一份分層測試地圖。

## 設計目的

原本 QA / 測試資訊散在：

- `QA_MISSION_FOR_AGENTS.md`
- `docs/security/FUNCTIONAL_SMOKE.md`
- `docs/security/PENTEST.md`
- `docs/security/FUNCTIONAL_PERMISSION_PENTEST.md`
- `docs/security/TRADING_STRESS_PENTEST.md`
- `docs/security/PRE_RELEASE_CHECKLIST.md`

這份文件的目標不是取代它們，而是先回答「我要驗什麼、該先跑哪個、哪些其實是 wrapper、哪些是深層 runbook」。

## 2026-07-29 現行回歸基線與判讀

- 完整 Python suite 已以隔離 runtime 收集 `4703` 個 nodeid 並通過；後續改動仍應
  依下方「全量 pytest」命令重跑，而不是把這筆結果當成永久 green。
- AI Agent 的實際瀏覽器回歸必須確認 planner 與一般 chat 都取得 `200`、頁面沒有
  console/page error，且設定面顯示已連線。雲端模型的 schema-heavy planner 預算
  與 browser timeout 必須一起保持在總 chat timeout 以內，避免健康回應被前端過早
  中止而誤報 `502`。
- ComfyUI 的 i2i、outpaint、upscale、blend 與 HF/Diffusers 都必須保存可開啟的
  圖片供人工視覺檢查；queue 成功、檔案存在或非空白檢查都不足以證明沒有框線、
  主體漂移或不可用結果。具體步驟見
  [comfyui/I2I_OUTPAINT_VALIDATION.md](comfyui/I2I_OUTPAINT_VALIDATION.md)。
- 交易回歸至少要覆蓋現貨、借貸、DCA、Grid、Workflow 和 backtest。新增的 DCA
  價格界限與 Workflow UTC 每日上限要同時用背景掃描與手動掃描觸發，確認 skipped
  reason、原子計數與失敗補償都正確。
- 本機 BT formal probe 已驗證 tracker-free、暫停/恢復與 daemon restart 的單機
  生命週期。正式多 IP 簽核仍必須在具有兩個可互通私有位址的主機或測試網段執行；
  單一介面不能冒充多 IP 成功。probe 維持 fail-closed：

  ```bash
  python3 scripts/testing/bt_formal_local_probe.py --out /tmp/bt_formal.json
  ```

## 使用方法

### 最常用的測試層級

#### 1. Repo / 快速 gate

```bash
python3 scripts/prepush/pre_push_checks.py
```

如果有安裝 `hooks/pre-push`，push 前也會先自動執行一輪 `--clean --yes
--ci`：先清掉 repo 內的 Python 快取與誤生在 repo 根目錄的 `runtime/`，
再進 blocking gate。

#### 2. 全量 pytest

```bash
scripts/testing/pytest_in_tmp.sh -q tests
```

Wrapper 會把 source copy 與 runtime/test artifacts 分放在同一個 `/tmp`
run root 的不同子目錄，避免測試誤把 `runtime/` 寫回原始 repo 或測試中的
copied checkout。

#### 3. 功能 smoke

```bash
scripts/security/pentest/run_functional_smoke.sh --qa-full --port 50741
```

`tests/security/smoke/smoke_suite.py`、`scripts/security/pentest/run_functional_smoke.sh`、`scripts/security/pentest/run_pentest.sh`
只在各自的 `/tmp` 隔離 runtime 使用測試帳號。對既有 target 的 probe 必須從環境變數
讀取密碼，不把密碼放進 argv、教學命令或報告；launcher/restart log 也只顯示帳號名稱。

`--qa-full` 是既有 QA 腳本的完整產品回歸模式，包含 community/chat/storage/video/
ComfyUI/reports/moderation 等較廣的功能 bug 類檢查。上線前 production gate
呼叫同一支腳本時只用 `--core-only`，不把 ComfyUI、chess/game、video 等產品 QA
混進解鎖條件。

產品 QA pytest 也走既有 QA pytest wrapper，例如：

```bash
scripts/testing/pytest_in_tmp.sh -q \
  tests/community tests/storage tests/video tests/comfyui tests/games \
  tests/frontend/community tests/frontend/storage tests/frontend/video \
  tests/frontend/comfyui tests/frontend/games
```

三棋 AI 的棋力量化有獨立 benchmark，不走西洋棋 self-play pipeline：

```bash
python3 scripts/games/board_ai_benchmark.py \
  --games reversi,go,gomoku \
  --engines random,easy,normal,hard \
  --rounds 1
```

報告會輸出到 `/tmp/hackme_web_test_artifacts/games/board_ai_benchmark/`，內容包含
round-robin standings、head-to-head matrix、Elo estimate、非法步統計與
deterministic skill suite。教學與欄位解讀見
[games/references/BOARD_AI_BENCHMARK.md](games/references/BOARD_AI_BENCHMARK.md)。

#### 4. 平台中心 Playwright 驗收

```bash
python3 scripts/testing/playwright_platform_health_check.py
```

這支腳本會自建 `/tmp/hackme_web_platform_phase15_*` 隔離 runtime、挑選隨機非
`5000` port、啟動 QA server，並用 Playwright Chromium 進行真實頁面互動。它會
驗證：

- Job Center 的 queued/running/succeeded/failed 顯示、取消確認、retry、一般使用者
  不可讀 `/api/admin/jobs`
- Notification Center 的 info / warning / error、unread count、dismissed_at 與
  audience 隔離
- Share Link Management 的 file / album / video 顯示、撤銷、max views、expires、
  password_required 與外部 URL 防誤用
- Trading Asset Overview 的可用點數、鎖定點數、現貨市值、借貸 / 融資權益、利息與
  API 失敗前端錯誤提示
- `390x844`、`768x1024`、`1366x768` viewport 不爆版、不水平捲動

報告會輸出到該次隔離 runtime 的 `reports/qa/playwright_platform_health_check_*.json`
與 `.md`。這是前後端實測，不能用單純 pytest passed 取代。

#### 4A. PointsChain Phase 1A economy acceptance

Phase 1A / 1A.5 驗收只驗 economy foundation，不接產品交易流。最小檢查：

```bash
python3 -m pytest -q \
  tests/economy/test_economy_layer.py \
  tests/frontend/trading/test_frontend_economy.py \
  tests/static/test_wallet_direct_call_inventory.py

python3 scripts/security/gate/wallet_direct_call_inventory.py --fail-on-blocker
```

前端要用 isolated server 登入 root，打開「積分系統 / 積分私有鏈」，確認 max supply、
minted total、mint remaining、reserved locked、active supply、circulating supply、
official treasury、PROMO fund、EXCHANGE fund、BURN、replay snapshot、derived verify、
health / stress 都由 root report replay read model 顯示。重整頁面 5 次和重啟同一
runtime 後，`minted_total`、`replay_height`、`wallet_root_hash`、snapshot hash 必須不變。

#### 5. 權限與安全掃描

```bash
scripts/security/pentest/run_pentest.sh --target https://127.0.0.1:5000
```

若測正式部署，target 應是 Nginx 對外 URL，不是 Gunicorn loopback upstream：

```bash
scripts/security/pentest/run_pentest.sh --target https://<host>
curl -ksS https://<host>/api/version
curl -ksS https://<host>/readyz
```

若只跑 `whole-site-production-gate`，wrapper 會自動把 timeout floor 拉高到
`10800s`，避免舊版預設 `180s` 永遠先把 gate timeout 掉。gate 內的完整 pytest
另有 `7200s` 預算，可用 `--full-pytest-timeout` 或
`WHOLE_SITE_FULL_PYTEST_TIMEOUT` 調整；一般子檢查仍使用較短的 `--timeout`。

#### 6. 角色 / 權限專測

```bash
scripts/security/pentest/run_pentest.sh --target https://127.0.0.1:5000 --only functional-permissions
```

#### 7. 交易壓力 / 正確性

```bash
python3 scripts/security/pentest/trading_stress_pentest.py --target https://127.0.0.1:5000
```

若這次改到交易價格融合或定投上限，另外補跑：

```bash
scripts/testing/pytest_in_tmp.sh -q tests/trading/core/test_trading_engine.py tests/trading/pricing/test_trading_reference_prices.py
python3 scripts/trading/validation/trading_exchange_validation.py --out /tmp/trading_exchange_validation_followup
```

若這次改到 workflow / Grid / backtest 驗證腳本本身，另外補跑：

```bash
PYTHONPATH=. python3 scripts/trading/validation/trading_workflow_template_validation.py --no-download --limit 200 --out /tmp/trading_workflow_validation_followup
python3 scripts/trading/probes/backtest_20000_probe.py --include-route --json-out /tmp/trading_backtest_20000_followup.json
```

#### 8. 二十四小時雙目標營運 campaign

最終 production sign-off 使用一個全新 `/tmp` campaign root；腳本自行管理 primary 與
recovery target，並在取得本機 bind/process 授權、兩站 readiness 與帳號預檢完成後才
開始活動時鐘：

```bash
python3 scripts/testing/operational_campaign_24h.py \
  --campaign-root /tmp/hackme_web_campaign_24h_YYYYMMDD_HHMM \
  --duration-seconds 86400 \
  --account-count 10 \
  --round-ops 1000 \
  --concurrency 32 \
  --session-pool 20 \
  --resource-interval 5
```

它不是把同一個 cookie 複製成多個假帳號，也不把 sleep 算成測試。primary 持續執行
`operational_soak_probe.py`；recovery target 同步執行破壞性演練。強制範圍包含：

- member 全功能 API rotation 與併發壓力
- root readiness / Security Center / server-mode requirements / log chain / AI Agent sentinel
- manager users / reports / community / notifications sentinel
- 一小時以上雙音軌/字幕影音、多帳戶上傳、HLS、seek、密碼分享與撤銷
- 交易/背景交易與 PointsChain 高頻、攻擊、錢包事故、治理與 recovery branch
- server snapshot、CLI runtime archive、storage preserve、restore、SQLite check 與 restart
- AI Agent Drive/share、server ops、治理、交易、媒體與上線前 dry-run；外部 ComfyUI 有設定時補真實生圖
- Standard realtime proxy、prepared HLS、跨瀏覽器與手機播放
- 360x800、390x844、768x1024 與桌面真實 Chromium 全模組巡覽
- frontend caught-failure buffer、瀏覽器 console/page error 與最後控制面查核
- 原子 checkpoint、來源 SHA-256 防漂移、PID/RSS/CPU/DB/WAL/disk、DB lock 與 secret scan

production sign-off 必須同時滿足 `active_test_seconds >= 86400`、八個 mandatory scenario
全過、所有帳號/必要 positive path 至少一次 `2xx`、沒有 hard failure/DB lock/來源漂移/
秘密洩漏或靜默前端失敗，且 final control plane 全過。預期拒絕案例只算 attempted
coverage，不拿 `4xx` 冒充成功。`--allow-short-duration` 永遠不具簽核資格。詳細矩陣、
artifact 與失敗判讀見
[AGENTS/24H_OPERATIONAL_CAMPAIGN.md](AGENTS/24H_OPERATIONAL_CAMPAIGN.md)。

### 腳本關係

- `scripts/prepush/pre_push_checks.py`
  是本機快速 gate，不預設啟 server。
- `scripts/security/pentest/run_functional_smoke.sh --qa-full`
  是隔離 runtime 的主要功能 QA 回歸；它會保留自己的 `/tmp` runtime 邊界。
- `scripts/security/pentest/run_functional_smoke.sh --core-only`
  是 production gate 用的核心模式，只保留 auth、admin、security-center、用戶、
  PointsChain、trading、越權與必要 runtime hardening 檢查。
- `scripts/security/pentest/run_pentest.sh`
  是外層 orchestrator，會呼叫多種檢查，包含 `functional-permissions`、
  server-mode-v2、whole-site-production-gate 等子檢查；whole-site gate 會套
  額外 timeout floor。
- `scripts/testing/system_stress_probe.py`
  是單輪混合負載引擎；`operation-mode=rotation` 搭配
  `require-all-accounts/require-operation-coverage` 才能作為功能輪訓證據。
- `scripts/testing/long_needle_simulation_probe.py`
  是較短的 economy + 全功能潛在問題探針，會建立真實帳號，但不是長時簽核。
- `scripts/testing/operational_soak_probe.py`
  是可設定時長的單 target core soak；八小時基線可用來找趨勢，但最終上線簽核由
  `operational_campaign_24h.py` 管理雙 target 與完整複雜場景。
- `scripts/testing/operational_campaign_24h.py`
  是最終雙 target orchestrator，至少 24 小時活動時間，並統一判定長影音、AI Agent、
  交易、PointsChain、事故、備份/還原/重啟與桌機/手機 UX。
- `scripts/security/server_mode/server_mode_v2_full_smoke.py`
  是 Server Mode v2 教學腳本 bundle 的隔離 runtime smoke harness；它會依序跑
  `docs/server_mode_v2/01、02、04、05、06、07`，最後確認 shadow
  與 production tables 沒有互相污染。
- root 安全中心的 `上線前檢查`
  現在除了 A/B 區卡片外，也內建：
  - 站內文件檢視（playbook / tests 捷徑不再跳 `NOT FOUND`）
  - per-report JSON upload 入口（可直接貼上或上傳 `.json`）
  - upload 後自動重整 B 區 report 狀態
- root AI Agent 的普通「上線前檢查」只回傳 dry-run、缺口與 `next_actions`；只有
  明確要求切換且提供 `GO_LIVE` 才能進 production，後端不接受缺少確認字串的
  `auto_switch=true`。
- `scripts/security/pentest/functional_permission_pentest.py`
  是權限濫用 / 角色矩陣專測，不是一般 port scanner。
- `scripts/security/pentest/video_module_pentest.py`
  是 Cloud Drive-backed video 專測，涵蓋 share-link、strict E2EE 共享邊界、manager regenerate / revoke 與 tip/idempotency。
- `tests/security/smoke/smoke_suite.py`
  是極薄的 Python smoke；它現在會在跑完後把暫時打開的 feature flags 還原，
  避免污染同一個測試 runtime。
- `QA_MISSION_FOR_AGENTS.md`
  是 agent 深度 QA runbook，包含人工逐步測試、DB 對帳、異常輸入矩陣與直接修正模式。
- `CLI_ADMIN_PLAYBOOK.md`
  是正式的 `curl` / shell 管理與 API 驗證手冊，適合 root / admin / developer
  在隔離 runtime 直接操作網站。
- `docs/AGENTS/TRADING_QA_REGRESSION_MATRIX.md`
  是交易系統專用的固定回歸清單；只要改到 backtest / workflow / grid / DCA / liquidation /
  融合價格，就不能只跑 `test_trading_engine.py` 或歷史回測。

### Production Gate 報告對照表

`services/snapshots/schema.py::PRODUCTION_REQUIRED_REPORT_TYPES` 是 production gate 的 source of truth。
截至 2026-06-23，required report set 為 14 份。
欄位說明：

- `使用腳本 / 驗證面`：產生或驗證該報告的主要入口。
- `測試筆數`：
  - `動態` 代表腳本執行時計算 `total/passed/failed`，不是固定 pytest case 數。
  - `固定 N` 代表目前 repo 內對應 pytest 檔的 collected case 數。
- `Production staging / expected path`：列出有明確 `HACKME_RUNTIME_DIR` 時的
  canonical staging 路徑。獨立執行安全/QA 腳本而未選 live runtime 時，輸出預設在
  `/tmp/hackme_web_test_artifacts/reports/security/`，不會建立 source checkout
  `runtime/`。

另外要注意：

- `runtime/reports/security/production_gate/*.json` 的 filesystem auto-detect 只輔助顯示最新候選報告。
- 這些檔案預設是 `unverified`，只有驗簽成功且 target 與當前 runtime 一致時才會被視為 `verified`。
- unsigned、invalid JSON、`report_type` mismatch、replay 舊 commit 等檔案只能在後台顯示 warning，
  不可讓 production gate 亮綠。
- production gate 的 live 驗收至少要再補一條：
  **完整 required report set verified 但 old/fake `target_commit` 的 reports 不得解鎖 production。**
  這條不可只靠 `tests/snapshots/test_snapshots.py`；必須對隔離 `/tmp` server 實際呼叫：
  - `GET /api/root/server-mode/requirements`
  - `POST /api/root/production/enter`
  並確認回應 reason 含 `target_commit_mismatch`。

若要一次生成完整 required report set，使用：

```bash
HACKME_RUNTIME_DIR=/absolute/external/runtime \
HACKME_OPERATIONAL_CAMPAIGN_REPORT=/tmp/<campaign>/reports/operational_campaign_24h.json \
ROOT_PASSWORD='<root-password>' \
MANAGER_PASSWORD='<manager-password>' \
TEST_PASSWORD='<test-user-password>' \
python3 scripts/security/gate/on_live_reports_make.py --base-url https://127.0.0.1:5000
```

或從 `scripts/on_live_reports/` 的捷徑執行（每個 report type 一個 `.py` 入口；
詳見 [scripts/on_live_reports/README.md](../scripts/on_live_reports/README.md)）：

```bash
# orchestrator (= 上面那行的 alias)
HACKME_RUNTIME_DIR=/absolute/external/runtime \
HACKME_OPERATIONAL_CAMPAIGN_REPORT=/tmp/<campaign>/reports/operational_campaign_24h.json \
ROOT_PASSWORD='...' MANAGER_PASSWORD='...' TEST_PASSWORD='...' \
  python3 scripts/on_live_reports/on_live_reports_make.py --base-url ...
# 單一 report type
python3 scripts/on_live_reports/clean_smoke.py
python3 scripts/on_live_reports/permission.py
python3 scripts/on_live_reports/snapshot_restore.py
# ... 每個 required report type 都有一個 entry
```

它會把 raw artifacts 放到：

- `$HACKME_RUNTIME_DIR/reports/security/production_gate/runs/<RUN_ID>/`

並把上傳前的穩定 payload staging 到：

- `$HACKME_RUNTIME_DIR/reports/security/production_gate/<report_type>_report.json`

若要做 live target-commit regression，建議另外保留一份驗收證據目錄，例如：

- `runtime/reports/server_mode_gate_live_<RUN_ID>/`

其中至少保存：

- current target context
- old-commit verified required-report scenario JSON
- current-commit verified required-report scenario JSON
- production enter response
- final mode response

若要把整條 happy-path 補齊，而不只驗 `target_commit mismatch`，至少再多做這 6 步：

1. 先寫入完整 required report set **signed + target 一致** 的
   `runtime/reports/security/production_gate/<report_type>_report.json`，
   再呼叫 `GET /api/root/server-mode/requirements`，確認 **只靠 filesystem
   auto-detect 就能 `ok=true`**。
2. 把同一批 payload 上傳到 `/api/root/production-report/upload`，再確認
   `requirements ok=true`。
3. 故意把其中一份 canonical filesystem report 改成 invalid JSON，再呼叫
   `GET /api/root/server-mode/requirements`，確認 **仍是 `ok=true`**，
   而且該 report 來源回退到 DB 的 verified `prodrep_*` record。
4. `POST /api/root/production/enter`、`confirm=GO_LIVE`，確認切換成功。
5. production 後用真正瀏覽器或 browser-like UA 重新登入 root，確認：
   - `browser_only_mode` 會擋掉 script-style client
   - root 預設帳號首次登入會被要求改密
   - 改密後可重新登入並讀 `/api/root/server-mode`、`/api/admin/security-center`
6. 對下面兩條 API 做並發 hammer，並掃描 server log：
   - `GET /api/root/server-mode/requirements`
   - `GET /api/admin/security-center`
   期待：
   - 沒有 500
   - 沒有 `database is locked`
   - `security-center.readiness.status = ok`
   - `security-center.anomaly.signals = []`

2026-05-09 的 live full-generator 驗收已在隔離副本實跑完一次：

- 副本：`/tmp/hackme_web_dev_20260509_171716_15908/hackme_web`
- 證據：`runtime/reports/server_mode_gate_full_generators_20260509T115237Z/`
- focused happy-path 證據：`runtime/reports/server_mode_gate_live_20260509T092856Z/`

這輪最後證明：

- focused live gate：通過
- full generator gate：通過
- `database is locked`：`0`

但有一個實務細節不能省略：

- 若你在隔離副本上先修改了 protected files，再跑 `integrity_guard`，
  integrity report 會先因 pending findings 失敗。
- 正確流程不是跳過，而是：
  1. `GET /api/root/integrity/findings?status=pending`
  2. review/approve 預期變更
  3. `POST /api/root/integrity/rescan`
  4. `GET /api/root/integrity/report`
  5. 重新上傳 `integrity_guard` report
  6. 再做 final requirements / `GO_LIVE`

換句話說，full-generator 驗收不是只看 raw reports，有改過隔離副本本身時，
`integrity_guard` 的 finding review 也是正式流程的一部分。

| report_type | 使用腳本 / 驗證面 | 測試筆數 | Production staging / expected path |
|---|---|---:|---|
| `clean_smoke` | `python3 scripts/security/server_mode/server_mode_v2_clean_smoke.py` | 動態 | `runtime/reports/security/server_mode_v2_clean_smoke_<timestamp>.json|.md` |
| `adversarial` | `python3 scripts/security/server_mode/server_mode_v2_adversarial.py` | 動態 | `runtime/reports/security/server_mode_v2_adversarial_<timestamp>.json|.md` |
| `redteam_l2` | `python3 scripts/security/server_mode/server_mode_v2_redteam_l2.py` | 動態 | `runtime/reports/security/server_mode_v2_redteam_l2_<timestamp>.json|.md` |
| `pytest` | `scripts/testing/pytest_in_tmp.sh -q tests` | 動態 | `runtime/reports/security/production_gate/pytest_production_report.json` |
| `log_chain_verify` | `GET /api/root/server-mode/logs/verify` | 動態 | `runtime/reports/security/production_gate/log_chain_verify_report.json` |
| `integrity_guard` | `POST /api/root/integrity/rescan` + `GET /api/root/integrity/report` + `tests/security/integrity/test_integrity_guard.py` | 固定 15 | `runtime/reports/security/production_gate/integrity_guard_report.json` |
| `stress` | `python3 scripts/security/pentest/stress_test.py` + `python3 scripts/security/pentest/trading_stress_pentest.py` | 動態 | `runtime/reports/security/stress_<timestamp>.json|.md`；`runtime/reports/security/trading_stress_report_<timestamp>.json|.md` |
| `permission` | `python3 scripts/security/pentest/functional_permission_pentest.py` | 動態 | `runtime/reports/security/functional_permission_pentest_<timestamp>.json|.md` |
| `functional` | `scripts/security/pentest/run_functional_smoke.sh --core-only` + `tests/security/smoke/smoke_suite.py` | 動態 | `runtime/reports/security/functional_<run_id>/00_FUNCTIONAL_SMOKE.md`、`results.tsv`、`server.out`、`raw/` |
| `pentest` | `scripts/security/pentest/run_pentest.sh` + `python3 scripts/security/pentest/session_security_pentest.py` | 動態 | `runtime/reports/security/<run_id>/00_SUMMARY.md` + `raw/*.json|*.md|*.txt` |
| `snapshot_restore` | `scripts/testing/pytest_in_tmp.sh -q tests/snapshots/test_snapshots.py` + 手動 snapshot integrity / restore-boundary verify；PointsChain ledger backup/restore 必須維持停用 | 固定 40 | `runtime/reports/security/production_gate/snapshot_restore_report.json` |
| `points_chain_consistency` | `scripts/testing/pytest_in_tmp.sh -q tests/points/test_points_chain.py` + `services/points_chain.verify_chain()` | 固定 27 | `runtime/reports/security/production_gate/points_chain_consistency_report.json` |
| `cloud_drive_quota_permission` | `scripts/testing/pytest_in_tmp.sh -q tests/storage/test_cloud_drive_attachments.py tests/storage/test_storage_albums_schema.py` | 固定 55 | `runtime/reports/security/production_gate/cloud_drive_quota_permission_report.json` |
| `ai_agent_boundary` | `scripts/on_live_reports/ai_agent_boundary.py`，跑 deterministic AI Agent write-tool / lockdown / server-filesystem boundary pytest；不呼叫 LLM | 固定 7 | `runtime/reports/security/production_gate/ai_agent_boundary_report.json` |
| `operational_campaign_24h` | 匯入 `operational_campaign_24h.py` 正式報告並重驗 86,400 秒、八個 mandatory scenario、source manifest、secret/control checks | 動態 | `runtime/reports/security/production_gate/operational_campaign_24h_report.json` |

補充：

- `pytest` / `log_chain_verify` / `snapshot_restore` / `points_chain_consistency` / `cloud_drive_quota_permission` / `ai_agent_boundary` / `operational_campaign_24h`
  這幾類若不是由單一腳本直接產生檔案，仍應把最後簽署/上傳前的 raw report staging 到
  `runtime/reports/security/production_gate/`，不要散落在 repo root。
- `artifacts/` 與 `runtime/reports/` 都是可再生測試輸出，預設不進 git；需要長期保留的結論請整理到
  `docs/AGENTS/reports/` 或 `docs/RELEASE/`。
- `functional` 與 `pentest` 都是目錄型報告，不是單一 `.json`；前者偏功能流程，後者偏安全/攻擊面。
- `stress` 一次通常會有兩份報告：一般 HTTP 壓測與 trading stress，各自獨立保存。
- `on_live_reports_make.py` 會額外生成：
  - `runtime/reports/security/production_gate/on_live_reports_make_<RUN_ID>.json`
  - `runtime/reports/security/production_gate/on_live_reports_make_<RUN_ID>.md`
  這兩份是本次整批產製的總結，不是 required report 之一。
- 若 production profile 內的某些鍵（例如 `allow_register`、`captcha_mode`、
  `production_single_*`）沒有出現在 `security-center` payload，請直接查
  runtime DB 的 `system_settings` 補核對，不要把「payload 沒帶出來」誤判成
  「production hardening 沒套用」。

## 原理

- 不是所有測試都在做一樣的事。
- `smoke_suite.py`、focused pytest、functional smoke、pentest、trading stress
  彼此有交集，但測的是不同層：
  - pytest 偏單元 / 回歸
  - functional smoke 偏隔離 runtime 的實際操作
  - pentest 偏外部攻擊面與權限濫用
  - QA runbook 偏人工逐步驗證
- 因此不要用單一腳本通過就宣稱功能完整。

## 失敗情境與提示

- 只跑 pytest 就想宣稱上線可用：
  不夠，至少還要 functional smoke 與對應安全檢查。
- 想測 production host 卻沒授權：
  不要執行 pentest / stress。
- 測試污染 repo runtime：
  請改用隔離 `/tmp` runtime，參考 `run_functional_smoke.sh` 與 `QA_MISSION_FOR_AGENTS.md`。
- `whole-site-production-gate` 還是被 wrapper 提前 timeout：
  先確認是不是舊版腳本；新版會自動給 `900s` floor。若仍不夠，再明確調高
  `--tool-timeout`。
- 看起來像重複腳本：
  先看這份文件的「腳本關係」；`run_pentest.sh` 多半是 wrapper，不等於和子腳本重複。

## 測試方式

- 確認 README、Start Here、Feature Overview 都把這份文件列為測試主入口
- 若改到 Cloud Drive 檔案瀏覽器，至少手動確認資料夾單擊不誤開、雙擊可進入、右側 `開啟` 按鈕仍可用，且雙擊不會誤觸刪除/下載等 action button
  - PDF 預覽是否在 plain / `server_encrypted` 模式下直接走原生 viewer，日文 / 中文檔名不會再破 viewer header
  - strict `e2ee` PDF 若無法內嵌，是否保留「新分頁開啟 PDF」備援
  - 壓縮檔預覽是否為結構化清單而不是單段文字
  - 同一次登入 session 內再開第二個 E2EE 檔案時，是否會先嘗試最近成功密碼，而不是每次都直接跳出詢問
  - 檔案管理與容量管理是否分頁清楚，容量狀態是否用水位式視覺，不再使用電池隱喻
  - resumable/chunk upload 重新整理後是否在任務中心保留 session，並提醒使用者重新選擇同一檔案後繼續
  - BT/direct link 是否同時可排隊，任務中心是否顯示速度、可用度、pause/resume/cancel，且多個 BT 時優先跑可用度較高者
  - 分享連結複製後是否在按鈕下方顯示已完成複製；分享頁是否可在允許 preview 時直接瀏覽器預覽
  - 分享管理每筆 file / album / video 分享是否都有編輯按鈕，且可更新分享密碼、到期、次數、預覽與指定使用者
  - 指定使用者分享時，一般使用者是否只能看到好友，root / manager 在站務 context 是否可看到全站用戶且好友置頂
  - 相簿是否為連續照片流，hover 放大、點選全頁、左右切換都可用，手機版不可整排擠爆
- 確認功能新增後，同步更新 smoke / pentest / QA runbook / troubleshooting
- 若本次改到個人面板 / 好友 / 指定對象，至少補：
  - 個人面板是否顯示好友代碼，重新產生後舊代碼失效，複製後有已複製提示
  - 透過好友代碼加入是否直接 accepted，錯誤代碼 / 已是好友 / 加自己都有明確訊息
  - 一般帳號的 PM / private group 指定對象只列好友，直接打 API 邀請非好友仍被拒絕
  - root / manager 為管理目的可 PM 非好友，但這個例外不應自動套到雲端硬碟分享或遊戲邀請
  - 遊戲邀請與直接 strict-E2EE 檔案金鑰分享需驗證非好友直接 API 呼叫為 403、好友路徑成功，且 service 層仍有 defense-in-depth
- 若本次改到聊天室匿名，至少補：
  - 官方聊天室對一般帳號是否匿名、不顯示人數；root 顯示 `root`、manager 顯示編號管理員且標示官方
  - root / manager 是否能看到原發言者，一般帳號是否看不到
  - 一般群建立者是否可決定允不允許匿名，加入者是否可選匿名
  - 匿名時頭像與顯示名稱是否都匿名；一對一 PM 是否沒有匿名選項
  - `建立聊天室` 表單是否只在按下按鈕後展開，不再常駐成大卡片
- 若本次改到安全中心的 root 測試面板，至少補：
  - 滲透 / 越權 / 全功能 / 壓力四張卡是否都有獨立按鈕、進度條、最近任務狀態與詳細 log
  - `GET /api/root/security-tests` 與各自的 `POST /api/root/security-tests/*` 是否仍維持 root-only
  - 越權測試是否真的走 `functional_permission_pentest.py`，而不是只是 UI 多一顆按鈕
  - 任務清單刷新時，四張卡是否只更新對應 kind 的最新 job，不會互相覆蓋
- 若本次改到系統資源看板，至少補：
  - CPU / GPU / VRAM / RAM 是否以半弧形 gauges 顯示，GPU 不存在時是否明確顯示 unavailable
  - 連續刷新是否命中短暫快取，不會每次點擊都重新啟動 GPU probe 或 `nvidia-smi`
  - 手機版 gauges 是否可讀，不互相重疊
- 若本次改到帳號復原 / 密碼流程，至少補：
  - 一般使用者 `password reset request/confirm` 仍可正常運作
  - `root` 不可再透過 web `忘記密碼`、email token 或審核流程重設
  - `scripts/admin/root_recovery.py` 是否會撤銷 root 現有 session、清掉 root CSRF token、強制下次登入改密碼
  - 離線 recovery 後是否留下審計紀錄；若 runtime 缺審計 secret，是否至少不會把 recovery 本身做失敗
  - `scripts/security/pentest/run_functional_smoke.sh` 是否仍能驗證 offline root recovery CLI 可執行
- 若本次改到 ComfyUI，至少補：
  - 設定頁切到 `HF` family 時是否只顯示 Hugging Face / Diffusers 欄位，不混入 `Civitai API Key`、ComfyUI Account API Key、batch/default-size 等 ComfyUI 專屬欄位
  - 設定頁切到 `ComfyUI` family 時，`Civitai API Key` 與 root 模型匯入工具是否在本地/遠端模式都維持可見；切到 `remote` 時文案需明確說明會寫入本站設定的 ComfyUI models 目錄
  - AI 產圖前台 root 是否看得到明確的 `Civitai / 模型匯入` 或 `Civitai / 模型管理` 入口，而不是只能猜在一般模型管理頁
  - model list 是否回傳 `models / loras / embeddings / vaes / generation_modes / controlnet_types / controlnet_models / upscale_models`
  - LoRA metadata / `trained_words` 是否會在重新整理後仍存在，不是只在下載當下有
  - 使用者加入 LoRA 時，是否只會補上缺少的 trigger words，而不會每次重複疊加
  - prompt helper 是否能把 Embedding token 正確送進後端
  - workflow template preview 若有正/負 `CLIPTextEncode.text`，`ui_schema.panels[text]` 是否包含 synthetic `text:embeddings` / `embedding_shortcuts` 子項，且不被列為必填欄位
  - 前端從 template 文字面板插入 Embedding 後，已展開的 text panel 是否保持展開，不可被重新折疊
  - custom VAE 是否真的改到 workflow，而不是只有 UI 多一個欄位
  - Civitai inspect / download 是否顯示 trigger words，且 remote mode 顯示的模型匯入工具不會誤導成直接安裝到遠端主機
  - Civitai 搜尋是否支援關鍵字、base model、Checkpoint / LoRA / Embedding / ControlNet、Safe/NSFW 篩選，且搜尋結果會顯示版本、檔案大小、hash、相容模型摘要
  - root 若未設定 Civitai API Key，搜尋與 inspect/download 是否回人性化錯誤，而不是靜默失敗或 500
  - Civitai 下載前是否真的會跳二次確認；下載中斷時是否顯示「下載中斷或連線失敗」而非模糊錯誤
  - `functional_permission_pentest.py` 是否仍擋住 anonymous / user / manager 對 root-only ComfyUI / Civitai 搜尋、inspect、model upload 與 download-job poll 的越權存取
  - 生圖、本地啟動、模型下載進行中時，閒置登出倒數是否改成暫停，而不是做到一半被踢出
  - `img2img / inpaint / outpaint / upscale` 是否能正確接收來源圖 / 遮罩圖 / 控制圖，手機版表單不可擠壞
  - ControlNet 模型缺失、workflow 缺 node、控制圖格式錯誤、`control strength` 超出範圍時，是否回人性化錯誤而非靜默失敗
  - history replay 是否能套回來源圖、遮罩圖、ControlNet、outpaint 與 upscale 設定，不可只回填純文字 prompt
  - root 模型匯入面板是否可在 `Civitai 網址 / 本地檔案上傳` 兩種來源間切換；本地上傳只允許合法副檔名，remote mode 仍需清楚標示寫入本站設定的 models 目錄
  - `ComfyUI Workflow 工作台` 是否可把目前表單匯出為 sanitized workflow JSON，再匯入成 private/public preset
  - workflow 匯入是否會拒絕壞 JSON、absolute path、shell / exec / script 類節點、外部 URL 與可疑敏感欄位
  - private workflow preset 是否不可被其他使用者讀取、更新或刪除
  - root 是否可把自己的 preset 發布為官方 preset，manager / user 不可越權發布
  - workflow preset run 若缺 checkpoint / LoRA / ControlNet / workflow node，是否回 `409` 與明確依賴錯誤，而不是靜默 fallback
  - 一鍵重跑是否會保留 seed / CFG / steps / LoRA / ControlNet 上下文；匯出 JSON 不得含本機絕對路徑或 server storage path
  - 手機版 workflow workbench 是否仍可閱讀 preset 清單、依賴狀態、官方標記與主要操作按鈕
  - 若要做 live smoke，可額外跑 `python3 scripts/comfyui/feature_probe.py --base-url https://127.0.0.1:PORT --username root --password ... --insecure --json-out /tmp/comfyui_probe.json`，確認 status、model list、txt2img、img2img、inpaint、outpaint、upscale、history rerun 全都真的能通；ControlNet 若缺模型 / node，應回 `expected_unavailable` 或明確錯誤，而不是卡死
  - 小 VRAM / 大模型載入時是否只讓 ComfyUI job 慢，不可讓主 Flask request 同步等待到 timeout 或整站下線
  - Diffusers in-process mode 未明確設定 `HTML_LEARNING_ALLOW_IN_PROCESS_DIFFUSERS=1` 時是否被拒絕，避免模型載入主程序
  - Diffusers job progress 是否使用 `Diffusers / Hugging Face` 文案與 sanitized Python log tail，不可再顯示 ComfyUI backend 無回報
  - Diffusers repo inspect 是否可用 GET 安全讀取路徑，不應因缺少 mutation CSRF token 產生 security alert
  - remote ComfyUI / local external process 的 status、interrupt、generate 是否都有 bounded timeout
- 若本次改到影音串流 / E2EE 分享，至少補：
  - 發布影音欄位是否按下發布按鈕後才展開，不再常駐成卡片
  - 影音列表搜尋是否可用，空結果與錯誤都有人性化提示
  - HLS 準備完成前是否不在一般影音列表一直顯示準備中；上傳者是否看到處理提示，完成後收到通知
  - HLS 準備是否走外部 worker / job，不可在主 request 裡同步 ffmpeg 解碼
  - 我的影音按分享後是否跳到分享管理並帶入該影片，讓使用者繼續設定分享選項
  - Safari 是否仍走原生 HLS，而不是被 `hls.js` 蓋掉
  - 桌機 Chrome / Firefox / Edge 是否能載入同源 `hls.js` 並播放 prepared HLS
  - `hls.js` 初始化或 fatal error 時，是否會自動退回 direct `/stream` 並顯示人性化錯誤
  - strict `e2ee` 是否仍只走瀏覽器端解密播放，不可偷偷走伺服器端 HLS
  - strict `e2ee` 若有 `Streaming v2` manifest，是否改走 encrypted chunk + Worker + `MediaSource`；沒有 manifest 時是否明確退回完整解密播放
  - strict `e2ee` 的 manifest / chunk endpoint 是否只回密文、不接收 `raw_file_key` / `e2ee_password` / `vk`
  - chunk index 越界、token 過期 / 撤銷 / 觀看次數耗盡時，manifest / chunk endpoint 是否一致拒絕
  - `持連結可看` 的 E2EE 影音分享管理面板，是否會顯示分享狀態、剩餘觀看次數、到期日、分享密碼狀態與重新產生 / 撤銷入口
  - manager/root 若能管理 unlisted 影音分享，是否能更新或撤銷 share-link，不可只在 UI 顯示按鈕卻在後端被 owner-only 擋掉
  - 分享頁若沒有 fragment，是否會明確提示「無法復原，只能重新產生分享」
  - 分享頁是否會清楚顯示 `讀取分享授權 / 下載加密影音 / 瀏覽器端解密` 三個階段，而不是大檔只看起來像卡住
  - 分享頁若有第二層密碼，是否正確要求「完整連結 + 分享密碼」
  - 分享頁若 wrapped file key 被竄改，是否顯示人性化錯誤，不可把原始 exception 直接丟給使用者
  - 手機版播放頁 / 分享頁是否仍能看到播放狀態、錯誤提示與主要按鈕，不可被播放器擠掉
- 若本次改到認證 / CAPTCHA，至少補：
  - `Turnstile site key` 是否只在 `turnstile` 模式出現
  - 切到 `none / math / image` 後，token 欄位是否會隱藏而不是殘留在畫面上誤導部署者
- 若本次改到公告 / 社群編輯流程，至少補：
  - manager/root 是否可直接編輯既有公告，不必刪除重發
  - 一般使用者是否仍會被 `403` 擋下，不能偷改公告
  - 前端是否正確切到編輯模式，按鈕文案會從 `發布公告` 變成 `更新公告`
  - 取消編輯後，公告表單是否恢復成新增模式，而不是把舊內容殘留到下一次發布
- 若本次改到設定頁 / feature flags，至少補：
  - `全開` 是否真的把全部 feature flag 勾開，而不是只補勾目前畫面可見那幾個欄位
  - `最低維運` 是否會把站點收斂到帳號、Audit、健康燈、Server Mode、Snapshot 這組最小骨架，而不是保留舊勾選殘值
  - `設定已儲存` 成功訊息是否會自動消失，而不是長時間誤導 root 以為目前狀態已再次寫入
  - 功能被擋下時，503 / UI 訊息是否有指出真正被關閉的是哪個父功能，而不是只回一句 generic 的 `root 關閉`
  - 若某個父功能關閉，但其子功能仍已開啟，訊息是否有提醒哪些已開功能會一起受影響
- 若本次改到交易價格來源 / 交易所備援，至少補：
  - `融合價格（多交易所加權平均）` 是否真會抓多家交易所，而不是只回單一來源
  - `自動依深度權重` 是否會在單一 API 掛掉時自動用剩餘來源重算，不會整個交易頁直接停擺
  - `root 手動權重` 是否能個別調整 Binance / OKX / Coinbase / Kraken / Gemini / Bitstamp 占比
  - 所有手動權重設成 0 時，是否會安全退回自動深度權重，且 root dashboard / log 會明確標成 `manual weights invalid`
  - root-only `融合價格即時比例` dashboard 是否會列出實際 normalized weights、excluded source 與 `價格來源降級`
  - order book 全失敗時，是否明確標示 fallback source，且不把它當成正常 fused price
  - `GET /api/trading/live-price` 是否每 `2` 秒更新目前價格、漲綠跌紅，且在 fallback / excluded source 時亮黃燈
  - 買入 / 賣出預估是否會跟著同一輪 `live-price` 更新節奏同步重算，而不是停留在舊價
  - 積分錢包裡的現貨 / 進階交易浮盈虧、root 虛擬總額是否也跟著同一輪 `live-price` 更新，而不是等 full dashboard reload
  - `live-price` 回應是否含 `price_health / fallback_reason / excluded_sources / defaulted_market`
  - `live-price` 是否另外明確回 `price_type / source / confidence / stale / degraded / provider_count`
  - `live-price` 與 `root /trading/price-fusion-status` 是否另外回 `connected / fallback / last_update_at / exclusion_reason / transport_state`
  - `reference-prices` 是否明確標成 `price_type=reference`，且帶 `price_context`
  - 目前價格、現貨估值、現貨盈虧、融資維持率、強平價、下單預估是否都有清楚標示自己用的是 `reference price` 還是 `risk-grade price`
  - 當 `risk-grade price` 不可用時，前台是否明確顯示「目前風控級價格不可用，已暫停市價單與高風險交易；限價單仍可使用」，而不是誤導成整個市場沒價格
  - 高風險價格降級時，市價單 / 融資估算 / bot 風控是否都顯示人性化阻擋訊息，而不是靜默沿用 generic market price
  - Binance / OKX / Coinbase / Kraken websocket provider input 斷線時，是否自動退回 HTTP polling，且 UI 顯示 `fallback / stale / degraded` 而不是假裝正常
  - 單一 provider 異常或只剩單源 degraded 參考價時，`risk-grade price` 是否維持 blocked，不得拿單源 WS/HTTP 資料偷偷通過風控
  - `live-price` 是否會同步刷新 DB 內 `trading_markets.manual_price_points / price_source` 快取，文件也要寫清楚這不是純 read-only API
  - `scripts/trading/validation/trading_exchange_validation.py` 是否已和目前引擎結果同步，不再出現過時 expected value
  - `scripts/trading/validation/trading_exchange_validation.py` 是否會額外檢查連續加倉後 `avg_cost_points` 仍維持合理，不會悄悄爆成異常大值
  - `scripts/security/pentest/trading_stress_pentest.py` 是否會在強迫 `conservative` 融合價格時，驗證市價單與融資開倉都被高風險 gate 阻擋
  - root `交易市場 registry` 是否可新增 / 編輯 / 停用市場，且 provider mapping、precision、lot size、tick size 會立即反映到下單驗證
  - disabled market 是否確實阻擋新下單，但既有歷史、持倉與報表仍可查
  - provider mapping 錯誤或 probe 未通過時，是否拒絕啟用 `risk-grade` 用途
  - market registry audit log 是否保留 before / after，且只有 root 可改
  - seeded 市場是否回傳 `registry_source=catalog_seed`、`seed_version` 與 `seed_sync_status`
  - root 修改 seeded 市場後，後台是否明確顯示 `drifted`，而不是把 DB / catalog 差異藏起來
- 若本次改到 Server Mode v2 / production gate，至少補：
  - unsigned / hash mismatch / signature mismatch 的 production report upload 是否被拒絕
  - `production_requirements()` 是否把 `trust_level != verified` 或 `signature_valid = false` 的報告列為 failed
  - `internal_test` login token 是否只能給綁定帳號使用，其他帳號拿到同一顆 token 仍應被拒絕
- 若本次改到交易圖表 / 技術指標，至少補：
  - 參考 K 線圖的 checkbox 是否有同步接進前端事件，不是只有 HTML 多了控制項
  - `MA10 / MA30 / EMA50 / RSI14 / KD(9,3,3)` 是否真的會進入 chart render，而不是只出現在 legend
  - `RSI / KD` 是否走副圖刻度，不可直接沿用價格軸
  - tooltip 是否對價格線與震盪指標用不同格式顯示，不可把 RSI/KD 顯示成 `$54.3`
  - 手機版下指標列是否仍可橫向滑動，不會因新增控制項直接換行爆版
- 若本次改到借貸利息 / backtest 上限，至少補：
  - 現貨 fee 預設 `0.10%`、Grid fee 預設為 spot fee 的 `75%`（25% 折扣）時，
    spot / grid / backtest / 預估 UI 是否全部一致
  - 手續費與利息是否先累積小數殘值，只有現貨賣出、機器人停止、借貸結算或清算時才轉整數 POINT 並無條件進位
  - 借貸交易的 open / close fee 是否以名目金額計算，例如保證金 100、借 400 時按 500 計費
  - 若本次改到交易市場定義或 provider 對應，至少補 `tests/test_trading_markets.py`
    與 `tests/test_trading_reference_prices.py`，確認 market catalog、display alias、
    live/reference provider id 與 route normalization 都一致
  - `BTC / ETH 8% APR`、`USDT / POINTS 10% APR` 是否會依實際借入資產正確套用，
    而不是所有倉位都吃同一組利率
  - 每 `1` 小時計息、不足 `1` 小時以 `1` 小時計時，前台是否顯示 `累積利息`、
    `已實扣`、`下一次計息`
  - `principal=50, daily_rate=1%, 24h` 是否改成保留 `0.5` 點殘值，而不是直接記成 `1`
  - root 把 `borrow_interest_pool_pressure_multiplier` 設成 `0` 時，實際利率是否真的不再被額外放大
  - 現貨 / 借貸成交後，`volume_stats` 與 root report `volume_summary` 是否同步增加，
    供後續 VIP 系統使用
  - `BTC/USDT 2024-01-01 ~ 2024-12-31 @ 1h` 是否可直接回測，不再被 `5000` 根上限擋住
  - 回測頁在使用者選 `start_time / end_time / timeframe` 時，是否會直接提示另一側日期最遠可設到哪裡，而不是只丟出 `20,000` 根上限
  - 改變 `timeframe` 後，開始 / 結束日期欄位的 `min / max` 是否同步更新
  - `candles < 2` 不可再靜默覆蓋成 live public history；只能在明確 opt-in 時抓 reference candles
  - flat Bollinger 序列不應誤觸發
  - 異常 jump / outlier candle 不能靜默吃成正常高報酬
- 若本次改到 DCA 機器人，至少補：
  - `max_runs=-1` 是否會被正確保存為不限制，而不是被前端或後端偷偷改回 1
  - 跑過一次後再 backdate/重啟，是否仍可繼續執行，不會被誤判為已達上限
  - 不限制模式下 UI 是否不再顯示「增加次數」這種多餘操作
- 若本次改到 BTC_trade 整合，至少補：
  - root 設定 `repo / branch / project path` 後，`檢查 BTC_trade` 是否會正確列出腳本缺漏與資料 / 模型 / 預測狀態
  - `一鍵啟動預測` 是否先檢查資料過期與模型新舊，再決定是否補跑 `update_data.py` / `retrain_models.py`
  - 訓練很久時是否改成背景工作持續輪詢，而不是直接因 timeout 顯示失敗
  - `hourly_check.py` 跑完後是否真的等到新的 `runtime/report_log_4h.jsonl`，或至少明確說明沿用的是仍有效期內的既有預測
  - 長時間執行中途重新整理頁面後，root 是否仍可從背景工作狀態知道目前停在哪個 step
- 若本次改到任何交易核心邏輯，另外強制補：
  - Grid Bot preview 是否同時顯示每格毛利、每格手續費、每格扣費後淨利、
    損益兩平間距與紅 / 黃 / 綠燈，而不是只顯示毛利價差
  - `grid spacing <= break-even spread` 是否會被標成紅燈並阻擋建立
  - `0 < net spread < 0.10%` 是否會變成黃燈並要求二次確認
  - `NaN / Infinity` 是否被 preview API 拒絕
  - `empty candles`、`single candle`、`negative / zero / NaN / missing tick` 是否被正確拒絕或明確標示略過
  - workflow `flat sequence` 是否仍不會誤觸發
  - `scripts/trading/validation/trading_workflow_template_validation.py` 是否仍包含 workflow `flat sequence` guard，且不再用過時 replay oracle 誤判 graph workflow
  - workflow `stop_loss_percent` 是否使用 scan window low、`take_profit_percent`
    是否使用 scan window high，且目前只標示 long-only 語義
  - `100 -> 10 -> 150` 類 jump / gap collapse 是否有風險警示或 filter，不會製造不真實回測幻覺
  - `full tick [100,80,120]` 與 `sampled [100,120]` 是否仍會出現 stop-loss / liquidation 漏觸發
  - `wallet=0 + trial_credit_only` 與小本金利息案例是否仍維持正確帳務
  - `scripts/trading/probes/backtest_20000_probe.py` 的 Grid 20k case、single-candle reject、outlier skip、flat Bollinger guard 是否都與目前引擎一致
  - Grid Bot 建立時是否凍結底倉、掛單用 POINTS / 現貨與預留手續費，使用者不能手動賣掉 bot 正在使用的底倉
  - Grid Bot 停止時是否詢問保留底倉或賣出，並正確釋放未用資金與結算費用
  - Workflow bot 預算增減是否只釋放 free budget，不可低於已凍結、待成交、未結費用與風控保留
  - root `run-once` 是否回 202 enqueue，不可把重報表或交易 job 放回 root request 同步執行
  - root sitewide report / 交易所基金與借貸流動性 / 全用戶倉位是否讀 snapshot；沒有 snapshot 時回 503，而不是現場重算到卡住
  - 詳細清單見 `docs/AGENTS/TRADING_QA_REGRESSION_MATRIX.md`
- 若本次改到站點外觀 / 個人外觀，至少補：
  - root 改全站預設後，未登入與一般使用者是否都先看到新預設
  - 一般使用者儲存個人外觀後，重新整理與重新登入是否仍會套用
  - 一般使用者按 `恢復全站預設` 後，是否立即預覽 root 的全站外觀，且按 `儲存` 後會真的清掉個人 appearance override
  - root 關閉 `允許使用者覆寫個人外觀` 後，使用者是否看到明確停用提示，而不是靜默失敗
  - 新增的字體風格、背景風格、面板風格、側邊欄寬度在桌面與手機版是否都沒有把按鈕、訊息或側邊欄擠壞
- 檢查腳本重疊是否已有清楚定位，而不是兩份文件各寫一套不同說法

## 相關文件連結

- [AGENTS/QA_MISSION_FOR_AGENTS.md](AGENTS/QA_MISSION_FOR_AGENTS.md)
- [AGENTS/TRADING_QA_REGRESSION_MATRIX.md](AGENTS/TRADING_QA_REGRESSION_MATRIX.md)
- [security/FUNCTIONAL_SMOKE.md](security/FUNCTIONAL_SMOKE.md)
- [security/PENTEST.md](security/PENTEST.md)
- [security/FUNCTIONAL_PERMISSION_PENTEST.md](security/FUNCTIONAL_PERMISSION_PENTEST.md)
- [security/TRADING_STRESS_PENTEST.md](security/TRADING_STRESS_PENTEST.md)
- [security/PRE_RELEASE_CHECKLIST.md](security/PRE_RELEASE_CHECKLIST.md)
- [security/PRODUCTION_SIGNOFF_CHECKLIST.md](security/PRODUCTION_SIGNOFF_CHECKLIST.md)
