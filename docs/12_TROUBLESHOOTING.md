# 12 Troubleshooting

一句話說明：這份文件集中列出新部署者、root、一般使用者最常遇到的錯誤與第一輪排查方式。

## 設計目的

過去錯誤排查散在各功能文件，部署者容易知道「它壞了」但不知道「先去哪一份找解法」。這份文件把常見故障改成固定分類與穩定 ID，方便快速搜尋與轉貼。

## 使用方法

先找對分類，再搜尋穩定 ID，例如：

- `TRB-BOOT-001`
- `TRB-AUTH-001`
- `TRB-TRADING-001`

## A. 啟動 / 部署

### TRB-BOOT-001 服務啟動了但頁面打不開

先檢查：

- host / port 是否正確
- 先看 server console 印出的實際 URL，不要先假設一定是 HTTP
- 若你是直接執行 `python3 server.py`，本機通常應優先嘗試
  `https://127.0.0.1:5000/`
- 若是正式部署，先看 Nginx 是否對外、Gunicorn 是否只在 `127.0.0.1:8000`
  listening，並查 `systemctl status hackme-web.service`
- port 是否被占用

### TRB-BOOT-002 頁面正常但登入或寫入 API 全部 `403`

優先看：

- CSRF token 是否失效
- 是否剛改完 bootstrap 密碼；改密碼後舊 session 會立即失效
- 是否在舊分頁上送請求

### TRB-BOOT-003 repo 根目錄冒出很多 runtime 檔

不能 commit。DB、logs、storage、reports、keys、certs 都應留在部署地 runtime。
若你不確定 runtime 邊界，回頭看 [02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md)。

### TRB-BOOT-004 Nginx 回 `502 Bad Gateway`

先看三件事：

- `systemctl status hackme-web.service`
- `journalctl -u hackme-web.service -n 100 --no-pager`
- Nginx upstream 是否指向 `127.0.0.1:8000`

常見原因：

- `/opt/hackme_web/.venv/bin/gunicorn` 尚未建立或套件未安裝。
- `/etc/hackme_web/hackme-web.env` 的 secrets 還是 placeholder。
- runtime 目錄不存在或權限不是 `hackme:hackme`。
- `python3 server.py --doctor` 在 `ExecStartPre` 失敗。

### TRB-BOOT-005 登入後一直跳回登入頁或 secure cookie 不生效

若 TLS 在 Nginx 終止，確認 env 與 proxy header 一致：

```env
FORCE_HTTPS=true
SESSION_COOKIE_SECURE=true
USE_XFF=true
TRUSTED_PROXY_IPS=127.0.0.1,::1
GUNICORN_FORWARDED_ALLOW_IPS=127.0.0.1,::1
```

Nginx 必須送出：

```nginx
proxy_set_header X-Forwarded-Proto https;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header Host $host;
```

如果 Nginx 不在同一台主機，`TRUSTED_PROXY_IPS` 必須改成實際 proxy IP。

## B. 登入 / CSRF / Session

### TRB-AUTH-001 root 忘記密碼

不要走登入頁的 `忘記密碼`。正式補救方式是到站台實體 runtime 上執行：

```bash
python3 scripts/admin/root_recovery.py --prompt-password
```

這會撤銷 root 既有 session，並要求下次登入立刻改密碼。

### TRB-AUTH-002 改完 bootstrap 密碼後被登出

這是預期安全行為。重新登入、重新取得 CSRF token 後再繼續操作。

### TRB-AUTH-003 看到 `invalid_authenticated` CSRF 安全警訊

先檢查是不是舊頁面、多分頁或腳本持續送出很久以前的 token。正常同一登入 session 的近期
authenticated token 會被保留短暫相容窗口；若仍出現，重新整理頁面並確認前端請求使用
`apiFetch()` 或送出 `X-CSRF-Token`，不要把 token 放在 URL query string。

## C. 權限 / Feature Flags

### TRB-FEATURE-001 看到「此功能目前已由 root 關閉」

這通常不是 bug，而是 feature flag 或父功能未完整開啟。先看：

- [03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md)
- [05_FEATURES_OVERVIEW.md](05_FEATURES_OVERVIEW.md)

### TRB-FEATURE-002 看到權限不足

先分辨：

- 你真的角色不夠
- 該模組需要更高 member level
- root 只開了入口，沒有開完整底層功能

## D. Cloud Drive / Upload / Preview

