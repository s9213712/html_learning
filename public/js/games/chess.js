'use strict';

const CHESS_PIECES = {
  K: "♔", Q: "♕", R: "♖", B: "♗", N: "♘", P: "♙",
  k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟",
};
const CHESS_BOARD_PIECES = {
  k: "♔", q: "♕", r: "♖", b: "♗", n: "♘", p: "♙",
};
const CHESS_MATERIAL_VALUES = { p: 1, n: 3, b: 3, r: 5, q: 9, k: 0 };
const CHESS_PROMOTION_CHOICES = [
  { key: "q", label: "后", piece: "♕", hint: "最強攻擊力" },
  { key: "r", label: "車", piece: "♖", hint: "直線控制" },
  { key: "b", label: "象", piece: "♗", hint: "斜線控制" },
  { key: "n", label: "馬", piece: "♘", hint: "跳躍戰術" },
];

const CHESS_PIPELINE_FALLBACK_COMMANDS = {
  prepare: "python3 scripts/games/chess_replay_prepare.py --replace-output --include-quarantine",
  seed_train: "python3 scripts/games/chess_seed_train.py --preset standard",
  exp3_refine: "python3 scripts/games/chess_exp3_dataset_train.py --input-jsonl runtime/reports/games/chess_datasets/train.jsonl",
  benchmark: "python3 scripts/games/chess_self_play_train.py --exp1-games 0 --exp2-games 0 --exp3-games 0 --exp4-games 0 --hard-exp1-games 0 --hard-exp2-games 0 --hard-exp3-games 0 --hard-exp4-games 0 --cross-games 0 --cross-exp1-exp3-games 0 --cross-exp2-exp3-games 0 --cross-exp1-exp4-games 0 --cross-exp2-exp4-games 0 --cross-exp3-exp4-games 0 --benchmark-rounds 1 --smoke-games-per-pair 1",
  full_pipeline: "python3 scripts/games/chess_train_pipeline.py --preset standard --include-quarantine",
};

let chessClockMeta = { matchId: null, moveCount: 0, turn: "" };
let chessPracticeDifficultyUiBound = false;
let chessDialogResolve = null;
let chessDialogPreviousFocus = null;
const chessCompetitionClock = window.createHackmeCompetitionClock?.({
  onExpire(side) {
    const loser = side === "white" ? "白方" : "黑方";
    const winner = side === "white" ? "黑方" : "白方";
    setGameMsg(`${loser}棋鐘超時，${winner}超時勝。`, false);
    renderChessClockDisplay();
  },
}) || null;

window.resetChessAccountState = function resetChessAccountState() {
  closeChessDialog(null);
  chessClockMeta = { matchId: null, moveCount: 0, turn: "" };
  chessCompetitionClock?.stop?.();
};

function ensureChessDialogOverlay() {
  let overlay = $("chess-action-dialog");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "chess-action-dialog";
  overlay.className = "user-edit-overlay chess-action-dialog-overlay";
  overlay.setAttribute("aria-hidden", "true");
  overlay.innerHTML = `
    <div class="user-edit-modal chess-action-dialog-modal" role="dialog" aria-modal="true" aria-labelledby="chess-action-dialog-title">
      <div class="mini-title" id="chess-action-dialog-title"></div>
      <div class="drive-card-sub" id="chess-action-dialog-body"></div>
      <div class="chess-action-dialog-choices" id="chess-action-dialog-choices"></div>
    </div>
  `;
  document.body.appendChild(overlay);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closeChessDialog(null);
  });
  return overlay;
}

function closeChessDialog(value) {
  const overlay = $("chess-action-dialog");
  if (overlay) {
    overlay.classList.remove("show");
    overlay.setAttribute("aria-hidden", "true");
  }
  document.body.classList.remove("modal-open");
  if (typeof chessDialogResolve === "function") {
    const resolve = chessDialogResolve;
    chessDialogResolve = null;
    resolve(value);
  }
  if (chessDialogPreviousFocus && typeof chessDialogPreviousFocus.focus === "function") {
    chessDialogPreviousFocus.focus();
  }
  chessDialogPreviousFocus = null;
}

function showChessChoiceDialog({ title, message, choices, cancelLabel = "取消" }) {
  const overlay = ensureChessDialogOverlay();
  const titleEl = $("chess-action-dialog-title");
  const bodyEl = $("chess-action-dialog-body");
  const choicesEl = $("chess-action-dialog-choices");
  if (!overlay || !choicesEl) return Promise.resolve(null);
  if (chessDialogResolve) closeChessDialog(null);
  chessDialogPreviousFocus = document.activeElement;
  if (titleEl) titleEl.textContent = title || "確認操作";
  if (bodyEl) bodyEl.textContent = message || "";
  choicesEl.innerHTML = "";
  (choices || []).forEach((choice) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `btn game-mini-btn ${choice.primary ? "btn-primary" : ""} ${choice.danger ? "btn-danger" : ""}`;
    button.dataset.chessDialogValue = String(choice.value ?? "");
    if (choice.piece) {
      const piece = document.createElement("span");
      piece.className = "chess-promotion-piece";
      piece.textContent = choice.piece;
      button.appendChild(piece);
    }
    const text = document.createElement("span");
    text.textContent = choice.label || String(choice.value || "");
    button.appendChild(text);
    if (choice.hint) {
      const hint = document.createElement("small");
      hint.textContent = choice.hint;
      button.appendChild(hint);
    }
    button.addEventListener("click", () => closeChessDialog(choice.value));
    choicesEl.appendChild(button);
  });
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "btn game-mini-btn";
  cancel.textContent = cancelLabel;
  cancel.addEventListener("click", () => closeChessDialog(null));
  choicesEl.appendChild(cancel);
  overlay.classList.add("show");
  overlay.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  window.setTimeout(() => choicesEl.querySelector("button")?.focus(), 0);
  return new Promise((resolve) => {
    chessDialogResolve = resolve;
  });
}

function confirmChessAction(title, message, confirmLabel = "確認", danger = false) {
  return showChessChoiceDialog({
    title,
    message,
    choices: [{ value: "confirm", label: confirmLabel, primary: !danger, danger }],
    cancelLabel: "取消",
  }).then((value) => value === "confirm");
}

async function chooseChessPromotion(candidateMoves) {
  const available = new Set(candidateMoves.map((move) => String(move.promotion || "").toLowerCase()).filter(Boolean));
  const choices = CHESS_PROMOTION_CHOICES
    .filter((choice) => !available.size || available.has(choice.key))
    .map((choice) => ({ ...choice, value: choice.key, primary: choice.key === "q" }));
  const selected = await showChessChoiceDialog({
    title: "選擇兵升變",
    message: "請選擇這步兵升變要變成的棋子。",
    choices,
    cancelLabel: "取消升變",
  });
  return selected ? String(selected).toLowerCase() : null;
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && chessDialogResolve) {
    event.preventDefault();
    closeChessDialog(null);
  }
});

function chessClockSideLabel(side) {
  return side === "white" ? "白" : "黑";
}

function selectedChessMatchForClock() {
  return (gameState.matches || []).find((match) => match.id === gameSelectedMatchId) || null;
}

function chessClockMoveCount(match) {
  return Array.isArray(match?.move_history) ? match.move_history.length : 0;
}

