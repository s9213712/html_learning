# Chess Training Pipeline

本專案的 chess AI 正式採用：

- small-engine-first
- offline-eval promotion
- stable-production-model

也就是：

1. 線上只收集 replay
2. 離線整理資料集
3. 離線訓練 candidate model
4. 跑 benchmark / smoke
5. 通過後才 stage / promote

## 目前暫停狀態

截至 2026-05-15，Exp5 棋力實驗暫停在 V28e：

- accepted profile:
  `fixed_depth_fianchetto_tail_castle_guard_v28e_depth3_no_null_mate_net30_fast_king_mobility4`
- accepted commit: `5414b21`
- handoff report:
  `docs/games/reports/2026-05-15_exp5_v28_pause_and_restart_handoff.md`
- restart playbook:
  `docs/games/references/exp5_restart_playbook.md`

在重新啟動實驗前，先依 playbook 做快篩。不要把 private validation 題目、FEN、走法、
teacher PV、source game id、chosen/source move 或逐題答案寫入公開 docs/evidence。

本文件以下的 `runtime/...` 都是 `$HACKME_RUNTIME_DIR/...` 的簡寫，不是
source checkout 內的目錄。離線驗證應明確把 `HACKME_RUNTIME_DIR` 指到 `/tmp`；
正式 promotion 則指到受備份管理的外部 runtime。

Auto-retrain 目前仍視為斷開：線上可以繼續記錄 replay 與篩選優質棋局，但不得直接改
production model。任何 replay learning 都必須先經過離線 candidate、teacher audit、
Blockfish staged match、percent-tail/expanded validation 與 promotion gate。

## 1. 線上收集 replay

線上對局結束時不直接改 production model，而是把 replay 寫到：

- trusted: `runtime/reports/games/chess_replays.jsonl`
- quarantine: `runtime/reports/games/chess_replays_quarantine.jsonl`
- rejected: `runtime/reports/games/chess_replays_rejected.jsonl`

每筆資料至少包含：

- `source`
- `engine_name`
- `opening_seed`
- `result`
- `winner_color`
- `move_count`
- `confidence_score`
- `duplicate_flag`
- `resign_abuse_flag`
- `suspicious_flag`

## 2. 準備 train / eval dataset

用 replay prepare 腳本把 trusted / quarantine replay 轉成可重訓資料：

```bash
REPO_ROOT=/path/to/hackme_web
RUNTIME_DIR=/tmp/chess_runtime

cd "$REPO_ROOT"
HACKME_RUNTIME_DIR="$RUNTIME_DIR" \
PYTHONPATH="$REPO_ROOT" \
python3 scripts/games/chess_replay_prepare.py \
  --replace-output \
  --include-quarantine
```

預設輸出：

- train: `runtime/reports/games/chess_datasets/train.jsonl`
- eval: `runtime/reports/games/chess_datasets/eval.jsonl`

並額外產生報告：

- `runtime/reports/games/chess_replay_prepare_*.json`
- `runtime/reports/games/chess_replay_prepare_*.md`

### prepare 規則

- 太短的 replay 直接跳過
- quarantine 資料會自動降權
- 決勝局預設只保留勝方著法；輸方著法預設不進 dataset
- 和局著法會保留，但 target 較低
- train / eval split 依 `replay_id` 做 deterministic hash split
- 若資料太少導致 eval 空集合，腳本會自動挪一筆 train 到 eval

## 3. 先產 seed model

用 seed trainer 離線訓練可直接上線用的初始模型。
這支腳本的預設輸出是 repo 內建 seed 位置：

- `services/games/models/chess_experiment.db`
- `services/games/models/chess_experiment_2_nn.json`
- `services/games/models/chess_experiment_3_dl.json`
- `services/games/models/chess_experiment_4_pv.json`

伺服器啟動時，若 runtime 工作模型不存在，會自動把上述 seed 複製到：

- `runtime/games/models/chess_experiment.db`
- `runtime/games/models/chess_experiment_2_nn.json`
- `runtime/games/models/chess_experiment_3_dl.json`
- `runtime/games/models/chess_experiment_4_pv.json`

之後重訓、對局、replay、promotion 都只使用 runtime 工作副本，不直接改 repo 內建 seed。

離線訓練指令：

```bash
REPO_ROOT=/path/to/hackme_web
RUNTIME_DIR=/tmp/chess_runtime

cd "$REPO_ROOT"
HACKME_RUNTIME_DIR="$RUNTIME_DIR" \
PYTHONPATH="$REPO_ROOT" \
python3 scripts/games/chess_seed_train.py --preset standard
```

