# Board AI Benchmark Tutorial

這份文件說明黑白棋、19 路圍棋、五子棋的基礎 AI、棋力量化方式、腳本功能、KataGo 安裝、報告欄位與調用地圖。它是非西洋棋 board-game AI 的主要操作文件。

## 目標

三棋 AI 強化前先建立可重複的量化基準：

- 確認 AI 不下非法步。
- 用固定題庫檢查基本棋感與 tactical floor。
- 用 round-robin 對戰產生 score rate、head-to-head matrix 與 in-pool Elo。
- 產出可保存的 JSON report，作為後續演算法、神經網路、promotion gate 的比較基準。

這套流程刻意不引用西洋棋 self-play/promotion pipeline，避免污染正在開發的西洋棋模型與實驗紀錄。

## 目前 AI 來源

| 遊戲 | game key | runtime AI | 基礎技術 |
|---|---|---|---|
| 黑白棋 | `reversi` | `services/games/board_ai.py` | legal move + flip validation、alpha-beta、transposition cache、角落 / mobility / frontier / 穩定邊角 / parity 評估、困難終盤 exact solve |
| 19 路圍棋 | `go` | `services/games/board_ai.py` | legal move、提子、避免自殺手、死活網路、輕量 rollout；`katago` 難度可接本機 KataGo neural-network analysis engine |
| 五子棋 | `gomoku` | `services/games/board_ai.py` | candidate move、立即成五、擋五、threat-space classifier、活四 / 雙活三 / 雙勝點封鎖、threat alpha-beta |

前端三棋共用棋盤在 `public/js/games/board-game-shared.js`。使用者切換 `對電腦` 後會呼叫後端 AI API，不在瀏覽器內跑搜尋。

## API 調用地圖

```text
public/js/games/reversi.js
public/js/games/go.js
public/js/games/gomoku.js
        |
        v
public/js/games/board-game-shared.js
        |
        | POST /api/games/<game_key>/ai-move
        v
routes/games.py::board_game_ai_move(...)
        |
        v
services/games/board_ai.py::choose_board_game_ai_move(...)
        |
        +--> reversi stability alpha-beta + endgame exact solve
        +--> go life-death network / rollout / KataGo adapter
        +--> gomoku threat alpha-beta
```

API body:

```json
{
  "board": ["", "", "black"],
  "turn": "black",
  "difficulty": "normal"
}
```

實際 `board` 長度依遊戲而定：

- `reversi`: 64
- `go`: 361
- `gomoku`: 225

API response 的核心欄位：

```json
{
  "ok": true,
  "game_key": "gomoku",
  "turn": "black",
  "difficulty": "normal",
  "action": "move",
  "move": { "index": 112, "x": 7, "y": 7 },
  "score": 123,
  "reason": "pattern-search"
}
```

`action` 可能是：

- `move`: 有著手，前端套用 `move.index`
- `pass`: 無合法手或圍棋選擇 pass
- `finish`: 對局可結束

## Benchmark 腳本

主要腳本：

```bash
python3 scripts/games/board_ai_benchmark.py
```

預設會跑：

- games: `reversi,go,gomoku`
- engines: `random,easy,normal,hard`
- rounds: `1`
- report: `/tmp/hackme_web_test_artifacts/games/board_ai_benchmark/board_ai_benchmark_*.json`

常用參數：

```bash
python3 scripts/games/board_ai_benchmark.py \
  --games reversi,go,gomoku \
  --engines random,easy,normal,hard \
  --rounds 2 \
  --seed 20260513
```

快速 smoke：

```bash
python3 scripts/games/board_ai_benchmark.py \
  --games gomoku \
  --engines random,easy \
  --rounds 1 \
  --max-plies 6 \
  --output-dir /tmp/hackme_board_ai_benchmark_smoke
```

輸出完整 JSON 到 stdout：

```bash
python3 scripts/games/board_ai_benchmark.py --json
```

## KataGo 自動安裝

圍棋 `katago` 難度使用本機 KataGo analysis engine。預設一鍵安裝：

```bash
python3 scripts/games/setup_katago.py
```

腳本會做：

- 下載 KataGo v1.16.4 OpenCL Linux x64 release asset。
- 下載 `kata1-zhizi-b40c768nbt-fdx6d.bin.gz` 模型。
- 執行 `katago genconfig` 產生 `$HACKME_RUNTIME_DIR/katago/analysis.cfg`。
- 寫出 `$HACKME_RUNTIME_DIR/katago/hackme_katago.env`，包含 `HACKME_KATAGO_BIN`、`HACKME_KATAGO_CONFIG`、`HACKME_KATAGO_MODEL`、visits 與 timeout。

後端會自動偵測外部 runtime 的 `katago/`，所以使用預設路徑時不需要 source env file。自訂路徑時可用：

```bash
python3 scripts/games/setup_katago.py --install-dir /opt/hackme-katago
source /opt/hackme-katago/hackme_katago.env
```

也可以只設定 `HACKME_KATAGO_HOME=/opt/hackme-katago`，讓後端在該目錄內尋找 `katago`、`analysis.cfg` 與 `*.bin.gz`。

只看下載計畫、不寫檔：

```bash
python3 scripts/games/setup_katago.py --dry-run
```

常用參數：

```bash
python3 scripts/games/setup_katago.py \
  --backend opencl \
  --max-visits 96 \
  --timeout-seconds 10
```

`--backend` 可選 `opencl`、`eigen`、`eigenavx2`、`cuda12.1`、`cuda12.5`、`cuda12.8`。預設使用 OpenCL，因為它對 CUDA/TensorRT 版本綁定較少。若官方 release asset 名稱變動，可用 `--binary-url` 指到新的 zip。