function readChessClockConfig() {
  const presetKey = $("game-clock-preset")?.value || chessCompetitionClock?.state.presetKey || "rapid_10_0";
  const preset = window.gameClockPreset?.(presetKey) || { mainSeconds: 600, incrementSeconds: 0 };
  const custom = presetKey === "custom";
  const mainMinutes = custom
    ? Number($("game-clock-main-minutes")?.value || 10)
    : preset.mainSeconds / 60;
  const incrementSeconds = custom
    ? Number($("game-clock-increment-seconds")?.value || 0)
    : preset.incrementSeconds;
  if (!custom) {
    const mainInput = $("game-clock-main-minutes");
    const incInput = $("game-clock-increment-seconds");
    if (mainInput) mainInput.value = String(Math.round(preset.mainSeconds / 60));
    if (incInput) incInput.value = String(preset.incrementSeconds);
  }
  return {
    enabled: Boolean($("game-clock-enabled")?.checked),
    presetKey,
    mainSeconds: Math.max(10, Math.round(mainMinutes * 60)),
    incrementSeconds: Math.max(0, Math.round(incrementSeconds)),
  };
}

function renderChessClockDisplay(snapshot = chessCompetitionClock?.state) {
  if (!snapshot) return;
  const panel = $("game-clock-panel");
  if (panel) {
    panel.classList.toggle("enabled", Boolean(snapshot.enabled));
    panel.classList.toggle("expired", Boolean(snapshot.expiredSide));
  }
  ["white", "black"].forEach((side) => {
    const el = document.querySelector(`[data-chess-clock-side="${side}"]`);
    if (!el) return;
    const value = side === "white" ? snapshot.whiteMs : snapshot.blackMs;
    el.textContent = `${chessClockSideLabel(side)} ${window.formatHackmeGameClock?.(value) || "--:--"}`;
    el.classList.toggle("active", snapshot.enabled && snapshot.running && snapshot.activeSide === side && !snapshot.expiredSide);
    el.classList.toggle("expired", snapshot.expiredSide === side);
  });
}

function applyChessClockConfig({ reset = true } = {}) {
  if (!chessCompetitionClock) return false;
  const config = readChessClockConfig();
  chessCompetitionClock.configure(config);
  const match = selectedChessMatchForClock();
  const activeTurn = match?.current_turn || "white";
  if (reset) {
    chessClockMeta = { matchId: match?.id || null, moveCount: chessClockMoveCount(match), turn: activeTurn };
    chessCompetitionClock.reset(activeTurn);
  } else if (!config.enabled) {
    chessCompetitionClock.stop();
  } else {
    chessCompetitionClock.start(activeTurn);
  }
  renderChessClockDisplay();
  return true;
}

function syncChessClockWithMatch(match) {
  if (!chessCompetitionClock) return;
  if (!match || match.status !== "active") {
    chessCompetitionClock.stop();
    chessClockMeta = { matchId: match?.id || null, moveCount: chessClockMoveCount(match), turn: match?.current_turn || "" };
    renderChessClockDisplay();
    return;
  }
  const moveCount = chessClockMoveCount(match);
  const turn = match.current_turn || "white";
  if (chessClockMeta.matchId !== match.id) {
    chessClockMeta = { matchId: match.id, moveCount, turn };
    chessCompetitionClock.reset(turn);
  } else if (chessClockMeta.turn && chessClockMeta.turn !== turn && moveCount >= chessClockMeta.moveCount) {
    chessClockMeta = { matchId: match.id, moveCount, turn };
    chessCompetitionClock.switchTurn(turn);
  } else if (chessCompetitionClock.state.enabled && !chessCompetitionClock.state.expiredSide) {
    chessCompetitionClock.start(turn);
  }
  renderChessClockDisplay();
}

chessCompetitionClock?.subscribe(renderChessClockDisplay);

function gameDifficultyOptionDescription(difficulty) {
  const normalizedDifficulty = normalizeChessPracticeDifficultyKey(difficulty);
  const strength = window.HACKME_GAME_AI_STRENGTH?.chess?.[normalizedDifficulty] || window.HACKME_GAME_AI_STRENGTH?.chess?.[difficulty];
  const suffix = strength ? `｜${strength}` : "";
  if (normalizedDifficulty === "stockfish") return `Stockfish（本機外部引擎）${suffix}`;
  if (normalizedDifficulty === "experiment 6:neuralnet") return `實驗 6：Neural Network${suffix}`;
  if (normalizedDifficulty === "experiment 5:nnue") return `實驗 5：NNUE + AlphaBeta/PVS${suffix}`;
  if (normalizedDifficulty === "experiment 4:pv") return `實驗 4：Policy/Value + MCTS${suffix}`;
  if (normalizedDifficulty === "experiment 3:dl") return `實驗 3：DL 語義平衡學習${suffix}`;
  if (normalizedDifficulty === "experiment 2:nn") return `實驗 2：NN 評估${suffix}`;
  if (normalizedDifficulty === "experiment 1:search") return `實驗 1：引擎搜尋 + 對局學習${suffix}`;
  if (normalizedDifficulty === "experiment 0:minimax2ply") return `實驗 0：2 層物質 minimax${suffix}`;
  // Legacy DB rows (kept for old games)
  if (normalizedDifficulty === "experiment") return `實驗 1：引擎搜尋 + 對局學習（legacy）${suffix}`;
  if (normalizedDifficulty === "hard") return `實驗 0：2 層物質 minimax（legacy）${suffix}`;
  return `普通：優先吃子與將軍${suffix}`;
}

function renderChessPracticeDifficultyOptions(games) {
  const select = $("game-practice-difficulty");
  if (!select) return;
  const chessGame = (Array.isArray(games) ? games : []).find((game) => game?.key === "chess");
  const rows = Array.isArray(chessGame?.computer_difficulties) && chessGame.computer_difficulties.length
    ? chessGame.computer_difficulties
    : [
        { key: "experiment 0:minimax2ply", label: "實驗 0：2 層物質 minimax" },
        { key: "experiment 1:search", label: "實驗 1：引擎搜尋 + 對局學習" },
        { key: "experiment 2:nn", label: "實驗 2：NN 評估" },
        { key: "experiment 3:dl", label: "實驗 3：DL 語義平衡" },
        { key: "experiment 4:pv", label: "實驗 4：Policy/Value + MCTS" },
        { key: "experiment 5:nnue", label: "實驗 5：NNUE + AlphaBeta/PVS" },
        { key: "experiment 6:neuralnet", label: "實驗 6：Neural Network" },
      ];
  const current = select.value || "experiment 0:minimax2ply";
  select.innerHTML = rows.map((row) => {
    const key = String(row?.key || "normal");
    const label = String(row?.label || gameDifficultyLabel(key));
    return `<option value="${sanitize(key)}">${sanitize(gameDifficultyOptionDescription(key) || label)}</option>`;
  }).join("");
  const allowed = new Set(rows.map((row) => String(row?.key || "")));
  const fallback = allowed.has("experiment 0:minimax2ply") ? "experiment 0:minimax2ply" : String(rows[0]?.key || "experiment 0:minimax2ply");
  select.value = allowed.has(current) ? current : fallback;
  bindChessPracticeDifficultyUi();
  updateChessStockfishDepthControl();
}

function normalizeChessPracticeDifficultyKey(value) {
  const key = String(value || "normal").trim().toLowerCase();
  if (key === "stockfisher" || key === "stockfish") return "stockfish";
  return key || "normal";
}

function isChessStockfishDifficulty(value) {
  return normalizeChessPracticeDifficultyKey(value) === "stockfish";
}

function normalizeChessStockfishDepth(value) {
  const parsed = Number.parseInt(String(value || ""), 10);
  if (!Number.isFinite(parsed)) return 10;
  return Math.max(1, Math.min(20, parsed));
}

function selectedChessStockfishDepth() {
  const input = $("game-practice-stockfish-depth");
  const depth = normalizeChessStockfishDepth(input?.value || 10);
  if (input) input.value = String(depth);
  return depth;
}

function updateChessStockfishDepthControl() {
  const field = $("game-practice-stockfish-depth-field");
  const select = $("game-practice-difficulty");
  const input = $("game-practice-stockfish-depth");
  if (!field || !select) return;
  const enabled = isChessStockfishDifficulty(select.value);
  field.hidden = !enabled;
  field.style.display = enabled ? "" : "none";
  field.setAttribute("aria-hidden", enabled ? "false" : "true");
  if (input) {
    input.disabled = !enabled;
    input.required = enabled;
  }
  if (enabled) selectedChessStockfishDepth();
}

