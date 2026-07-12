# 09 Snapshot Reset Restore

一句話說明：這份文件專門說明 snapshot、portable restore、runtime reset、PointsChain recovery 的邊界與實際操作觀念。

## 設計目的

部署者最容易混淆的就是：

- server snapshot restore
- PointsChain safe-mode / forensic / branch recovery
- runtime reset
- server mode checkpoint / rollback

這幾個都像「恢復」，但責任完全不同。這份文件就是把這些責任邊界拉清楚。

## 使用方法

### 你要整站回滾時

用 server snapshot restore。

### 你只要修復經濟鏈時

先看 PointsChain recovery，不要先動全站 restore。

### 你要把站點清回最小可跑狀態時

用 runtime reset，但先理解它是 destructive cleanup。

### 你在做模式切換或高風險測試時

看 server mode checkpoint / superweak rollback。

## RC1 備份範圍

- server snapshot：保護主要站台 DB、split runtime DB、runtime file roots、config archive。
- 主要 DB：`runtime/database/database.db`，仍保留 legacy `db.sqlite3.backup` 檔名，讓舊 snapshot 匯入/驗證可相容。
- split runtime DB：`auth.db`、`audit.db`、`control.db`、`storage_catalog.db`、`points_chain.db`、`trading.db`、`finance.db`、`jobs.db`、`chess_experiment.db` 會備份到 `databases/*.sqlite3.backup`。其中 `finance`、`points_chain`、`trading` 只作 forensic archive，metadata 會標成 `forensic_archive_only_append_only_recovery`，restore 明確略過；其他 DB 才依本機 label 還原。
- runtime file roots：`runtime/chats`、`runtime/storage`，以及 PointsChain forensic bundle 目錄。
- 新增功能資料面：
  - AI Agent 對話、工具執行稽核、Job Center 任務狀態：在 runtime DB / jobs DB 中，由 database backup 覆蓋。
  - ComfyUI 產圖歷史、工作流 preset/run、圖檔 reference：DB 由 database backup 覆蓋；實際圖檔若落在 `runtime/storage` 則由 file root archive 覆蓋。
  - BT/direct download、相簿、雲端分享、E2EE key reference：DB 由 database backup 覆蓋；payload 檔案由 `runtime/storage` archive 覆蓋。
  - HLS / realtime media stream metadata、字幕、變體：DB 由 database backup 覆蓋；segment/asset 檔案若落在 `runtime/storage` 則由 file root archive 覆蓋。
  - 交易 bot、grid、回測/試用交易、market registry audit：會進 snapshot 供稽核，但目前正式執行中的 PointsChain 與交易共用 `finance.db` 保持同一 SQLite transaction；因此 restore 不覆寫整個 finance DB，避免只回滾交易或鏈的一側而破壞結算原子性。
- `runtime/storage/snapshots` 與 `.imports` 是 snapshot repository 本身，不會被打包進 `uploads.tar.gz`，restore 清檔時也會保留，避免 snapshot 自我遞迴或被還原流程刪掉。
- server snapshot 也會帶入目前設定的 runtime secret files，例如 `runtime/.chain_seed`、`runtime/.csrfkey`、`runtime/.filekey`、`runtime/.fkey`、`runtime/.integrity_key`、`runtime/integrity_manifest.json`、`runtime/cert.pem`、`runtime/key.pem`
- PointsChain ledger/finance/trading overwrite restore：已停用；snapshot 內副本只作 forensic/export。鏈或結算異常不得以備份覆寫，需走 safe mode、forensic bundle、分支、緊急治理與 append-only correction。
- `test_for_develop.sh --backup/--restore` 是較低層 runtime archive，不等於 server snapshot：archive 不含 storage payload；restore 必須保留 target 現有 storage、live financial DB 與 `.chain_seed`。若 target 還沒有 live ledger，工具會拒絕從 archive 初始化金融歷史。
- runtime reset：清掉可重建 runtime 與 live data，並要求重啟
- server mode checkpoint：保護 mode switch / rollback 場景

## 不納入預設備份

- `runtime/logs`：屬操作觀測資料，會快速膨脹；需要長期保存時應走 log archive。
- `runtime/reports`：QA/稽核輸出可由 artifact 管理，不當作站台恢復真相。
- Python cache、臨時 import staging、snapshot export tarball 本身。

## Runtime reset 範圍

runtime reset 是 destructive cleanup，不是歷史回復工具。執行前會先建立 `pre_reset` snapshot；之後清除：

- 使用者內容：forum/chat/album/video/share/storage/upload、remote download、HLS/media stream、通知與檢舉。
- AI Agent runtime 對話、Job Center 任務、ComfyUI generation/history/run/preset runtime tables。
- Trading runtime 狀態：orders/fills/positions/bots/grid/trial/idempotency/market runtime audit。
- 社交互動與治理 runtime：好友/follow、制裁申訴、moderation actions/proposals/votes。

