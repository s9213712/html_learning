# Games Documentation Map

本目錄是遊戲模組的教學與操作入口。先從這裡判斷你要處理的是一般遊戲前端、非西洋棋三棋 AI、還是西洋棋實驗引擎。

## 快速入口

| 主題 | 先讀文件 | 適用情境 |
|---|---|---|
| 黑白棋 / 圍棋 / 五子棋 AI 與棋力量化 | [references/BOARD_AI_BENCHMARK.md](references/BOARD_AI_BENCHMARK.md) | 要跑三棋 AI benchmark、看 Elo、檢查 skill suite、安裝 KataGo、規劃後續強化 |
| 西洋棋模型檔與 runtime/bundled 邊界 | [references/chess_model_files.md](references/chess_model_files.md) | 要理解 exp3/exp4/exp5 模型檔、warm-start、promotion artifact |
| 西洋棋訓練與 replay pipeline | [references/chess_training_pipeline.md](references/chess_training_pipeline.md) | 要跑 replay prepare、seed train、self-play、promotion pipeline |
| 西洋棋實驗任務與 queue 架構 | [../architecture/ASYNC_JOB_QUEUE_FEASIBILITY.md](../architecture/ASYNC_JOB_QUEUE_FEASIBILITY.md) | 要評估 Redis、RQ/Celery、RabbitMQ 或 Java service 是否適合 Exp5 長任務 |
| Exp5 暫停交接與重啟 | [reports/2026-05-15_exp5_v28_pause_and_restart_handoff.md](reports/2026-05-15_exp5_v28_pause_and_restart_handoff.md), [references/exp5_restart_playbook.md](references/exp5_restart_playbook.md) | 要從目前最強 V28e baseline 安全重啟實驗、跑快篩、避免洩題 |
| 外部棋類 / solver 工具邊界 | [../EXTERNAL_INTEGRATION_PLAYBOOK.md](../EXTERNAL_INTEGRATION_PLAYBOOK.md) | 要接 Blockfish/Stockfish、Kociemba/Rubik solver、KataGo，或確認哪些外部工具不可 commit |
| Exp5 最新補充參考 | [references/2026-05-13_exp5_nnue_fix.md](references/2026-05-13_exp5_nnue_fix.md), [references/2026-05-13_exp5_conversion_fix.md](references/2026-05-13_exp5_conversion_fix.md), [references/2026-05-13_exp5_phase1_engine_upgrade.md](references/2026-05-13_exp5_phase1_engine_upgrade.md), [references/2026-05-13_exp5_model_snapshot_and_high_engine_plan.md](references/2026-05-13_exp5_model_snapshot_and_high_engine_plan.md) | 要追 Exp5 模型修補、轉換、phase 1 engine upgrade 與 high-engine plan |
| 西洋棋 debug / engine roadmap | [archive/chess_debug/README.md](archive/chess_debug/README.md) | 要追 exp3/exp4/exp5 歷史與目前治理結論 |
| 2026-05-13 評測與優化歸檔 | [ARCHIVE_INDEX.md](ARCHIVE_INDEX.md) | 要找報告、JSON/JSONL 證據、實驗資料夾、模型快照 |

## 目錄結構

| 目錄 | 用途 |
|---|---|
| `reports/` | 已整理的人讀報告與階段結論 |
| `references/` | 長期維護文件、技術設計、訓練與模型檔說明 |
| `evidence/` | 評測輸出的 JSON/JSONL 證據，依棋種或 exp5 版本分層 |
| `experiments/` | 可追溯的完整實驗輸出資料夾 |
| `model_snapshots/` | 模型快照，路徑保持穩定，不併入 archive |
| `archive/` | 舊 debug、歷史脈絡與不再作為主要入口的資料 |

## 遊戲分類

### 同頁本機遊戲模組

這些遊戲由 `public/js/games/*.js` 註冊到同一個遊戲頁，不再使用額外分頁或 `inline/` 資料夾。

- `snake`
- `game_2048`
- `brick_breaker`
- `real_tetris`
- `reversi`
- `go`
- `gomoku`
- `rubiks_cube`

若本機遊戲模組 JS 沒有載入或沒有註冊成功，前端 catalog 會把該遊戲過濾掉，不會影響其他已載入遊戲。

#### 3D 魔術方塊

`rubiks_cube` 是同頁本機 3D cubie renderer，前端檔案是
`public/js/games/rubiks-cube.js`。手機版必須維持單欄 layout：方塊舞台、
Solver 狀態、提示文案與控制按鈕不可水平溢出或互相覆蓋。色塊與舞台使用
`touch-action: none`，讓手指拖曳色塊時由 Rubik gesture handler 接管；空白
舞台拖曳則旋轉視角。

Solver 由 `POST /api/games/rubiks_cube/solve` 呼叫後端 Kociemba wrapper。
「提示」每局最多輔助三步，且會直接替使用者轉動下一步；若使用者要放棄手解，
必須明確按「自動還原」。不要把下一步解法長期顯示在 UI 上，避免提示一路按到
解完整顆方塊。

