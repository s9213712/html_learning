# QA Mission For Agents

先讀 [11_QA_TESTING.md](../11_QA_TESTING.md) 了解各測試腳本與 QA 層級關係；本文件是
agent / 深度人工 QA 的完整 runbook。

你是一位專業 QA / 測試工程師，負責 `hackme_web/` 的完整測試、缺陷分析、證據整理、重複 issue 去重，以及必要時建立 GitHub issues。

這份文件不是「理念宣言」，而是可直接照抄執行的 QA runbook。目標是讓 agent 能快速建立功能地圖、在不污染原 repo runtime 的前提下啟動隔離測試環境，並用可重跑的命令做到高覆蓋、可驗證、可追溯的 QA。

## 絕對限制

1. 不直接修改正式源碼。
2. 不在原專案目錄留下 runtime、DB、log、cache、報告、測試產物。
3. 測試必須在 `/tmp/hackme_web_qa_*` 下的隔離環境進行。
4. 不可占用、重啟、污染開發中的 `5000` port。
5. 測試伺服器必須綁 `127.0.0.1`，且用自動偵測的空閒 port。
6. 所有測試資料、DB、storage、logs、reports 都必須指向 `/tmp`。
7. 找到 bug 後先查是否已有 issue，避免重複建立。
8. 不可只看 automated tests 通過就宣稱功能正確，必須做人工作業流程與邏輯驗算。
9. 不可只測 happy path，必須測邊界、錯誤、惡意輸入、權限不足、狀態切換、snapshot/restore 後狀態。
10. 每一階段都必須用中文回報進度、風險、阻塞點、發現。
11. 除非任務明確要求只做 triage，否則發現可直接修正的問題時，應直接修正並重新驗證，不可只留 issue。

## QA 核心原則

1. UI、API、DB 三方必須對帳。
2. 所有數字功能必須手算驗證。
3. 權限功能必須逐角色測試。
4. 模式切換、restore、reset、風險鎖定、rollback 必須測前後狀態。
5. 若產品方向不合理、不可維護、不可驗證，也要建立 design issue。

## 人工 QA 強制原則

以下原則適用所有功能，尤其是交易、積分、金額、時間與權限系統。

1. 禁止完全依賴腳本測試。
2. 每個高風險功能都必須有人工作業流程測試。
3. 測試必須模擬真實使用者逐步操作，而不是只打 API。
4. 每條主流程至少要覆蓋正常流程、錯誤流程、邊界條件、中途取消 / 重複點擊 / 重新整理、未登入 / 權限不足。
5. 每一步都要對帳 UI 顯示、API 回應與實際 DB / state 變化。

## 異常輸入強制矩陣

所有涉及數值、時間、交易、點數、配額、批次或下載任務的功能，至少要測以下輸入：

- `0`
- `0.0`
- `0.00000001`
- 非常大的數字
- 負數
- 純字串，例如 `abc`
- 混合字串，例如 `100abc`
- 科學記號，例如 `1e-8`
- 超過精度限制的小數位
- `0 秒` / `0 天`
- 非法時間格式
- 過去時間 / 極大未來時間

必須確認：

1. 系統會阻擋或合理處理。
2. 不會 crash。
3. 不會產生錯誤計算。
4. 不會寫入錯誤資料。

## 金額 / 積分 / 交易精度驗證

所有涉及金額、點數、利息、收益、手續費、撮合、槓桿、報酬、排名的功能，至少要驗證：

1. 小數點不會被截斷成 `0`。
2. 不會出現 precision loss。
3. 前端顯示與後端實際值一致，或差異可被明確解釋。
4. 小數位數、捨入規則、最小單位有清楚定義。
5. 關鍵運算不得使用 `float`。

必測案例：

1. 極小額交易。
2. 多次累加，檢查誤差累積。
3. 買入 + 賣出循環，確認資產不會憑空增加或消失。

## 時間 / 週期邏輯驗證

所有利息、收益、冷卻時間、定投、排程、封鎖、快照、到期、下載輪詢都必須驗證：

1. 前台顯示時間與後端計算一致。
2. 時間流逝有正確反映，不是固定值。
3. 重新整理頁面後仍正確。
4. server restart 後仍正確。
5. 不會出現計息停住、倍數計算、跳秒錯誤。

建議測法：

1. 盡量把測試週期縮短到秒級。
2. 連續觀察多次更新。
3. 用手算或 SQL 對照預期數學結果。

## 阻斷 / 濫用 / 提權測試要求

每輪至少要有一組：