### TRB-DRIVE-001 root 上傳大檔直接收到 HTTP `413`

先分清楚這不是配額，而是單次 request body 上限。先看：

- `HTML_LEARNING_MAX_CONTENT_MB`
- 反向代理的 body size 限制
- 這台 runtime 是否其實還跑舊版 server code

### TRB-DRIVE-002 PDF / 壓縮檔 / 一般檔案預覽不正常

先分辨：

- 是不是舊前端快取
- plain / `server_encrypted` PDF 是否能走原生 viewer
- strict `e2ee` PDF 是否改走瀏覽器端解密或新分頁備援
- 壓縮檔預覽是否仍像單段文字；若是，多半是舊快取

### TRB-DRIVE-003 第二個 E2EE 檔案又要求重新輸入密碼

新版會先嘗試同一次登入 session 最近成功過的密碼。若仍跳出詢問，通常代表：

- 兩個檔案不是同一組密碼
- 你中途已登出
- 或前端仍是舊快取

### TRB-DRIVE-004 重新整理後分段上傳沒有繼續

這通常是瀏覽器檔案權限限制，不是伺服器遺失任務。任務中心應顯示未完成
resumable upload session，但你需要重新選擇同一個本機檔案，系統才能補送缺少的 chunk。

### TRB-DRIVE-005 分享連結提示缺少 E2EE 片段金鑰

strict E2EE 分享的解密片段在 URL `#...` fragment 內，伺服器看不到也不能復原。
重新按複製連結，確認完整連結包含 `#` 後面的片段；若仍不完整，只能重新產生分享。

### TRB-DRIVE-006 BT / direct link 下載看起來停住

先看任務中心的速度、phase、可用度與 pause/cancel 狀態。BT timeout 是 idle-progress
timeout，不是固定總時間；若速度持續為 0，先檢查 tracker 是否被安全策略阻擋、Transmission RPC / aria2c 是否可用、
以及是否有其他高可用度任務正在佔用 worker。Transmission 串接細節看
[13_REMOTE_DOWNLOAD_TRANSMISSION.md](13_REMOTE_DOWNLOAD_TRANSMISSION.md)。

## E. Video / E2EE / HLS

### TRB-VIDEO-001 影片無法預覽 / 播放

先看：

- E2EE 檔案本來就不能走伺服器端 HLS
- `server_encrypted` 影音的舊 key 是否仍可用
- 檔案是否被 quarantine / 權限不足 / visibility 不符

### TRB-VIDEO-002 strict E2EE 共享影音一直顯示讀取中

共享頁新版應依序顯示：

1. `正在讀取 E2EE 分享授權`
2. `正在下載加密影音檔`
3. `正在瀏覽器端解密影音`

若你只看到單句 `讀取中`，多半是前端快取未更新。

### TRB-VIDEO-003 上傳者看到影音處理中，但公開列表找不到影片

這是預期設計：HLS 或 E2EE streaming 衍生檔準備完成前，不應把影片長時間放在公開列表顯示
`準備中`。先到任務中心或通知看處理狀態；完成後會通知上傳者。

## F. ComfyUI / Civitai

### TRB-AI-001 ComfyUI 有頁面但不能下載模型

若 root 把 ComfyUI 設成 `remote` API 模式，這是預期限制；模型下載只對
`local` 模式有意義。

### TRB-AI-002 找不到 Embedding / VAE / trigger words

先分辨：

- `Embedding` / `VAE` 清單是否真的有被 ComfyUI API 回傳
- LoRA 是否是透過 root 的 Civitai 下載面板下載
- 目前模型類型是否屬於 UI 支援範圍

### TRB-AI-003 ComfyUI 長任務逾時

先分辨：

- 是 ComfyUI 真正在排隊 / 載模型 / 生成太久
- 還是你目前瀏覽器頁面仍是舊前端快取

若 log 裡仍是後端先回 `ComfyUI 產圖逾時`，再檢查模型大小、顯卡負載與
ComfyUI 服務本身狀態。

### TRB-AI-004 ComfyUI 一產圖整站變慢

優先看 Security Center 的 CPU / GPU / VRAM / RAM 資源看板。小 VRAM 主機若載入大模型，
可能因 VRAM offload、CPU RAM 與磁碟 I/O 變慢。部署上應優先使用遠端 ComfyUI 或外部
ComfyUI process；不要把 Diffusers in-process 打開給一般服務。

### TRB-AI-005 Diffusers 任務看起來卡在下載或載入

