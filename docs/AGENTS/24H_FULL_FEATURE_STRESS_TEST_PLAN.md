# hackme_web 24 小時全功能高壓長測執行規格

計畫審核：`CONDITIONAL APPROVAL`

正式 24 小時授權：`NOT GRANTED`

目前階段：`HARNESS HARDENING`

日期：2026-07-12（Asia/Taipei）  
已確認計畫 checkpoint：`05.AI_Agent@94146cc559fd435241dccfce41dd30e55233e27a`  
正式 24 小時計時：`尚未開始`

本文件是已條件式核准的目標規格，不是 QA 結果報告，也不代表目前 harness 已經具備所有能力。
只有第 3 節列出的 machine gate 全數 PASS，且使用者另行給予 commit-bound 正式授權後，才可 freeze 並啟動 H0。

## 1. 目標與不可變規則

本輪目標是在隔離環境中完成一次連續 24 小時的全功能高壓長測，先修復所有已確認問題，
再驗證功能正確性、資料一致性、長時間穩定性、延遲、資源使用、DB lock、靜默失敗、
桌機 UI、手機 UI 與操作人性化程度。

不可變規則：

1. 只有 source freeze 後，primary、recovery、連續負載、資源監控與 watchdog 全部成功啟動後，
   才開始累計 `continuous_active_seconds`。
2. 權限取得、依賴安裝、測試設計、當機保護、短版 smoke、修碼、回歸與人工等待不計入 24 小時。
3. 正式輪中若需要修改任何 Python、Shell、JS、CSS、HTML、SQL 或測試 harness，該輪立即失效；
   修完、驗完、重新 source freeze 後從 0 計滿 86,400 秒。
4. WSL／主機重啟後可以恢復診斷與尚未完成的功能 coverage，但不得把兩段運行時間拼成正式 24 小時。
5. HTTP 200／202 本身不是成功。所有非同步工作必須到 terminal success，並驗證真實副作用、內容 hash、
   ledger／audit 或輸出檔。
6. 不允許 `skip`、`expected_gap`、fallback status request 或缺少外部依賴被計為正式 PASS。
7. 正式運行期間 agent 可做唯讀監控與分析，但不得修改 frozen source。
8. 不另寫 QA 結果 Markdown 報告；保留 machine-readable JSON/JSONL、log、screenshot、影音與 checkpoint，
   最終結果直接在對話交付。這份執行前計畫文件不受此限制。

## 2. 範圍解讀與既有證據去重

### 2.1 「整個伺服器備份」的核准定義

本計畫暫定將「整個伺服器備份」解讀為：隔離 hackme_web 測試實例的完整 application runtime，包含
config、storage、uploads、HLS、database、logs、snapshot、server-mode state 與 restart metadata；不包含
Windows 主機或整個 WSL OS 的 block-level image。archive 排除 `/proc`、socket、PID file 等非持久物件，
並保存檔案 manifest、hash、可讀性與 restore 後一致性證據。

### 2.2 不重跑已充分驗證的相同測試

既有 3335 pytest、pre-push、deep Playwright、member probe、交易數學、PointsChain 壓力、snapshot policy、
長影音與 UI 報告只作歷史基線，用來避免逐支重跑完全相同的腳本；它們不能代替本次 current-source 的
24 小時正式證據。

本輪對未變更的一般功能採「連續多帳戶輪訓＋壓力下抽查」；對已變更、複雜、非同步、重啟相關或過去
只有單元測試的功能，執行完整正向、錯誤、權限、併發、持久化與恢復流程。

## 3. 開始前必須完成的 gate（不計時）

### 3.1 當機保護 gate

- campaign process tree 必須置於可驗證的 cgroup/systemd scope：
  - `MemoryHigh=7G`
  - `MemoryMax=8G`
  - `MemorySwapMax=1G`
  - `CPUQuota=600%`
  - `TasksMax=768`
  - 降低 I/O weight，避免壓測拖垮 Codex／WSL 互動程序。
- 若目前 WSL 無法建立或驗證硬限制，正式模式 fail closed，不啟動。
- supervisor、watchdog 必須是 campaign scope 外的獨立 process；primary、recovery、load generator、browser、
  ffmpeg、BT、ComfyUI 與 scenario worker 必須以 `/proc/<pid>/cgroup` 和 `cgroup.procs` 雙向證明位於 scope 內。
