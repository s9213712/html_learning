# Async Job Queue Feasibility Study

狀態：架構決策前置文件，不代表已部署。

目的：評估是否應在 `hackme_web` / Exp5 chess experiment workflow 引入 Redis、RQ/Celery、RabbitMQ 或 Java Web 技術棧，並決定最小可落地路線。

決策摘要：先做 Phase 0。也就是把 chess experiment pipeline 接進現有
Job Center，先取得可觀測、可取消、可重試、可 audit 的長任務框架；暫不引入
Redis/RQ，更不把 Java stack 混進 Flask monolith。

## 結論

文章的核心方向可行，且符合目前專案狀態：

- Redis：建議作為 optional infrastructure，可用於跨程序狀態、短期 cache、rate limit、分散式 lock 與 experiment job progress。
- RQ：最適合作為第一個輕量 queue 選項，但前提是 Redis 先成為 optional dependency。
- Celery：可行，但第一階段偏重。等任務類型、retry policy、排程與多 worker 需求明確後再評估。
- RabbitMQ：有條件可行。適合 durable queue、多 worker、多機或實驗量放大後，不適合作為目前第一步。
- Spring Boot / MyBatis：只有在刻意建立獨立 Java API service 或履歷展示時才有角色；不建議塞進現有 Flask app。
- Spring Cloud / JSP / Struts2 / Servlet stack：不適合目前專案。

最務實路線：

1. Phase 0 只把 chess experiment pipeline 接進現有 Job Center。
2. Redis 先留在 feasibility study，不作為第一個 patch 的 dependency。
3. 未來若需要跨程序 queue，再引入 optional Redis，用於 job state、lock、
   rate limit 與輕量 cache。
4. 需要真正 queue 時先用 Redis + RQ；RQ 是執行層，不取代 Job Center。
5. 任務量變大或需要 durable broker 時，再考慮 Celery + RabbitMQ。
6. Java stack 只作為獨立服務，不和 Flask monolith 混寫。

## 目前專案現況

`hackme_web` 目前是 Python / Flask 專案。`requirements.txt` 目前沒有 Redis、RQ、Celery、RabbitMQ、pika 或 kombu 依賴。
若未來新增 queue dependency，應放在 optional extra 或獨立 worker
requirements，例如 `requirements-worker.txt` / `requirements-redis.txt`，
避免主站部署被 queue dependency 綁死。

已有的長任務與 worker 基礎：

- `services/job_center.py`：SQLite-backed Job Center，已有 job / event / progress / cancel / retry / notification。
- `routes/jobs.py`：使用者與 root 查看任務、取消、重試的 API。
- `services/server/startup.py`：啟動 daily snapshot、storage maintenance、PointsChain block、trading background worker、trading bot worker 等常駐 thread。
- `services/trading/background_engine.py`：交易背景 job 已具備 job table、run table、lease lock 與 run-once/status API。
- `scripts/media/hls_prepare_worker.py`：HLS 已改成外部 Python worker，並同步寫回 Job Center。
- `services/games/chess_pipeline.py`：西洋棋訓練 pipeline 目前可用 subprocess 啟動，並用 runtime JSON 記錄 autorun status。
- `services/games/chess_stockfish_teacher.py`：Stockfish 透過外部 process 呼叫，已有 concurrency semaphore 與 timeout。
- `services/games/chess_search.py` / `services/games/chess_nnue.py`：棋力 hot path 使用本機記憶體 search cache、transposition table、eval cache。

這代表專案不是缺少所有 background 能力，而是缺少一個跨程序、可排隊、可觀測、可復原的 experiment job layer。

## 已落地基礎

以下是目前已經在專案內落地、可作為 Phase 0 基礎的部分：

- Job Center 已存在，提供 job / event / progress / cancel / retry /
  notification 的 DB-backed 任務框架。
- `/api/jobs`、`/api/admin/jobs`、`/api/jobs/<job_uuid>`、
  `/api/jobs/<job_uuid>/events`、cancel、retry API 已存在。
- HLS preparation 已經改為外部 Python worker，並會寫回 Job Center。
- Trading Background Engine 已有 SQLite job table、lease lock、run log、
  root status / pause / resume / enqueue run-once，以及 root snapshot path。