註：

- `stdout` 保留最終 JSON summary
- 訓練進度會寫到 `stderr`

常用 preset：

- `micro`: 測試用，最小訓練
- `quick`: 快速產一版 seed
- `standard`: 建議一般離線訓練使用
- `warmup10`: 約 10 分鐘級別的較強 seed warm-up
- `strong`: 比較重，但適合正式 candidate

可選：

- `--include-exp2`
- `--skip-exp3`
- `--skip-exp4`
- `--with-smoke`
- `--with-benchmark`

若你想直接覆蓋別的目標，也可以明確傳：

- `--experiment-db-path`
- `--experiment-2-model-path`
- `--experiment-3-model-path`
- `--experiment-4-model-path`

並產生報告：

- `runtime/reports/games/chess_seed_train_*.json`
- `runtime/reports/games/chess_seed_train_*.md`

## 4. exp1-4 用 user dataset 再 refine

如果這一輪要用使用者棋局繼續補強 exp1-4，可以用 `chess_replay_prepare.py`
產出的同一份 `train.jsonl` 餵給各 engine 專用 trainer：

```bash
REPO_ROOT=/path/to/hackme_web
RUNTIME_DIR=/tmp/chess_runtime

cd "$REPO_ROOT"
HACKME_RUNTIME_DIR="$RUNTIME_DIR" \
PYTHONPATH="$REPO_ROOT" \
python3 scripts/games/chess_exp1_dataset_train.py \
  --input-jsonl "$RUNTIME_DIR/reports/games/chess_datasets/train.jsonl"

HACKME_RUNTIME_DIR="$RUNTIME_DIR" \
PYTHONPATH="$REPO_ROOT" \
python3 scripts/games/chess_exp2_dataset_train.py \
  --input-jsonl "$RUNTIME_DIR/reports/games/chess_datasets/train.jsonl"

HACKME_RUNTIME_DIR="$RUNTIME_DIR" \
PYTHONPATH="$REPO_ROOT" \
python3 scripts/games/chess_exp3_dataset_train.py \
  --input-jsonl "$RUNTIME_DIR/reports/games/chess_datasets/train.jsonl"

HACKME_RUNTIME_DIR="$RUNTIME_DIR" \
PYTHONPATH="$REPO_ROOT" \
python3 scripts/games/chess_exp4_dataset_train.py \
  --input-jsonl "$RUNTIME_DIR/reports/games/chess_datasets/train.jsonl"
```

`chess_train_pipeline.py` 會自動在 seed 後跑 exp1 / exp3 / exp4 refine；
若帶 `--include-exp2`，也會一起跑 exp2 refine。報告會列出
`exp1_refine_samples` 到 `exp4_refine_samples`。

## 5. 跑 benchmark / smoke

可以直接用 full trainer 只跑 smoke / benchmark：

```bash
REPO_ROOT=/path/to/hackme_web
RUNTIME_DIR=/tmp/chess_runtime

cd "$REPO_ROOT"
HACKME_RUNTIME_DIR="$RUNTIME_DIR" \
PYTHONPATH="$REPO_ROOT" \
python3 scripts/games/chess_self_play_train.py \
  --exp1-games 0 \
  --exp2-games 0 \
  --exp3-games 0 \
  --exp4-games 0 \
  --hard-exp1-games 0 \
  --hard-exp2-games 0 \
  --hard-exp3-games 0 \
  --hard-exp4-games 0 \
  --cross-games 0 \
  --cross-exp1-exp3-games 0 \
  --cross-exp2-exp3-games 0 \
  --cross-exp1-exp4-games 0 \
  --cross-exp2-exp4-games 0 \
  --cross-exp3-exp4-games 0 \
  --smoke-games-per-pair 1 \
  --benchmark-rounds 1
```

如果你已經在 repo 根目錄，也可以省略 `REPO_ROOT`：

```bash
HACKME_RUNTIME_DIR=/tmp/chess_runtime PYTHONPATH=. \
python3 scripts/games/chess_seed_train.py --preset standard
```

報告會落在：

- `runtime/reports/games/chess_self_play_train_*.json`
- `runtime/reports/games/chess_self_play_train_*.md`

### benchmark suite 組成

正式 benchmark 現在不只看 round-robin standings，還包含：

- `human_probes`
  - scripted opening probe
  - hanging piece / forced capture / fork threat 類單步 tactical probe