## 腳本功能地圖

| 層級 | 檔案 / 函式 | 功能 |
|---|---|---|
| CLI | `scripts/games/board_ai_benchmark.py` | 解析參數、顯示進度、執行 benchmark、寫 report |
| KataGo setup | `scripts/games/setup_katago.py` | 下載 KataGo binary / 模型、解壓、產生 `analysis.cfg`、寫 env file |
| 報告路徑 | `test_artifact_path()` | standalone benchmark 預設輸出到 `/tmp/hackme_web_test_artifacts/games/board_ai_benchmark` |
| Benchmark | `run_board_ai_benchmark(...)` | 依 game/engine/rounds 產生 round-robin matches |
| 單場對局 | `play_board_ai_match(...)` | 執行單局、處理 pass/finish/illegal、記錄 final board |
| 規則共用 | `initial_board(...)`、`legal_moves(...)`、`apply_board_move(...)`、`score_board(...)` | 三棋規則與評分的 benchmark 版本 |
| Skill suite | `run_board_skill_suite(...)` | 固定題庫 tactical floor |
| Elo | `_elo_summary(...)` | 用同一 rating pool 估算 in-pool Elo |
| Report writer | `write_board_ai_benchmark_report(...)` | 寫出 JSON artifact |

## Report 欄位解讀

頂層欄位：

- `generated_at`: report 產生時間。
- `seed`: benchmark seed。
- `games`: 本次納入的 game keys。
- `engines`: 本次納入的 engines。
- `rounds`: 每組 unordered engine pair 的輪數；每輪會交換黑白。
- `games_played`: 實際對局數。
- `matches`: 每局完整紀錄。
- `standings`: 依 score rate 排名。
- `elo`: 同一 pool 內的 Elo estimate。
- `matrix`: engine 對 engine 的 head-to-head。
- `skill_suite`: 固定題庫結果。

`standings` 重要欄位：

- `games`: 對局數。
- `wins/draws/losses`: 勝和負。
- `score`: 勝 1、和 0.5、負 0。
- `score_rate`: `score / games`。
- `illegal_moves`: 該 engine 造成的非法步數。
- `avg_ms_per_move`: 平均每手耗時。

`elo` 是同一批 engines 的相對估計，只能在同一遊戲 / 同一 benchmark 設定內比較。不要把三棋 Elo 與西洋棋 Elo 或真人平台 Elo 直接對齊。

`skill_suite` 目前覆蓋：

- 黑白棋 opening legal move。
- 黑白棋角落優先。
- 黑白棋困難終盤 exact solve。
- 圍棋單子提子。
- 五子棋 open four 直接成五。
- 五子棋擋對方 open four。
- 五子棋建立活四 threat-space。
- 五子棋封鎖對手活四建構點。

後續每加一個重要能力，都應先加 skill case，再讓 candidate 通過。

## Promotion Gate 建議

在真正引入更強演算法或神經網路前，先用這些最低門檻：

- `illegal_moves == 0`
- `skill_suite.by_engine[candidate].pass_rate >= baseline`
- `standings.score_rate >= baseline`
- `elo[candidate] >= elo[baseline] - tolerance`
- `avg_ms_per_move` 不超過前端可接受範圍

若要 promotion 到 production，可再加：

- 固定 seed 連跑 3 次，排名不可劇烈波動。
- 對 `random/easy/normal/hard` 的 head-to-head 不可出現明顯退步。
- report artifact 必須跟 candidate model / code hash 綁定。

## 後續強化路線

### 黑白棋

先做：

- bitboard
- iterative deepening

再做：

- pattern evaluator
- ProbCut / Multi-ProbCut
- supervised evaluator tuning

已完成基礎強化：

- transposition cache
- move ordering
- stability / frontier / parity evaluator
- hard endgame exact search

### 五子棋

先做：

- PVS move ordering

再做：

- proof-number search
- self-play data distillation

已完成基礎強化：

- threat pattern table
- open three / open four / double threat classifier
- threat-space immediate selector
- alpha-beta move ordering

### 圍棋

先做：

- 更完整的 ko / pass / scoring
- UCT MCTS
- visit count / win rate diagnostics

再做：

- policy/value network
- PUCT
- self-play training loop

圍棋是三者中最適合走神經網路 + MCTS 的遊戲；但在 NN 前仍要先讓 benchmark report 能穩定量化。

## 測試路線

快速測三棋 AI 與 benchmark：

```bash
pytest -q tests/games/test_board_ai.py tests/games/test_board_arena.py
```

完整遊戲前端 wiring：

```bash
pytest -q tests/frontend/games/test_frontend_games.py
```

腳本 smoke：

```bash
python3 scripts/games/board_ai_benchmark.py \
  --games gomoku \
  --engines random,easy \
  --rounds 1 \
  --max-plies 6 \
  --output-dir /tmp/hackme_board_ai_benchmark_smoke
```

## 常見問題

### 為什麼不直接訓練神經網路？

因為沒有量化框架時，模型變大不等於棋力變強。先用 benchmark 固定 `illegal_moves`、skill suite、score rate、Elo，再做訓練才有可比較的結果。

### 為什麼三棋不共用西洋棋 self-play？

西洋棋 pipeline 已經包含 exp3/exp4/exp5 的模型檔、promotion gate、replay policy 與 runtime warm-start 邊界。三棋目前是另一條產品線，應先保持獨立，等量化框架成熟後再抽共用 utility。

### 為什麼 Elo 只叫 estimate？

目前 benchmark 對局數少，且 pool 只包含 `random/easy/normal/hard`。Elo 可以用來看同批候選人的相對變化，但不是公開平台等級。正式 promotion 時要搭配 score rate、skill suite 與非法步檢查。
