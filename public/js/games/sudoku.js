'use strict';

const SUDOKU_PUZZLES = [
  {
    puzzle: "530070000600195000098000060800060003400803001700020006060000280000419005000080079",
    solution: "534678912672195348198342567859761423426853791713924856961537284287419635345286179",
  },
  {
    puzzle: "003020600900305001001806400008102900700000008006708200002609500800203009005010300",
    solution: "483921657967345821251876493548132976729564138136798245372689514814253769695417382",
  },
  {
    puzzle: "000260701680070090190004500820100040004602900050003028009300074040050036703018000",
    solution: "435269781682571493197834562826195347374682915951743628519326874248957136763418259",
  },
];
const SUDOKU_HINT_LIMIT = 3;
const SUDOKU_HINT_PENALTY_SECONDS = 60;
let sudokuState = null;

function startSudokuGame() {
  const dailyChallenge = window.hackmeGameDailyChallenge?.("sudoku") || null;
  const rng = dailyChallenge?.seed ? window.createHackmeGameSeededRandom?.(dailyChallenge.seed) : Math.random;
  const puzzleIndex = Math.floor((rng?.() ?? Math.random()) * SUDOKU_PUZZLES.length);
  const item = SUDOKU_PUZZLES[puzzleIndex];
  sudokuState = {
    puzzleId: dailyChallenge?.key || `builtin-${puzzleIndex + 1}`,
    puzzle: item.puzzle,
    solution: item.solution,
    values: item.puzzle.split("").map((value) => value === "0" ? "" : value),
    fixed: item.puzzle.split("").map((value) => value !== "0"),
    notes: Array.from({ length: 81 }, () => ""),
    noteMode: false,
    assistMode: true,
    selectedIndex: null,
    mistakes: 0,
    mistakeLimit: 3,
    hintsUsed: 0,
    hintLimit: SUDOKU_HINT_LIMIT,
    hintPenaltySeconds: SUDOKU_HINT_PENALTY_SECONDS,
    startedAt: Date.now(),
    completedAt: null,
    failed: false,
    penaltySeconds: 0,
    scoreSubmitted: false,
    difficulty: dailyChallenge?.difficulty || "standard",
    dailyChallenge,
  };
  renderSudokuBoard();
  ensureSoloGameTimer();
  updateSudokuStatus(`${dailyChallenge?.label || "計時開始"}。填入 1-9，完成後檢查答案。`);
}

function renderSudokuBoard() {
  const board = $("sudoku-board");
  if (!board) return;
  if (!sudokuState) {
    board.innerHTML = '<div class="single-game-placeholder">按「開始」後才會出現題目並開始計時。</div>';
    updateSudokuNoteButton();
    updateSudokuAssistButton();
    updateSudokuHintButton();
    renderSudokuCandidatePanel();
    return;
  }
  board.innerHTML = sudokuState.values.map((value, index) => {
    const fixed = sudokuState.fixed[index];
    const selected = Number(sudokuState.selectedIndex) === index;
    const row = Math.floor(index / 9);
    const col = index % 9;
    return `
      <input class="sudoku-cell ${fixed ? "fixed" : ""} ${selected ? "selected" : ""} ${sudokuState.notes[index] ? "has-notes" : ""}" inputmode="numeric" maxlength="1"
             aria-label="數獨第 ${row + 1} 列第 ${col + 1} 欄"
             title="${sanitize(sudokuState.notes[index] ? `筆記 ${sudokuState.notes[index]}` : "")}"
             placeholder="${sanitize(sudokuState.notes[index] || "")}"
             data-sudoku-index="${index}" value="${sanitize(value || "")}" ${fixed || sudokuState.completedAt || sudokuState.failed ? "readonly" : ""} />
    `;
  }).join("");
  updateSudokuNoteButton();
  updateSudokuAssistButton();
  updateSudokuHintButton();
  renderSudokuCandidatePanel();
}