- 檔案上傳、HLS、remote download、ComfyUI 等長任務已逐步接入任務中心或外部
  process，不再把全部工作塞在前端頁面生命週期內。
- Chess pipeline 目前可用 subprocess 啟動，並以 runtime JSON 記錄 autorun
  status。
- Stockfish teacher 已有單 process semaphore 與 timeout。
- Chess search / NNUE hot path 已使用 process memory cache，不依賴 Redis。

尚未落地：

- Chess experiment pipeline 尚未成為 Job Center first-class job type。
- Chess experiment 尚未有 Job Center progress event / cancel checkpoint /
  final redacted summary。
- Redis、RQ、Celery、RabbitMQ 都尚未成為 dependency。
- Cross-process Stockfish semaphore 尚未落地。
- Promotion lock / promotion idempotency 尚未落地。

因此本文件的第一個工程結論是：補齊 chess experiment -> Job Center，而不是先新增
queue infrastructure。

## Redis 可行性

### 適合用途

Redis 很適合放在 Flask app、外部 worker、未來多 worker 之間，承接短生命週期狀態：

- experiment job status。
- staged match progress。
- training task progress。
- promotion gate summary。
- web session / rate limit。
- short-lived cache。
- cross-process lock。
- worker heartbeat。

可行的 key pattern：

```text
exp5:job:{job_id}:status
exp5:job:{job_id}:progress
exp5:job:{job_id}:profile
exp5:job:{job_id}:started_at
exp5:job:{job_id}:finished_at
exp5:job:{job_id}:summary
exp5:worker:{worker_id}:heartbeat
```

Redis 裡只應放摘要與狀態，不放私有 validation 題庫、逐題答案、teacher PV、完整 replay JSONL 或 Stockfish binary path。

### 不適合用途

Redis 不應進 chess engine hot path：

- 不放 search transposition table。
- 不放每個 search node 的 memo。
- 不放 killer/history table。
- 不放 NNUE eval cache 的核心 lookup。
- 不在每個 legal move / node 上打 Redis。

目前 `chess_search.py` 的 `TranspositionTable`、`ZobristHasher`、killer/history heuristic，以及 `chess_nnue.py` 的 eval cache 都應繼續留在 process memory。這些資料存取頻率太高，跨 socket 到 Redis 只會拖慢棋力計算。

### 部署要求

Redis 必須是 optional dependency：

- 沒設定 `HTML_LEARNING_REDIS_URL` 時，系統沿用 SQLite Job Center。
- Redis 掛掉時，不能讓登入、交易、檔案、影音主流程整站不可用。
- 對資金、交易、PointsChain 這類強一致資料，Redis 只能做 cache / lock / progress，不能當權威資料源。

### Degraded Mode

Redis / RQ disabled 時：

- 可以查看既有 Job Center jobs。
- 可以跑 Phase 0 類型的本機 subprocess job。
- 不允許 enqueue RQ-only job。
- UI 顯示 `queue unavailable` / `degraded`，不要假裝 queue 正常。

Redis 掛掉時：

- 不要 silently fallback 成 Flask request 內同步執行長任務。
- 不要自動重複建立同一 experiment。
- Job Center 應寫入 `queue_degraded` event。
- 已完成結果仍以 DB / artifact digest 為準。

## RQ / Celery / RabbitMQ 可行性

| 技術 | 可行性 | 適合時機 | 主要風險 |
|---|---|---|---|
| Redis + RQ | 高 | 第一階段 queue，單機或少量 worker | Redis 變成新增部署依賴 |
| Redis + Celery | 中高 | 需要 retry policy、排程、較多任務型別 | 設定與 worker lifecycle 較複雜 |
| RabbitMQ + Celery | 中 | 多 worker、多機、durable queue、任務量明顯放大 | 運維成本高，不適合作為小站第一步 |
| RabbitMQ standalone | 中低 | 已有明確 producer/consumer 拆分 | 專案目前 Python monolith 會增加複雜度 |

目前最合適的是 Redis + RQ，但建議先完成 Job Center integration，再導入 RQ。理由是 UI、audit、cancel/retry、owner permission、root 全站任務檢視已經在 Job Center 中存在；RQ 應該成為執行層，不應取代整個任務中心。

## Source of Truth and Reconciliation