- watchdog 只以 PID/starttime/boot ID、cgroup path/device/inode、heartbeat、checkpoint 與 control file 溝通；
  不共享 orchestrator 記憶體。任何歸屬或控制檔無法確認時 fail closed。
- 禁止在長測期間對整個 `$HOME` 做無界 `find`、`du`、hash 或全文掃描；所有掃描限制在 repo 或 run root。
- 大型來源 manifest：H0 與 H24 做完整 hash；中間使用 Git status、inotify、mtime、size 輕量偵測，
  只有變動檔才重算 hash。formal baseline 必須 clean；非正式 dirty baseline 也必須 hash 所有 untracked 內容。
- 會影響 launcher 的 reviewed gitignored inputs（目前為 `.hackme_capacity_defaults.env`、
  `.hackme_capacity_report.json`）必須以獨立 protected manifest 凍結「存在／缺失」、型別與內容 digest；
  create/change/delete/rename/symlink 都立即 invalid，不能被一般 ignored-runtime 規則排除。

### 3.2 持久 checkpoint 與現場保存

- 大型 runtime 與測試產物：`/tmp/hackme_web_campaign_24h_<UUID>/`。
- 不含密碼的小型 checkpoint mirror：
  `$HOME/logs/hackme_web_campaign_24h/<UUID>/campaign.checkpoint.json`。
- checkpoint 每 60 秒 atomic replace，至少包含：
  - campaign UUID、source digest、commit、正式連續開始時間；
  - 目前 phase、load level、完成 scenario／step；
  - artifact path、SHA-256、非同步 correlation/job IDs；
  - primary/recovery PID、port、runtime root、health；
  - 資源與安全門檻狀態。
- heartbeat 超過 120 秒未更新時，watchdog 保存受限範圍的 `ps`、`iostat`、PSI、GPU、cgroup、
  server log tail 與 journal tail，先停 load generator，再標記 `interrupted`。
- checkpoint、stdout、log 不能包含 root/manager/test/member 密碼或 API key。

### 3.3 資源安全閥與分級升壓

正式 active time 從 concurrency 4 開始，10 分鐘後升 8、再 20 分鐘升 16、再 30 分鐘升 32；
之後維持 target load。HLS、BT、full backup、三瀏覽器與 ComfyUI 使用重工作協調器錯峰。

降載條件（連續 3 個樣本）：

- ordinary p95 或 sentinel p95 超過門檻；
- controlled 503 rate 超過 5%；
- memory PSI／I/O PSI 顯著上升；
- campaign RSS、WAL growth、FD 或 task count 異常成長；
- GPU VRAM／溫度／queue backlog 過高。

降載順序：32 → 16 → 8 → 4，並暫停新的 HLS、BT、backup、ComfyUI job。持續降載超過 10 分鐘，
正式輪判 FAIL，但繼續保存診斷，不可把低壓時間冒充高壓時間。

硬停條件：

- host `MemAvailable < 1 GiB`；
- 測試磁碟可用空間 `< 20 GiB`；
- GPU VRAM `> 92%` 且無法釋放／降載；
- cgroup OOM counter 增加；
- 連續未規劃 health transport failure；
- SQLite quick/integrity check failure；
- append-only finance／PointsChain invariant failure；
- watchdog heartbeat stale。

硬停時先停止新負載與 load generator、atomic checkpoint、保存現場；server 最後才停止。

### 3.4 依賴與來源 gate

- Git worktree 除明確允許的測試計畫／source freeze commit 外必須 clean。
- quick pre-push、harness focused tests、180 秒 campaign smoke 必須全 PASS。
- 十一項 startup gate 不能信任摘要中的 PASS/布林/數字；每項必須由 gate-specific raw artifact 重新推導，
  並驗證 schema、campaign/commit/source binding、絕對路徑、size、SHA-256、穩定重讀與格式。raw artifact
  不得跨 gate 重用；舊 v2、自填、simulated 或 component-only evidence 全部拒絕。
- Chromium、Firefox、WebKit desktop/mobile executable 必須能實際 launch。
- `ffmpeg`、`ffprobe`、gunicorn、Playwright、Transmission／受控 BT seed、真實 ComfyUI endpoint、
  真實 AI provider 必須可用；缺少任一強制依賴即 BLOCKED，不得 optional PASS。
