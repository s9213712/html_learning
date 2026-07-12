# External Integration Playbook

This playbook documents features that depend on an external repository,
third-party executable, or separately running service.  Keep this file updated
whenever a feature is wired through an upstream project instead of code that is
fully owned by this repository.

## Rules

- Do not vendor external repositories into tracked source.
- Use ignored local paths such as `external/`, `runtime/`, or
  `reference_repos/` for cloned tools, model caches, generated reports, and
  downloaded binaries.
- Keep bridge code in this repo.  The bridge should validate inputs, expose
  bounded APIs, and make failures explicit instead of letting the external tool
  leak implementation details into the frontend.
- Record every external command in an operator doc or script README before
  using it from root/admin UI.
- Prefer root-only setup and start actions for tools that download code,
  install packages, train models, or touch model directories.
- Never treat an optional external tool as a required server startup dependency
  unless it is listed in `requirements-minimal.txt` and has a safe fallback.

## Current Integration Inventory

| Integration | Kind | Runtime required for normal users | Main repo entry points | External state location |
|---|---|---:|---|---|
| Blockfish / Stockfish | UCI chess executable | No | `scripts/games/chess_exp5_blockfish_match.py`, `services/games/chess_stockfish_teacher.py` | `reference_repos/Stockfish/` or `STOCKFISH_PATH` |
| Kociemba Rubik solver | Python package | Yes for Rubik solver hints | `services/games/rubiks_solver.py`, `POST /api/games/rubiks_cube/solve` | Python environment package `kociemba` |
| `BTC_trade` | External git repo | No unless BTC_trade signal feature is enabled | `services/trading/btc_bridge.py`, `scripts/trading/bridges/btc_signal_bridge.py`, root BTC_trade APIs | `external/BTC_trade/` by default |
| ComfyUI | External API service / local process | Only when ComfyUI feature is enabled | `routes/comfyui.py`, `services/comfyui/*`, `public/js/36-comfyui.js` | Local/remote ComfyUI install and model directories |
| Civitai | External model API | Only for root model import | `routes/comfyui.py`, `services/comfyui/*` | ComfyUI model directories plus `.civitai.json` sidecars |
| KataGo | External Go engine binary/model | Only for Go `katago` difficulty | `scripts/games/setup_katago.py`, `services/games/board_ai.py` | `$HACKME_RUNTIME_DIR/katago/` or external XDG state fallback |

## Blockfish / Stockfish

Purpose:

- Local chess teacher and sparring opponent for Exp5 validation.
- Optional playable Stockfish difficulty when a UCI binary is configured.
- Not a production dependency for ordinary users.

Expected external program:

- A UCI-compatible Stockfish binary.
- Historical docs may call the sparring target `Blockfish`; the current script
  still uses the Stockfish UCI adapter and labels the opponent as `blockfish`
  in result summaries.

Install / locate:

```bash
cd /home/s92137/hackme_web
export STOCKFISH_PATH=/home/s92137/reference_repos/Stockfish/src/stockfish
```

Primary script:

```bash
PYTHONPATH=. python3 scripts/games/chess_exp5_blockfish_match.py \
  --profile "$EXP5_BASELINE_PROFILE" \
  --stockfish-path "$STOCKFISH_PATH" \
  --stockfish-depth-schedule 2,3,4,5,6 \
  --games 5 \
  --max-plies 600 \
  --private-jsonl /tmp/exp5_blockfish_replay.jsonl \
  --summary-json /tmp/exp5_blockfish_summary.json
```

Project integration points:

- `services/games/chess_stockfish_teacher.py` resolves and calls the UCI binary.
- `scripts/games/chess_exp5_blockfish_match.py` runs full Exp5 versus
  Blockfish/Stockfish games.
- `docs/games/references/exp5_restart_playbook.md` is the detailed Exp5
  operator playbook.
- `docs/games/README.md` explains where Stockfish fits in the game domain.

Safety boundary:

- Do not commit Stockfish binaries or cloned Stockfish source.
- Do not publish private replay JSONL, FENs, move lists, PVs, source game ids,
  or chosen/source moves from private validation.