- `endgame_suite`
  - mate in one
  - promotion
  - avoid stalemate
  - check escape

### human probe / endgame JSON 欄位

每筆 benchmark result 都至少包含：

- `pass`
- `reason`
- `final_fen`
- `engine_illegal_move`

human probe 另外會帶：

- `engine_moves`
- `human_side`
- `human_has_mate_in_one`
- `human_mate_in_one_moves`
- `material_gain`
- `is_capture`
- `is_promotion`
- `promotion`

endgame suite 另外會帶：

- `move_uci`
- `material_gain`
- `is_promotion`
- `promotion`
- `stalemate_after_move`
- `checkmate_after_move`

### 合法性規則

所有 benchmark case 在 `push` UCI 前都必須先檢查 `legal_moves`。

- 非法 UCI：`reason=invalid_uci:*`
- 不合法著法：`reason=illegal_uci:*`

這兩種情況都會直接記成 fail，不允許靜默通過。

## 6. Stage / Promote

root backend 只負責：

- 看 pipeline 狀態
- stage candidate
- promote candidate

相關 API：

- `GET /api/root/games/chess/engines/dashboard`
- `POST /api/root/games/chess/warm-start`
- `POST /api/root/games/chess/promotion/stage`
- `POST /api/root/games/chess/promotion/promote`

promotion gate 至少會檢查：

- benchmark report 存在
- `games >= 6`
- `score_rate >= 0.45`
- `win_rate >= 0.30`
- `draw_rate <= 0.85`
- `suspicious_matches == 0`
- smoke pass

`chess_train_pipeline.py --skip-benchmark` 只允許產生 candidate / stage candidate；因為沒有新的 benchmark report，pipeline 會自動禁止 promote，報告中會標記 `benchmark.skipped=true`。

autorun retrain 目前預設關閉。原因是 2026-05-14 的 exp3/exp4/exp5 retrain
實驗顯示，直接把少量 replay 或 Stockfish 篩過的 replay 寫回主模型仍可能退步；
在 teacher/gate/rehearsal 設計完成前，server 只保留「對局紀錄、trusted/quarantine
分類、優質紀錄篩選、手動推薦命令」，不自動 fork 訓練程序。

需要短期手動驗證 auto-retrain 時才設定：

- `HTML_LEARNING_CHESS_AUTORETRAIN_ENABLED=1`
  允許 `maybe_launch_chess_train_pipeline()` 在 replay threshold 達標時啟動 pipeline。
- `HTML_LEARNING_CHESS_PIPELINE_AUTORUN_ENABLED=1`
  舊別名，作用同上。

autorun 啟用後仍可用環境變數保守降級：

- `HTML_LEARNING_CHESS_AUTORUN_SKIP_BENCHMARK=1`
  autorun command 追加 `--skip-benchmark`，只做 replay prepare / seed / refine / stage，不跑 benchmark。
- `HTML_LEARNING_CHESS_AUTORUN_SKIP_PROMOTE=1`
  autorun command 追加 `--skip-promote`，即使 benchmark 通過也不覆蓋 production model。

## 6.5 線上學習回路 end-to-end 驗收

`scripts/games/chess_live_learning_validation.py` 是一支獨立的驗收腳本，用來確認「user game → replay 收進來 → classify → 觸發 autorun retrain → benchmark」整條回路在 exp1 ~ exp4 上都能跑完。**不在 server boot / pipeline 裡，純手動驗收用。**

預設會跑 4 個 engine alias（`exp1`, `exp2`, `exp3`, `exp4`），對每個各排幾局 scripted opening probe，把產生的 replay 存進 runtime，必要時等 `chess_train_pipeline` autorun 完成，最後對 model 做 move-agreement 評估，把每階段結果寫到 `--output-root`。

```bash
HACKME_RUNTIME_DIR=/tmp/chess_runtime \
PYTHONPATH=. \
python3 scripts/games/chess_live_learning_validation.py \
  --output-root /tmp/chess_live_validation_$(date +%Y%m%d_%H%M%S) \
  --engines exp1,exp2,exp3,exp4 \
  --wait-timeout 1800 \
  --autorun-threshold 10
```

可調：