1. 提權測試：匿名、一般用戶、manager、root 交叉測同一 API / UI。
2. 阻斷測試：rate limit、重複送單、重複按鈕、重複下載、壓力請求、CSRF 缺失、舊 session 重放。
3. 資料一致性測試：UI 顯示成功但 DB 未寫入、DB 已寫入但 UI 提示失敗、重試造成重複扣款 / 重複建立。
4. race condition 測試：多人同時操作同一資源，例如交易、點數調整、quota、share link、snapshot。

## 與全專案工作原則對齊

QA 不只要找 bug，還要檢查功能是否符合
[RULES_FOR_AGENTS.md](RULES_FOR_AGENTS.md)。

每次驗收至少要確認：

1. 相關 README、使用教學、管理員文件、開發者文件是否同步更新。
2. 測試是否覆蓋正常流程、錯誤流程、權限不足流程、邊界值流程。
3. 前端失敗提示是否清楚說明「發生什麼事、為什麼可能失敗、下一步怎麼做」。
4. 後端是否保留可供管理員 / root 查核的 log 或 audit 線索。
5. 新增 UI 是否檢查過手機版，不只桌面版可用。
6. 金額、積分、交易、排名、權限、安全判斷等可信運算是否仍在伺服器端。
7. 最終 QA 回報是否交代文件、測試、錯誤回饋、手機版、伺服器端驗證與未完成項。

## 專案功能地圖

先理解地圖，再跑測試。這能顯著降低遺漏。

| 領域 | 主要 UI / 頁面 | 主要 route modules | 主要 service modules | 高價值 tests | 主要文件 |
|---|---|---|---|---|---|
| 認證 / 帳號 / 權限 | `public/js/40-auth-users.js`, `public/js/10-users.js` | `routes/public.py`, `routes/users.py`, `routes/appeals.py` | `services/users/auth.py`, `services/users/recovery.py`, `services/security/password_strength.py`, `services/security/permissions.py`, `services/security/identity.py`, `services/governance/sanction_notices.py`, `services/governance/violations.py` | `tests/security/auth/test_auth_csrf_safe.py`, `tests/account/auth/test_account_lockout.py`, `tests/account/recovery/test_account_recovery.py` | `README.md`, `WEB.md`, `For_developer.md` |
| 社群 / 討論區 / 檢舉 / 通知 | `public/js/20-chat.js`, `public/js/25-community.js`, `public/js/30-appeals.js`, `public/js/32-notifications.js` | `routes/chat.py`, `routes/community.py`, `routes/moderation.py`, `routes/reports_notifications.py`, `routes/bug_reports.py` | `services/chat/support.py`, `services/governance/moderation.py`, `services/system/notifications.py`, `services/governance/records.py` | `tests/community/test_chat_permissions.py`, `tests/community/test_community_permissions.py`, `tests/regressions/test_bug_reports.py` | `WEB.md`, `For_developer.md` |
| Cloud Drive / 上傳 / 相簿 / 下載器 | `public/js/35-drive.js` | `routes/files.py` | `services/storage/cloud_drive.py`, `services/media/previews.py`, `services/security/upload_security.py`, `services/storage/storage_albums.py`, `services/storage/paths.py`, `services/storage/maintenance.py`, `services/storage/capacity_audit.py`, `services/storage/quota_*.py`, `services/storage/remote_downloads.py` | `tests/storage/test_upload_security.py`, `tests/storage/test_cloud_drive_attachments.py`, `tests/storage/test_remote_downloads.py`, `tests/frontend/storage/test_frontend_drive_preview.py` | `WEB.md`, `For_developer.md`, `VIDEO_PLATFORM.md` |
| 影片分享平台 | `public/js/39-videos.js`, `public/js/32-notifications.js` | `routes/videos.py` | `services/media/videos.py`, `services/storage/cloud_drive.py`, `services/points_chain/service.py` | `tests/video/api/test_video_publish.py`, `tests/video/api/test_video_permission.py`, `tests/video/security/test_video_security.py` | `VIDEO_PLATFORM.md`, `WEB.md`, `For_developer.md` |
| PointsChain / Economy / 健康度 | `public/js/50-admin.js`, `public/js/55-economy.js` | `routes/economy.py`, `routes/system_admin.py` | `services/points_chain/service.py`, `services/security/events.py`, `services/system/integrity_guard.py`, `services/platform/settings.py` | `tests/points/test_points_chain.py`, `tests/security/integrity/test_health_center.py`, `tests/security/integrity/test_integrity_guard.py`, `tests/frontend/trading/test_frontend_economy.py` | `WEB.md`, `For_developer.md`, `docs/security/*.md` |
| 交易 / Bot / Workflow / BTC_trade | `public/js/56-trading.js`, `public/js/trading-workflow-editor.js` | `routes/trading.py`, `routes/economy.py` | `services/trading/engine.py`（相容入口：`trading_engine.py`）、`services/trading/btc_bridge.py`, `services/points_chain/service.py` | `tests/trading/core/test_trading_engine.py`, `tests/trading/pricing/test_trading_reference_prices.py`, `tests/trading/workflow/test_trading_workflow_editor_ui.py` | `TRADING.md`, `WEB.md`, `docs/security/TRADING_STRESS_PENTEST.md`, `TRADING_QA_REGRESSION_MATRIX.md` |
| ComfyUI 整合 | `public/js/36-comfyui.js` | `routes/comfyui.py` | `services/comfyui/client.py`, `services/storage/cloud_drive.py` | `tests/comfyui/generation/test_comfyui_generation.py`, `tests/comfyui/workflows/test_comfyui_workflows.py`, `tests/comfyui/storage/test_comfyui_storage.py` | `WEB.md`, `For_developer.md` |
| Server Mode / Security Center / Access Controls | `public/js/50-admin.js`, `public/js/90-bootstrap.js` | `routes/system_admin.py`, `routes/system_admin_sections/`, `routes/public.py` | `services/security/access_controls.py`, `services/system/audit.py`, `services/security/events.py`, `services/system/integrity_guard.py`, `services/server/bind.py`, `services/platform/settings.py` | `tests/security/gates/test_production_gate_enforcement.py`, `tests/frontend/admin/test_frontend_security_center_layout.py`, `tests/security/integrity/test_integrity_guard.py` | `SERVER_MODE_V2_*.md`, `docs/security/*.md`, `For_developer.md` |
| Snapshot / Restore / Reset / Bootstrap | root 管理頁 | `routes/operations.py`, `routes/system_admin.py` | `services/snapshots/schema.py`, `services/platform/bootstrap.py`, `services/points_chain/service.py` | `tests/snapshots/test_snapshots.py`, `tests/platform/test_release_policy.py` | `RUNTIME_RESET_AND_RECOVERY.md`, `For_developer.md`, `PRE_RELEASE_CHECKLIST.md` |
| 遊戲 / 其他附屬功能 | 遊戲頁 | `routes/games.py` | `services/games/chess.py` | `tests/games/test_games.py`, `tests/frontend/games/test_frontend_games.py` | `WEB.md` |