Job Center / DB 是權限、owner、audit 與 final state 的 source of truth。
Redis 是 volatile state。RQ 是 execution queue。三者狀態不一致時：

1. 權限、owner、可見性與 audit 看 DB。
2. final result 看 DB / artifact digest。
3. progress 優先看 Redis；Redis 不可用時退回 DB last checkpoint。
4. RQ job missing 但 DB job 長時間 `running` 且 heartbeat timeout，標記
   `abandoned` / `needs_retry`，不得直接當作 succeeded。
5. retry 必須使用同一個 Job Center `job_uuid` 或明確的新 retry job，避免 UI
   與 queue 狀態分裂。

2026-05-16 已落地的中間層：

- `services/core/progress_backend.py` 提供 `memory` / `file` / `redis` /
  `auto` backend interface。
- `services/job_center.py` 會把高頻 `running` progress coalesce 到 progress
  backend，依固定間隔回寫 DB；終態、錯誤、audit 與 notification 仍以 DB 為準。
- 主 server 預設 `HACKME_JOB_PROGRESS_BACKEND=auto`：有 Redis URL 時走 Redis，
  否則用 `runtime/job_progress_cache` file backend。這讓外部 HLS worker、
  ComfyUI、BT/direct link、resumable upload 可以共享最新進度，同時避免每個
  chunk / progress tick 都寫 SQLite。

## SQLite and Multi-Worker Risk

Phase 0 使用 SQLite Job Center 是合理的，但 Phase 2 之後如果 RQ worker 多開，
SQLite 寫入競爭會變成現實風險。

要求：

- SQLite 多 worker 寫入必須啟用 WAL 與 `busy_timeout`。
- 每個 worker 必須使用獨立 DB connection，不共享 connection。
- 高頻 progress update 不應每 ply / 每 move 寫 DB。
- progress 應 throttle，例如每 N 秒或每 N game 更新一次。
- Redis 可承接高頻 progress；DB 只保存 checkpoint / final event / audit。

這是避免 staged match / validation worker 把 Job Center event table 打爆的
基本邊界。

## Stockfish Cross-Process Limit

`chess_stockfish_teacher.py` 目前已有 concurrency semaphore 與 timeout。這對單一
Python process 有效，但 Phase 2 多 worker process 後，不一定能限制全站
Stockfish process 數量。

Phase 2 後，Stockfish concurrency limit 必須升級成 cross-process semaphore。
可選方案：

- Redis lock / semaphore。
- SQLite lease table。
- worker-level single-Stockfish policy。

否則多個 worker 可能同時啟動 Stockfish，造成 CPU 爆炸、timeout 失真，以及
staged match 結果不可信。

## Java 技術棧可行性

| 技術 | 對目前 Flask 專案的適配度 | 建議 |
|---|---|---|
| Spring Boot | 低到中 | 只適合做獨立 API service，不建議嵌入現有專案 |
| Spring Cloud | 低 | 現階段過度工程 |
| MyBatis | 低到中 | 只有在獨立 Spring Boot API service 時才考慮 |
| Hibernate | 低 | 不適合目前大量明確 SQL / experiment result 型資料 |
| Spring MVC / Servlet | 低 | 學習價值有，但專案不需要直接採用 |
| JSP / Struts2 | 很低 | 不建議用在新功能 |

若未來要展示 Java 技術，較合理架構是獨立服務：

```text
Spring Boot API
  -> Redis / RabbitMQ
  -> Python Exp5 Worker
  -> PostgreSQL / SQLite artifact store
```

但在目前部署角度，這會增加語言、build、IPC、觀測、憑證與資料一致性成本；不會直接解決 Exp5 現在最重要的痛點。

## 與目前痛點的對應

| 痛點 | Redis/RQ 能否解決 | 說明 |
|---|---:|---|
| experiment 可排隊 | 可 | RQ 或自建 worker 可管理待跑任務 |
| experiment progress 可觀測 | 可 | Redis + Job Center events 可提供即時狀態 |
| staged match 可重跑 | 可 | job payload + idempotency key + artifact digest |
| promotion gate 可追蹤 | 可 | gate summary 寫 DB，progress 寫 Redis / Job Center |
| Flask request 被長任務卡住 | 可 | worker process 化 |
| mate-net blind spot | 不直接解決 | 這是棋力與驗證問題，不是 queue 問題 |
| private validation 洩漏 | 不能自動解決 | 需要 artifact policy 與 redaction gate |
| search hot path 變快 | 不會 | Redis 進 hot path 反而會變慢 |

