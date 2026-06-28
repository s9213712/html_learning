# hackme_web

[繁體中文入口](docs/README.zh-TW.md)

![status](https://img.shields.io/badge/status-active-2ea043)
![backend](https://img.shields.io/badge/backend-Flask-000000)
![database](https://img.shields.io/badge/database-SQLite-0f6ab4)
![security](https://img.shields.io/badge/focus-auth%20%2B%20RBAC%20%2B%20audit-b31d28)

**Current Release ID: `05_2026.06.08-001`**

`hackme_web` 是一個部署者優先的 Flask 單機站點，整合了帳號與權限、
Cloud Drive、ComfyUI、PointsChain、交易實驗、Snapshot/Restore 與
Server Mode 等能力。

這份 README 故意只保留最短入口。最近功能更新請看
[docs/UPDATE_SUMMARY.md](docs/UPDATE_SUMMARY.md)，完整文件地圖請看
[docs/README.md](docs/README.md)，系統依賴總表請看
[docs/SYSTEM_DEPENDENCIES.md](docs/SYSTEM_DEPENDENCIES.md)。
PointsChain 模擬鏈經濟層與 Phase gate 見
[docs/10_BLOCKCHAIN_WALLETIZATION_PREWORK_PLAN.md](docs/10_BLOCKCHAIN_WALLETIZATION_PREWORK_PLAN.md)。
finance 50K 後的 root/admin 非同步化與 snapshot 架構見
[docs/architecture/MANAGEMENT_PLANE_SCALING.md](docs/architecture/MANAGEMENT_PLANE_SCALING.md)。
root/admin 管理頁現在會在瀏覽器背景分頁停止 server output、request capacity、
system resource 輪詢，並補強手機版健康/資源面板排版；root operations
手機版 smoke 與前端 timing 觀測結果見
[docs/AGENTS/reports/2026-05-28_root_operations_long_needle_probe.md](docs/AGENTS/reports/2026-05-28_root_operations_long_needle_probe.md)；
營運端負載規則見
[docs/For_developer.md](docs/For_developer.md#management-ui-load-discipline)。
QoS 分類、app-level edge burst guard、抗 DoS 最後防線與 reverse-proxy 分工見
[docs/For_developer.md](docs/For_developer.md#server-qos-and-edge-guard)。
任務中心 list API 的 maintenance sweep 已節流並可觀測；帳號、會員治理、
註冊禮補發與 auth hot-state 索引的近期調整見
[docs/UPDATE_SUMMARY.md](docs/UPDATE_SUMMARY.md) 的 `05_2026.06.08-001`。
影音直接串流、即時轉封裝、預處理 HLS 三種客戶服務層與費率差異、X-Accel
送檔 offload、Standard 即時轉封裝的同機併發控制，以及 Premium HLS worker sizing / profile matrix 見
[docs/video/VIDEO_STREAMING_SERVICE_TIERS.md](docs/video/VIDEO_STREAMING_SERVICE_TIERS.md)。
ComfyUI / GGUF 與 HF / Diffusers 是兩組不同設定：ComfyUI / GGUF
只負責本地或遠端 ComfyUI 執行後端，HF / Diffusers 只負責 Hugging Face
模型來源與純 Python 推論設定；切到 HF 設定頁不會停用或覆寫
ComfyUI / GGUF 的遠端/本地設定，也不會顯示 ComfyUI Account/Civitai
等 ComfyUI 專屬欄位。前台 `Civitai / 模型管理` 現在和
`Workflow`、`歷史重跑` 一樣固定在 AI 產圖 subnav；root 在 ComfyUI
本地與遠端模式都可直接使用模型匯入。ComfyUI GGUF 只能走官方建檔 profile；
新增 profile、遠端實測、已安裝 GGUF 清單與多精度選單流程見
[docs/AGENTS/skills/hackme-gguf-profile/SKILL.md](docs/AGENTS/skills/hackme-gguf-profile/SKILL.md)。

## AI Agent / ComfyUI Image Editing Status

本輪更新把站內 AI Agent 從固定捷徑式操作往「自然語言理解 + 受控站內工具執行」
推進：前台聊天介面可由自然語言選用 ComfyUI 生圖、圖生圖、歷史重跑、
下載/雲端/治理等站內 write tools；root 端可管理可寫 tools；模型列表改由
後端/供應商設定提供，不再在前台硬寫死已不存在模型。Agent 仍被限制在站內
runtime、雲端硬碟與已授權 API 範圍，不能直接寫伺服器任意檔案。

ComfyUI 產圖路徑也做了低 VRAM / 慢速 workflow hardening：Qwen Image Edit
長步驟不再被過短 stale timeout 誤判失敗；Qwen edit workflow 可從 partial /
history 取回正式後處理輸出；產圖進度、tokens/s / total tokens、歷史重跑、
GGUF / Anything2Real / SDXL inpaint workflow profile 與 ComfyUI 設定分區都已納入
前台與測試覆蓋。完整的大量圖片與對話紀錄保留在本機
`docs/AGENTS/reports/`，不進 GitHub；GitHub 只保留摘要：
[docs/AGENTS/AI_AGENT_I2I_TEST_SUMMARY.md](docs/AGENTS/AI_AGENT_I2I_TEST_SUMMARY.md)。

目前已確認的 i2i 能力邊界：

- 可用：保留 1080x1920 source 比例、Qwen Image Edit 2509 語意改圖、正式輸出
  1080x1920 後處理、髮色/髮型/表情/配飾/一般服裝替換、背景保留、進度追蹤。
- 部分可用：高強度服裝 + 體態混合修改。`denoise=0.85` 能把和服換成白色蕾絲
  長裙並改體態，但會改變原本雙手 clasped 姿勢，且容易漂到高開衩旗袍感。
- 仍需修正：高強度 i2i 時的姿勢保真、negative prompt 合併、透明/高開衩/
  qipao/cheongsam 漂移，以及 workflow run `params_json` 中 width/height /
  denoise 記錄與實際 queue 參數不一致。

本輪關鍵測試參數摘要：

| Run | Source / Output | Workflow | Model / LoRA | Params | Result |
| --- | --- | --- | --- | --- | --- |
| v5 body/lace/proportions | source 被錯誤壓成 `1024x1024`，要求 output `1080x1920` | `origin_qwen_image_edit_2509` | `qwen_image_edit_2509_fp8_e4m3fn.safetensors` + Lightning 4-step LoRA | steps 4, cfg 1, denoise 0.55 | FAIL。測試工具先破壞比例，產生上下模糊延展帶，不能當模型能力判定。 |
| v6 body/lace/proportions | source `1080x1920`，final output `1080x1920` | `origin_qwen_image_edit_2509` | 同上 | steps 4, cfg 1, denoise 0.55 | FAIL。比例修好，但模型過度保守，仍保留和服，服裝/體態目標未達成。 |
| v7 body/lace/proportions | source `1080x1920`，final output `1080x1920` | `origin_qwen_image_edit_2509` | 同上 | steps 4, cfg 1, denoise 0.85, strengthened English edit prompt | PARTIAL / FAIL。服裝與體態有改，但動作被改、裙子漂到高開衩旗袍感，不符合保真規則。 |

下一輪建議測試：`denoise=0.70-0.75`，明確加入 `keep both hands clasped in front
of chest`，並補強 negative prompt：`qipao, cheongsam, high slit, exposed leg,
transparent dress, pose drift`。若混合任務仍不穩定，改成兩段式：先只換服裝，
再測體態比例。

## First-Time Deployer Route

1. [docs/00_START_HERE.md](docs/00_START_HERE.md)
2. [docs/01_DEPLOY_QUICKSTART.md](docs/01_DEPLOY_QUICKSTART.md)
3. [docs/02_DEPLOY_PRODUCTION.md](docs/02_DEPLOY_PRODUCTION.md)
4. Production templates: [deploy/README.md](deploy/README.md)

## Quick Start

本 repo 的公開入口現在只保留三條：

- `python3 -m pip install -r requirements-minimal.txt`
  只安裝最小啟動伺服器所需套件。開發測試請再加
  `requirements-dev.txt`。遊戲 / puzzle 相關套件已拆到
  `requirements-games.txt`，但因目前遊戲 routes 會在啟動期載入，所以 minimal
  仍會引用它。連線到外部 ComfyUI API 不需要額外 heavyweight
  AI runtime；只有啟用本機 Hugging Face / Diffusers 後端時才加
  `requirements-hf.txt`。
  舊流程仍可用 `requirements.txt` 一次安裝主站與開發測試相容依賴。

- `python3 server.py --doctor`
  檢查目前 runtime 環境是否已存在且可寫；缺目錄時會明確報錯，不會靜默補建。
- `python3 server.py`
  手動 / 本機檢查入口。只有 doctor 通過時才會繼續啟 server。正式對外服務請使用
  `deploy/nginx/` + `deploy/systemd/` 的 Nginx / Gunicorn 範本。
- `./test_for_develop.sh`
  開發專用入口。它會先把 repo 複製到 `/tmp/.../hackme_web`，把開發用 runtime、
  cache、venv 都留在 `/tmp`，再從 `/tmp` 內的 `server.py` 啟動。
  若你要驗 `server mode` / `production gate` 的 `target_commit` 規則，
  `HTML_LEARNING_GIT_REPO_DIR` 必須指向**真實 git repo**，不可指向沒有 `.git`
  的 `/tmp` copy；`test_for_develop.sh` 現在預設會保留 source repo 當 target
  commit 來源。

日常原則仍然不變：**不要直接在 repo 工作樹內啟 server 或 pytest**。
請優先使用 `/tmp` 複本，避免 `runtime/`、cache、pycache 汙染 repo。

## Local Workflow

```bash
python3 server.py --doctor
./test_for_develop.sh --port 50785
scripts/testing/pytest_in_tmp.sh -q tests
```

臨時用 LAN / NAT public IP 測試 dev server 時，請明確加入 public Host allowlist：

```bash
./test_for_develop.sh --host 0.0.0.0 --port 5000 --public-host 203.121.227.18
```

這會把 public host 與 `host:port` 變體加進 `HTML_LEARNING_TRUSTED_HOSTS`，
並印出外部測試 URL；背景模式也會在 runtime logs 目錄產生並列出
`server_direct.out`、Gunicorn access log 與 error log。
開發時若只想先排除 Host allowlist 干擾，可暫時關閉 trusted-host 檢查：

```bash
./test_for_develop.sh --host 0.0.0.0 --port 5000 --allow-any-host
```

這會設定 `HTML_LEARNING_DISABLE_TRUSTED_HOSTS=1`，只建議用於臨時開發測試；
正式部署不要關閉 trusted-host 防護。
若手動直接啟動 `server.py`，請設定 public host env，程式會自動加入目前 port 變體：

```bash
HTML_LEARNING_HOST=0.0.0.0 HTML_LEARNING_PORT=5001 HTML_LEARNING_PUBLIC_HOST=203.121.227.18 python3 server.py
```

手動開發啟動也可用 `HTML_LEARNING_DISABLE_TRUSTED_HOSTS=1` 暫時關閉 Host 檢查。

互動模式若執行 capacity test，腳本會先輸出實測結論：推薦的 workers x threads、
worker-thread lanes、最大安全 concurrent accounts、p50/p95/p99/max 延遲、status / failure
counts、CPU peak、測過的 profiles / account ladder、load profile、測項分類、最慢 labels、
UX degradation / application limit / server instability 邊界，以及 JSON report 路徑。接著再
詢問要套用本次結果、重新測試、改用手動參數，或放棄 probe 改採保守硬體 fallback。
CLI 模式會直接套用 probe 結果。若 probe 沒有產生可用 recommendation，互動模式不會提供
apply 選項，而會列出各 profile 的 setup/round error 並要求重測、手動輸入或 fallback。
capacity probe 預設允許 isolated profile 建立 venv；只有明確設定
`HACKME_DEV_CAPACITY_PROBE_INSTALL=0` 時才禁止安裝。
若機器資源有限，先用硬體量級 preset 限制 probe 壓力：
`--capacity-probe-tier sbc|legacy|laptop|midrange|highend`。`sbc` 適合單板電腦 /
小型 VM，會限制為最小讀取型 probe 並設 60 秒總時限；`legacy` 適合老桌機或低功耗 NAS，
會限制為低衝擊讀取型 probe 並設 120 秒總時限；`laptop` 適合一般筆電，會使用小型 basic
member workflow 並設 180 秒總時限；`midrange` 適合中階主機。
`highend` 沒有 account / round 上限，會持續增加負載直到 UX degradation、application limit、
server instability 或 hard failure 停止，可能讓主機暫時卡死或崩潰；只有在能接受這個風險時使用。
若要乾淨停止前一次由此腳本啟動、正在佔用同一 port 的 dev server 與其衍生
process group / child tree：

```bash
./test_for_develop.sh --port 5000 --shutdown
```

若你真的要在目前工作樹直接啟動，先自己準備好 runtime 目錄，再執行：

```bash
python3 server.py --doctor
python3 server.py
```

正式部署不要直接對外開 `python3 server.py`。請先看
[docs/02_DEPLOY_PRODUCTION.md](docs/02_DEPLOY_PRODUCTION.md)，再套用
[deploy/README.md](deploy/README.md) 的 Nginx/systemd 範本。

對外部署時務必設定基底層 request guard：

- `HTML_LEARNING_TRUSTED_HOSTS=example.com,www.example.com`
  必須列出實際 public Host；反向代理要保留原始 `Host` header。
- 臨時手動測試可用 `HTML_LEARNING_PUBLIC_HOST=203.121.227.18` 或
  `HTML_LEARNING_PUBLIC_HOSTS=203.121.227.18,host.example`，伺服器會自動加入目前 port 變體。
- `HTML_LEARNING_DISABLE_TRUSTED_HOSTS=1` 只供開發排錯使用；production 不要設定。
- 維護旁路 token 只接受 `X-Maintenance-Bypass-Token` header，不接受
  `?maintenance_bypass_token=...` query string，避免 token 被 access log、
  browser history 或 referrer 洩漏。
- multipart 表單限制預設為
  `HTML_LEARNING_MAX_FORM_MEMORY_KB=512`、
  `HTML_LEARNING_MAX_FORM_PARTS=1000`。正式站可依上傳需求調整，但不要移除。

啟動後請以 server console 或腳本印出的實際 URL 為準。一般情況：

- `./test_for_develop.sh`：通常會是 `https://127.0.0.1:<port>/`
- `python3 server.py`：依 `server_ssl_enabled` 與 runtime cert 狀態決定 HTTP/HTTPS

若不確定，直接探測：

```bash
for scheme in https http; do
  curl -ksSf "${scheme}://127.0.0.1:5000/api/version" && echo "$scheme works"
done
```

Fresh local databases 會建立：

- `root/root`
- `admin/admin`
- `test/test`

`test_for_develop.sh` 會額外關掉強制改密碼、登入安全限制、Integrity Guard、
audit chain 等妨礙開發的保護，並保留預設帳密方便反覆 debug。同時它也會把
trading market registry 切成開發可測狀態（`allow_spot / allow_margin /
allow_bots / allow_risk_grade_usage = 1`），避免 `/tmp` 開發站一開機就把現貨、
Grid Bot 與借貸交易整體封死。若你要手動啟動，
仍可在第一次建 DB 前先設：

- `HTML_LEARNING_ROOT_PASSWORD`
- `HTML_LEARNING_MANAGER_PASSWORD`
- `HTML_LEARNING_TEST_PASSWORD`

## Documentation Map

- 文件總索引：[docs/README.md](docs/README.md)
- 系統依賴總表：[docs/SYSTEM_DEPENDENCIES.md](docs/SYSTEM_DEPENDENCIES.md)
- root/admin 管理入口：[docs/03_ADMIN_GUIDE.md](docs/03_ADMIN_GUIDE.md)
- 使用者教學：[docs/04_USER_GUIDE.md](docs/04_USER_GUIDE.md)
- 功能總覽：[docs/05_FEATURES_OVERVIEW.md](docs/05_FEATURES_OVERVIEW.md)
- QA 與驗證路線：[docs/11_QA_TESTING.md](docs/11_QA_TESTING.md)
- 故障排查：[docs/12_TROUBLESHOOTING.md](docs/12_TROUBLESHOOTING.md)
- 最近更新摘要：[docs/UPDATE_SUMMARY.md](docs/UPDATE_SUMMARY.md)

## Local Checks

```bash
python3 scripts/prepush/pre_push_checks.py
scripts/testing/pytest_in_tmp.sh -q tests
python3 scripts/testing/playwright_platform_health_check.py
python3 scripts/testing/long_needle_simulation_probe.py --profile quick
```

`playwright_platform_health_check.py` 會啟動隔離 QA server 到 `/tmp`、使用隨機非
`5000` port，並以真實瀏覽器驗 Job Center、Notification Center、Share Link
Management、Trading Asset Overview 與 mobile viewport。它不是正式 runtime 測試，
報告會寫到該次 `/tmp/.../reports/qa/`。

`long_needle_simulation_probe.py` 會在同一個隔離 runtime 內串接
PointsChain/private-chain destructive stress 與全功能 system stress；GitHub
Actions 的 `long-needle-simulation` workflow 會在 PR/push 相關路徑跑 quick，
nightly 跑 medium。