## 封存 / 非現行功能

不要把封存功能誤當成現行 regression fail。

- `WebTerminal` 目前是封存設計，不在現行 `routes/` / `public/js/` 主線。
- 舊設計文件已移出日常入口；需要追溯時查 Git history 與 `docs/archive/history/VERSION_STORY.md`。
- 若現行 release 沒宣稱它存在，就不要把「找不到 WebTerminal UI」直接當功能 bug。
- 只有當新 release 明確重新啟用 terminal 類功能時，才建立對應 QA 任務。

確認是否為封存功能的命令：

```bash
rg -n "webterminal|terminal" routes services public docs -g '!*.pyc'
```

## 交易 QA 固定回歸矩陣

只要這輪 QA 有碰到：

- `routes/trading.py`
- `services/trading/trading_engine.py`
- `public/js/56-trading.js`
- `public/js/trading-workflow-editor.js`
- price-source / scheduler / liquidation / DCA / grid / backtest

就不能只跑一般 smoke / pytest。必須另外套用：

- [TRADING_QA_REGRESSION_MATRIX.md](TRADING_QA_REGRESSION_MATRIX.md)

最低要求：

1. `empty / single / invalid / over-limit candles` reject-path
2. workflow `flat sequence` 與 `no-trigger` 語義驗證
3. `jump / gap collapse / sampled vs full tick` 對抗序列
4. 小本金利息、`trial_credit-only`、negative balance invariant
5. 真實歷史區間與 synthetic 對抗序列雙線並跑

## 先建立功能地圖

進入 repo 後，先把 routes / services / tests / docs 清單輸出成報告。不要憑印象測。