function bindChessPracticeDifficultyUi() {
  if (chessPracticeDifficultyUiBound) return;
  const select = $("game-practice-difficulty");
  const input = $("game-practice-stockfish-depth");
  if (!select) return;
  select.addEventListener("change", updateChessStockfishDepthControl);
  if (input) input.addEventListener("change", selectedChessStockfishDepth);
  chessPracticeDifficultyUiBound = true;
}

function gameMatchLabel(match) {
  const side = match.my_side === "black" ? "黑方" : "白方";
  const opponentName = match.my_side === "black" ? match.white_username : match.black_username;
  const stockfishDepth = match.computer_difficulty === "stockfish" && match.stockfish_depth
    ? ` depth ${Number(match.stockfish_depth || 0)}`
    : "";
  const difficulty = match.mode === "computer" ? ` · ${gameDifficultyLabel(match.computer_difficulty)}${stockfishDepth}` : "";
  return `${side} vs ${opponentName || "電腦"}${difficulty}`;
}

function gameDifficultyLabel(difficulty) {
  if (difficulty === "stockfish") return "Stockfish（本機）";
  if (difficulty === "experiment 6:neuralnet") return "實驗 6：Neural Network";
  if (difficulty === "experiment 5:nnue") return "實驗 5：NNUE + AlphaBeta/PVS";
  if (difficulty === "experiment 4:pv") return "實驗 4：Policy/Value + MCTS";
  if (difficulty === "experiment 3:dl") return "實驗 3：DL 語義平衡";
  if (difficulty === "experiment 2:nn") return "實驗 2：NN 評估";
  if (difficulty === "experiment 1:search") return "實驗 1：引擎搜尋 + 對局學習";
  if (difficulty === "experiment 0:minimax2ply") return "實驗 0：2 層物質 minimax";
  // Legacy labels (old DB rows)
  if (difficulty === "experiment") return "實驗 1（legacy）";
  if (difficulty === "hard") return "實驗 0（legacy）";
  if (difficulty === "normal") return "普通（legacy）";
  return difficulty || "普通";
}

function gameOpponentColor(color) {
  return color === "white" ? "black" : "white";
}

function chessKingSquare(boardMap, color) {
  const king = color === "black" ? "k" : "K";
  for (const [square, piece] of Object.entries(boardMap || {})) {
    if (piece === king) return square;
  }
  return "";
}

function normalizeChessUciInput(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-h1-8qrbn]/g, "")
    .slice(0, 5);
}

function chessResultText(match) {
  if (!match || match.status === "active") return "";
  const reason = String(match.result_reason || "");
  const winnerId = String(match.winner_user_id || "");
  const me = String(currentUserId || "");
  if (reason === "resign") {
    if (winnerId && winnerId === me) return "對手已認輸，你獲勝";
    if (winnerId) return `${match.winner_username || "對手"} 因對方認輸獲勝`;
    return "你已認輸，棋局結束";
  }
  if (reason === "checkmate") {
    return match.winner_username ? `Checkmate：王死，遊戲結束，勝者：${match.winner_username}` : "Checkmate：王死，遊戲結束";
  }
  if (reason === "agreed_draw") return "雙方同意和棋";
  if (reason === "stalemate") return "逼和，棋局結束";
  if (reason === "insufficient_material") return "子力不足，和棋";
  if (reason === "threefold_repetition") return "三次重複，和棋";
  if (reason === "fifty_moves") return "50 步規則，和棋";
  if (reason === "seventyfive_moves") return "75 步規則，和棋";
  if (reason === "fivefold_repetition") return "五次重複，和棋";
  return match.winner_username ? `已結束，勝者：${match.winner_username}` : `已結束：${reason || "平手"}`;
}

function chessPieceValue(piece) {
  return CHESS_MATERIAL_VALUES[String(piece || "").toLowerCase()] || 0;
}

function chessMaterialSummary(boardMap = {}) {
  let white = 0;
  let black = 0;
  Object.values(boardMap || {}).forEach((piece) => {
    const value = chessPieceValue(piece);
    if (piece === String(piece).toUpperCase()) white += value;
    else black += value;
  });
  return { white, black, diff: white - black };
}

function chessBestHintMove(match) {
  const moves = Array.isArray(match?.legal_moves) ? match.legal_moves : [];
  if (!moves.length) return null;
  const scored = moves.map((move) => {
    let score = 0;
    if (move.captured) score += chessPieceValue(move.captured) * 100;
    if (move.promotion) score += chessPieceValue(move.promotion) * 90;
    if (move.castle) score += 45;
    if (move.en_passant) score += 35;
    const centerFiles = { d: 1, e: 1, c: 0.5, f: 0.5 };
    score += (centerFiles[String(move.to || "")[0]] || 0) * 8;
    score += ["4", "5"].includes(String(move.to || "")[1]) ? 6 : 0;
    return { move, score };
  }).sort((a, b) => b.score - a.score || String(a.move.from).localeCompare(String(b.move.from)));
  return scored[0]?.move || moves[0];
}

function chessMoveSan(move) {
  if (!move) return "";
  return `${move.from}${move.to}${move.promotion || ""}`;
}

function chessVisibleMoveHistory(moves, limit = 16) {
  const allMoves = Array.isArray(moves) ? moves : [];
  const visibleMoves = allMoves.slice(-Math.max(1, Number(limit) || 16));
  const startPly = Math.max(0, allMoves.length - visibleMoves.length);
  return visibleMoves.map((move, index) => ({
    move,
    plyNumber: startPly + index + 1,
  }));
}

function renderChessMoveHistory(match, history) {
  if (!history) return;
  const rows = chessVisibleMoveHistory(match?.move_history || []);
  history.innerHTML = rows.length
    ? rows.map(({ move, plyNumber }) => `<span data-chess-ply="${plyNumber}">${plyNumber}. ${sanitize(move.from)}→${sanitize(move.to)}${move.computer ? " · CPU" : ""}</span>`).join("")
    : "<span style=\"color:var(--muted);\">尚未走棋</span>";
}

function renderChessAnalysis(match, prefix = "") {
  const panel = $("game-chess-analysis-panel");
  if (!panel) return;
  if (!match) {
    panel.innerHTML = "";
    return;
  }
  const material = chessMaterialSummary(match.board || {});
  const moves = Array.isArray(match.move_history) ? match.move_history : [];
  const captures = moves.filter((move) => move.captured).length;
  const computerMoves = moves.filter((move) => move.computer).length;
  const hint = match.status === "active" && match.my_side === match.current_turn ? chessBestHintMove(match) : null;
  const strength = match.mode === "computer"
    ? window.HACKME_GAME_AI_STRENGTH?.chess?.[match.computer_difficulty || "normal"]
    : "";
  const result = match.status !== "active" ? chessResultText(match) : "對局進行中";
  const materialText = material.diff === 0
    ? "子力相等"
    : material.diff > 0 ? `白方多 ${material.diff} 分子力` : `黑方多 ${Math.abs(material.diff)} 分子力`;
  panel.innerHTML = `
    <div class="game-analysis-card"><strong>${sanitize(prefix || "棋局分析")}</strong><br>${sanitize(result)} · ${sanitize(materialText)} · 吃子 ${captures} 次 · AI 回手 ${computerMoves} 次</div>
    <div class="game-analysis-card"><strong>錯著提示</strong><br>${hint ? `候選手 ${sanitize(chessMoveSan(hint))}：${hint.captured ? `可吃 ${sanitize(CHESS_PIECES[hint.captured] || hint.captured)}` : hint.castle ? "可王車易位改善王安全" : "優先中心與出子"}` : "結束後顯示子力、吃子與節奏；輪到你時會顯示候選手。"}</div>
    <div class="game-analysis-card"><strong>AI 難度</strong><br>${sanitize(strength || "玩家對戰沒有 AI 難度。")}</div>
  `;
}