function updateSudokuCell(index, value) {
  if (!sudokuState || sudokuState.fixed[index] || sudokuState.completedAt || sudokuState.failed) return;
  const normalized = /^[1-9]$/.test(value || "") ? value : "";
  if (sudokuState.noteMode && normalized) {
    const notes = new Set(String(sudokuState.notes[index] || "").split("").filter(Boolean));
    if (notes.has(normalized)) notes.delete(normalized);
    else notes.add(normalized);
    sudokuState.notes[index] = [...notes].sort().join("");
    const input = document.querySelector(`[data-sudoku-index="${index}"]`);
    if (input) input.value = sudokuState.values[index] || "";
    renderSudokuBoard();
    updateSudokuStatus("已更新筆記。");
    return;
  }
  sudokuState.values[index] = normalized;
  if (normalized) sudokuState.notes[index] = "";
  const input = document.querySelector(`[data-sudoku-index="${index}"]`);
  if (input && input.value !== normalized) input.value = normalized;
  renderSudokuCandidatePanel();
}

function updateSudokuNoteButton() {
  const btn = $("sudoku-note-btn");
  if (!btn) return;
  const enabled = Boolean(sudokuState?.noteMode);
  btn.classList.toggle("btn-primary", enabled);
  btn.setAttribute("aria-pressed", enabled ? "true" : "false");
  btn.textContent = enabled ? "筆記中" : "筆記";
}

function updateSudokuAssistButton() {
  const btn = $("sudoku-assist-btn");
  if (!btn) return;
  const enabled = sudokuState?.assistMode !== false;
  btn.classList.toggle("btn-primary", enabled);
  btn.setAttribute("aria-pressed", enabled ? "true" : "false");
  btn.textContent = enabled ? "輔導開" : "輔導關";
}

function updateSudokuHintButton() {
  const btn = $("sudoku-hint-btn");
  if (!btn) return;
  const remaining = Math.max(0, Number(sudokuState?.hintLimit ?? SUDOKU_HINT_LIMIT) - Number(sudokuState?.hintsUsed || 0));
  const active = Boolean(sudokuState && !sudokuState.completedAt && !sudokuState.failed);
  btn.disabled = !active || remaining <= 0;
  btn.textContent = `提示 ${remaining}`;
  btn.title = active
    ? `每次提示會填入 1 格，並加時 ${SUDOKU_HINT_PENALTY_SECONDS} 秒。`
    : "開始新局後可使用提示。";
}

function updateSudokuStatus(prefix = "") {
  const status = $("sudoku-status");
  if (!status) return;
  if (!sudokuState) {
    status.textContent = "按開始後才會出現題目並開始計時。";
    return;
  }
  const time = formatSoloGameTime(soloElapsedMs(sudokuState));
  const penalty = Number(sudokuState.penaltySeconds || 0);
  const hintsRemaining = Math.max(0, Number(sudokuState.hintLimit || 0) - Number(sudokuState.hintsUsed || 0));
  if (sudokuState.completedAt) {
    status.textContent = `完成時間 ${time}${penalty ? `（含加時 ${penalty} 秒）` : ""} · 提示 ${sudokuState.hintsUsed}/${sudokuState.hintLimit}`;
    return;
  }
  if (sudokuState.failed) {
    status.textContent = `錯誤達上限，本局結束。時間 ${time}`;
    return;
  }
  status.textContent = `${prefix ? `${prefix} ` : ""}目前時間 ${time}${penalty ? ` · 加時 ${penalty} 秒` : ""} · 錯誤 ${sudokuState.mistakes}/${sudokuState.mistakeLimit} · 提示剩 ${hintsRemaining}${sudokuState.noteMode ? " · 筆記模式" : ""}`;
}

function sudokuCandidateNumbers(index) {
  if (!sudokuState || sudokuState.fixed[index] || sudokuState.values[index]) return [];
  const row = Math.floor(index / 9);
  const col = index % 9;
  const used = new Set();
  for (let i = 0; i < 9; i += 1) {
    if (sudokuState.values[row * 9 + i]) used.add(sudokuState.values[row * 9 + i]);
    if (sudokuState.values[i * 9 + col]) used.add(sudokuState.values[i * 9 + col]);
  }
  const boxRow = Math.floor(row / 3) * 3;
  const boxCol = Math.floor(col / 3) * 3;
  for (let r = boxRow; r < boxRow + 3; r += 1) {
    for (let c = boxCol; c < boxCol + 3; c += 1) {
      const value = sudokuState.values[r * 9 + c];
      if (value) used.add(value);
    }
  }
  return "123456789".split("").filter((digit) => !used.has(digit));
}

