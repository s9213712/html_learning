# 01 Deploy Quickstart

一句話說明：這份文件只做一件事，讓你用最短路線在 10 到 20 分鐘內把
`hackme_web` 跑起來並完成第一輪基本驗證。

## 設計目的

部署者第一次接手時，最需要的是「成功啟動、知道資料放哪裡、知道下一步驗什麼」。
因此這份文件只保留最短路徑，不先塞入完整 API、風險模型、歷史設計或全部模組細節。

## 使用方法

### 你需要準備

- Linux / WSL / 可跑 Python 3 的環境
- `git`
- `python3`
- `curl`

最小 Python 依賴：

```bash
python3 -m pip install -r requirements-minimal.txt
```

若要跑 Playwright / pytest 類開發驗證，再加：

```bash
python3 -m pip install -r requirements-dev.txt
```

若你需要完整依賴總表，而不是只看最短路線，請直接看
[SYSTEM_DEPENDENCIES.md](SYSTEM_DEPENDENCIES.md)。

### 本機 / staging 最短流程

一般開發與功能驗證直接使用隔離啟動：

```bash
./test_for_develop.sh --port 50785
```

它會把程式、runtime、cache 與測試資料放到 `/tmp`，不需要先在 source checkout
建立 `runtime/`。

只有在你已準備 checkout 外部 runtime、要驗直接啟動時才執行：

```bash
export HACKME_RUNTIME_DIR=/absolute/path/to/runtime
python3 server.py --doctor
python3 server.py
```

doctor 不會替你建立 production runtime；缺少目錄、金鑰或權限時會失敗。正式
runtime 的建立與 systemd/Nginx 設定請接著看
[02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md)。

臨時用 LAN / NAT public IP 測試開發站時，明確加入 public Host allowlist：

```bash
./test_for_develop.sh --host 0.0.0.0 --port 5000 --public-host 203.121.227.18
```

`--public-host` 會同時 allowlist 裸 host 與 `host:port`，避免透過
`https://public-ip:port/` 測試時被 trusted-host guard 擋成 `400 untrusted_host`。
如果你不用 `test_for_develop.sh`、而是手動直接啟動 `server.py`，請帶 public host env：

```bash
HTML_LEARNING_HOST=0.0.0.0 HTML_LEARNING_PORT=5001 HTML_LEARNING_PUBLIC_HOST=203.121.227.18 python3 server.py
```

`HTML_LEARNING_PUBLIC_HOST` / `HTML_LEARNING_PUBLIC_HOSTS` 會自動加入裸 host 與目前
`HTML_LEARNING_PORT` 的 `host:port` 變體。

開發排錯時若只想先避免 trusted-host guard 擋住 public IP / NAT 測試，可暫時關閉：

```bash
./test_for_develop.sh --host 0.0.0.0 --port 5000 --allow-any-host
```

手動啟動則可設定 `HTML_LEARNING_DISABLE_TRUSTED_HOSTS=1`。這是 dev-only escape hatch；
正式部署請改用 `HTML_LEARNING_TRUSTED_HOSTS` / `HTML_LEARNING_PUBLIC_HOSTS`。

互動模式若執行 capacity test，腳本會輸出實測結論：推薦的 workers x threads、
worker-thread lanes、最大安全 concurrent accounts、p50/p95/p99/max 延遲、status / failure
counts、CPU peak、測過的 profiles / account ladder、load profile、測項分類、最慢 labels、
UX degradation / application limit / server instability 邊界，以及 JSON report 路徑。然後
詢問要套用結果、重測、手動輸入參數或使用保守 fallback。CLI 模式會直接套用 probe 結果。
若 probe 沒有產生可用 recommendation，互動模式不會提供 apply 選項，而會列出各 profile 的
setup/round error 並要求重測、手動輸入或 fallback。capacity probe 預設允許 isolated profile
建立 venv；只有明確設定 `HACKME_DEV_CAPACITY_PROBE_INSTALL=0` 時才禁止安裝。
低規格機器請先用 `--capacity-probe-tier sbc|legacy|laptop|midrange|highend` 選量級：
`sbc` 針對單板電腦 / 小型 VM，使用最小讀取型 probe 並設 60 秒總時限；`legacy`
針對老桌機 / 低功耗 NAS，使用低衝擊讀取型 probe 並設 120 秒總時限；`laptop`
使用小型 basic member workflow 並設 180 秒總時限；`midrange` 再放寬帳號與 profile。
`highend` 沒有 account / round 上限，會持續增加負載
直到達到停止條件，可能讓主機卡死或崩潰，只有能接受風險時使用。

背景模式會在 runtime logs 目錄產生並列出 `server_direct.out`、Gunicorn access
log 與 error log。若要停止前一次由腳本啟動、正在佔用同一 port 的 dev server
與其衍生 process group / child tree：

```bash
./test_for_develop.sh --port 5000 --shutdown
```

如果你要手動啟動目前工作樹：

```bash
python3 server.py --doctor
python3 server.py
```

這只適合本機 / staging / 緊急維修。正式對外服務請改走
[02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md) 的 Nginx + systemd + Gunicorn
流程，不要直接暴露 Flask development server。