function showChessHint() {
  const match = (gameState.matches || []).find((item) => item.id === gameSelectedMatchId);
  if (!match || match.status !== "active") {
    renderChessAnalysis(match, "賽後分析");
    return;
  }
  if (match.my_side !== match.current_turn) {
    setGameMsg("目前等待對手或 AI 走棋。", false);
    renderChessAnalysis(match, "提示");
    return;
  }
  const hint = chessBestHintMove(match);
  if (!hint) {
    setGameMsg("目前沒有合法走法。", false);
    return;
  }
  gameSelectedSquare = hint.from;
  renderChessBoard(match);
  renderChessAnalysis(match, `建議候選手：${chessMoveSan(hint)}`);
  setGameMsg(`提示：可考慮 ${chessMoveSan(hint)}`, true);
}

async function createChessEndgameChallenge() {
  try {
    const presets = ["rook_endgame", "queen_mate", "knight_fork"];
    const preset = presets[(Date.now() / 1000 | 0) % presets.length];
    const json = await gameRequest("/games/chess/challenge", { method: "POST", body: { preset } });
    gameSelectedMatchId = json.match_id;
    gameSelectedSquare = null;
    const msg = `已建立${json.title || "殘局題"}：${json.hint || "請找最佳續著"}`;
    setGameMsg(msg, true);
    await refreshGameZoneAfterMutation(msg);
  } catch (err) {
    setGameMsg(err.message || "建立殘局題失敗", false);
  }
}

function buildOptimisticChessMatch(match, move) {
  if (!match || !move) return match;
  const board = { ...(match.board || {}) };
  const piece = move.piece || board[move.from] || "";
  delete board[move.from];
  if (move.en_passant) {
    const captureRank = (match.current_turn === "white" ? Number(move.to[1]) - 1 : Number(move.to[1]) + 1);
    delete board[`${move.to[0]}${captureRank}`];
  }
  board[move.to] = move.promotion
    ? (match.current_turn === "white" ? String(move.promotion || "q").toUpperCase() : String(move.promotion || "q").toLowerCase())
    : piece;
  if (move.castle) {
    if (move.to === "g1") {
      delete board.h1;
      board.f1 = "R";
    } else if (move.to === "c1") {
      delete board.a1;
      board.d1 = "R";
    } else if (move.to === "g8") {
      delete board.h8;
      board.f8 = "r";
    } else if (move.to === "c8") {
      delete board.a8;
      board.d8 = "r";
    }
  }
  const moveHistory = Array.isArray(match.move_history) ? [...match.move_history] : [];
  moveHistory.push({
    by: match.current_turn,
    from: move.from,
    to: move.to,
    piece,
    captured: move.captured || null,
    promotion: move.promotion || null,
    castle: Boolean(move.castle),
    en_passant: Boolean(move.en_passant),
  });
  return {
    ...match,
    board,
    move_history: moveHistory,
    current_turn: gameOpponentColor(match.current_turn),
    legal_moves: [],
    pending_computer_response: true,
    pending_status_message: "你已走棋，電腦思考中...",
  };
}

function resolveChessMoveSelection(match, from, clickedSquare, selectedPiece, clickedPiece) {
  const moves = match.legal_moves || [];
  const direct = moves.filter((move) => move.from === from && move.to === clickedSquare);
  if (direct.length) {
    return { moves: direct, to: clickedSquare, castleByRookClick: false };
  }
  const isKing = selectedPiece === "K" || selectedPiece === "k";
  const isOwnRook = clickedPiece
    && ((match.my_side === "white" && clickedPiece === "R") || (match.my_side === "black" && clickedPiece === "r"));
  if (!isKing || !isOwnRook) return { moves: [], to: clickedSquare, castleByRookClick: false };
  const rank = match.my_side === "white" ? "1" : "8";
  const castleTo = clickedSquare === `h${rank}` ? `g${rank}` : (clickedSquare === `a${rank}` ? `c${rank}` : "");
  if (!castleTo) return { moves: [], to: clickedSquare, castleByRookClick: false };
  const castleMoves = moves.filter((move) => move.from === from && move.to === castleTo && move.castle);
  return { moves: castleMoves, to: castleTo, castleByRookClick: castleMoves.length > 0 };
}

function renderGameMatches(matches) {
  const wrap = $("game-match-list");
  if (!wrap) return;
  const rows = Array.isArray(matches) ? matches : [];
  if (!gameSelectedMatchId && rows.length) {
    gameSelectedMatchId = rows[0].id;
  }
  if (!rows.length) {
    wrap.innerHTML = "<p style=\"color:var(--muted);\">還沒有棋局</p>";
    renderChessBoard(null);
    return;
  }
  wrap.innerHTML = rows.map((match) => {
    const canDelete = match.status !== "active";
    const meta = match.status === "active"
      ? `${match.current_turn === "white" ? "白方走" : "黑方走"}`
      : chessResultText(match);
    return `
      <div class="game-match-item ${match.id === gameSelectedMatchId ? "active" : ""}">
        <button class="game-match-row ${match.id === gameSelectedMatchId ? "active" : ""}" type="button" data-game-match-id="${match.id}">
          <span><strong>${sanitize(gameMatchLabel(match))}</strong><small>${sanitize(match.status)} · ${sanitize(meta)}</small></span>
          <span>${match.mode === "computer" ? "練習" : "對戰"}</span>
        </button>
        ${canDelete ? `<button class="btn btn-danger game-mini-btn" type="button" data-game-delete-match="${match.id}" title="刪除已結束棋局">刪除</button>` : ""}
      </div>
    `;
  }).join("");
  const selected = rows.find((match) => match.id === gameSelectedMatchId) || rows[0];
  if (selected) {
    gameSelectedMatchId = selected.id;
    renderChessBoard(selected);
  }
}