## 建議資料邊界

權威資料：

- 長期 experiment result：DB 或 runtime artifact。
- promotion gate result：DB + redacted report。
- job owner / permission / audit：Job Center / DB。
- private validation detail：private runtime，不進 public docs，不進 Redis dump。

Redis 暫存：

- job heartbeat。
- progress percent。
- queue lease。
- short summary。
- worker availability。

不進 Redis：

- private FEN。
- 逐題答案。
- teacher PV。
- source game id。
- 完整 replay JSONL。
- Stockfish binary 或私有資料根目錄。

### Job Payload Boundary

Queue payload 必須最小化。RQ job 不應塞完整實驗資料，而只塞定位資訊：

```json
{
  "job_uuid": "job-uuid",
  "profile_name": "profile_name_or_digest",
  "experiment_type": "staged_match",
  "staged_suite_id": "staged_blockfish_5",
  "depth_schedule": "depth_1_5",
  "redaction_mode": "public_staged",
  "artifact_output_dir": "runtime/experiments/job-uuid"
}
```

不要塞：

- private FEN。
- validation answers。
- teacher PV。
- replay JSONL content。
- Stockfish binary path。
- private validation 的完整 PGN。

worker 應根據 `job_uuid` 從 DB / private runtime 讀必要資料。Queue payload
本身不能變成新的外洩面。

## Redaction Policy

Public staged match 可以保存完整 PGN 與可重現摘要。Private validation 必須預設
redacted：

- private validation 只保存 aggregate metrics。
- private case detail 只留在 private runtime。
- Job Center event 預設 redacted。
- public docs / general job event 只允許 redacted case id，例如
  `validation_case_001`。
- 不輸出 plain FEN hash。若真的需要 digest，使用 `HMAC(secret, fen)`，
  或完全不輸出 digest。

原因：private validation 題庫如果很小或可枚舉，plain SHA digest 仍可能被
offline 對照。HMAC digest 比 plain hash 更適合私有驗證資料。

## Idempotency and Promotion Safety

所有 experiment job 必須有 idempotency key。Promotion gate 不能因 retry /
worker crash / 手動重跑而重複 promote。

要求：

- Promotion idempotency key 使用 `job_uuid + candidate_digest + baseline_digest`。
- 寫入 promoted model 前必須取得 promotion lock。
- promoted model 寫入採 atomic write + rename / compare-and-swap，不直接覆蓋
  目前 best model。
- 同一 job retry 不得產生第二次 promotion。
- cancel / retry 不得覆蓋已完成 artifact。
- worker crash 後，同一 job 不得重複寫 final result 或重複 promote。

Promotion / model save / validation report 寫入期間應視為不可中斷區段，或必須具備
可回滾設計。

## 建議落地階段

### Phase 0：Job Center 接 chess experiment

不新增外部依賴。先把 chess pipeline 變成 Job Center 可觀測任務：

- 新增 experiment job type。
- `chess_pipeline.py` 可接受 `job_uuid`。
- 將 `services/games/chess_pipeline.py` subprocess 狀態同步到 `job_center_jobs` / `job_center_events`。
- UI 可看 queued/running/succeeded/failed。
- cancel 先做到 request flag，worker 在安全 checkpoint 停止。
- final result 寫 redacted summary。
- 不新增 Redis、RQ、Celery、RabbitMQ 或 Java dependency。

這階段即可解決「使用者不知道實驗是否在跑」與「重新整理後狀態消失」的問題。
同時取得可觀測、可取消、可重試、可 audit、可看歷史的最小價值。

最小 job payload：

```json
{
  "job_type": "chess_experiment",
  "experiment_kind": "staged_match",
  "profile": "fixed_depth_fianchetto_tail_castle_guard_v28e_depth3_no_null_mate_net30_fast_king_mobility4",
  "candidate_label": "v29r_diag",
  "suite": "staged_blockfish_5",
  "redaction": "public_staged",
  "early_stop_on_loss": false
}
```