### 第一次啟動後要確認

1. 先看 server console 印出的實際 URL，不要先假設一定是 HTTP 或 HTTPS。
2. 一般情況：
   - `./test_for_develop.sh`：通常會是 `https://127.0.0.1:<port>/`
   - `python3 server.py`：本機開發模式通常會自動準備本地 TLS，因此多半是
     `https://127.0.0.1:5000/`
3. 若不確定，直接探測：

   ```bash
   for scheme in https http; do
     curl -ksSf "${scheme}://127.0.0.1:5000/api/version" && echo "$scheme works"
   done
   ```

4. `GET /api/version` 有回應。
5. 你知道 bootstrap 帳號：
   - `root/root`
   - `admin/admin`
   - `test/test`
6. 直接 runtime 第一次登入後，預設密碼會被要求立刻修改。
7. `test_for_develop.sh` 會停用強制改密碼並保留弱化的開發安全設定；該站不可直接暴露到 LAN、Internet 或 production。
8. 直接 runtime 改完 bootstrap 密碼後請重新登入；高權限 session 會被立即撤銷，這是預期安全行為。

### 若要改第一次密碼

在第一次建 DB 前先設定：

- `HTML_LEARNING_ROOT_PASSWORD`
- `HTML_LEARNING_MANAGER_PASSWORD`
- `HTML_LEARNING_TEST_PASSWORD`

### 最短驗證

```bash
python3 scripts/prepush/pre_push_checks.py
scripts/security/pentest/run_functional_smoke.sh --port 50741
```

如果你只想先看版本：

```bash
for scheme in https http; do
  curl -ksSf "${scheme}://127.0.0.1:5000/api/version" && echo "$scheme works"
done
```

## 原理

- `python3 server.py --doctor` 會先檢查 runtime 目錄是否存在且可寫，不會靜默補建。
- `python3 server.py` 是本機手動啟動，不是正式對外 serving 架構。
- `./test_for_develop.sh` 會把運行伺服器需要的原始碼複製到 `/tmp`，並關掉妨礙開發的安全限制。
  它不應複製大型 docs、reports、archive、cache、runtime 產物；若 `/tmp` 很快滿，先檢查是否有舊版腳本或殘留測試副本。
- 正式部署的 env、runtime、依賴與權限仍應由部署者自己明確準備。
- 正式部署請使用 `deploy/nginx/` 與 `deploy/systemd/` 範本，讓 Nginx 對外、
  Gunicorn 只綁 loopback，並把長任務留給獨立 worker。
- `BT/magnet` 遠端下載可用 Transmission RPC 或 `aria2c`；正式部署建議看
  [13_REMOTE_DOWNLOAD_TRANSMISSION.md](13_REMOTE_DOWNLOAD_TRANSMISSION.md) 設定 `HACKME_BT_BACKEND`、
  Transmission RPC URL 與併發限制。upload malware 掃描若要啟用則需 `clamscan` 或 `clamdscan`；完整依賴請看
  [SYSTEM_DEPENDENCIES.md](SYSTEM_DEPENDENCIES.md)。
- repo 只追蹤原始碼；DB、logs、storage、keys、TLS 憑證、reports 都是
  runtime 檔，啟動後才生成。
- 這種設計降低 clone 後的人工整理成本，也避免把別人的 runtime 狀態帶進來。

## 失敗情境與提示

- `python3 server.py --doctor` 失敗：
  代表 runtime 環境尚未準備完成；先補齊目錄與權限。
- 能啟動但頁面打不開：
  先看 bind host / port 是否被占用，再看
  [12_TROUBLESHOOTING.md](12_TROUBLESHOOTING.md)。
- 啟動後生成很多 runtime 檔，不知道能不能 commit：
  不行；runtime 檔不應提交到 Git。看 [02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md)
  與 [For_developer.md](For_developer.md) 的 runtime 說明。
- 只想用遠端 ComfyUI：
  先把站跑起來，再由 root 在設定頁配置；不要把 ComfyUI 整合視為第一步部署阻塞項。

## 測試方式

- `python3 scripts/prepush/pre_push_checks.py`
- `scripts/security/pentest/run_functional_smoke.sh --port 50741`
- `for scheme in https http; do curl -ksSf "${scheme}://127.0.0.1:5000/api/version" && echo "$scheme works"; done`
- 手動登入 `root`，確認首頁、設定頁、主要模組頁可載入

## 相關文件連結

- [00_START_HERE.md](00_START_HERE.md)
- [02_DEPLOY_PRODUCTION.md](02_DEPLOY_PRODUCTION.md)
- [../deploy/README.md](../deploy/README.md)
- [03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md)
- [11_QA_TESTING.md](11_QA_TESTING.md)
- [12_TROUBLESHOOTING.md](12_TROUBLESHOOTING.md)
- [DEPLOYMENT.md](DEPLOYMENT.md)
- [SYSTEM_DEPENDENCIES.md](SYSTEM_DEPENDENCIES.md)
- [For_developer.md](For_developer.md)