```bash
export REPO_ROOT="$(pwd)"
export QA_MAP_DIR="/tmp/hackme_web_qa_map_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$QA_MAP_DIR"

find routes -maxdepth 1 -type f | sort | tee "$QA_MAP_DIR/routes.txt"
find services -maxdepth 1 -type f | sort | tee "$QA_MAP_DIR/services.txt"
find tests -maxdepth 1 -type f -name 'test_*.py' | sort | tee "$QA_MAP_DIR/tests.txt"
find docs -maxdepth 2 -type f | sort | tee "$QA_MAP_DIR/docs.txt"

rg -n '@app\.route' routes | sort | tee "$QA_MAP_DIR/route_inventory.txt"
rg -n 'apiFetch\(|fetch\(' public/js | sort | tee "$QA_MAP_DIR/frontend_api_inventory.txt"
rg -n 'def test_' tests | sort | tee "$QA_MAP_DIR/test_inventory.txt"
```

## Issue 去重命令

如果 `gh auth status` 正常，先查 issue。查不到再建。

```bash
gh auth status
gh issue list --limit 200 --search 'is:issue is:open sort:updated-desc'
gh issue list --limit 200 --search '"api/videos" in:title,body'
gh issue list --limit 200 --search '"decrypt_unavailable" in:title,body'
gh issue list --limit 200 --search '"trading bot" OR "grid bot" OR "PointsChain"'
```

若 `gh` 未登入，至少輸出候選 issue markdown 到 `/tmp`，不要直接跳過去重。

## 建立隔離 QA 工作區

這一段是標準起手式。目的：

1. 複製目前工作樹到 `/tmp`。
2. 保留 `.git` 供 diff / blame / issue 引用。
3. 不污染原 repo 的 runtime。
4. 自動挑空閒 port。

```bash
export REPO_ROOT="$(pwd)"
export RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
export QA_ROOT="/tmp/hackme_web_qa_${RUN_ID}"
export QA_REPO="$QA_ROOT/repo"
export QA_RUNTIME="$QA_ROOT/runtime"
export QA_REPORTS="$QA_ROOT/reports"
export QA_LOG="$QA_ROOT/server.out"
export QA_COOKIE_JAR="$QA_ROOT/cookies.txt"

pick_free_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

export QA_PORT="$(pick_free_port)"

mkdir -p "$QA_ROOT" "$QA_REPORTS"
git clone --no-hardlinks "$REPO_ROOT" "$QA_REPO"
rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.mypy_cache' \
  --exclude '.ruff_cache' \
  --exclude 'database' \
  --exclude 'logs' \
  --exclude 'chats' \
  --exclude 'anchors' \
  --exclude 'storage' \
  --exclude 'reports' \
  "$REPO_ROOT/" "$QA_REPO/"

cd "$QA_REPO"
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-minimal.txt -r requirements-dev.txt
```

## 啟動隔離伺服器

伺服器一定要指向 `/tmp` runtime，且不能使用 `5000`。

```bash
mkdir -p "$QA_RUNTIME"/{database,logs,chats,anchors,storage,reports}

qa_generate_password() {
  python3 -c 'import secrets; print("Qa-" + secrets.token_urlsafe(18) + "-Aa1!")'
}

export HTML_LEARNING_HOST=127.0.0.1
export HTML_LEARNING_PORT="$QA_PORT"
export HTML_LEARNING_DB_DIR="$QA_RUNTIME/database"
export HTML_LEARNING_LOG_DIR="$QA_RUNTIME/logs"
export HTML_LEARNING_CHAT_DIR="$QA_RUNTIME/chats"
export HTML_LEARNING_ANCHOR_DIR="$QA_RUNTIME/anchors"
export HTML_LEARNING_STORAGE_DIR="$QA_RUNTIME/storage"
export HTML_LEARNING_REPORTS_DIR="$QA_RUNTIME/reports"
export HTML_LEARNING_ROOT_PASSWORD="${HTML_LEARNING_ROOT_PASSWORD:-$(qa_generate_password)}"
export HTML_LEARNING_MANAGER_PASSWORD="${HTML_LEARNING_MANAGER_PASSWORD:-$(qa_generate_password)}"
export HTML_LEARNING_TEST_PASSWORD="${HTML_LEARNING_TEST_PASSWORD:-$(qa_generate_password)}"
export ROOT_PASSWORD="$HTML_LEARNING_ROOT_PASSWORD"
export MANAGER_PASSWORD="$HTML_LEARNING_MANAGER_PASSWORD"
export TEST_PASSWORD="$HTML_LEARNING_TEST_PASSWORD"
export PLAYWRIGHT_ROOT_PASSWORD="$HTML_LEARNING_ROOT_PASSWORD"
export PLAYWRIGHT_MANAGER_PASSWORD="$HTML_LEARNING_MANAGER_PASSWORD"
export PLAYWRIGHT_TEST_PASSWORD="$HTML_LEARNING_TEST_PASSWORD"
export HACKME_QA_ROOT_PASSWORD="$HTML_LEARNING_ROOT_PASSWORD"
export HACKME_QA_TEST_PASSWORD="$HTML_LEARNING_TEST_PASSWORD"
export PYTHONPATH="$QA_REPO"

PYTHONUNBUFFERED=1 python3 server.py >"$QA_LOG" 2>&1 &
echo $! > "$QA_ROOT/server.pid"
```

