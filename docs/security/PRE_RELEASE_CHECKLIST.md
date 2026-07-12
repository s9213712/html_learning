# 上線前檢查清單

本清單是 production / 對外上線前的 release gate。任何標示為「阻擋」的項目未完成時，不得切換到上線模式，也不得對外開放服務。

## Release 資訊

- [ ] Release ID / commit SHA：
- [ ] 目標分支：
- [ ] 部署環境：
- [ ] 負責人：
- [ ] 檢查日期：
- [ ] 若本次包含上線新功能，Release ID 尾碼已 +1，且 README / For_developer / `services/platform/release_info.py` 已同步。
- [ ] 若本次有功能新增、修改或重構，已依 [RULES_FOR_AGENTS.md](../AGENTS/RULES_FOR_AGENTS.md) 檢查文件、測試、錯誤回饋、手機版與伺服器端驗證。

## 必要阻擋項目

- [ ] 阻擋：已完成滲透測試。
  - 指令：
    ```bash
    scripts/security/pentest/run_pentest.sh --target https://<production-or-staging-host>
    ```
  - 報告路徑：
  - 結論：沒有未處理的 high / critical 風險；若有 findings，已建立 issue 並完成修復或由 root 明確接受風險。

- [ ] QA：已完成產品全功能測試。
  - 指令：
    ```bash
    scripts/security/pentest/run_functional_smoke.sh --qa-full --port 50741
    ```
  - 報告路徑：
  - 結論：`failures` 必須為 `0`；若有 `skip`，必須確認是外部服務未啟用或預期條件，而不是功能缺失。這是 QA 產品回歸，不是 production unlock 的核心 gate。

- [ ] 阻擋：已完成基礎壓力測試。
  - 指令：
    ```bash
    python3 scripts/security/pentest/stress_test.py --target https://<staging-host> --i-own-this-target
    ```
  - 報告路徑：
  - 結論：基準容量內沒有 HTTP 500/502/503；若刻意超載，controlled `503 server_busy`
    必須有使用者可讀提示、`Retry-After`，並記錄目前主機容量上限。

- [ ] 阻擋：本機自動測試通過。
  ```bash
  python3 scripts/prepush/pre_push_checks.py
  scripts/testing/pytest_in_tmp.sh -q tests
  ```

- [ ] 阻擋：安全中心沒有未處理的高風險項目。
  - Integrity Guard 無 pending/rejected high risk finding。
  - Audit chain 驗證通過。
  - PointsChain 驗證通過。
  - Server log 無啟動錯誤、未捕捉例外或重複 HTTP 500。

## 部署設定

- [ ] 已確認正式 serving 架構。
  - Nginx 是唯一對外入口。
  - Gunicorn 只綁 `127.0.0.1:8000` 或其他 loopback upstream。
  - systemd service 使用 `deploy/systemd/` 範本或等效設定。
  - 沒有直接把 Flask development server 或 Gunicorn upstream 暴露到公網。
  - 已讀 [../../deploy/README.md](../../deploy/README.md) 並替換所有 domain、path、TLS cert 與 secret placeholder。

- [ ] 已確認 production 模式設定。
  - 安全機制全開。
  - 測試帳戶停用。
  - 預設帳戶要求重設密碼。
  - root / manager 屬特殊階級，不使用一般會員等級。

- [ ] HTTPS / SSL 設定確認。
  - root 設定 `server_ssl_enabled` 符合部署策略。
  - `runtime/cert.pem` / `runtime/key.pem` 或反向代理 TLS 設定已就緒。
  - `SESSION_COOKIE_SECURE=true`。
  - 若 TLS 在 Nginx 終止，`USE_XFF=true`、`TRUSTED_PROXY_IPS` 與 `GUNICORN_FORWARDED_ALLOW_IPS` 已限制為受控 proxy。

- [ ] Secrets 與 runtime 檔案確認。
  - `runtime/.fkey`、`runtime/.filekey`、`runtime/.csrfkey`、`runtime/.chain_seed`、`runtime/.integrity_key` 由部署地生成或由 secret manager 注入。
  - 沒有把 database、log、chat、storage、hash chain、integrity manifest、測試報告殘留提交到 git。
  - `git status --ignored --short` 已檢查，只有預期忽略檔。

- [ ] 資料與備份確認。
  - 部署前已建立 snapshot。
  - snapshot 可下載保存。
  - 已用上傳 snapshot 測試異機 restore。
  - reset server 功能已在非 production 環境驗證。

## 功能驗收

- [ ] 認證與帳號：登入、登出、密碼重設、預設密碼強制變更、root / manager 權限。
- [ ] 社群：公告、討論區、主題、留言、按讚/倒讚、版主權限。
- [ ] 雲端硬碟：上傳、下載、預覽、資料夾、移動、垃圾桶、還原、清空、相簿。
- [ ] 下載器：direct link、magnet、`.torrent` 上傳；失敗時有可讀錯誤。
- [ ] ComfyUI：服務存在檢測、模型清單、產圖逾時、保存到雲端或丟棄。
- [ ] Security Center：audit log、server log、即時輸出、模式切換、安全開關、閾值、自定義設定檔。
- [ ] PointsChain：wallet、ledger、admin 調整、封塊、proof、全鏈驗證。
- [ ] Snapshot / Restore / Reset：restore 後只保留 checkpoint 前資料；reset 後清除 runtime 狀態。

## 上線決策

- [ ] 所有阻擋項目已完成。
- [ ] 所有 high / critical issue 已關閉或有 root 風險接受紀錄。
- [ ] 滲透測試與全功能測試報告已從 `/tmp/hackme_web_test_artifacts/` 保存到受控 release artifact；未提交 raw cookie/token。
- [ ] README / WEB / For_developer 與實際功能一致。
- [ ] 若本次有新功能，最終交付說明已列出功能、文件、測試、錯誤提醒、手機版檢查、伺服器端運算與未完成項。
- [ ] 已記錄最終 commit SHA。

簽核：

- root：
- manager / reviewer：
- 上線時間：