- 正式啟動前清除 repo 內 `.pytest_cache` 與 `__pycache__`，執行時 pycache 導向 `/tmp`。
- 先一次取得本機 bind、cgroup、背景 supervisor 與必要外部 endpoint 權限；等待授權不計時。

### 3.5 正式啟動前 machine gate

以下 gate 必須綁定同一 Git commit，狀態為 `PASS`、`machine_verified=true`，並由 schema-versioned gate bundle
重新讀回驗證；否則正式 supervisor 拒絕啟動：

1. `cgroup_limits_verified`
2. `external_watchdog_verified`
3. `hard_stop_injection_verified`
4. `checkpoint_recovery_verified`
5. `source_drift_detection_verified`
6. `sample_schema_completeness_verified`
7. `production_security_sentinel_verified`
8. `all_mandatory_dependencies_verified`
9. `180_second_smoke_passed`
10. `60_minute_rehearsal_passed`
11. `worktree_clean_and_frozen`

### 3.6 四級啟動流程

- Level 0 — Harness 自測：刻意注入 orchestrator `SIGSTOP`、frozen source 變動、disk-low、SQLite lock、
  primary death、heartbeat stale；證明 admission 會原子關閉、active clock 立即停止、artifact 可恢復且錯誤不會判綠。
- Level 1 — 依賴與功能 preflight：三瀏覽器真啟動、真 ComfyUI 生圖、真 AI provider、受控 BT、
  ffmpeg/HLS、backup/restore 與 production security sentinel 全數成功；正式開始前缺少依賴為 `BLOCKED`。
- Level 2 — 短版 campaign：固定 180 秒只驗 harness lifecycle；再跑固定 60 分鐘 rehearsal，縮時觸發
  每個 mandatory scenario、planned restart、backup、ComfyUI、BT 與 UI，任何漏跑均 FAIL。
- Level 3 — 正式 24 小時：只有前三級全部 PASS 且取得另行正式授權後，才可建立新 UUID 開始。

### 3.7 狀態、計時與結果分類

durable state machine 僅允許明確轉移：`PREPARING → PREFLIGHT → FROZEN → ACTIVE/DEGRADED →
STOPPING_LOAD → PRESERVING_EVIDENCE → INTERRUPTED/FAILED`，正常完成則為 `COMPLETED → AUDITING → PASS`。
OOM、DB invariant、source drift、heartbeat stale、disk <20 GiB 或 MemAvailable <1 GiB 必須在同一原子更新中
關閉 admission 與 active clock，不能等 cleanup 才停止計時。

每秒只有下列條件同時成立才可增加 `continuous_active_seconds`：source frozen、primary/recovery/security ready、
watchdog/monitor/load generator alive、無 hard stop 且 state 為 ACTIVE。一旦任一條件失效，該 formal segment 永久
invalid，修復後也不能接續補時。

診斷分類分為：`PASS`、`FAIL_PRODUCT`、`FAIL_HARNESS`、`FAIL_INFRA`、`FAIL_EXTERNAL`、`BLOCKED`、
`INVALIDATED`、`INTERRUPTED`。正式總結仍只有 PASS 與非 PASS；正式開始前 dependency outage 是 BLOCKED，
正式輪中的 dependency outage 是 FAIL_EXTERNAL，不可混成產品 PASS。

### 3.8 Scenario execution contract

每個 mandatory scenario 在 source freeze 前必須固定 schema version、角色、前置條件、steps、terminal state、
side-effect assertions、cleanup assertions、artifact、timeout，以及 `earliest_start`、`preferred_window`、
`hard_deadline`、`resource_class`、`conflicts_with`。HTTP 200/202 只能證明受理；沒有 terminal success、真實副作用、
資料一致性、restart persistence、cleanup 與 artifact validation 時，scenario 不得 PASS。

重工作以 deadline scheduler 排程，不以固定時段互相阻塞；但所有 mandatory contract 仍須在 H24 前完成。
每次失敗保存獨立 `attempt_<UUID>`，不得覆寫舊證據。

## 4. 全功能 coverage matrix