- Public summaries may contain only aggregate win/draw/loss, runtime, and
  redacted pass/fail conclusions.

## Kociemba / `rubik_solver`

Purpose:

- Generate valid Rubik's cube solver hints for the 3D Rubik game.
- Replace the earlier heuristic/fallback hint path with solver-backed next
  moves.

Chosen package:

- Current implementation uses the Python `kociemba` package.
- `rubik_solver` may be used in the future only behind the same service
  boundary; do not call either solver directly from frontend code.

Install:

```bash
python3 -m pip install -r requirements-minimal.txt
```

Direct smoke:

```bash
PYTHONPATH=. python3 - <<'PY'
from services.games.rubiks_solver import solve_facelets
print(solve_facelets("UUUUUUUUURRRRRRRRRFFFFFFFFFDDDDDDDDDLLLLLLLLLBBBBBBBBB"))
PY
```

Project integration points:

- `requirements-games.txt` includes `kociemba>=1.2.1`.
- `requirements-minimal.txt` currently includes `requirements-games.txt`
  because the game route bundle is imported during server startup.
- `services/games/rubiks_solver.py` validates the 54-character facelet string
  and calls `kociemba.solve(...)`.
- `routes/games.py` exposes `POST /api/games/rubiks_cube/solve`.
- `public/js/games/rubiks-cube.js` converts the current cubie state to
  `URFDLB` facelets and asks the backend for solver output.
- `public/js/38-games.js` exposes `window.hackmeGameRequest` so local game
  modules can call authenticated APIs with CSRF handling.

API contract:

```http
POST /api/games/rubiks_cube/solve
Content-Type: application/json

{"facelets":"UUUUUUUUURRRRRRRRRFFFFFFFFFDDDDDDDDDLLLLLLLLLBBBBBBBBB","max_depth":24}
```

Response shape:

```json
{
  "ok": true,
  "solver": "kociemba",
  "solution": ["R'"],
  "expanded_solution": ["R'"],
  "length": 1,
  "quarter_turn_length": 1,
  "next_move": "R'",
  "solved": false
}
```

Implementation rules:

- The frontend must not invent solver hints.
- Half turns such as `U2` are expanded before animation because the current UI
  animates and counts one quarter-turn at a time.
- If Kociemba is unavailable, the API must return a visible error.  Do not
  silently fall back to a fake theoretical minimum.
- The displayed text should say `Kociemba solver` or `solver 解長度`, not
  `真正理論最低`, unless a proven optimal solver is added.

## BTC_trade

Purpose:

- Optional external BTC strategy signal source.
- Root can check, clone/update/install, and start prediction from the web UI.
- A bridge can translate external BTC_trade events into hackme_web simulated
  spot orders.

Default external repo settings:

| Setting key | Default |
|---|---|
| `trading.btc_trade_repo_url` | `https://github.com/s9213712/BTC_trade.git` |
| `trading.btc_trade_branch` | `strategy/v15b-plus` |
| `trading.btc_trade_project_dir` | Empty unless configured; service default is `external/BTC_trade` |
| `trading.btc_trade_enabled` | `false` |

Root/admin API surface:

- `GET /api/root/trading/btc-trade/check`
- `POST /api/root/trading/btc-trade/setup`
- `POST /api/root/trading/btc-trade/start`
- `GET /api/root/trading/btc-trade/start-status`
- `GET /api/trading/btc-signal`

Expected external project commands:

```text
update_data.py
retrain_models.py --timeframe 4h
hourly_check.py --timeframe 4h
backtest_report.py --timeframe 4h
```

Project integration points:

- `services/trading/btc_bridge.py` owns setup, status, freshness, start jobs,
  report parsing, and dependency fallback names.
- `scripts/trading/bridges/btc_signal_bridge.py` is the repo-local CLI bridge
  from BTC_trade runtime signals to hackme_web simulated spot orders.
- `services/trading/admin.py` stores root-configurable repo URL, branch, and
  project directory.
- `services/trading/catalog.py` and `services/trading/markets.py` restrict
  BTC_trade enablement to BTC markets.