function showSudokuCandidates(index) {
  if (!sudokuState || sudokuState.completedAt || sudokuState.failed) return;
  sudokuState.selectedIndex = Number(index);
  renderSudokuBoard();
}

function renderSudokuCandidatePanel(message = "") {
  const panel = $("sudoku-candidate-panel");
  if (!panel) return;
  if (!sudokuState || sudokuState.selectedIndex === null || sudokuState.completedAt || sudokuState.failed) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  const index = Number(sudokuState.selectedIndex);
  if (!Number.isInteger(index) || index < 0 || index >= 81 || sudokuState.fixed[index] || sudokuState.values[index]) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  const row = Math.floor(index / 9) + 1;
  const col = (index % 9) + 1;
  const assistEnabled = sudokuState.assistMode !== false;
  const candidates = assistEnabled ? sudokuCandidateNumbers(index) : "123456789".split("");
  panel.hidden = false;
  panel.innerHTML = `
    <div class="sudoku-candidate-title">${assistEnabled ? `第 ${row} 列第 ${col} 欄可填數字` : `第 ${row} 列第 ${col} 欄數字鍵盤`}</div>
    <div class="sudoku-candidate-list">
      ${candidates.length
        ? candidates.map((digit) => `<button class="btn game-mini-btn" type="button" data-sudoku-candidate="${digit}">${digit}</button>`).join("")
        : '<span class="drive-card-sub">目前沒有合法候選，請先檢查同列、同欄或同宮格是否有衝突。</span>'}
    </div>
    ${message ? `<div class="drive-card-sub">${sanitize(message)}</div>` : ""}
  `;
}

function checkSudokuGame() {
  const status = $("sudoku-status");
  if (!sudokuState) return;
  const current = sudokuState.values.join("");
  let wrongCount = 0;
  document.querySelectorAll("[data-sudoku-index]").forEach((input) => {
    const index = Number(input.getAttribute("data-sudoku-index") || 0);
    const wrong = !!sudokuState.values[index] && sudokuState.values[index] !== sudokuState.solution[index];
    if (wrong) wrongCount += 1;
    input.classList.toggle("wrong", wrong);
  });
  if (wrongCount > 0) {
    sudokuState.mistakes += 1;
    sudokuState.penaltySeconds += 10;
    if (sudokuState.mistakes >= sudokuState.mistakeLimit) {
      sudokuState.failed = true;
      sudokuState.completedAt = Date.now();
      renderSudokuBoard();
      updateSudokuHintButton();
      updateSudokuStatus();
      stopSoloGameTimerIfIdle();
      setGameMsg("數獨錯誤達上限，本局不列入排行榜。", false);
      return;
    }
    updateSudokuStatus(`發現 ${wrongCount} 格錯誤，已加時 10 秒。`);
    return;
  }
  if (sudokuState.values.some((value) => !value)) {
    updateSudokuStatus("尚未填完，目前填入的格子沒有錯。");
    return;
  }
  if (current === sudokuState.solution) {
    sudokuState.completedAt = Date.now();
    renderSudokuBoard();
    updateSudokuHintButton();
    updateSudokuStatus();
    stopSoloGameTimerIfIdle();
    if (sudokuState.noteMode || sudokuState.notes.some(Boolean)) {
      window.recordHackmeGameAchievement?.("sudoku", "note-master", "筆記整理", "使用筆記模式完成一題。");
    }
    if (!sudokuState.mistakes && !sudokuState.hintsUsed) {
      window.recordHackmeGameAchievement?.("sudoku", "no-mistake", "零錯誤完成", "不使用提示且不觸發錯誤檢查完成數獨。");
    }
    window.recordHackmeGameAchievement?.("sudoku", "first-solve", "數獨初解", "完成一題數獨。");
    submitSoloGameScore("sudoku", sudokuState);
    setGameMsg(`數獨完成，成績 ${formatSoloGameTime(soloElapsedMs(sudokuState))}`, true);
  } else if (status) {
    status.textContent = "還有錯誤，請檢查紅色格子。";
  }
}