若啟動失敗，先用前景抓錯誤，不要盲跑後續測試：

```bash
timeout 30s python3 server.py
tail -n 80 "$QA_LOG"
```

## 自動偵測 HTTP / HTTPS base URL

`hackme_web` 可能用 HTTPS 起來。先探測再測。

```bash
unset QA_BASE_URL
for scheme in https http; do
  if curl -ksSf "${scheme}://127.0.0.1:${QA_PORT}/api/version" >/dev/null; then
    export QA_BASE_URL="${scheme}://127.0.0.1:${QA_PORT}"
    break
  fi
done

test -n "${QA_BASE_URL:-}"
echo "$QA_BASE_URL"
curl -ksS "$QA_BASE_URL/api/version"
curl -ksS -D - -o /dev/null "$QA_BASE_URL/"
```

## API 測試輔助函式

這些 helper 讓你用 cookie + CSRF 反覆測各角色。

```bash
qa_csrf() {
  curl -ksS -b "$QA_COOKIE_JAR" -c "$QA_COOKIE_JAR" \
    "$QA_BASE_URL/api/csrf-token"
}

qa_login() {
  local username="$1"
  local user_pass="$2"
  local csrf_value
  csrf_value="$(qa_csrf | python3 -c 'import sys, json; print(json.load(sys.stdin)["csrf_token"])')"
  QA_LOGIN_USERNAME="$username" QA_LOGIN_PASSWORD="$user_pass" \
  python3 -c 'import json, os; print(json.dumps({"username": os.environ["QA_LOGIN_USERNAME"], "password": os.environ["QA_LOGIN_PASSWORD"]}))' | \
  curl -ksS -b "$QA_COOKIE_JAR" -c "$QA_COOKIE_JAR" \
    -H "Content-Type: application/json" \
    -H "X-CSRF-Token: $csrf_value" \
    --data-binary @- \
    "$QA_BASE_URL/api/login"
}

qa_api() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  local csrf_value
  csrf_value="$(qa_csrf | python3 -c 'import sys, json; print(json.load(sys.stdin)["csrf_token"])')"
  if [ -n "$body" ]; then
    curl -ksS -b "$QA_COOKIE_JAR" -c "$QA_COOKIE_JAR" \
      -X "$method" \
      -H "Content-Type: application/json" \
      -H "X-CSRF-Token: $csrf_value" \
      -d "$body" \
      "$QA_BASE_URL$path"
  else
    curl -ksS -b "$QA_COOKIE_JAR" -c "$QA_COOKIE_JAR" \
      -X "$method" \
      -H "X-CSRF-Token: $csrf_value" \
      "$QA_BASE_URL$path"
  fi
}

rm -f "$QA_COOKIE_JAR"
qa_login root "$HTML_LEARNING_ROOT_PASSWORD"
qa_api GET /api/me
```

## DB 對帳命令

所有關鍵結果都要驗證 DB 狀態，而不是只看 API。

```bash
export QA_DB="$QA_RUNTIME/database/database.db"

sqlite3 "$QA_DB" '.tables'
sqlite3 "$QA_DB" "SELECT id, username, status, role FROM users ORDER BY id;"
sqlite3 "$QA_DB" "SELECT COUNT(*) AS audit_rows FROM secure_audit;"
sqlite3 "$QA_DB" "SELECT user_id, soft_balance, hard_balance, wallet_status, risk_level FROM points_wallets ORDER BY user_id;"
sqlite3 "$QA_DB" "SELECT order_uuid, user_id, market_symbol, side, status, frozen_points FROM trading_orders ORDER BY id DESC LIMIT 20;"
sqlite3 "$QA_DB" "SELECT id, owner_user_id, visibility, status, coin_total FROM videos ORDER BY id DESC LIMIT 20;"
sqlite3 "$QA_DB" "SELECT id, owner_user_id, privacy_mode, scan_status, risk_level, deleted_at FROM uploaded_files ORDER BY rowid DESC LIMIT 20;"
```

## 自動化 QA 命令矩陣

先快、再深、再領域化、再跨模組。

### 1. Repo 衛生與快速 gate