function renderChessBoard(match) {
  const board = $("chess-board");
  const title = $("game-current-title");
  const status = $("game-current-status");
  const history = $("game-move-history");
  const uciInput = $("game-chess-uci-input");
  const uciSubmit = $("game-chess-uci-submit-btn");
  const resign = $("game-resign-btn");
  const offerDraw = $("game-offer-draw-btn");
  const acceptDraw = $("game-accept-draw-btn");
  const rejectDraw = $("game-reject-draw-btn");
  const claimDraw = $("game-claim-draw-btn");
  if (!board) return;
  if (!match) {
    syncChessClockWithMatch(null);
    board.innerHTML = "<div class=\"chess-empty\">尚未選擇棋局</div>";
    if (title) title.textContent = "尚未選擇棋局";
    if (status) status.textContent = "選擇棋局後即可走棋";
    if (history) history.innerHTML = "";
    renderChessAnalysis(null);
    if (uciInput) {
      uciInput.value = "";
      uciInput.disabled = true;
    }
    if (uciSubmit) uciSubmit.disabled = true;
    if (resign) resign.style.display = "none";
    if (offerDraw) offerDraw.style.display = "none";
    if (acceptDraw) acceptDraw.style.display = "none";
    if (rejectDraw) rejectDraw.style.display = "none";
    if (claimDraw) claimDraw.style.display = "none";
    return;
  }
  const boardMap = match.board || {};
  if (title) title.textContent = gameMatchLabel(match);
  syncChessClockWithMatch(match);
  const clockSnapshot = chessCompetitionClock?.snapshot?.() || {};
  const clockExpired = Boolean(clockSnapshot.enabled && clockSnapshot.expiredSide);
  const myTurn = match.status === "active" && match.my_side === match.current_turn;
  if (uciInput) uciInput.disabled = !myTurn || chessMoveInFlight || clockExpired;
  if (uciSubmit) uciSubmit.disabled = !myTurn || chessMoveInFlight || clockExpired;
  if (status) {
    if (clockExpired) {
      const loser = clockSnapshot.expiredSide === "white" ? "白方" : "黑方";
      const winner = clockSnapshot.expiredSide === "white" ? "黑方" : "白方";
      status.textContent = `${loser}超時，${winner}超時勝`;
    } else if (match.status !== "active") {
      status.textContent = chessResultText(match);
    } else if (match.current_check) {
      status.textContent = `${match.current_turn === "white" ? "白方" : "黑方"}王被鎖定：Check，必須先解將`;
    } else if (match.draw_offer_pending && match.can_accept_draw_offer) {
      status.textContent = `${match.draw_offer_by_username || "對手"} 已提和，等待你接受或拒絕`;
    } else if (match.draw_offer_pending && String(match.draw_offer_by_user_id || "") === String(currentUserId || "")) {
      status.textContent = `已向 ${match.my_side === "white" ? match.black_username : match.white_username} 提和，等待對方回覆`;
    } else if (match.pending_computer_response) {
      status.textContent = match.pending_status_message || "你已走棋，電腦思考中...";
    } else {
      status.textContent = myTurn ? "輪到你走棋" : `等待${match.current_turn === "white" ? "白方" : "黑方"}走棋`;
    }
  }
  if (resign) resign.style.display = match.status === "active" ? "" : "none";
  if (offerDraw) {
    offerDraw.textContent = "求和";
    offerDraw.title = "向對手提出和棋";
    offerDraw.style.display = match.status === "active" && match.can_offer_draw ? "" : "none";
  }
  if (acceptDraw) acceptDraw.style.display = match.status === "active" && match.can_accept_draw_offer ? "" : "none";
  if (rejectDraw) rejectDraw.style.display = match.status === "active" && match.can_reject_draw_offer ? "" : "none";
  if (claimDraw) claimDraw.style.display = match.status === "active" && myTurn && match.can_claim_draw ? "" : "none";
  if (gameSelectedSquare && !boardMap[gameSelectedSquare]) gameSelectedSquare = null;
  const legalTargets = new Set((match.legal_moves || [])
    .filter((move) => move.from === gameSelectedSquare)
    .map((move) => move.to));
  const specialTargets = new Map((match.legal_moves || [])
    .filter((move) => move.from === gameSelectedSquare && (move.castle || move.en_passant || move.promotion))
    .map((move) => [move.to, move]));
  const checkmatedKingSquare = match.status !== "active" && match.result_reason === "checkmate"
    ? chessKingSquare(boardMap, match.current_turn)
    : "";
  const checkedKingSquare = match.status === "active" && match.current_check
    ? (match.current_king_square || chessKingSquare(boardMap, match.current_turn))
    : "";
  const deadKingSquare = match.status !== "active" && match.result_reason === "checkmate"
    ? checkmatedKingSquare
    : "";
  const squares = [];
  for (let rank = 8; rank >= 1; rank -= 1) {
    for (const file of "abcdefgh") {
      const square = file + rank;
      const piece = boardMap[square] || "";
      const isDark = (file.charCodeAt(0) + rank) % 2 === 0;
      const pieceSideClass = piece ? (piece === piece.toUpperCase() ? "white-piece" : "black-piece") : "";
      const selectable = myTurn && piece && ((match.my_side === "white" && piece === piece.toUpperCase()) || (match.my_side === "black" && piece === piece.toLowerCase()));
      const special = specialTargets.get(square);
      const specialClass = special?.castle ? " castle-target" : (special?.en_passant ? " en-passant-target" : (special?.promotion ? " promotion-target" : ""));
      const specialLabel = special?.castle ? "王車易位" : (special?.en_passant ? "吃過路兵" : (special?.promotion ? "升變" : ""));
      const kingStateClass = [
        square === checkedKingSquare ? "checked-king" : "",
        square === checkmatedKingSquare ? "checkmated-king" : "",
        square === deadKingSquare ? "dead-king" : "",
      ].filter(Boolean).join(" ");
      squares.push(`
        <button class="chess-square ${isDark ? "dark" : "light"} ${pieceSideClass} ${square === gameSelectedSquare ? "selected" : ""} ${legalTargets.has(square) ? `target${specialClass}` : ""} ${kingStateClass}"
                type="button" data-chess-square="${square}" title="${sanitize(specialLabel || square)}" ${match.status !== "active" || chessMoveInFlight || clockExpired ? "disabled" : ""}>
          <span class="${piece ? "chess-piece-glyph" : ""}">${sanitize(CHESS_BOARD_PIECES[String(piece || "").toLowerCase()] || "")}</span>
          <small>${specialLabel ? sanitize(specialLabel) : (selectable || legalTargets.has(square) ? sanitize(square) : "")}</small>
        </button>
      `);
    }
  }
  board.innerHTML = `
    <div class="chess-rank-labels" aria-hidden="true">${[8, 7, 6, 5, 4, 3, 2, 1].map((rank) => `<span>${rank}</span>`).join("")}</div>
    <div class="chess-board-grid">${squares.join("")}</div>
    <div class="chess-file-labels" aria-hidden="true">${"abcdefgh".split("").map((file) => `<span>${file}</span>`).join("")}</div>
  `;
  renderChessMoveHistory(match, history);
  renderChessAnalysis(match);
}

async function submitChessMove(match, chosenMove, { from, to, promotion = null, optimisticMessage = "已送出走棋，等待電腦回應..." } = {}) {
  if (!match || chessMoveInFlight) return;
  const operation = gameAccountOperationContext();
  const moveToken = {};
  const clockSnapshot = chessCompetitionClock?.snapshot?.() || {};
  if (clockSnapshot.enabled && clockSnapshot.expiredSide) {
    setGameMsg("棋鐘已超時，不能再走棋。", false);
    renderChessBoard(match);
    return;
  }
  const previousMatch = match;
  const targetMatchId = previousMatch.id;
  const optimisticMatch = buildOptimisticChessMatch(match, chosenMove);
  gameSelectedSquare = null;
  chessMoveInFlight = true;
  chessMoveToken = moveToken;
  gameState.matches = gameState.matches.map((item) => item.id === previousMatch.id ? optimisticMatch : item);
  renderGameMatches(gameState.matches);
  setGameMsg(optimisticMessage, true);
  try {
    const json = await gameRequest(`/games/chess/matches/${encodeURIComponent(targetMatchId)}/move`, {
      method: "POST",
      body: { from, to, promotion },
    });
    gameAccountAssertOperationCurrent(operation);
    if (chessMoveToken !== moveToken) return;
    const updated = json.match;
    gameState.matches = gameState.matches.map((item) => item.id === updated.id ? updated : item);
    renderGameMatches(gameState.matches);
    await loadGameZone();
    gameAccountAssertOperationCurrent(operation);
  } catch (err) {
    if (!gameAccountOperationIsCurrent(operation) || chessMoveToken !== moveToken || err?.name === "AbortError") return;
    gameState.matches = gameState.matches.map((item) => item.id === previousMatch.id ? previousMatch : item);
    setGameMsg(err.message || "走棋失敗", false);
  } finally {
    if (!gameAccountOperationIsCurrent(operation) || chessMoveToken !== moveToken) return;
    chessMoveToken = null;
    chessMoveInFlight = false;
    const latest = (gameState.matches || []).find((item) => item.id === targetMatchId);
    if (latest) renderChessBoard(latest);
  }
}

function resolveChessPromotionMove(candidateMoves, requestedPromotion) {
  const normalized = String(requestedPromotion || "").trim().toLowerCase();
  if (normalized) {
    return candidateMoves.find((move) => String(move.promotion || "").toLowerCase() === normalized) || null;
  }
  if (candidateMoves.some((move) => move.promotion)) {
    return candidateMoves.find((move) => String(move.promotion || "").toLowerCase() === "q") || candidateMoves[0] || null;
  }
  return candidateMoves[0] || null;
}

