# Database Layout

本文件描述 **目前實作中** 的資料庫切分，而不是未來理想狀態。

## 現況總覽

目前 runtime 內有 5 類主要 DB / DB-like 持久化：

1. `runtime/database/database.db`
2. `runtime/database/auth.db`
3. `runtime/database/audit.db`
4. `runtime/database/control.db`
5. `runtime/games/models/chess_experiment.db`

## 2026-05-16 DB lock hardening

目前 SQLite 仍是單機部署的主要資料庫，因此這輪先處理兩個最容易造成
`database is locked` / 500 的來源：

- 所有主要 runtime DB helper 改走 hardened SQLite connection：啟用 WAL、
  `busy_timeout`、同一 process 內依 DB path 序列化寫入，並對 transient lock
  做短 retry。runtime PRAGMA 也加入 `cache_size`、`mmap_size`、
  `journal_size_limit`、`wal_autocheckpoint`，讓小型單機部署可依硬體調整。
- 主要 runtime DB helper 另提供 readonly connection。列表、狀態頁與
  session 查詢應優先使用 readonly helper，只有實際更新資料時才開寫入連線。
- Job Center 的高頻 progress update 不再每次都直接寫 DB。`running`
  進度會先寫到 progress backend，依固定間隔回寫 DB；`succeeded`、`failed`、
  `cancelled`、`expired` 與 audit/notification 仍即時落 DB。
- Auth session `last_seen` 不再每次 request 都刷新。預設採間隔與 jitter
  合併寫入，避免大量登入用戶同時輪詢時把 `auth.db` 寫入 queue 打滿。

Progress backend 目前由 `services/core/progress_backend.py` 提供介面：

| Backend | 用途 |
|---|---|
| `memory` | 單 process 開發與測試用 |
| `file` | 無 Redis 時的跨 process 最新進度快取，預設放在 `runtime/job_progress_cache` |
| `redis` | 未來正式 queue / 多 worker 的共享進度快取 |
| `auto` | 有 Redis URL 時用 Redis，否則用 file backend |

Server 啟動時預設 `HACKME_JOB_PROGRESS_BACKEND=auto`。Redis 不是必備依賴；
未安裝或未設定時，HLS worker、ComfyUI、BT/direct link、resumable upload 等
長任務仍可使用 file backend 同步最新進度，DB 只保存 checkpoint / final state。

可調整的環境變數：

- `HACKME_SQLITE_BUSY_TIMEOUT_MS`
- `HACKME_SQLITE_LOCK_RETRY_ATTEMPTS`
- `HACKME_SQLITE_LOCK_RETRY_BASE_SLEEP`
- `HACKME_SQLITE_CACHE_SIZE_KB`
- `HACKME_SQLITE_MMAP_SIZE_BYTES`
- `HACKME_SQLITE_JOURNAL_SIZE_LIMIT_BYTES`
- `HACKME_SQLITE_WAL_AUTOCHECKPOINT_PAGES`
- `HACKME_JOB_PROGRESS_BACKEND`
- `HACKME_REDIS_URL` / `REDIS_URL`
- `HACKME_PROGRESS_CACHE_DIR`
- `HACKME_JOB_PROGRESS_FLUSH_INTERVAL_SECONDS`
- `HACKME_JOB_PROGRESS_EVENT_FLUSH_INTERVAL_SECONDS`
- `HTML_LEARNING_SESSION_LAST_SEEN_REFRESH_INTERVAL`
- `HTML_LEARNING_SESSION_LAST_SEEN_REFRESH_JITTER_SECONDS`

## 1. `runtime/database/database.db`

主業務 DB，負責大多數網站功能。

目前放在這裡的資料大致包含：

- `users`
- `user_passwords`
- 一般站台設定與 feature flags
- 論壇 / 聊天 / 通知 / 申覆
- Cloud Drive / 影片 / 分享 / E2EE metadata
- ComfyUI workflow / 一般 AI 功能資料
- Trading / Points / Snapshot / Server Mode 等尚未拆出的主資料