runtime reset 不直接清：

- 帳號登入基礎資料與 root/admin/test 可登入性。
- `system_settings` 這類管理設定；只套用 `MANAGEMENT_ONLY_RESET_SETTINGS` 的安全預設。
- PointsChain ledger / governance / wallet 核心表；這些由 `reset_points_chain` 專用流程處理，且 ledger backup/restore 仍維持停用。
- cloud-drive security policy 這類站台策略設定。

## 還原原則

- snapshot 先驗證 checksum、SQLite `integrity_check`、tar path safety，再還原。
- split DB portable restore 以 label 對應本機 DB path；本機未設定的 label 會被略過，不會寫到未知路徑。
- `finance`、`points_chain`、`trading` 即使 archive 存在也會以 `append_only_financial_restore_disabled` 略過；驗證重點是 snapshot 後的新鏈交易仍存在，而 ordinary dirty state 已回復。
- restore 成功後會重新寫入 snapshot record、restore event，並跑 PointsChain / trading post-restore validators。
- runtime secret files 還原後會做 hash 驗證；失敗不能靜默成功。

## 失敗情境與提示

- restore 完後 runtime key / TLS cert 沒回來，或 hash 驗證失敗：
  先看 snapshot metadata 的 `runtime_secret_files`，以及 restore event 的 `runtime secret validation failed`。目前 server snapshot 會一起帶入這些檔案。
- reset 後發現東西都沒了：
  這是設計目的；先去找 `pre_reset` snapshot。
- 只壞了 PointsChain 卻直接做整站 restore：
  不會覆寫 live finance ledger，但可能回滾其他模組；先走 PointsChain incident/governance recovery，避免造成跨模組時間點不一致。
- 做了 restore 卻沒驗 post-restore consistency：
  視同沒完成恢復演練。

## 測試方式

- create/list/download/upload-restore
- pre-reset snapshot 是否存在
- reset 後離線 / 重連 / `started_at` 更新
- restore 後 baseline data 保留、殘留 dirty data 被清掉
- snapshot 後新建的 PointsChain 交易仍存在，`finance/points_chain/trading` 顯示 protected skip
- CLI archive restore 後 storage 與 live financial DB hash 保留，沒有把 archive 舊值蓋回去
- PointsChain verify / recovery / wallet rebuild

## 相關文件連結

- [RUNTIME_RESET_AND_RECOVERY.md](ops_boundaries/RUNTIME_RESET_AND_RECOVERY.md)
- [SERVER_MODE_V2_PROFILE_MATRIX.md](server_mode_v2/SERVER_MODE_V2_PROFILE_MATRIX.md)
- [SERVER_MODE_V2_TEST_PLAN.md](server_mode_v2/SERVER_MODE_V2_TEST_PLAN.md)
- [11_QA_TESTING.md](11_QA_TESTING.md)
- [security/FUNCTIONAL_SMOKE.md](security/FUNCTIONAL_SMOKE.md)


---

## PointsChain v2 區塊鏈化規劃 (2026-05-04 拍板, 尚未實作)

本模組未來將與全站 PointsChain v2 區塊鏈化整合：

- 工程設計：[`docs/AGENTS/research/BLOCKCHAIN/POINTSCHAIN_ENGINEERING.md`](AGENTS/research/BLOCKCHAIN/POINTSCHAIN_ENGINEERING.md)
- 用戶白皮書：[`docs/AGENTS/research/BLOCKCHAIN/POINTSCHAIN_WHITEPAPER.md`](AGENTS/research/BLOCKCHAIN/POINTSCHAIN_WHITEPAPER.md)
- 地址規格：[`docs/AGENTS/research/BLOCKCHAIN/POINTS_WALLET_ADDRESSING.md`](AGENTS/research/BLOCKCHAIN/POINTS_WALLET_ADDRESSING.md)
- 轉帳 API：[`docs/AGENTS/research/BLOCKCHAIN/POINTS_TRANSFER_API.md`](AGENTS/research/BLOCKCHAIN/POINTS_TRANSFER_API.md)
- 多簽錢包：[`docs/AGENTS/research/BLOCKCHAIN/MULTISIG_WALLETS.md`](AGENTS/research/BLOCKCHAIN/MULTISIG_WALLETS.md)
- QA Mining / 貢獻獎勵 (Phase 7)：[`docs/AGENTS/research/BLOCKCHAIN/POINTS_MINING_REWARDS.md`](AGENTS/research/BLOCKCHAIN/POINTS_MINING_REWARDS.md)
- QA / Release Gate：[`docs/AGENTS/research/BLOCKCHAIN/POINTSCHAIN_QA.md`](AGENTS/research/BLOCKCHAIN/POINTSCHAIN_QA.md)

**狀態：設計已拍板（root, 2026-05-04），尚未實作完成。**