- `docs/trading/BTC_TRADE_INTEGRATION.md` is the domain-specific operator doc.

Bridge examples:

```bash
PYTHONPATH=. python3 scripts/trading/bridges/btc_signal_bridge.py \
  --btc-trade-dir external/BTC_trade \
  --status
```

```bash
PYTHONPATH=. python3 scripts/trading/bridges/btc_signal_bridge.py \
  --btc-trade-dir external/BTC_trade \
  --bridge-username btc_bridge \
  --market-symbol BTC/USDT \
  --dry-run
```

Safety boundary:

- Do not commit the cloned `BTC_trade` repo, runtime CSVs, trained models, or
  generated reports.
- Keep bridge orders in the simulated trading layer unless a separate real
  exchange integration is explicitly designed and reviewed.
- Treat long-running setup/start as background jobs.  The root UI should poll
  status instead of blocking request threads.

## ComfyUI

Purpose:

- External image generation backend for text-to-image, image-to-image,
  workflow presets, Civitai model import, and official GGUF profiles.

Supported deployment shapes:

- Remote ComfyUI API server.
- Local external ComfyUI process managed by root settings.
- In-process Hugging Face Diffusers only when deliberately enabled for local
  experiments.

Install / dependency layers:

```bash
python3 -m pip install -r requirements-comfyui.txt
```

Local startup template:

```bash
cp scripts/comfyui/comfyui_run_in_linux.template.sh /path/to/ComfyUI/run_hackme_comfyui.sh
```

Project integration points:

- `routes/comfyui.py` exposes authenticated user generation APIs and root-only
  management APIs.
- `services/comfyui/client.py` wraps ComfyUI HTTP and websocket calls.
- `services/comfyui/settings.py` normalizes local/remote configuration.
- `services/comfyui/execution.py` submits prompts, polls history, fetches
  outputs, handles stale/unresponsive backends, and reports progress.
- `services/comfyui/workflow/builder.py` builds supported shortcut workflows.
- `services/comfyui/template/*` validates and materializes user/official
  workflow presets.
- `services/comfyui/gguf_profiles.py` is the only supported path for
  customer-facing GGUF profile exposure.
- `public/js/36-comfyui.js` is the main frontend integration surface.

Upstream ComfyUI API commands used:

- `GET /system_stats`
- `GET /object_info/<NodeClass>`
- `GET /embeddings`
- `POST /prompt`
- `GET /history/<prompt_id>`
- `GET /view?...`
- `POST /interrupt`
- `WS /ws?clientId=<client_id>`

Root model management:

- Civitai search/inspect/download writes into the configured ComfyUI
  `models/` tree.
- Direct model upload is root-only and must stay inside allowed ComfyUI model
  directories.
- Remote ComfyUI mode cannot safely push local model downloads unless the
  operator has separately mounted the correct model path.

Live probe examples:

```bash
PYTHONPATH=. python3 scripts/comfyui/local_connection_smoke.py
```

```bash
PYTHONPATH=. python3 scripts/comfyui/official_workflow_probe.py \
  --comfyui-url http://127.0.0.1:8188 \
  --preflight-only
```

Safety boundary:

- Do not let ComfyUI model downloads escape `ComfyUI/models/`.
- Do not enable in-process Diffusers for normal production traffic.
- Keep generated images and temporary workflow outputs in runtime/storage, not
  tracked docs.
- If a backend times out or becomes unresponsive, surface that state to the UI
  instead of retrying indefinitely.

## Adding Another External Tool

Before wiring a new external repo or binary:

- Add a small service wrapper under `services/<domain>/`.
- Add root/admin setup only if the tool downloads code, installs packages, or
  writes outside runtime.
- Add a CLI probe under `scripts/<domain>/` for reproducible diagnostics.
- Add ignored runtime/output paths to `.gitignore` if needed.
- Add API reference entries if any route is exposed.
- Add a section to this playbook with install, config, call sites, and safety
  boundaries.
- Add Playwright or script-level smoke only after the integration can fail with
  clear user-facing errors.