| 功能域 | 正向主流程 | 必測錯誤／權限／壓力／恢復 | 必要證據 |
|---|---|---|---|
| Auth／帳戶 | 註冊、登入、登出、session refresh、CSRF、頭像、profile、好友 | 錯密碼、expired/missing CSRF、double submit、root/manager/user/member level、quota/rate-limit、跨帳戶隔離 | 每角色 2xx、預期 4xx、session/CSRF rotation、無越權 |
| Server/Admin/Security | health/readiness/security center、設定、launch requirements、log chain | safe/dev/production 邊界、敏感操作 confirm、失敗訊息、壓力中 readiness | readiness latency、audit/log-chain、設定還原 |
| Cloud Drive | txt/md/json/html/pdf/png/jpg/zip/video upload、preview、E2EE、direct URL | malformed E2EE、SSRF/private URL、quota、分享密碼/max views/revoke、重啟持久化 | 檔案 hash、preview/download、分享前後狀態 |
| 長影音 | 至少 3900 秒、雙音軌、字幕、多人平行 upload、publish、HLS | upload/轉碼等待、playlist/variant/segment、錯密碼、撤銷、隨機 seek、手機、primary planned restart | terminal ready、segment/hash、首幀與 seek latency、重啟後同一資源可播 |
| Cloud Drive 分享串流 | Drive MP4/MKV → stream prepare → storage share | password unlock、master/variant/segment/subtitle、realtime proxy、desktop/mobile、revoke | `/api/storage/shared/...` live chain 與撤銷後拒絕 |
| BT/magnet/.torrent | 受控本地 seed、magnet 與 torrent file 真下載 | terminal success、bytes/hash、pause/resume、服務重啟、失敗可見、下載後 preview/share/stream/HLS | seed/download hash、progress history、重啟續傳、影音播放 |
| Video/Album/Chat share | 影片 publish、album share、聊天嵌入 | 密碼錯誤、max views、privacy mode、撤銷、mobile embed | playback/share session 與撤銷行為 |
| ComfyUI | 真 backend 生圖、history/favorites、官方 templates | 全部官方模板實跑、自創 workflow create/import/edit/run/output/delete、付費確認、離線/缺 node/timeout、手機 UI | prompt/job terminal、輸出 image hash、workflow round-trip、錯誤可見 |
| 現貨交易 | market/order/match/cancel、wallet/ledger | concurrent order、cancel race、fee/Decimal、permission、idempotency、reserve/nonnegative | order/ledger/audit、獨立 Decimal 對算 |
| 借貸／保證金 | open/add/withdraw collateral/accrue interest/close | liquidation、risk rejection、insufficient collateral、restart recovery | position、interest、collateral、liquidation與 ledger invariant |
| Bot／背景交易 | grid、DCA、conditional、TP/SL、background scan/match | 無瀏覽器執行、job terminal、停用/重啟、重複觸發、stuck job | bot status、job center、orders/trades、restart continuity |
| 自創交易 workflow | UI create/edit/save/export/import、backtest、enable、trigger、trade | invalid graph、權限、重啟持久化、cleanup | workflow JSON round-trip、backtest、真成交與 ledger |
| Points wallet/ledger | wallet、ledger、catalog、admin adjustment、transfer | overspend、replay、idempotency、external address、高頻 transfer/trade | balance/nonnegative、transaction hash、chain verify |
| PointsChain 分支／治理 | finality、branch、dispute、proposal/sponsor/vote/execute | 非法投票、重複執行、權限、append-only correction | proposal lifecycle、branch/hash-chain、audit |
| 錢包遭駭 | 模擬 key compromise、theft、attacker second spend | freeze、risk marker、公開 dispute、補償、governed recovery branch | theft tx、阻擋證據、補償 tx、branch verify |
| Snapshot／完整 server backup | snapshot、完整 runtime archive、restore | storage/core restore、finance/PointsChain/trading forensic-only、archive corruption、SQLite quick check | archive hash/size、restore policy、DB checks、狀態差異 |
| Restart／緊急事件 | planned restart、incident enter、診斷/repair、resolve、mode restore | outage/ready、PID/boot change、incident restrictions、失敗 fallback | outage timeline、readiness、security/log/finance/chain verify |
| 時鐘／排程／重投 | timezone、expired session/job lease、idempotency key | clock jump、client timeout/retry、重複投遞、跨 worker session/CSRF rotation | 同一動作只產生一次副作用、lease/session/audit 證據 |
| 檔案系統故障 | atomic write、archive、rename、snapshot while writing | read-only、ENOSPC、partial archive、rename failure、持續寫入中的 point-in-time backup | fault ID、admission stop、archive manifest、restore 一致性 |
| Migration／舊 runtime | current schema 啟動、舊 runtime restore 到 current source | migration 中斷、版本不相容、rollback policy | schema version、migration log、SQLite check、domain invariant |
| 程序／埠清理 | planned restart 與 final cleanup | 舊 gunicorn、ffmpeg、browser、BT child、process group、listening port、open FD 殘留 | PID/starttime、descendant、socket、`/proc/<pid>/fd` 與 orphan inventory |
| 社群治理 | forum/thread/reply/report/moderate、chat/PM/notification、profile/friend/block | manager/root/user 權限、proposal/vote/execute、rate-limit、mobile | thread/moderation/proposal lifecycle、通知與 audit |
| Games | chess、solo score、主要遊戲路由 | invalid score、權限、重複 submit、壓力中載入 | score/leaderboard、console/network 無錯 |
| AI Agent | 所有 exposed tool 做 schema/role/API mapping；每個 domain 做真實正向 journey | Drive/share、影片/HLS、交易/借貸/bot/workflow、ComfyUI、社群治理、launch preflight、incident/restart；confirm/role/audit guard；settings finally restore | role-scoped catalog、每個 live action 的副作用、job terminal、audit、設定還原 |
| UI/桌機/手機 | 全功能 navigation、form、modal、loading/error、back/refresh | 360/390/412 mobile、1366/1440 desktop、double submit、slow network、touch/overflow/clipping、三瀏覽器 | screenshot、console/network、touch geometry、interaction latency |