Diffusers 模式不會透過 ComfyUI server。前端進度應顯示 Hugging Face 下載、
Diffusers 載入或 Python 推論階段，並在進度面板下方顯示已遮蔽敏感資訊的 Python log tail。
若訊息仍寫 ComfyUI 後端無回報，代表瀏覽器仍使用舊前端快取。

## G. PointsChain / Wallet / Ledger

### TRB-POINTS-001 餘額看起來不對

先驗證：

- PointsChain verify
- 是否其實在看 root 模擬交易餘額，而不是真實 wallet
- 是否是 snapshot restore / safe-mode recovery 後尚未完成 ledger replay

### TRB-POINTS-002 snapshot restore 或 recovery 後 wallet / ledger 不一致

先停在 root-only 工具鏈，不要直接手改 DB。先看：

- [07_POINTSCHAIN.md](07_POINTSCHAIN.md)
- [09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md)

PointsChain 不允許用備份覆寫 ledger。若 verify 失敗，先進 safe mode，建立
forensic bundle，再用分支、緊急治理或追加補償交易處理。

## H. Trading / Price / Bot / Backtest

### TRB-TRADING-001 交易頁數字正常但成交失敗

先看：

- live price provider 是否失效
- 如果 root 用的是融合價格，是否只剩太少健康來源
- circuit breaker 是否擋下異常價格
- 餘額 / 借貸池 / 手續費條件是否不足

### TRB-TRADING-002 交易圖表看不到 RSI / KD，或指標線顯示怪異

先分辨：

- 你是不是只開了價格 overlay，沒有勾 `RSI14` 或 `KD(9,3,3)`
- 頁面是不是仍為舊快取
- 資料窗口是否不足以畫出部分指標

### TRB-TRADING-003 bot / backtest / dashboard 行為不如預期

先看：

- 回測 K 棒上限是否足夠
- 目前價格是不是 degraded / fallback
- Bot 是否仍處於 `未稽核`
- 若牽涉 BTC_trade，外部 repo 狀態是否完整

### TRB-TRADING-004 root 交易報表或交易所基金頁回 `503`

新版 root report / sitewide pools / all-user positions 應讀背景 snapshot，不在 root request
裡現場重算。`503` 通常代表 background worker 尚未產生 snapshot、被暫停，或目前 server mode
讓交易背景作業 paused。先看 root trading background status，再決定是否 enqueue run-once。

### TRB-TRADING-005 小額手續費或借貸利息看起來太高

目前規則是先累積小數殘值，只有現貨賣出、機器人停止、借貸結算或清算時才轉整數 POINT，
且有小數就進位。若每次預估、每小時計息或每個 grid 小步都直接進位，應視為 bug。
借貸交易手續費要用完整名目金額計算，不是只用使用者保證金。

## I. Snapshot / Restore / Reset

### TRB-RESTORE-001 restore 後狀態不對

先分辨：

- 是 DB 還原問題
- storage / secrets / reports 未同步回來
- 還是 wallet / ledger 需要重建

先看 [09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md)，不要直接在 production runtime 手改資料。

### TRB-RESTORE-002 不知道要 rollback 還是重新 restore

先確認：

- 這次 restore 前是否已有 `pre_restore` snapshot
- 問題是資料不一致，還是單純功能開關 / server mode 狀態不同

## J. Production Gate / Security Center

### TRB-GATE-001 launch-check / 文件捷徑打不開

先確認這台 runtime 是否已更新到最新 code，並重新整理 root 安全中心頁面。

### TRB-GATE-002 production gate 被擋下，但看不懂原因

先看：

- 哪一張卡是紅燈
- 是缺報告、報告未驗簽、還是前置條件未完成
- 是 root 狀態問題，還是 staging/prod 設定問題

### TRB-GATE-003 安全測試卡片顯示異常

新版安全中心應把滲透、越權、全功能、壓力測試拆成獨立卡片。若看起來仍是舊的單一卡片，多半是舊前端快取。

## 相關文件連結

- [01_DEPLOY_QUICKSTART.md](01_DEPLOY_QUICKSTART.md)
- [02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md)
- [03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md)
- [05_FEATURES_OVERVIEW.md](05_FEATURES_OVERVIEW.md)
- [07_POINTSCHAIN.md](07_POINTSCHAIN.md)
- [09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md)
- [11_QA_TESTING.md](11_QA_TESTING.md)