function toggleSudokuNotes() {
  if (!sudokuState) startSudokuGame();
  if (!sudokuState || sudokuState.completedAt || sudokuState.failed) return;
  sudokuState.noteMode = !sudokuState.noteMode;
  updateSudokuNoteButton();
  updateSudokuStatus();
}

function toggleSudokuAssist() {
  if (!sudokuState) startSudokuGame();
  if (!sudokuState) return;
  sudokuState.assistMode = sudokuState.assistMode === false;
  updateSudokuAssistButton();
  renderSudokuCandidatePanel(sudokuState.assistMode ? "點按空白格會顯示目前可用數字。" : "");
  updateSudokuStatus(sudokuState.assistMode ? "輔導模式已開啟。" : "輔導模式已關閉。");
}

function chooseSudokuCandidate(value) {
  if (!sudokuState || sudokuState.selectedIndex === null) return;
  const index = Number(sudokuState.selectedIndex);
  if (sudokuState.assistMode !== false && !sudokuCandidateNumbers(index).includes(String(value || ""))) {
    renderSudokuCandidatePanel("這個數字目前不符合列、欄、宮格限制。");
    return;
  }
  updateSudokuCell(index, String(value || ""));
  renderSudokuBoard();
  updateSudokuStatus(`已填入 ${value}。`);
}

function fillSudokuHint() {
  if (!sudokuState || sudokuState.completedAt || sudokuState.failed) return;
  const limit = Number(sudokuState.hintLimit || SUDOKU_HINT_LIMIT);
  if (Number(sudokuState.hintsUsed || 0) >= limit) {
    updateSudokuHintButton();
    updateSudokuStatus("提示已用完，本局請自行完成。");
    return;
  }
  const empty = sudokuState.values.map((value, index) => value ? null : index).filter((index) => index !== null && !sudokuState.fixed[index]);
  if (!empty.length) return;
  const index = empty[0];
  sudokuState.values[index] = sudokuState.solution[index];
  sudokuState.notes[index] = "";
  sudokuState.hintsUsed += 1;
  sudokuState.penaltySeconds += Number(sudokuState.hintPenaltySeconds || SUDOKU_HINT_PENALTY_SECONDS);
  renderSudokuBoard();
  updateSudokuStatus(`提示已填入 1 格，代價加時 ${SUDOKU_HINT_PENALTY_SECONDS} 秒。`);
}

(function () {
  window.registerHackmeGameViewModule?.({
    key: "sudoku",
    panelIds: ["sudoku-game-panel"],
    ensure() {
      if (!sudokuState) {
        renderSudokuBoard();
        updateSudokuStatus();
      }
    },
    updateStatus() {
      updateSudokuStatus();
    },
    isActive() {
      return !!sudokuState && !sudokuState.completedAt;
    },
    leaderboardPath() {
      return "/games/sudoku/solo-leaderboard";
    },
    dispatch(type, event) {
      if (type === "click" && event.target?.closest?.("#sudoku-new-btn")) {
        startSudokuGame();
        return true;
      }
      if (type === "click" && event.target?.closest?.("#sudoku-check-btn")) {
        checkSudokuGame();
        return true;
      }
      if (type === "click" && event.target?.closest?.("#sudoku-note-btn")) {
        toggleSudokuNotes();
        return true;
      }
      if (type === "click" && event.target?.closest?.("#sudoku-assist-btn")) {
        toggleSudokuAssist();
        return true;
      }
      if (type === "click" && event.target?.closest?.("#sudoku-hint-btn")) {
        fillSudokuHint();
        return true;
      }
      const candidateBtn = type === "click" ? event.target?.closest?.("[data-sudoku-candidate]") : null;
      if (candidateBtn) {
        chooseSudokuCandidate(candidateBtn.dataset.sudokuCandidate || "");
        return true;
      }
      const clickedSudokuInput = type === "click" ? event.target?.closest?.("[data-sudoku-index]") : null;
      if (clickedSudokuInput) {
        showSudokuCandidates(Number(clickedSudokuInput.dataset.sudokuIndex || 0));
        return true;
      }
      const sudokuInput = type === "input" ? event.target?.closest?.("[data-sudoku-index]") : null;
      if (sudokuInput) {
        updateSudokuCell(Number(sudokuInput.dataset.sudokuIndex || 0), sudokuInput.value || "");
        return true;
      }
      return false;
    },
  });
}());