Public staged event example：

```json
{
  "event_type": "game_finished",
  "game_index": 2,
  "result": "draw",
  "plies": 187,
  "summary": "fivefold repetition",
  "elapsed_ms": 123456
}
```

Private validation event example：

```json
{
  "event_type": "validation_finished",
  "suite": "private_validation_100",
  "total": 100,
  "passed": 73,
  "failed": 27,
  "redacted": true
}
```

不要寫入：

```json
{
  "fen": "...",
  "expected_move": "...",
  "teacher_pv": "..."
}
```

### Cancellation Safety

Running experiment 不應直接 hard kill，除非 worker crash recovery 已完成。
cancel 應透過 Job Center `cancel_requested` flag，在以下 checkpoint 停止：

- game boundary。
- ply boundary。
- artifact boundary。

Promotion / model save / validation report 寫入期間必須是不可中斷或可回滾區段。
避免留下：

- 半套 promoted model。
- 半個 gate result。
- 半個 runtime JSON。
- 錯誤標記成 `succeeded` 的 job。

### Phase 1：Optional Redis adapter

已先新增共用 progress backend interface：

```text
services/core/progress_backend.py
services/job_center.py
```

設定：

```text
HACKME_JOB_PROGRESS_BACKEND=auto
HACKME_REDIS_URL=
REDIS_URL=
HACKME_PROGRESS_CACHE_DIR=
```

Redis 只當 optional accelerator。沒有 Redis 時功能仍可用，並退回
`runtime/job_progress_cache` file backend；DB 仍是 final state、owner、
permission 與 audit 的 source of truth。

### Phase 2：Redis + RQ worker

規劃新增（目前尚未實作，不能當成可執行入口）：

- `services/experiments/jobs.py`
- `scripts/workers/experiment_worker.py`

worker 負責：

- staged match。
- Stockfish teacher / auditor。
- replay training。
- validation smoke。
- promotion gate。

RQ job id 要和 Job Center job_uuid 對齊，避免 UI 與 queue 狀態分裂。

### Phase 3：壓力與部署 gate

必測：

1. Redis 未啟用時，站台可正常啟動。
2. Redis 掛掉時，主站功能不崩潰，experiment queue 顯示 degraded。
3. worker crash 後同一 job 不重複 promote。
4. cancel 不會留下半套 promoted model。
5. private validation detail 不出現在 Redis、public docs、Job Center event。
6. 同時跑多個 staged match 不拖垮 Flask request latency。
7. Stockfish concurrency limit 生效。
8. Job Center 與 Redis 狀態不一致時，以 DB 權限與 audit 為準。
9. SQLite WAL / busy timeout 下多 worker progress 不打爆 DB。
10. Redis degraded 時不會退回 Flask request 同步長任務。
11. private validation digest 使用 HMAC 或完全不輸出。

### Phase 4：再評估 Celery / RabbitMQ

只有當以下條件成立才升級：

- 同時多 worker / 多機。
- Redis queue durability 不夠。
- 任務 retry、排程、dead-letter、priority queue 明確需要 broker。
- 單機 RQ 已成瓶頸。

## 不建議做的事

- 不要把 Redis 放進 chess search node path。
- 不要讓 Redis 成為交易、PointsChain、wallet 的權威資料源。
- 不要在 Flask request 裡同步跑 staged match、Stockfish audit 或 full validation。
- 不要把 Spring Boot、Spring Cloud、JSP、Struts2 混進現有 Flask app。
- 不要把 private validation 題目、答案、FEN、teacher PV 寫進 public docs、Redis dump 或一般 Job Center event。

## 最終判斷

Review 結論：approve with minor additions。文章建議大致正確，但落地順序應更保守：

1. 立刻做 Phase 0：用現有 Job Center 統一 chess experiment 任務狀態。
2. Redis 作為 optional cross-process state / lock / cache，先不落地。
3. RQ 作為未來第一個 queue worker，但只當執行層。
4. RabbitMQ / Celery 等任務量真的放大後再上。
5. Java stack 只適合獨立服務或展示用途，不適合直接塞進 `hackme_web`。

這條路線能改善長任務管理、實驗可重現性、progress 可視化與 Flask 主程序穩定性，同時不干擾棋力 hot path。