```bash
python3 scripts/prepush/pre_push_checks.py --clean --yes
python3 scripts/prepush/pre_push_checks.py --ci
python3 scripts/prepush/pre_push_checks.py --full --keep-temp
```

### 2. 全量 pytest

```bash
scripts/testing/pytest_in_tmp.sh -q tests
```

### 3. 分領域 pytest

#### 認證 / 帳號 / 權限

```bash
scripts/testing/pytest_in_tmp.sh -q \
  tests/security/auth/test_auth_csrf_safe.py \
  tests/security/auth/test_access_controls.py \
  tests/account/auth/test_account_lockout.py \
  tests/account/recovery/test_account_recovery.py \
  tests/account/sessions/test_account_sessions.py \
  tests/security/input/test_password_strength.py \
  tests/users/test_identity_schema.py \
  tests/security/auth/test_session_idle_timeout.py \
  tests/platform/test_feature_flags.py \
  tests/security/smoke/test_functional_permission_pentest.py
```

#### 社群 / 聊天 / 檢舉 / 通知

```bash
scripts/testing/pytest_in_tmp.sh -q \
  tests/community/test_chat_permissions.py \
  tests/community/test_dm_routes.py \
  tests/community/test_community_permissions.py \
  tests/community/test_moderation_proposals.py \
  tests/community/test_reports_notifications.py \
  tests/regressions/test_bug_reports.py \
  tests/users/test_member_level_rules.py
```

#### Cloud Drive / 上傳 / 相簿 / 影片 / ComfyUI

```bash
scripts/testing/pytest_in_tmp.sh -q \
  tests/storage/test_upload_security.py \
  tests/storage/test_cloud_drive_attachments.py \
  tests/storage/test_remote_downloads.py \
  tests/storage/test_storage_paths.py \
  tests/storage/test_storage_maintenance.py \
  tests/storage/test_storage_albums_schema.py \
  tests/frontend/storage/test_frontend_drive_preview.py \
  tests/video/api/test_video_publish.py \
  tests/video/api/test_video_permission.py \
  tests/video/api/test_video_comments.py \
  tests/video/api/test_video_tips.py \
  tests/video/security/test_video_security.py \
  tests/comfyui/generation/test_comfyui_generation.py \
  tests/comfyui/workflows/test_comfyui_workflows.py \
  tests/comfyui/settings/test_comfyui_settings.py \
  tests/comfyui/civitai/test_comfyui_civitai.py \
  tests/comfyui/storage/test_comfyui_storage.py
```

#### PointsChain / 健康中心 / Snapshot / 交易

```bash
scripts/testing/pytest_in_tmp.sh -q \
  tests/points/test_points_chain.py \
  tests/security/integrity/test_health_center.py \
  tests/security/integrity/test_integrity_guard.py \
  tests/security/integrity/test_integrity_repair.py \
  tests/security/gates/test_security_events.py \
  tests/platform/test_settings_audit_reseal.py \
  tests/snapshots/test_snapshots.py \
  tests/trading/core/test_trading_engine.py \
  tests/trading/pricing/test_trading_reference_prices.py \
  tests/regressions/test_security_issue_regressions.py
```

#### 前端 / 版面 / RWD / 互動回歸

```bash
scripts/testing/pytest_in_tmp.sh -q \
  tests/frontend \
  tests/trading/workflow/test_trading_workflow_editor_ui.py
```

### 4. 直接 smoke suite

這是最快的黑箱 API / session / CSRF / major-flow 檢查。

```bash
PYTHONPATH=. python3 tests/security/smoke/smoke_suite.py \
  --base-url "$QA_BASE_URL" \
  --suite all
```

可拆成功能或安全子集：

```bash
PYTHONPATH=. python3 tests/security/smoke/smoke_suite.py --base-url "$QA_BASE_URL" --suite functional
PYTHONPATH=. python3 tests/security/smoke/smoke_suite.py --base-url "$QA_BASE_URL" --suite security
```

### 5. Functional smoke script

這個腳本會自己再起一個獨立 runtime。請再挑一個新 port。

```bash
export SMOKE_PORT="$(pick_free_port)"

scripts/security/pentest/run_functional_smoke.sh \
  --port "$SMOKE_PORT" \
  --runtime "$QA_ROOT/functional_runtime" \
  --out "$QA_REPORTS"
```

### 6. Pentest runner

先看有哪些檢查：

```bash
scripts/security/pentest/run_pentest.sh --list-checks
```

#### 權限 / session / headers

```bash
PYTHONPATH=. scripts/security/pentest/run_pentest.sh \
  --target "$QA_BASE_URL" \
  --only functional-permissions,session-security,header-security
```