### 角色

- 預設主資料庫
- 目前仍承載最多業務表
- 尚未拆出的模組都在這裡

### 風險

- 容易成為 SQLite 寫入熱點
- 若背景 worker、交易、snapshot、server mode、聊天室同時寫入，最容易互鎖

## 2. `runtime/database/auth.db`

Auth hot-path 專用 DB。

目前已拆出的表：

- `csrf_tokens`
- `captcha_challenges`
- `login_attempts`
- `sessions`

### 角色

- 專門承接登入 / session / CSRF / captcha 這些高頻、短生命週期資料
- 降低登入與 `/api/me` 被主業務 DB 鎖住的機率

### 為什麼先拆它

這些資料具備共同特徵：

- 高寫入頻率
- 生命周期短
- 和主業務內容弱耦合
- 一旦被鎖，使用者會直接感知成「登入失敗 / 500 / 沒反應」

### 目前已搬走的熱路徑

- `/api/csrf-token`
- `/api/login`
- `/api/logout`
- `/api/session/idle-timeout`
- `/api/account/sessions*`
- captcha challenge / verify

### 2026-05-16 session read/write split

`db_get_user_from_token()` 現在優先用 readonly auth connection 讀取 session。
只有遇到 security epoch 旋轉、strict IP binding 撤銷、idle timeout 撤銷或
`last_seen` 到達刷新間隔時，才短暫開啟寫入連線。

帳號安全頁與 root/manager 用戶列表需要讀 session 狀態時，也應優先走
readonly auth helper。這個做法不能消除 SQLite 單寫入者限制，但可以降低
普通 GET / 狀態頁 / 後台列表對寫入 queue 的壓力。

## 3. `runtime/database/audit.db`

Audit 專用 DB。

目前已拆出的主表：

- `secure_audit`

### 角色

- 保存安全稽核鏈
- 避免一般業務寫入拖慢 audit append
- 降低 root 後台 / 安全中心 / 審計鏈查詢與主業務熱寫入互鎖

### 資料生命週期

- 長期保留
- 屬於 append-heavy / read-forensic 的資料
- 不應與短生命週期 session/captcha 混在一起

## 4. `runtime/database/control.db`

Control-plane 專用 DB。

目前已拆出的主表：

- `server_modes`
- `server_checkpoints`
- `mode_switch_logs`
- `security_keys`
- `production_entry_reports`
- `incident_reports`
- `security_profiles`

### 角色

- 保存 server mode / production gate / incident / security key 這類高風險控制面資料
- 降低 root 後台、launch-check、production gate 與主業務 DB 熱寫入互鎖
- 保持模式切換、production report 驗證、incident lock 這類控制流與一般論壇/聊天/交易寫入解耦

### 為什麼現在要拆它

這些資料具備共同特徵：

- 高風險控制面
- rollback 邊界和一般業務資料不同
- live root 後台會頻繁讀取
- 一旦被鎖，使用者會直接感知成「上線前檢查讀取失敗 / 模式切換 500 / 無法進 production」

### 注意

- `control.db` 目前是 production gate 與 server mode 的權威來源
- 為了相容舊讀取路徑，部分 mode state 仍會 best-effort mirror 回 `database.db`
- 實際判定應以 `control.db` 內容為準
- `control.db` 的 schema ensure 現在在啟動期與首次開檔時完成，不應再在
  `production gate` / `server mode` request path 內反覆做 DDL；這是 2026-05-09
  live 驗證用來消除 `launch-check` / `production enter` 自鎖的重要修正

## 5. `runtime/games/models/chess_experiment.db`

西洋棋 `exp1` 的學習 DB。

### 角色

- 不是整站通用 DB
- 只服務西洋棋 `experiment` / `exp1`
- 保存搜尋器的 learning bias / 經驗記憶

### 特性