## 5. 24 小時排程

continuous multi-account full-feature soak、root/manager sentinel、資源取樣、DB/WAL/lock 與 silent-failure
偵測從 H0 持續到 H24。下列是重功能的主排程；一般功能不會停止輪訓。

| 正式時間 | 主要工作 | 說明 |
|---|---|---|
| H0–H1 | 4→8→16→32 分級升壓 | 建立 baseline；驗證安全閥、latency、503、RSS/CPU/IO/GPU |
| H1–H4 | 長影音＋Cloud Drive 分享串流 | upload、HLS、雙音軌、字幕、desktop/mobile、分享、primary restart continuity |
| H4–H7 | BT＋media proxy | magnet/`.torrent` 真完成、hash、pause/resume、restart、share/stream/HLS |
| H7–H11 | 交易全域 | spot、lending/margin、interest/liquidation、bots/background、自創 workflow、full concurrency |
| H11–H14 | PointsChain＋錢包事故 | HFT、finality、branch、治理、theft/freeze/dispute/compensation/recovery |
| H14–H17 | Backup/restore/restart/incident | snapshot、完整 runtime archive、restore、SQLite、緊急事件與上線 readiness |
| H17–H20 | AI Agent＋真 ComfyUI | 全 domain 正向 actions、營運輔助、官方與自創 workflow 真執行 |
| H20–H22 | 社群／治理／其他全功能 | forum/chat/PM/notification/profile/friends/games/album/權限在壓力下交互抽查 |
| H22–H24 | 三瀏覽器 UI/手機＋最終 invariants | 長片與分享終檢、UX、launch gate、DB/log/finance/chain/silent-failure 終檢 |

重工作若因安全協調器等待，不得超出 H24 才在無壓力狀態補做；未在連續負載期間完成即正式 FAIL。

## 6. 監測項目與取樣

每 5 秒取樣，目標至少 17,280 筆；完整率必須 ≥95%。

每筆樣本必須包含 `sample_schema_version`、`expected_fields`、`valid_fields`、`missing_fields`、
`collector_errors` 與 `hard_limit_state`。完整率按每個 mandatory field 的有效樣本比例計算；只有 JSONL 行數、
空值或採集器持續錯誤都不能達成 95%。

### 6.1 延遲與錯誤

- request latency：p50/p95/p99/max，依 ordinary、sentinel、upload、HLS playlist、segment、AI/Comfy job、
  trading、admin 分組。