#### Server Mode v2 全套

```bash
PYTHONPATH=. scripts/security/pentest/run_pentest.sh \
  --target "$QA_BASE_URL" \
  --only server-mode-v2,server-mode-v2-adversarial,server-mode-v2-live-http,server-mode-v2-enterprise,server-mode-v2-redteam-l2
```

#### 影片模組

```bash
PYTHONPATH=. scripts/security/pentest/run_pentest.sh \
  --target "$QA_BASE_URL" \
  --only video-module
```

#### 整站 production gate

```bash
PYTHONPATH=. scripts/security/pentest/run_pentest.sh \
  --target "$QA_BASE_URL" \
  --only whole-site-production-gate
```

#### 本機快速安全掃描

```bash
PYTHONPATH=. scripts/security/pentest/run_pentest.sh \
  --target "$QA_BASE_URL" \
  --skip ffuf,gobuster,dirsearch,sqlmap,sslscan,testssl
```

### 7. 交易壓力 / 正確性 / 風險流程

```bash
PYTHONPATH=. python3 scripts/security/pentest/trading_stress_pentest.py \
  --base-url "$QA_BASE_URL" \
  --mode full \
  --users 3 \
  --orders-per-user 8 \
  --concurrency 4 \
  --rate 8
```

針對 restore / margin / permission probe 單跑：

```bash
PYTHONPATH=. python3 scripts/security/pentest/trading_stress_pentest.py --base-url "$QA_BASE_URL" --mode restore_consistency
PYTHONPATH=. python3 scripts/security/pentest/trading_stress_pentest.py --base-url "$QA_BASE_URL" --mode margin_liquidation
PYTHONPATH=. python3 scripts/security/pentest/trading_stress_pentest.py --base-url "$QA_BASE_URL" --mode permission_probe
```

### 8. HTTP 壓力基準

```bash
python3 security/stress_test.py \
  --target "$QA_BASE_URL" \
  --requests 500 \
  --concurrency 50 \
  --out "$QA_REPORTS"
```

## 手動頁面 / API / DB 對帳清單

## 手動逐步測試紀錄格式

每個功能測試報告至少要包含：

1. 測試功能 / 模組名稱。
2. 人工操作步驟。
3. 測試輸入，包含異常輸入。
4. 預期結果 vs 實際結果。
5. UI 顯示與 API 回應摘要。
6. DB / state 對帳結果。
7. 是否有精度問題。
8. 是否有時間 / 計息 / 週期錯誤。
9. 是否有 UI 誤導。
10. 發現的 bug 與重現步驟。
11. 是否已直接修正；若未修正，要寫明原因與風險。

### 認證 / session / CSRF

```bash
rm -f "$QA_COOKIE_JAR"
qa_login root "$HTML_LEARNING_ROOT_PASSWORD"
qa_api GET /api/me
qa_api POST /api/logout
curl -ksS "$QA_BASE_URL/api/me"
```

檢查：

- 未登入 `/api/me` 應為 `401`。
- 有 cookie 無 CSRF 的寫操作應拒絕。
- 首次密碼變更 / 密碼重設 / 登出後舊 session 是否失效。

### 管理員 / root / 權限邊界

```bash
rm -f "$QA_COOKIE_JAR"
qa_login root "$HTML_LEARNING_ROOT_PASSWORD"
qa_api GET /api/admin/users

rm -f "$QA_COOKIE_JAR"
qa_login admin "$HTML_LEARNING_MANAGER_PASSWORD"
qa_api GET /api/admin/users

rm -f "$QA_COOKIE_JAR"
qa_login test "$HTML_LEARNING_TEST_PASSWORD"
qa_api GET /api/admin/users
```

檢查：

- `root` / `manager` / `user` 對同一端點結果是否符合預期。
- UI 按鈕是否與實際 API 權限一致。
- 禁用功能 tile 是否只是 UI 隱藏，還是 API 真正封鎖。

### Snapshot / Restore / Reset

```bash
rm -f "$QA_COOKIE_JAR"
qa_login root "$HTML_LEARNING_ROOT_PASSWORD"
qa_api GET /api/root/server-mode
qa_api GET /api/root/production-report/status
qa_api GET /api/root/points/chain/verify
```

優先使用內建 automated commands：

```bash
PYTHONPATH=. scripts/security/pentest/run_pentest.sh --target "$QA_BASE_URL" --only server-mode-v2-live-http
PYTHONPATH=. python3 scripts/security/pentest/trading_stress_pentest.py --base-url "$QA_BASE_URL" --mode restore_consistency
scripts/testing/pytest_in_tmp.sh -q tests/snapshots/test_snapshots.py
```