async function submitChessUciMove() {
  if (chessMoveInFlight) return;
  const input = $("game-chess-uci-input");
  const raw = normalizeChessUciInput(input?.value || "");
  if (input) input.value = raw;
  if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(raw)) {
    setGameMsg("UCI 格式需為 d1c2 或 e7e8q。", false);
    return;
  }
  const match = (gameState.matches || []).find((item) => item.id === gameSelectedMatchId);
  if (!match || match.status !== "active") {
    setGameMsg("請先選擇進行中的棋局。", false);
    return;
  }
  if (match.my_side !== match.current_turn) {
    setGameMsg("還沒輪到你", false);
    return;
  }
  const from = raw.slice(0, 2);
  const to = raw.slice(2, 4);
  const requestedPromotion = raw.slice(4, 5);
  const candidateMoves = (match.legal_moves || []).filter((move) => move.from === from && move.to === to);
  if (requestedPromotion && !candidateMoves.some((move) => move.promotion)) {
    setGameMsg(`這步不需要升變棋子：${raw}`, false);
    return;
  }
  const chosenMove = resolveChessPromotionMove(candidateMoves, requestedPromotion);
  if (!chosenMove) {
    setGameMsg(`不是合法走法：${raw}`, false);
    return;
  }
  if (chosenMove.promotion && requestedPromotion && String(chosenMove.promotion || "").toLowerCase() !== requestedPromotion) {
    setGameMsg(`升變棋子不合法：${requestedPromotion}`, false);
    return;
  }
  await submitChessMove(match, chosenMove, {
    from,
    to,
    promotion: chosenMove.promotion || requestedPromotion || null,
    optimisticMessage: `已送出 ${raw}，等待電腦回應...`,
  });
  if (input) input.value = "";
}

function gameRootChessPanelVisible() {
  return currentUser === "root" && gameSelectedKey === "chess";
}

function renderChessRootDashboard(data) {
  const panel = $("game-root-chess-panel");
  const status = $("game-root-chess-status");
  const production = $("game-root-chess-production");
  const replay = $("game-root-chess-replay");
  const pipeline = $("game-root-chess-pipeline");
  const benchmark = $("game-root-chess-benchmark");
  const promotion = $("game-root-chess-promotion");
  const prepareCommand = $("game-root-chess-prepare-command");
  const seedCommand = $("game-root-chess-seed-command");
  const exp3Command = $("game-root-chess-exp3-command");
  const benchmarkCommand = $("game-root-chess-benchmark-command");
  const fullPipelineCommand = $("game-root-chess-pipeline-command");
  if (!panel || !status || !production || !replay || !pipeline || !benchmark || !promotion) return;
  panel.style.display = gameRootChessPanelVisible() ? "" : "none";
  if (!gameRootChessPanelVisible()) return;
  if (!data?.ok) {
    status.textContent = "chess engine dashboard 讀取失敗。";
    return;
  }
  const models = Array.isArray(data.production_models) ? data.production_models : [];
  const replayInfo = data.replay_buffer || {};
  const pipelineInfo = data.pipeline || {};
  const pipelineCommands = pipelineInfo.commands || CHESS_PIPELINE_FALLBACK_COMMANDS;
  const latestPrepare = data.latest_replay_prepare?.summary || {};
  const latestSeed = data.latest_seed_training_report?.summary || {};
  const latestPipeline = data.latest_pipeline_report?.summary || {};
  const bench = data.latest_benchmark?.benchmark || {};
  const smoke = data.latest_benchmark?.smoke_evaluation || {};
  const promo = data.promotion?.status || {};
  const recommendation = data.pipeline_recommendation || {};
  const readyLabel = recommendation.ready ? "可重訓" : "暫不建議重訓";
  status.textContent = `Warm Start ${data.warm_start?.ok ? "已就緒" : "失敗"} · usable replay ${Number(replayInfo.usable_replays || 0)} 筆 · ${readyLabel} · 最近 benchmark ${bench.games_played || 0} 場`;
  production.innerHTML = models.length
    ? models.map((row) => `<div class="drive-file-row game-list-row"><div><strong>${sanitize(row.engine || "-")}</strong><small>${sanitize(row.architecture || row.path || "-")}</small></div><strong>${sanitize(String(row.sample_count ?? row.size_bytes ?? 0))}</strong></div>`).join("")
    : "<p style=\"color:var(--muted);\">尚無 production model</p>";
  replay.innerHTML = `
    <div class="drive-file-row game-list-row"><div><strong>Replay Buffer</strong><small>${sanitize(replayInfo.path || "-")}</small></div><strong>${Number(replayInfo.total_replays || 0)}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Usable Replays</strong><small>trusted / trainable</small></div><strong>${Number(replayInfo.usable_replays || 0)}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Quarantine</strong><small>${sanitize(replayInfo.quarantine_path || "-")}</small></div><strong>${Number(replayInfo.quarantine_replays || 0)}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Rejected</strong><small>${sanitize(replayInfo.rejected_path || "-")}</small></div><strong>${Number(replayInfo.rejected_replays || 0)}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Recent User Games</strong><small>最近 7 天</small></div><strong>${Number(replayInfo.recent_user_games || 0)}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Suspicious Replays</strong><small>待降權 / 隔離</small></div><strong>${Number(replayInfo.suspicious_count || 0)}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Duplicate / Resign Abuse</strong><small>資料污染風險</small></div><strong>${Number(replayInfo.duplicate_count || 0)} / ${Number(replayInfo.resign_abuse_count || 0)}</strong></div>
  `;
  pipeline.innerHTML = `
    <div class="drive-file-row game-list-row"><div><strong>Retrain Recommendation</strong><small>${sanitize((recommendation.ready_reasons || recommendation.blocked_reasons || []).join(", ") || "no signal")}</small></div><strong>${recommendation.ready ? "READY" : "HOLD"}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Latest Prepare</strong><small>${sanitize(data.latest_replay_prepare?.path || "-")}</small></div><strong>${Number(latestPrepare.accepted_train_samples || 0)} / ${Number(latestPrepare.accepted_eval_samples || 0)}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Dataset Paths</strong><small>train / eval</small></div><strong>${sanitize(pipelineInfo.train_path || "-")} | ${sanitize(pipelineInfo.eval_path || "-")}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Latest Seed Train</strong><small>${sanitize(data.latest_seed_training_report?.path || "-")}</small></div><strong>${Number(latestSeed.games_played || 0)}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Latest Updates</strong><small>exp3 / exp4</small></div><strong>${Number((latestSeed.updates || {})["experiment 3:dl"] || 0)} / ${Number((latestSeed.updates || {})["experiment 4:pv"] || 0)}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Latest Full Pipeline</strong><small>${sanitize(data.latest_pipeline_report?.path || "-")}</small></div><strong>${sanitize(latestPipeline.run_id || "-")}</strong></div>
  `;
  const standings = Array.isArray(bench.standings) ? bench.standings.slice(0, 6) : [];
  benchmark.innerHTML = standings.length
    ? standings.map((row) => `<div class="drive-file-row game-list-row"><div><strong>${sanitize(row.engine || "-")}</strong><small>score_rate ${sanitize(String(row.score_rate ?? "-"))} · win_rate ${sanitize(String(row.win_rate ?? "-"))}</small></div><strong>${Number(row.games || 0)}</strong></div>`).join("")
    : "<p style=\"color:var(--muted);\">尚無 benchmark report</p>";
  const candidate = promo.candidate || null;
  const last = promo.last_promotion_result || null;
  promotion.innerHTML = `
    <div class="drive-file-row game-list-row"><div><strong>Current Candidate</strong><small>${sanitize(candidate?.engine || "none")}</small></div><strong>${candidate?.promotion_gate?.pass ? "PASS" : (candidate ? "HOLD" : "-")}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Smoke</strong><small>post-training smoke</small></div><strong>${smoke.pass ? "PASS" : (smoke.games_played ? "FAIL" : "-")}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Promotion Gate</strong><small>${sanitize((candidate?.promotion_gate?.reasons || []).join(", ") || "ready")}</small></div><strong>${candidate?.promotion_gate?.pass ? "PASS" : (candidate ? "HOLD" : "-")}</strong></div>
    <div class="drive-file-row game-list-row"><div><strong>Last Promotion</strong><small>${sanitize(last?.engine || "none")}</small></div><strong>${sanitize(last?.result || "-")}</strong></div>
  `;
  if (prepareCommand) prepareCommand.value = pipelineCommands.prepare || "";
  if (seedCommand) seedCommand.value = pipelineCommands.seed_train || "";
  if (exp3Command) exp3Command.value = pipelineCommands.exp3_refine || "";
  if (benchmarkCommand) benchmarkCommand.value = pipelineCommands.benchmark || "";
  if (fullPipelineCommand) fullPipelineCommand.value = pipelineCommands.full_pipeline || "";
}

