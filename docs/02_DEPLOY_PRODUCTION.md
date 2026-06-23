# 02 Deploy Production

一句話說明：這份文件給正式部署者，說明如何把 `hackme_web` 安全地放到可長期營運的環境，而不是只在本機跑起來。

## 設計目的

本專案的風險不在「能不能起來」，而在「起來後是否留下錯誤預設、沒有備份、沒有驗證、把 runtime 汙染進 repo、或讓未授權流量直接打進 root 控制面」。這份文件把正式部署必做項整理成可執行順序。

## 使用方法

### 推薦部署路線

1. 先用 [01_DEPLOY_QUICKSTART.md](01_DEPLOY_QUICKSTART.md) 完成本機或 staging 驗證。
2. 先建立部署用環境變數與 runtime 目錄，再執行 `python3 server.py --doctor`。
3. 確認 runtime 根目錄集中在部署目錄的 `runtime/`，或明確用 `HACKME_RUNTIME_DIR` 指到隔離資料區。
4. 套用 repo 內 [deploy/README.md](../deploy/README.md) 的 Nginx / systemd 範本，並依主機調整 domain、憑證、路徑與 secrets。
5. 決定是否由反向代理處理 HTTPS。
6. 建立上線前 snapshot。
7. 跑功能 smoke、權限 pentest、壓力測試與必要的 production gate。
8. 做文件新鮮度檢查：確認 README / API reference / Trading / Server Mode /
   QA 文件描述的是目前 code，而不是研究草案或歷史報告。

### 重要部署決策

#### 1. Runtime 路徑

預設 runtime 放在目前部署目錄的 `runtime/`：

```text
./runtime
```

如果要在 `/tmp` 複製專案做隔離驗證，請在那份複製目錄內執行，或明確設定
`HACKME_RUNTIME_DIR=/tmp/<run>/runtime`。不要讓測試流程回寫來源 repo 的
`runtime/`。

所有 runtime 產物都應集中在：

- database
- logs
- chats
- anchors
- storage
- reports
- secrets / key files / certs

#### 2. HTTPS 與 Cookie

若服務會透過 HTTPS 對外：

- `FORCE_HTTPS=true`
- `SESSION_COOKIE_SECURE=true`
- `SESSION_COOKIE_HTTPONLY=true`
- `SESSION_COOKIE_SAMESITE=Strict`

如果 TLS 終止在反向代理：

- 應由前端代理處理憑證
- app 仍需正確設定 HTTPS / secure cookie
- 若信任 `X-Forwarded-For`，只信任你自己的 proxy IP，並設定
  `USE_XFF=true` 與 `TRUSTED_PROXY_IPS=...`
- 若使用 Gunicorn，請同步設定 `GUNICORN_FORWARDED_ALLOW_IPS` 為同一組
  可信任 proxy IP，讓 Gunicorn 接受代理傳入的 `X-Forwarded-Proto`；
  否則 `FORCE_HTTPS=true` 只會停留在 app 設定層，後端仍可能把請求看成
  plain HTTP。部署前可用 `python3 server.py --doctor` 檢查目前環境設定。

#### 3. Bootstrap 帳號

- `root` 僅用於第一次進站與高風險操作
- 正式上線通常不建議建立 `test` 帳號
- `admin` / `manager` 是否建立，取決於是否有實際管理流程

#### 4. 備份與恢復

正式上線前至少要確認三件事：

1. 你知道如何建立 server snapshot
2. 你知道 PointsChain safe-mode/forensic/branch recovery 與整站 snapshot restore 的界線
3. 你已在非 production 環境驗證過 restore / reset

補充：

- root 後台的 `上線前檢查` 是 preflight gate，不要求你先把站切成
  `production`。
- production profile 的 HTTPS / audit chain / Integrity Guard /
  browser-only 等安全設定會在 `GO_LIVE` 切換成功時自動套用，不應被理解成
  「必須先手動打開才能過檢查」。
- 被動健康檢查與審計頁現在只會回報 audit chain / integrity 異常，不會因 root 單純
  查看 `/api/admin/health` 或 `/api/admin/audit` 就自動把站切成 maintenance mode。
