# 24-Hour Operational Campaign

這是上線前的長時間產品驗收，不是用 sleep 撐滿時數，也不是把單元測試重跑 24 小時。
正式證據必須由 `scripts/testing/operational_campaign_24h.py` 產生，且
`active_test_seconds >= 86400`。等待使用者授權、安裝依賴或修正 harness 的時間不計入。

## 執行架構

- primary target：全程承受多帳戶同步功能輪訓、root/manager sentinel、瀏覽器與資源取樣。
- recovery target：和 primary 同時運作，專門做 snapshot、完整 runtime archive、restore、
  restart、錢包事故與治理分支等破壞性演練。
- 所有 source copy、runtime、fixture、log、DB、HLS segment 與報告都必須位於 `/tmp`。
- campaign 啟動後不得修改 Python、Shell、前端 JS/CSS/HTML；來源 manifest 與子 harness
  SHA-256 發生漂移時，本次證據直接失效。

## 強制矩陣

| 類別 | 必須證明的行為 |
|---|---|
| 長影音 | 至少 1 小時 fixture、雙音軌、字幕、多帳戶同步上傳、HLS job、playlist/segment、隨機 seek、桌機/手機播放、密碼分享、錯誤密碼、撤銷後失效 |
| AI Agent | root/member 前端、Drive/share、server ops、治理、交易、媒體任務、權限/確認邊界、上線前 dry-run；有 `HACKME_CAMPAIGN_COMFYUI_API_URL` 時再跑真實生圖 |
| 交易 | 現貨、借貸/保證金、bot/grid/workflow、背景撮合、TP/SL、利息、清算、併發下單、reserve 與非負不變量 |
| PointsChain | 高頻轉帳/交易、idempotency、overspend/replay、外部地址、finality、hash chain、分支、治理、事故攻擊回歸與 post-stress UI |
| 錢包事故 | 模擬私鑰外洩、竊取、攻擊者二次花費、暫時凍結、公開治理、風險標記、補償與 governed recovery branch |
| 備份/還原 | server snapshot、portable/CLI runtime archive、storage、SQLite quick check、重啟時間與最後 readiness |
| 串流相容性 | prepared HLS、Standard realtime proxy、slot/busy/disconnect、音軌切換、Chromium/Firefox/WebKit 與手機 viewport |
| 最終營運 | member heuristic probe、完整桌機/手機 Playwright、whole-site gate、root control plane、log/DB lock/secret/silent-failure 掃描 |

ComfyUI 是外部依賴時，可以沒有真實 backend，但不能把缺席當成功：offline/missing-dependency
邊界仍是強制項。設定 `HACKME_CAMPAIGN_COMFYUI_API_URL` 後，真實生圖會變成本次 AI Agent
scenario 的必要步驟。

## 正式命令

先完成依賴、磁碟與沙盒/本機 bind 授權，再執行一次命令。campaign 會為全新隔離伺服器
產生隨機密碼，密碼不進 argv 或報告。

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

短版只能驗 harness，不是上線證據：

```bash
python3 scripts/testing/operational_campaign_24h.py \
  --campaign-root /tmp/hackme_web_campaign_smoke_YYYYMMDD_HHMM \
  --duration-seconds 180 \
  --allow-short-duration \
  --account-count 4 \
  --round-ops 160 \
  --concurrency 8
```

## 證據與判定

- 即時 checkpoint：`<campaign-root>/campaign.checkpoint.json`
- 主報告：`<campaign-root>/reports/operational_campaign_24h.json` 與 `.md`
- 連續主壓力：`<campaign-root>/core_soak/operational_soak.json`
- 場景證據：`<campaign-root>/reports/scenarios/`
- 資源時間序列：`<campaign-root>/reports/resources/resource_samples.jsonl`

只有主報告同時滿足以下條件才可簽核：

- `verdict=PASS`、`production_signoff_eligible=true`、活動秒數不少於 86,400。
- 八個 mandatory scenario 全部成功，沒有缺跑或 timeout。
- primary 連續 soak 成功，每個帳號與必要 positive path 都至少有一次 HTTP 2xx。
- 無來源漂移、憑證洩漏、未規劃 transport failure、DB lock、Traceback/OOM 或靜默前端失敗。
- 資源樣本完整且有真實 PID/RSS；sentinel p95 未超過設定值。
- snapshot/CLI restore 均證明 ordinary state 可回復，但 live finance/PointsChain 不被覆寫。
- 最終 primary/recovery control-plane checks 全部成功。

正式報告完成後，以 `HACKME_OPERATIONAL_CAMPAIGN_REPORT` 指向上述 JSON，再執行
`scripts/security/gate/on_live_reports_make.py`。Production gate 只接受重新簽章且
source manifest 仍與待上線程式一致的 `operational_campaign_24h` report。

PointsChain/交易 DB 會放進 snapshot/archive 作 forensic evidence，但 restore policy 是
`forensic_archive_only_append_only_recovery`。鏈上錯誤只能經 safe mode、forensic、治理分支與
append-only correction 處理，不能把舊 DB 蓋回 live ledger。