### Cloud Drive / 相簿 / 影片

```bash
rm -f "$QA_COOKIE_JAR"
qa_login test "$HTML_LEARNING_TEST_PASSWORD"
qa_api GET /api/files/quota
qa_api GET /api/files/privacy-modes
qa_api GET /api/cloud-drive/files
qa_api GET /api/videos
```

檢查：

- owner / non-owner / manager / root 的 preview / download / share / publish 權限。
- `server_encrypted` 與 `e2ee` 模式 UI/實際能力是否一致。
- `decrypt_unavailable` 是否回傳結構化錯誤，而不是 500。

### 交易 / 點數 / Bot

```bash
rm -f "$QA_COOKIE_JAR"
qa_login root "$HTML_LEARNING_ROOT_PASSWORD"
qa_api GET /api/trading/markets
qa_api GET /api/trading/dashboard
qa_api GET /api/points/wallet
qa_api GET /api/points/ledger
```

檢查：

- 下單前後 `wallet`、`ledger`、`trading_orders`、`trading_fills` 是否一致。
- 手續費、盈虧、抽成、強平、凍結點數必須手算。
- DCA / Grid / Workflow bot 的 UI 顯示與 backend 狀態要一致。

## 數字類手算原則

以下全部不能只信 API：

1. 點數餘額
2. 手續費
3. 撮合後剩餘凍結金額
4. 現貨成本 / 已實現盈虧 / 未實現盈虧
5. 槓桿 / 維持率 / 強平價
6. 影片投幣分潤 / 平台抽成
7. 商城 / 官方頭銜 / 權益購買後餘額與審計紀錄

手算證據要寫進 issue 或測試報告。

## 直接修正模式

若本輪任務要求「不要先開 issue、先直接修」，請遵守以下規則：

1. 先留下重現步驟、輸入、回應、DB 證據。
2. 直接修正可確認且範圍清楚的問題。
3. 修正後必須重跑受影響測試與至少一條人工流程。
4. 報告中要標明每個問題是：
   - 已修正並驗證
   - 已修正但仍需更大範圍驗證
   - 尚未修正
5. 只有在問題需要較大設計決策、跨版本拆解或目前無法安全修正時，才改為風險紀錄。

## Issue 建立模板

建立 issue 前先去重。去重完成後，先寫 markdown，再用 `gh` 建。

```bash
cat > "$QA_REPORTS/issue_template.md" <<'EOF'
## 摘要

- 類型：
- 嚴重程度：
- 影響範圍：

## 重現步驟

1.
2.
3.

## 預期結果

-

## 實際結果

-

## 證據

- API response 摘要：
- screenshot：
- sanitized log：
- DB 對帳：
- 手算過程：

## 建議修復方向

-

## 去重確認

- 已搜尋：
- 非重複原因：
EOF
```

建立 issue：

```bash
gh issue create \
  --title "[QA] <簡短標題>" \
  --body-file "$QA_REPORTS/issue_template.md" \
  --label bug
```

## 最終輸出要求

每次 QA 結束必須輸出：

1. 測試總報告
2. 測了哪些功能
3. 做了哪些人工操作、異常輸入、提權、阻斷與 race-condition 測試
4. 每個發現的問題是否已修正，修正後驗證結果是什麼
5. 未修正但值得追蹤的風險
6. 無法測試的項目與原因
7. 下一輪測試重點
8. 失敗命令、log 路徑、報告路徑

## 清理命令

測完後清理，不要殘留背景伺服器。

```bash
if [ -f "$QA_ROOT/server.pid" ]; then
  kill "$(cat "$QA_ROOT/server.pid")" || true
fi

rm -rf "$QA_ROOT"
```

若需要保留證據，刪除前先打包：

```bash
tar -C /tmp -czf "/tmp/hackme_web_qa_${RUN_ID}.tar.gz" "$(basename "$QA_ROOT")"
```

## QA 成功標準

以下全部成立，才可宣稱這輪 QA 完成：

1. 功能地圖已建立並覆蓋主要模組。
2. 至少跑過 quick gate、全量 pytest、smoke suite、functional smoke。
3. 權限、安全、Server Mode、snapshot/restore、PointsChain、交易、Cloud Drive、影片、ComfyUI 至少各有一輪 automated 或 manual 驗證。
4. 至少完成一次 UI / API / DB 三方對帳。
5. 所有 bug 都已整理證據，且已直接修正或明確記錄未修正原因。
6. 所有回報皆為中文，且附命令、路徑、結果摘要。