- Integrity Guard strict mode 在正常更新 / 重啟後若發現 high-risk findings，啟動會
  保持可用並留下警告，但 `GO_LIVE` / pre-production gate 仍會拒絕，直到 root review。

### 反向代理建議

正式部署建議使用 Nginx + systemd + Gunicorn：

- Nginx 是唯一對外入口，負責 TLS、proxy headers、第一層 rate limit 與大型 request timeout。
- Gunicorn 只綁 `127.0.0.1:8000`，不要直接裸露在公網。
- App 只信任本機 proxy 的 `X-Forwarded-*`。
- Web process 只負責 HTTP request lifecycle；交易 background engine、HLS、BT/direct link、
  local AI generation 等長任務必須獨立成 worker service，不應塞回 request worker。

repo 內提供可複製的範本：

- `deploy/nginx/hackme_web.conf.example`
- `deploy/systemd/hackme-web.service.example`
- `deploy/systemd/hackme-web.env.example`
- `deploy/systemd/hackme-web.tmpfiles.example`

#### Nginx

1. 複製範本：

```bash
sudo cp deploy/nginx/hackme_web.conf.example /etc/nginx/sites-available/hackme_web
sudo ln -s /etc/nginx/sites-available/hackme_web /etc/nginx/sites-enabled/hackme_web
```

2. 編輯以下值：

- `server_name hackme.example.com`
- `ssl_certificate`
- `ssl_certificate_key`
- `client_max_body_size`
- `limit_req_zone` rate / burst：範本已把 `auth`、`management`、`upload`、
  `static` 與一般 API 分成不同 zone。正式站請依硬體與客戶流量調整，不要讓登入、
  root/admin、上傳與靜態資產共用同一個邊界層桶。

3. 驗證並 reload：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

#### systemd web service

1. 建立部署使用者與目錄：

```bash
sudo useradd --system --home /opt/hackme_web --shell /usr/sbin/nologin hackme
sudo mkdir -p /opt/hackme_web /etc/hackme_web/secrets /var/lib/hackme_web/runtime /var/log/hackme_web
sudo chown -R hackme:hackme /opt/hackme_web /var/lib/hackme_web /var/log/hackme_web
sudo chown -R root:hackme /etc/hackme_web
```

2. 建立 venv，最小啟動只裝 runtime layer：

```bash
cd /opt/hackme_web
python3 -m venv .venv
.venv/bin/python3 -m pip install --upgrade pip
.venv/bin/python3 -m pip install -r requirements-minimal.txt
```

若 production 需要 hackme_web 自行載入 local Hugging Face / Diffusers 模型，再額外安裝：

```bash
.venv/bin/python3 -m pip install -r requirements-hf.txt
```

若 production 只是連線到已啟動的外部 ComfyUI API，例如 `http://127.0.0.1:8188`，
不需要安裝 `requirements-hf.txt`。

3. 安裝 env 與 tmpfiles：

```bash
sudo install -m 0640 -o root -g hackme deploy/systemd/hackme-web.env.example /etc/hackme_web/hackme-web.env
sudo install -m 0644 -o root -g root deploy/systemd/hackme-web.tmpfiles.example /etc/tmpfiles.d/hackme-web.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/hackme-web.conf
```

編輯 `/etc/hackme_web/hackme-web.env`，至少替換：

- `SESSION_SECRET`
- `CSRF_SECRET_KEY`
- `ROOT_INTEGRITY_SIGNING_KEY`
- `HTML_LEARNING_ROOT_PASSWORD`
- `TRUSTED_PROXY_IPS`
- `HTML_LEARNING_TRUSTED_HOSTS`

4. 安裝並啟動 service：

```bash
sudo install -m 0644 -o root -g root deploy/systemd/hackme-web.service.example /etc/systemd/system/hackme-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now hackme-web.service
sudo systemctl status hackme-web.service
```

5. 驗證：

```bash
curl -ksS https://<host>/api/version
curl -ksS https://<host>/readyz
journalctl -u hackme-web.service -n 100 --no-pager
```

#### Proxy / cookie 必要設定

若 TLS 在 Nginx 終止，env 必須一致：