- `--engines exp2,exp3` — 只跑特定 engine，跳過其餘
- `--wait-timeout` — 等 autorun pipeline 完成的秒數（預設 1800）
- `--autorun-threshold` — exp2/exp3/exp4 觸發 auto-retrain 所需的 trusted replay 數
- `--fast-retrain` — retrain checkpoint 觸發 autorun 時跳過 pipeline 內部 benchmark / promote，也跳過 validation 腳本在 checkpoint 前後的 benchmark snapshot
- `--skip-autorun-benchmark` / `--skip-autorun-promote` — 分別控制 autorun pipeline 的 benchmark / promote
- `--skip-retrain-benchmark-snapshots` — 只跳過 validation 腳本在 retrain checkpoint 前後的 benchmark snapshot
- `--seed` / `--max-plies` — 重現實驗 / 限制每局深度

如果 exp2 ~ exp4 retrain 太久，優先用：

```bash
PYTHONPATH=. \
python3 scripts/games/chess_live_learning_validation.py \
  --output-root /tmp/chess_live_validation_fast_$(date +%Y%m%d_%H%M%S) \
  --engines exp2,exp3,exp4 \
  --fast-retrain \
  --autorun-threshold 20 \
  --wait-timeout 600
```

`--fast-retrain` 省掉每次 autorun retrain 內部的 round-robin benchmark，也省掉 validation checkpoint 前後的 snapshot benchmark；預設每 10 盤有效局 retrain 一次，`--autorun-threshold 20` 會把每個 engine 的 retrain checkpoint 從預設 2 次降成 1 次，通常比單純跳過 benchmark 更明顯。

預設 output-root 在 `/tmp/chess_live_learning_validation_<timestamp>/`，內含每 engine 的 replay 樣本、autorun pipeline log、前後 model 的 move-agreement 對照。

報告也會列出耗時資訊：

- `game_timing.avg_think_ms_per_step` — 25 盤驗收棋局中有量測步驟的平均思考時間
- `retrain_timing.total_retrain_seconds` / `avg_retrain_seconds` — retrain checkpoint 等待 autorun pipeline 完成的時間
- `retrain_timing.total_checkpoint_seconds` — 包含 dataset prepare、前後 probe、retrain、benchmark snapshot 的 checkpoint 總耗時
- 每個 checkpoint 的 `retrain_duration_seconds` / `checkpoint_duration_seconds` 會寫進 `checkpoints/*/retrain_result.json`

報告會額外輸出 audit / gate 區塊：

- `dataset_integrity` — train/rejected row 數、unique/duplicate positions、duplicate ratio、invalid FEN、illegal moves、side mismatch、terminal/mate positions、平均局長、短 resign 局數
- `stability` — catastrophic regression、opening/tactical/endgame regression、illegal move delta、blunder rate before/after
- `promotion_gate` — 明確的 `passed` 與 blocking reasons；benchmark skipped、資料污染、trusted games 不足、catastrophic regression 都會擋 promotion
- `poison_detection` — repetition pattern、intentional blunder、duplicate/copy suspected、suspicious resign rate
- `replay_sources` / `rating_distribution` — replay provenance 與 rating bucket 統計
- `position_quality` — opening/middlegame/endgame 各階段 trusted/quarantine/rejected 分布
- `runtime_metrics` — train/eval 秒數、checkpoint count、dataset bytes
- `reproducibility` — python/torch/cuda、git dirty、dataset hash、trainer hash
- `SUMMARY.md` 會產生 `Why This Run Failed`，用人可讀方式列出 promotion gate 或資料/退化原因

`promotion_gate` 是 promotion decision 的證據鏈，不是 dashboard-only 欄位。以下情況必須讓 `promotion_gate.passed=false`：

- `--fast-retrain` 或其他路徑造成 benchmark skipped
- `catastrophic_regression=true`
- dataset integrity 超標：contaminated rows、duplicate ratio、invalid FEN、illegal moves、side mismatch、short resign games
- poison detection 超標：forced repetition、intentional blunder、engine-copy/duplicate、suspicious resign rate
- trusted/quarantine replay 數不符合驗收預期
- engine verdict 不是 `PASS`

每個 retrain checkpoint 也會保留 gate evidence：`dataset_hash`、before/after model hash、`benchmark_skipped`、`benchmark_skip_reason`、`gate_decision`。Root `SUMMARY.md` 和每個 engine `SUMMARY.md` 都會列出 `Can This Model Be Promoted?`，用人話說明可以或不可以。

## 7. 正式原則

禁止：

- startup 時重訓
- user game 即時覆蓋 production model
- 未經 benchmark 的 model 直接 promote

正式流程只應該是：

1. 收 replay
2. `chess_replay_prepare.py`
3. `chess_seed_train.py`
4. `chess_exp3_dataset_train.py`（只在需要 exp3 refine 時）
5. benchmark / smoke
6. stage / promote