- 與主網站登入、交易、論壇完全不同生命週期
- 應視為遊戲模型資產，而不是網站主資料

## 為什麼沒有一次拆很多 DB

目前採取的是 **風險導向、逐步拆分**：

先拆：

- 最常互鎖
- rollback 邊界清楚
- 對使用者體感影響最大的熱區

所以順序先是：

1. `audit.db`
2. `auth.db`
3. `control.db`

而不是一開始就把所有模組一次拆散。

## Schema Ownership Rule

在下一輪實體 DB split 以前，先採用 **schema ownership 切分**：

- 單一表只能由一個 domain schema module 建立與遷移。
- `services/server/database.py` 只能 orchestration，不應直接放 domain DDL。
- 新表必須登錄到所屬 schema module；跨 domain 讀寫要經過 service API。
- migration / ensure schema 必須可重複執行，且 fresh DB 與 old DB path 都要測。

目前主要 owner：

| Domain | Schema owner | 主要 DB |
|---|---|---|
| auth/session/csrf/captcha | `services/users/*` + auth DB bootstrap | `auth.db` |
| audit chain | platform audit bootstrap | `audit.db` |
| server mode / production gate / incident | `services/snapshots/schema.py` | `control.db` |
| snapshot metadata | `services/snapshots/schema.py` | `database.db` |
| PointsChain / wallet / ledger | `services/points_chain/schema.py`, `services/points_chain/economy_layer.py` | `database.db` |
| trading | `services/trading/schema_ddl.py`, `services/trading/settings_schema.py` | `database.db` |
| upload security | `services/security/upload_schema.py` | `database.db` |

目前不建議把 trading / points 立刻拆成多個實體 DB，因為 snapshot、
rollback、production gate、PointsChain block sealing、trading settlement
仍需要同一份 runtime 狀態的一致備份。下一步應先把 schema ownership 和
service ownership 固定，再評估是否把高寫入熱區拆成新的 DB。

## 下一個最值得拆的熱區

依目前觀察，下一個最值得拆的是 **trading / points / storage-media / forum-social** 這類仍在主 DB 內的熱區。

其中優先順序大致是：

1. trading / liquidation / bots
2. points ledger / chain
3. storage / media / share metadata
4. forum / social / notification

原因：

- 這些模組仍和主 DB 其他熱寫入共用同一把 SQLite writer lock
- 其中 trading / points 對安全與金流邊界影響最高
- storage / media / social 則較偏容量與 I/O 壓力問題

## 不要誤解成已經完全拆完

目前 **還沒有** 完成 trading / pointschain / forum / storage / AI workflow 的全面獨立 DB。

目前只是先把最常造成體感故障與控制面互鎖的三塊熱區拆開：

- `auth`
- `audit`
- `control`

其餘 DB split 仍會依：

- 功能耦合
- 風險等級
- rollback 邊界
- 資料生命週期

逐步往下做。

## 2026-05-09 live production gate 驗證結果

這次 `server mode / production gate` live 驗證已確認：

- `auth.db` 與 `control.db` 拆分後，`/api/csrf-token`、`/api/login`、
  `/api/me`、`/api/root/server-mode/requirements`、`/api/root/production/enter`
  可在隔離 `/tmp` runtime 下連續執行，不再因 request-path schema ensure 而自鎖。
- `control.db` 內的 verified production reports 才是 gate 權威來源；
  filesystem auto-detect 只輔助顯示，不可單獨放行。
- verified 但 `target_commit=old/fake` 的 required report set，live server 會回
  `target_commit_mismatch` 並拒絕進入 production。
- 只有完整 required report set verified 且 `target_commit / target_branch / server_mode` 全匹配時，
  live `production enter` 才會成功。

完整流程與證據請看：

- [docs/server_mode_v2/03_production_gate_playbook.md](../server_mode_v2/03_production_gate_playbook.md)
- [docs/server_mode_v2/04_production_gate_validation_report.md](../server_mode_v2/04_production_gate_validation_report.md)