async function loadChessRootDashboard() {
  const panel = $("game-root-chess-panel");
  if (panel) panel.style.display = gameRootChessPanelVisible() ? "" : "none";
  if (!gameRootChessPanelVisible()) return;
  const data = await gameRequest("/root/games/chess/engines/dashboard");
  renderChessRootDashboard(data);
}

async function createGameInvite() {
  const select = $("game-invite-user");
  const username = select ? select.value : "";
  if (!username) {
    setGameMsg("請先選擇要邀請的玩家；沒有其他玩家時可使用電腦練習。", false);
    return;
  }
  try {
    await gameRequest("/games/chess/invites", { method: "POST", body: { opponent_username: username } });
    setGameMsg("已送出對戰邀請", true);
    await refreshGameZoneAfterMutation("已送出對戰邀請");
  } catch (err) {
    setGameMsg(err.message || "送出邀請失敗", false);
  }
}

async function reviewGameInvite(inviteId, action) {
  try {
    const json = await gameRequest(`/games/chess/invites/${encodeURIComponent(inviteId)}/${encodeURIComponent(action)}`, { method: "POST", body: {} });
    if (json.match_id) gameSelectedMatchId = json.match_id;
    setGameMsg("邀請已更新", true);
    await refreshGameZoneAfterMutation("邀請已更新");
  } catch (err) {
    setGameMsg(err.message || "邀請處理失敗", false);
  }
}

async function createPracticeGame() {
  try {
    const side = $("game-practice-side")?.value || "white";
    const difficulty = normalizeChessPracticeDifficultyKey($("game-practice-difficulty")?.value || "normal");
    const body = { side, difficulty };
    if (isChessStockfishDifficulty(difficulty)) body.stockfish_depth = selectedChessStockfishDepth();
    const json = await gameRequest("/games/chess/practice", { method: "POST", body });
    gameSelectedMatchId = json.match_id;
    gameSelectedSquare = null;
    const depthText = isChessStockfishDifficulty(difficulty) ? ` depth ${body.stockfish_depth}` : "";
    const msg = `${side === "black" ? "已建立電腦練習局，你執黑方" : "已建立電腦練習局，你執白方"}，難度：${gameDifficultyLabel(difficulty)}${depthText}`;
    setGameMsg(msg, true);
    await refreshGameZoneAfterMutation(msg);
  } catch (err) {
    setGameMsg(err.message || "建立練習局失敗", false);
  }
}

async function selectChessSquare(square) {
  if (chessMoveInFlight) return;
  const match = (gameState.matches || []).find((item) => item.id === gameSelectedMatchId);
  if (!match || match.status !== "active") return;
  const piece = match.board?.[square] || "";
  const selectedPiece = gameSelectedSquare ? (match.board?.[gameSelectedSquare] || "") : "";
  const myTurn = match.my_side === match.current_turn;
  const isOwnPiece = piece && ((match.my_side === "white" && piece === piece.toUpperCase()) || (match.my_side === "black" && piece === piece.toLowerCase()));
  const resolvedSelection = gameSelectedSquare
    ? resolveChessMoveSelection(match, gameSelectedSquare, square, selectedPiece, piece)
    : { moves: [], to: square, castleByRookClick: false };
  const legal = resolvedSelection.moves.length > 0;
  if (gameSelectedSquare && !selectedPiece) {
    gameSelectedSquare = null;
    renderChessBoard(match);
    return;
  }
  if (gameSelectedSquare && legal && selectedPiece) {
    const from = gameSelectedSquare;
    const targetSquare = resolvedSelection.to || square;
    const candidateMoves = resolvedSelection.moves;
    let chosenMove = candidateMoves[0] || {
      from,
      to: targetSquare,
      piece: selectedPiece,
    };
    let promotion = chosenMove.promotion || null;
    if (candidateMoves.length > 1 && candidateMoves.some((move) => move.promotion)) {
      const selectedPromotion = await chooseChessPromotion(candidateMoves);
      if (!selectedPromotion) {
        setGameMsg("已取消升變走棋。", false);
        return;
      }
      chosenMove = candidateMoves.find((move) => String(move.promotion || "").toLowerCase() === selectedPromotion) || candidateMoves[0];
      promotion = chosenMove.promotion || selectedPromotion;
    }
    const previousMatch = match;
    await submitChessMove(previousMatch, chosenMove, { from, to: targetSquare, promotion });
    return;
  }
  if (isOwnPiece && myTurn) {
    gameSelectedSquare = square;
    renderChessBoard(match);
    if ((piece === "K" || piece === "k") && (match.legal_moves || []).some((move) => move.from === square && move.castle)) {
      setGameMsg("可王車易位：點亮起的 g/c 目標格，或直接點同側車。", true);
    }
  } else if (piece && !myTurn) {
    setGameMsg("還沒輪到你", false);
  }
}

async function claimChessDraw() {
  if (!gameSelectedMatchId) {
    setGameMsg("請先選擇要申請和棋的棋局。", false);
    return;
  }
  const match = (gameState.matches || []).find((item) => item.id === gameSelectedMatchId);
  if (!match) {
    setGameMsg("找不到目前棋局，請先刷新遊戲區。", false);
    return;
  }
  if (match.status !== "active") {
    setGameMsg("這局已經結束，不需要再申請和棋。", false);
    return;
  }
  if (!match.can_claim_draw) {
    setGameMsg("目前沒有三次重複或 50 步和棋可申請。", false);
    return;
  }
  try {
    const json = await gameRequest(`/games/chess/matches/${encodeURIComponent(gameSelectedMatchId)}/claim-draw`, { method: "POST", body: {} });
    gameSelectedSquare = null;
    if (json?.match?.id) {
      gameState.matches = (gameState.matches || []).map((item) => item.id === json.match.id ? json.match : item);
      renderGameMatches(gameState.matches);
    }
    setGameMsg("已申請和棋並結束棋局。", true);
    await loadGameZone();
  } catch (err) {
    setGameMsg(err.message || "申請和棋失敗", false);
  }
}