```env
FORCE_HTTPS=true
SESSION_COOKIE_SECURE=true
USE_XFF=true
TRUSTED_PROXY_IPS=127.0.0.1,::1
GUNICORN_FORWARDED_ALLOW_IPS=127.0.0.1,::1
HTML_LEARNING_HOST=127.0.0.1
HTML_LEARNING_PORT=8000
```

如果你的 Nginx 不在同一台主機，`TRUSTED_PROXY_IPS` 與
`GUNICORN_FORWARDED_ALLOW_IPS` 必須改成實際 proxy IP，不能填任意網段。

#### Host header 與 request size guard

正式對外 Host 必須白名單化：

```env
HTML_LEARNING_TRUSTED_HOSTS=hackme.example.com,www.hackme.example.com
```

手動臨時測試 public IP 時，可用 `HTML_LEARNING_PUBLIC_HOST=203.121.227.18`
或 `HTML_LEARNING_PUBLIC_HOSTS=203.121.227.18,staging.example.com`；伺服器會自動加入
目前 `HTML_LEARNING_PORT` 的 `host:port` 變體。正式部署仍建議直接維護
`HTML_LEARNING_TRUSTED_HOSTS` 的完整 domain 清單。

Nginx proxy 應保留使用者原始 Host，讓 Flask `TRUSTED_HOSTS` 能阻擋 Host
header injection：

```nginx
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Host $host;
proxy_set_header X-Forwarded-Proto $scheme;
```

若 staging 使用內網 domain，也要把 staging domain 另外列入
`HTML_LEARNING_TRUSTED_HOSTS`。不要把 `*`、空白或不受控 public domain 放進白名單。

multipart request guard 預設：

```env
HTML_LEARNING_MAX_FORM_MEMORY_KB=512
HTML_LEARNING_MAX_FORM_PARTS=1000
```

這兩個值限制非檔案表單記憶體與 multipart parts 數量，用來降低 multipart DoS
風險。大型檔案上傳上限仍由 `HTML_LEARNING_MAX_CONTENT_MB` 與 Nginx
`client_max_body_size` 控制；不要為了檔案上傳而無限制放大 form memory 或 parts。

#### Maintenance bypass token

維護旁路 token 只接受 HTTP header：

```bash
curl -H "X-Maintenance-Bypass-Token: <token>" https://<host>/api/admin/health
```

不要把 token 放在 query string。`?maintenance_bypass_token=...` 必須視為無效，
因為 query string 可能出現在 proxy access log、browser history、referer 或錯誤報告。

#### Worker 邊界

正式部署不要使用 `python3 server.py`。該入口會啟動 Flask/Werkzeug direct
development server，並執行 `server.py` 的 `__main__` 區塊；它只適合單程序 debug、
`--doctor`、本機救援或重現舊式 in-process worker 行為。

`gunicorn server:app` 不會執行 `server.py` 的 `__main__` 區塊，因此不會自動啟動
舊式 in-process worker。這是正式部署應有的邊界：web service 不應擁有長任務生命週期。

目前已可由 web route 產生或管理的外部工作包括 HLS preparation、remote download
worker、Job Center 與 trading background queue；production 若要常駐處理這些工作，
請在對應 daemon entrypoint 完成後另建 systemd service。不要用提高 Gunicorn
workers/threads 的方式處理 HLS、BT、direct link、local AI generation 或交易常駐計算。

### 上線前必跑

```bash
python3 scripts/prepush/pre_push_checks.py
scripts/testing/pytest_in_tmp.sh -q tests
scripts/security/pentest/run_functional_smoke.sh --port 50741
scripts/security/pentest/run_pentest.sh --target https://<host>
python3 scripts/security/pentest/stress_test.py --target https://<host> --i-own-this-target
```

如果本次要評估整站 production readiness：

```bash
PYTHONPATH=. scripts/security/pentest/run_pentest.sh \
  --target https://<host> \
  --only whole-site-production-gate
```

如果你要一次產出 production gate 要求的完整 required report set，可直接用：

```bash
python3 scripts/security/gate/on_live_reports_make.py --base-url https://<host> --root-password '<ROOT_PASSWORD>'
```