- UI：navigation、form submit、loading completion、video first frame、random seek、share unlock。
- HTTP status、controlled backpressure 503、transport error、timeout、retry、stale loading。
- 所有錯誤需保留 timestamp、route、role、scenario、correlation id，不記錄密碼/token。

### 6.2 主機與程序資源

- host/cgroup CPU、load、RSS、swap、process/thread/task/FD。
- cgroup `cpu.stat`、`memory.current/events/swap.current`、`pids.current` 與實際 hard-limit 值。
- disk free、block read/write throughput、await、util，以及 CPU／memory／I/O PSI。
- GPU VRAM、utilization、temperature、ComfyUI queue（可用時）。
- primary/recovery/ffmpeg/browser/BT/ComfyUI 分程序樹 RSS/CPU。
- DB/WAL/SHM/HLS/storage/archive size growth。

### 6.3 DB lock 與資料一致性

- log 中 `database is locked`/`table is locked`、SQLite busy/retry、request-level lock failure。
- 定期 read-only `PRAGMA quick_check`；重破壞場景後做完整 domain invariant。
- wallet/ledger/reserve/nonnegative、order/position、PointsChain/hash/log chain。
- 未處理 lock 或 user-visible 5xx：0 容忍；受控內部 retry 必須有耗時與最終結果，不可靜默。

### 6.4 靜默失敗

- HTTP 202 job registry：HLS、BT、ComfyUI、AI、trading bot/workflow、backup/restart 全追 terminal。
- terminal success 後再驗證副作用、hash、ledger/audit/output。
- browser console error、pageerror、failed request、按鈕無反應、spinner >30 秒、disabled state 未恢復、
  stale progress、空白 preview、分享顯示成功但實際不可用。
- fallback/status-only operation 使用獨立名稱，不能算原 positive operation 成功。

### 6.5 有效負載與 latency 類型

target load 不是只看 `concurrency=32`。升壓完成後每個有效時間窗必須同時滿足：

- `scheduled_load_level == 32`；
- active workers ≥28；
- `effective_load_ratio >= 0.85`；
- throughput ≥同機 baseline-32 的 80%；
- 非 allowlisted maintenance window。

並記錄 configured concurrency、active/blocked/idle worker、inflight、operations/min、queue depth、retry rate、
effective throughput。降載原因碼只能是 `LATENCY_HIGH`、`MEMORY_PRESSURE`、`IO_PRESSURE`、`GPU_PRESSURE`、
`DB_LOCK_PRESSURE`、`DISK_LOW` 或 `MANUAL_SAFETY_STOP`。

latency 不混算重工作完成時間：request acceptance、queue latency、terminal completion、media first-frame、seek、
throughput 分開統計；4 GB upload、ComfyUI、HLS、BT、backup、restart 使用各自 job SLA。

## 7. UI 與人性化判定

- viewport：desktop 1366×768、1440×900；mobile 360×800、390×844、412×915。
- browser：Chromium、Firefox、WebKit；每個 requested browser × desktop/mobile 都必須實跑，不可 skip。
- 關鍵觸控目標最小 44×44 CSS px；不得有水平 overflow、文字裁切、不可見 CTA、modal 無法關閉、
  keyboard focus trap 或表單錯誤訊息不可理解。
- 每一複雜流程保留代表性 desktop/mobile screenshot；同時記錄 console、network、interaction latency。
- H0 跑三瀏覽器核心 navigation；每 2 小時跑 Chromium desktop/mobile sentinel，每 4 小時輪替 Firefox/WebKit；
  每個重功能完成後立刻驗 UI，H22–H24 再做完整 final sweep，避免到第 22 小時才發現 blocker。
- UI artifact 包含 console、pageerror、failed request、精簡 HAR/network trace、DOM overflow、touch target、
  interaction timing 與必要 Playwright trace；trace、HAR、screenshot metadata 與影音字幕都納入 credential scan。
- 人性化評價維度：可發現性、術語清楚度、步驟數、回饋、錯誤復原、危險操作防誤觸、手機單手操作。
- 任何會阻止核心任務完成的 UX 問題為正式 FAIL；純美觀建議可列改善但不冒充功能缺陷。

## 8. 正式 PASS/FAIL 門檻

正式 PASS 必須同時符合：