### 非西洋棋 AI 遊戲

目前三個遊戲有基礎 AI：

- 黑白棋 `reversi`
- 19 路圍棋 `go`
- 五子棋 `gomoku`

runtime AI 入口是 `POST /api/games/<game_key>/ai-move`，只接受這三個 `game_key`。圍棋多一個 `katago` 難度，會呼叫本機 KataGo analysis engine；若尚未安裝，可執行：

```bash
python3 scripts/games/setup_katago.py
```

預設會安裝到 `runtime/katago`，後端會自動偵測，不需要額外 export。若改用自訂路徑，source 該目錄下的 `hackme_katago.env`，或同格式設定 `HACKME_KATAGO_BIN`、`HACKME_KATAGO_CONFIG`、`HACKME_KATAGO_MODEL`。西洋棋不走這條 API。

### 西洋棋

西洋棋仍保留獨立的 match/practice/replay/training/promotion pipeline。不要把三棋 benchmark 或後續三棋神經網路訓練直接塞進西洋棋 `self_play_training.py`；兩邊的模型、報告、promotion gate 要分開。

使用者端練習局可選本機 `Stockfish（本機）` 難度，但只有在後端偵測到可用 UCI binary 時才會出現。前端只有選到 Stockfish 時才顯示 depth 欄位；後端會把 `stockfish_depth` 正規化並限制在 `1` 到 `20`，預設仍由 Stockfish teacher 設定決定。一般內建難度與 exp3 / exp4 / exp5 / exp6 不顯示這個 depth 欄位。

Exp6 是新的 lightweight neural-network chess engine path，實作在 `services/games/chess_exp6.py`，和 exp3 / exp4 一樣屬於可訓練模型，但目前定位偏向輕量 NumPy CPU inference 與獨立模型檔，不取代 exp5 NNUE / search profile pipeline。PGN/replay 下載與 teacher audit 產出的乾淨訓練資料可以接到 exp6 訓練，但要和 exp3 / exp4 / exp5 的 promotion artifact 分開標記，避免不同模型家族互相覆蓋 baseline。

目前 Exp5 棋力實驗已暫停在 V28e。重啟時請先讀
[reports/2026-05-15_exp5_v28_pause_and_restart_handoff.md](reports/2026-05-15_exp5_v28_pause_and_restart_handoff.md)
與 [references/exp5_restart_playbook.md](references/exp5_restart_playbook.md)，先跑 Blockfish
快篩，再決定是否跑完整 percent-tail / expanded validation。公開文件不得包含 FEN、走法、teacher
PV、source game id、chosen/source move 或逐題答案。

## 調用地圖

### 使用者玩三棋 AI

1. 使用者在遊戲區選 `黑白棋 / 圍棋 / 五子棋`。
2. 前端模組由 `public/js/games/reversi.js`、`go.js`、`gomoku.js` 註冊。
3. 三棋共用棋盤邏輯在 `public/js/games/board-game-shared.js`。
4. 使用者切到 `對電腦` 後，前端呼叫 `POST /api/games/<game_key>/ai-move`。
5. `routes/games.py` 驗證登入、CSRF、game key、難度與棋盤 payload。
6. `services/games/board_ai.py` 產生 AI 決策；圍棋 `katago` 難度會先找環境變數，再找 `runtime/katago`。
7. 前端套用回傳的 `move/pass/finish`，並留在同一頁。

### 維護者量化三棋棋力

1. 執行 `python3 scripts/games/board_ai_benchmark.py`。
2. CLI 呼叫 `services/games/board_arena.py::run_board_ai_benchmark(...)`。
3. arena 跑 `random/easy/normal/hard` round-robin，並執行 deterministic skill suite。
4. 報告寫到 `runtime/reports/games/board_ai_benchmark_*.json`。
5. 先看 `illegal_moves`、`skill_suite.pass_rate`、`standings.score_rate`、`elo`，再決定是否允許後續 AI 強化或 promotion。

## 放置規則

- 三棋即時 AI：`services/games/board_ai.py`
- 三棋 benchmark / Elo / skill suite：`services/games/board_arena.py`
- 三棋 operator script：`scripts/games/board_ai_benchmark.py`
- KataGo 自動下載 / config 產生：`scripts/games/setup_katago.py`
- 三棋測試：`tests/games/test_board_ai.py`、`tests/games/test_board_arena.py`
- 真實版俄羅斯方塊：`public/js/games/real-tetris.js`
- 西洋棋仍使用 `services/games/chess*.py`、`scripts/games/chess_*.py`、`docs/games/references/chess_*.md`
- playable Stockfish depth：`routes/games.py::normalize_stockfish_depth`、`services/games/chess_stockfish_teacher.py`
- Exp6 neural engine：`services/games/chess_exp6.py`、`tests/games/test_chess_neural.py`

新增三棋 AI 強化前，先更新 [references/BOARD_AI_BENCHMARK.md](references/BOARD_AI_BENCHMARK.md) 的量化規則與 promotion gate，避免只改演算法但沒有可比較的棋力證據。