async function offerChessDraw() {
  if (!gameSelectedMatchId) {
    setGameMsg("請先選擇要提和的棋局。", false);
    return;
  }
  const match = (gameState.matches || []).find((item) => item.id === gameSelectedMatchId);
  if (!match) {
    setGameMsg("找不到目前棋局，請先刷新遊戲區。", false);
    return;
  }
  if (match.status !== "active") {
    setGameMsg("這局已經結束，不能再提和。", false);
    return;
  }
  if (!match.can_offer_draw) {
    setGameMsg("目前不能提和，可能已有待回覆的和棋。", false);
    return;
  }
  try {
    const json = await gameRequest(`/games/chess/matches/${encodeURIComponent(gameSelectedMatchId)}/offer-draw`, { method: "POST", body: {} });
    gameSelectedSquare = null;
    if (json?.match?.id) {
      gameState.matches = (gameState.matches || []).map((item) => item.id === json.match.id ? json.match : item);
      renderGameMatches(gameState.matches);
    }
    setGameMsg("已提出和棋，等待對方回覆。", true);
    await loadGameZone();
  } catch (err) {
    setGameMsg(err.message || "提和失敗", false);
  }
}

async function respondChessDraw(action) {
  if (!gameSelectedMatchId) {
    setGameMsg(`請先選擇要${action === "accept" ? "接受" : "拒絕"}和棋的棋局。`, false);
    return;
  }
  const match = (gameState.matches || []).find((item) => item.id === gameSelectedMatchId);
  if (!match) {
    setGameMsg("找不到目前棋局，請先刷新遊戲區。", false);
    return;
  }
  if (match.status !== "active") {
    setGameMsg("這局已經結束，不需要再回覆和棋。", false);
    return;
  }
  const canRespond = action === "accept" ? match.can_accept_draw_offer : match.can_reject_draw_offer;
  if (!canRespond) {
    setGameMsg("目前沒有待回覆的和棋。", false);
    return;
  }
  try {
    const json = await gameRequest(`/games/chess/matches/${encodeURIComponent(gameSelectedMatchId)}/respond-draw`, {
      method: "POST",
      body: { action },
    });
    gameSelectedSquare = null;
    if (json?.match?.id) {
      gameState.matches = (gameState.matches || []).map((item) => item.id === json.match.id ? json.match : item);
      renderGameMatches(gameState.matches);
    }
    setGameMsg(action === "accept" ? "已接受和棋，棋局結束。": "已拒絕和棋。", true);
    await loadGameZone();
  } catch (err) {
    setGameMsg(err.message || `${action === "accept" ? "接受" : "拒絕"}和棋失敗`, false);
  }
}

async function resignGame() {
  if (!gameSelectedMatchId) {
    setGameMsg("請先選擇要認輸的棋局。", false);
    return;
  }
  const match = (gameState.matches || []).find((item) => item.id === gameSelectedMatchId);
  if (!match) {
    setGameMsg("找不到目前棋局，請先刷新遊戲區。", false);
    return;
  }
  if (match.status !== "active") {
    setGameMsg("這局已經結束，不需要再認輸。", false);
    return;
  }
  const confirmed = await confirmChessAction("確認認輸", "認輸後這局會立即結束，不能復原。", "認輸", true);
  if (!confirmed) return;
  try {
    const json = await gameRequest(`/games/chess/matches/${encodeURIComponent(gameSelectedMatchId)}/resign`, { method: "POST", body: {} });
    gameSelectedSquare = null;
    if (json?.match?.id) {
      gameState.matches = (gameState.matches || []).map((item) => item.id === json.match.id ? json.match : item);
      renderGameMatches(gameState.matches);
    }
    setGameMsg("已認輸並結束棋局。", true);
    await loadGameZone();
  } catch (err) {
    setGameMsg(err.message || "認輸失敗", false);
  }
}

async function deleteFinishedGame(matchId) {
  const match = (gameState.matches || []).find((item) => String(item.id) === String(matchId));
  if (!match) {
    setGameMsg("找不到要刪除的棋局，請先刷新遊戲區。", false);
    return;
  }
  if (match.status === "active") {
    setGameMsg("進行中的棋局不能刪除，請先完成棋局或認輸。", false);
    return;
  }
  const confirmed = await confirmChessAction("刪除已結束棋局", "只會從你的棋局列表移除，排行榜紀錄不會被移除。", "刪除", true);
  if (!confirmed) return;
  try {
    await gameRequest(`/games/chess/matches/${encodeURIComponent(matchId)}`, { method: "DELETE", body: {} });
    if (String(gameSelectedMatchId) === String(matchId)) {
      gameSelectedMatchId = null;
      gameSelectedSquare = null;
    }
    await refreshGameZoneAfterMutation("已刪除已結束棋局");
  } catch (err) {
    setGameMsg(err.message || "刪除棋局失敗", false);
  }
}

async function awardGameRewards() {
  try {
    const json = await gameRequest("/root/games/chess/weekly-rewards/award", { method: "POST", body: {} });
    setGameMsg(`已發放 ${json.awarded?.length || 0} 筆週獎勵`, true);
    await loadGameZone();
  } catch (err) {
    setGameMsg(err.message || "週獎勵發放失敗", false);
  }
}

async function warmStartChessModels() {
  try {
    const json = await gameRequest("/root/games/chess/warm-start", { method: "POST", body: {} });
    setGameMsg(`warm start 完成，共檢查 ${json.artifacts?.length || 0} 個 artifact`, true);
    await loadChessRootDashboard();
  } catch (err) {
    setGameMsg(err.message || "warm start 失敗", false);
  }
}

async function stageChessCandidate() {
  try {
    const engine = $("game-root-chess-engine")?.value || "experiment 4:pv";
    const sourcePath = $("game-root-chess-candidate-path")?.value || "";
    const benchmarkPath = $("game-root-chess-benchmark-path")?.value || "";
    const json = await gameRequest("/root/games/chess/promotion/stage", {
      method: "POST",
      body: { engine, source_path: sourcePath, benchmark_report_path: benchmarkPath },
    });
    setGameMsg(`candidate staged: ${json.engine}`, true);
    await loadChessRootDashboard();
  } catch (err) {
    setGameMsg(err.message || "stage candidate 失敗", false);
  }
}

async function promoteChessCandidate() {
  try {
    const engine = $("game-root-chess-engine")?.value || "experiment 4:pv";
    const benchmarkPath = $("game-root-chess-benchmark-path")?.value || "";
    const json = await gameRequest("/root/games/chess/promotion/promote", {
      method: "POST",
      body: { engine, benchmark_report_path: benchmarkPath },
    });
    setGameMsg(`promotion 完成: ${json.engine}`, true);
    await loadChessRootDashboard();
  } catch (err) {
    setGameMsg(err.message || "promotion 失敗", false);
  }
}

(function () {
  window.registerHackmeGameViewModule?.({
    key: "chess",
    panelIds: ["chess-lobby-panel", "chess-game-panel"],
    leaderboardPath() {
      return "/games/chess/leaderboard";
    },
    ensure() {
      bindChessPracticeDifficultyUi();
      updateChessStockfishDepthControl();
    },
    dispatch(type, event) {
      const target = event.target;
      if (type === "click" && target?.closest?.("#game-chess-hint-btn")) {
        showChessHint();
        return true;
      }
      if (type === "click" && target?.closest?.("#game-chess-analysis-btn")) {
        const match = (gameState.matches || []).find((item) => item.id === gameSelectedMatchId);
        renderChessAnalysis(match, "賽後分析");
        setGameMsg(match ? "已更新棋局分析。" : "請先選擇棋局。", Boolean(match));
        return true;
      }
      if (type === "click" && target?.closest?.("#game-chess-challenge-btn")) {
        createChessEndgameChallenge();
        return true;
      }
      const clockControl = target?.closest?.("#game-clock-panel input, #game-clock-panel select");
      if (clockControl && (type === "change" || type === "input")) {
        applyChessClockConfig({ reset: true });
        const match = selectedChessMatchForClock();
        if (match) renderChessBoard(match);
        return true;
      }
      if (type === "change" && target?.closest?.("#game-practice-difficulty")) {
        updateChessStockfishDepthControl();
        return true;
      }
      if (type === "change" && target?.closest?.("#game-practice-stockfish-depth")) {
        selectedChessStockfishDepth();
        return true;
      }
      return false;
    },
  });
}());