這會把 raw outputs 放進 `runtime/reports/security/production_gate/runs/<RUN_ID>/`，
並把上傳用的穩定 payload 放在 `runtime/reports/security/production_gate/`。

production gate 報告對照表、固定 pytest 測項數與預設報告落點，統一放在
[11_QA_TESTING.md](11_QA_TESTING.md) 的「Production Gate 報告對照表」。
部署者上線前若要確認報告應該生成在哪裡、哪些是動態腳本、哪些是固定 pytest
回歸，請以那張表為準。

### 文件新鮮度檢查

部署者只應把下列文件當成現行操作依據：

- numbered guides：`00`、`01`、`02`、`03`、`05`、`06`、`08`、`09`、`11`、`12`
- domain `README.md`
- [API_REFERENCE.md](API_REFERENCE.md)
- [SYSTEM_DEPENDENCIES.md](SYSTEM_DEPENDENCIES.md)
- [security/PRODUCTION_SIGNOFF_CHECKLIST.md](security/PRODUCTION_SIGNOFF_CHECKLIST.md)

判讀規則：

- `archive/`、`evidence/`、舊 QA 報告只保留歷史脈絡，不是 deployment runbook。
- `AGENTS/research/` 是未來規格；除非同一功能也在正式操作文件中標成已實作，
  否則不要把它當成可上線功能。
- 文件若寫 `planned`、`staged`、`design`、`proposal`，部署時只能當待辦，不可
  當成已可操作能力。
- API 表若和 route handler 不一致，以上線前實際 route / smoke / API reference
  同步修正為 release blocker。

## 原理

- `python3 server.py --doctor` 不會幫你偷偷補建 runtime；部署者必須明確準備好目錄與權限。
- HTTPS、secure cookies、proxy trust、runtime 路徑與備份策略，決定的是營運風險，不只是功能是否可用。
- `hackme_web` 把 snapshot、PointsChain、audit chain、integrity guard 分開，是為了避免一個恢復動作默默覆蓋另一個保護層。

## 失敗情境與提示

- 上線後仍使用預設密碼：
  這不是小問題；立即 rotate，並檢查是否已有可疑登入。
- 把 repo 內 `bootstrap.schema.sql`、`logs/`、`storage/` 當 production runtime：
  之後很容易把營運資料誤提交或被開發流程覆蓋，應立即搬出 repo。
- 開了 HTTPS 但 cookie 仍不安全：
  檢查 `FORCE_HTTPS`、`SESSION_COOKIE_SECURE`、proxy 設定與瀏覽器 response header。
- snapshot 能建立但 restore 沒演練過：
  視同沒有備份。
- root 認為「只開某個功能就夠」但使用者一直看到功能關閉警告：
  請看 [03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md) 與 [05_FEATURES_OVERVIEW.md](05_FEATURES_OVERVIEW.md)
  的功能組合說明。

## 測試方式

- [security/PRE_RELEASE_CHECKLIST.md](security/PRE_RELEASE_CHECKLIST.md)
- [security/PRODUCTION_SIGNOFF_CHECKLIST.md](security/PRODUCTION_SIGNOFF_CHECKLIST.md)
- [11_QA_TESTING.md](11_QA_TESTING.md)
- 實際做一次 snapshot / restore / reset 演練
- 實際跑一次對外入口的 smoke / pentest / stress

## 相關文件連結

- [01_DEPLOY_QUICKSTART.md](01_DEPLOY_QUICKSTART.md)
- [03_ADMIN_GUIDE.md](03_ADMIN_GUIDE.md)
- [06_SECURITY_MODEL.md](06_SECURITY_MODEL.md)
- [09_SNAPSHOT_RESET_RESTORE.md](09_SNAPSHOT_RESET_RESTORE.md)
- [11_QA_TESTING.md](11_QA_TESTING.md)
- [12_TROUBLESHOOTING.md](12_TROUBLESHOOTING.md)
- [DEPLOYMENT.md](DEPLOYMENT.md)
- [security/PRE_RELEASE_CHECKLIST.md](security/PRE_RELEASE_CHECKLIST.md)
- [security/PRODUCTION_SIGNOFF_CHECKLIST.md](security/PRODUCTION_SIGNOFF_CHECKLIST.md)