- `continuous_active_seconds >= 86,400`，中間沒有 host/WSL/campaign 中斷。
- mandatory scenario coverage =100%，scenario contract pass =100%，沒有 skip/expected-gap/fallback 假綠。
- effective target-load coverage ≥90%；單次持續降載不得超過 10 分鐘。
- ordinary request p95 ≤3,000 ms、p99 ≤8,000 ms；sentinel p95 ≤3,000 ms；HLS playlist p95 ≤2,000 ms；
  HLS segment p95 ≤3,000 ms；UI navigation p95 ≤5,000 ms；video first-frame ≤8,000 ms；random seek ≤5,000 ms；
  async acceptance ≤3,000 ms；terminal completion 依 job contract SLA。
- mandatory resource field completeness ≥95%，且 PID/RSS/CPU/IO/GPU/DB evidence 有效。
- unhandled DB lock、OOM、未分類 traceback、production server uncaught traceback、資料不變量破壞、silent failure：全部為 0。
- fault-injection traceback 必須綁 scenario/correlation ID 且列入 reviewed allowlist；allowlist 外即 FAIL。
- 所有 async operation terminal success 並有副作用證據；stuck/unknown terminal state 為 FAIL。
- primary/recovery 最終 readiness、security center、log chain、finance invariant、PointsChain verify 全成功。
- Chromium/Firefox/WebKit desktop/mobile 完整，核心 UI 任務可完成，沒有 blocking UX 問題。
- source manifest 無漂移、secret finding=0、orphan process=0、artifact validation=100%、checkpoint/hash manifest 可驗證。
- JSON/JSONL 必須重 parse；圖片可 decode；影片可 ffprobe；archive 可逐項讀取；SQLite restore 後 quick check；
  account inventory 與 cleanup result 齊全。

任一門檻失敗：保存現場、停止正式計時、重現、直接修復、補精準回歸、quick gate、重新 push/freeze，
再以新 UUID 從 0 開始下一輪正式 24 小時。完成條件是本輪 blocking/critical/high 全部修復、精準回歸通過，
且最新正式輪符合所有明確門檻；不宣稱測試能證明「不存在其他問題」。

## 9. 執行階段與審核點

1. `PLAN REVIEW`：已條件式核准；正式 24 小時未授權。
2. `HARNESS HARDENING`（目前）：實作 cgroup、外部 watchdog、persistent checkpoint、狀態機、
   resource schema、coverage contracts、security sentinel、artifact index 與假綠修補。
3. `FOCUSED FIX/AUDIT`：修 AI schema、BT/Cloud share/ComfyUI/trading workflow/incident/UX gaps。
4. `PRE-FLIGHT VALIDATION`：focused tests、quick gate、180 秒 smoke、依賴與 cgroup launch 測試。
5. `SOURCE FREEZE/PUSH`：commit/push final candidate；取得正式背景執行授權。
6. `FORMAL H0–H24`：開始唯一有效的 86,400 秒連續段。
7. `POST-RUN AUDIT`：只讀驗證 artifact/invariants；若有問題回到第 3 步，不用失敗輪湊時數。
8. `CHAT HANDOFF`：不寫結果報告檔，直接告知已修問題、正式結果、效能/資源/UX 結論與剩餘外部限制。

## 10. 審核結論與未授權事項

- [x] 完整 server backup 限定隔離 application runtime，不做 Windows/WSL block image。
- [x] 正式 24 小時是單段連續 86,400 秒；中斷後從 0 重算。
- [x] 真 AI provider、真 ComfyUI、BT、ffmpeg/HLS 與三瀏覽器為 mandatory dependency。
- [x] 4→8→16→32 升壓以 effective load 判定，並受 cgroup、降載與 hard-stop state machine 約束。
- [x] 歷史證據只決定深度，不替代 current-source live touch。
- [x] 不產生 QA 結果 Markdown；machine artifact 必須有 schema、summary index 與 hash manifest。
- [x] 失敗輪保存獨立 UUID，修復、精準回歸、60 分鐘 rehearsal、重新 freeze 後從 0 重跑。
- [ ] 十一項 machine gate 全 PASS。
- [ ] worktree clean、final candidate 已 push 並 freeze。
- [ ] 使用者對該 frozen commit 另行授予正式 24 小時啟動權限。
