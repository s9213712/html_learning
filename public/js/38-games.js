'use strict';

let gameSelectedMatchId = null;
let gameSelectedSquare = null;
let gameSelectedKey = gameInitialSelectedKey();
let chessMoveInFlight = false;
let gameState = { matches: [], invites: [], leaderboard: [], users: [], multiplayer: { rooms: [], invites: [], modes: [] } };
let soloGameTimer = null;
let localGameModuleCleanup = null;
let localGameModuleActiveApi = null;
let localGameModuleMountedKey = "";
let localGameModuleTouchStart = null;
let localGameModulePressedControl = null;
let localGameModuleSwipeMode = "tap";
let gameMultiplayerInvitePollTimer = null;
let gameMultiplayerInviteKickoffTimer = null;
let gameMultiplayerInviteModalInvite = null;
let gameMultiplayerInviteActionBusy = false;
let gameMultiplayerInviteSeenUserId = null;
const gameMultiplayerInviteSeenIds = new Set();
const GAME_INVITE_POLL_ACTIVE_MS = 5000;
const GAME_INVITE_POLL_VISIBLE_IDLE_MS = 60000;
const GAME_INVITE_POLL_HIDDEN_MS = 180000;

function gameInvitePollMs(kind) {
  if (typeof configRefreshSeconds !== "function") {
    if (kind === "hidden") return GAME_INVITE_POLL_HIDDEN_MS;
    if (kind === "idle") return GAME_INVITE_POLL_VISIBLE_IDLE_MS;
    return GAME_INVITE_POLL_ACTIVE_MS;
  }
  if (kind === "hidden") return Math.round(configRefreshSeconds("game_invite_poll_hidden_seconds", GAME_INVITE_POLL_HIDDEN_MS / 1000, 30, 1800) * 1000);
  if (kind === "idle") return Math.round(configRefreshSeconds("game_invite_poll_idle_seconds", GAME_INVITE_POLL_VISIBLE_IDLE_MS / 1000, 10, 600) * 1000);
  return Math.round(configRefreshSeconds("game_invite_poll_active_seconds", GAME_INVITE_POLL_ACTIVE_MS / 1000, 2, 300) * 1000);
}

const GAME_MULTIPLAYER_MODES = {
  fps_arena: [
    { key: "coop", label: "合作破關" },
    { key: "pvp", label: "PvP 對戰" },
  ],
  stickman_shooter: [
    { key: "coop", label: "合作破關" },
  ],
};
let gameMultiplayerSelectedRoomByKey = {};
const GAME_KENNEY_ASSET_BASE = "/assets/games/vendor/kenney";
const GAME_SOUND_ASSETS = Object.freeze({
  uiClick: `${GAME_KENNEY_ASSET_BASE}/interface-sounds/audio/click_001.ogg`,
  uiSelect: `${GAME_KENNEY_ASSET_BASE}/interface-sounds/audio/select_001.ogg`,
  uiSuccess: `${GAME_KENNEY_ASSET_BASE}/interface-sounds/audio/confirmation_001.ogg`,
  uiError: `${GAME_KENNEY_ASSET_BASE}/interface-sounds/audio/error_001.ogg`,
  uiSwitch: `${GAME_KENNEY_ASSET_BASE}/interface-sounds/audio/switch_001.ogg`,
  uiTick: `${GAME_KENNEY_ASSET_BASE}/interface-sounds/audio/tick_001.ogg`,
  uiDrop: `${GAME_KENNEY_ASSET_BASE}/interface-sounds/audio/drop_001.ogg`,
  uiOpen: `${GAME_KENNEY_ASSET_BASE}/interface-sounds/audio/open_001.ogg`,
  uiClose: `${GAME_KENNEY_ASSET_BASE}/interface-sounds/audio/close_001.ogg`,
  hit: `${GAME_KENNEY_ASSET_BASE}/impact-sounds/audio/impactGeneric_light_000.ogg`,
  metalHit: `${GAME_KENNEY_ASSET_BASE}/impact-sounds/audio/impactMetal_light_000.ogg`,
  punch: `${GAME_KENNEY_ASSET_BASE}/impact-sounds/audio/impactPunch_medium_000.ogg`,
  woodHit: `${GAME_KENNEY_ASSET_BASE}/impact-sounds/audio/impactWood_light_000.ogg`,
  glassHit: `${GAME_KENNEY_ASSET_BASE}/impact-sounds/audio/impactGlass_light_000.ogg`,
  footstep: `${GAME_KENNEY_ASSET_BASE}/impact-sounds/audio/footstep_concrete_000.ogg`,
});
const gameAudioCache = new Map();
const gameAudioLastPlayedAt = new Map();
const GAME_RUNTIME_SCRIPT_SRCS = Object.freeze([
  "/js/games/snake.js?v=20260513-game-modules",
  "/js/games/game-2048.js?v=20260513-game-modules",
  "/js/games/brick-breaker.js?v=20260513-game-modules",
  "/js/games/rubiks-cube.js?v=20260601-rubiks-mobile-touch",
  "/js/games/bullet-hell.js?v=20260518-game-ux",
  "/js/games/stickman-shooter.js?v=20260517-level-layouts",
  "/js/games/open-world.js?v=20260518-game-ux",
  "/js/games/racing.js?v=20260520-racing-physics",
  "/js/games/board-game-shared.js?v=20260513-game-modules",
  "/js/games/reversi.js?v=20260513-game-modules",
  "/js/games/go.js?v=20260513-game-modules",
  "/js/games/gomoku.js?v=20260513-game-modules",
  "/js/games/chinese-chess.js?v=20260513-game-modules",
  "/js/games/real-tetris.js?v=20260518-game-ux",
  "/js/games/sudoku.js?v=20260518-game-ux",
  "/js/games/minesweeper.js?v=20260513-legacy-modules",
  "/js/games/onea2b.js?v=20260518-game-ux",
  "/js/games/tetris.js?v=20260513-legacy-modules",
  "/js/games/space-shooter.js?v=20260518-game-ux",
  "/js/38-fps-arena.js?v=20260518-game-ux",
  "/js/games/fps-arena.js?v=20260514-fps-stance-br",
]);
let gameRuntimeScriptsLoaded = false;
let gameRuntimeScriptsPromise = null;

async function ensureGameRuntimeScriptsLoaded() {
  if (gameRuntimeScriptsLoaded) return true;
  if (gameRuntimeScriptsPromise) return gameRuntimeScriptsPromise;
  if (typeof loadHackmeScriptOnce !== "function") {
    throw new Error("遊戲模組載入器尚未初始化");
  }
  gameRuntimeScriptsPromise = (async () => {
    for (const src of GAME_RUNTIME_SCRIPT_SRCS) {
      await loadHackmeScriptOnce(src);
    }
    gameRuntimeScriptsLoaded = true;
    document.dispatchEvent(new CustomEvent("hackme:game-runtime-loaded"));
    return true;
  })().catch((err) => {
    gameRuntimeScriptsPromise = null;
    throw err;
  });
  return gameRuntimeScriptsPromise;
}

function localGameCatalogKeys() {
  const catalog = Array.isArray(window.HACKME_GAME_CATALOG) ? window.HACKME_GAME_CATALOG : [];
  return new Set(catalog.filter((game) => !game.legacy).map((game) => game.key));
}

function isLocalGameCatalogKey(key) {
  return localGameCatalogKeys().has(key);
}

function isLocalGameModuleAvailable(key) {
  return Boolean(window.HACKME_GAME_MODULES?.[key]?.mount);
}

function filterAvailableGameCatalog(games) {
  const rows = Array.isArray(games) ? games : [];
  return rows.filter((game) => !isLocalGameCatalogKey(game.key) || isLocalGameModuleAvailable(game.key));
}

function gameUrlParams() {
  try {
    return new URLSearchParams(window.location.search || "");
  } catch (err) {
    return new URLSearchParams("");
  }
}

function gameInitialSelectedKey() {
  return String(gameUrlParams().get("game") || "chess").trim() || "chess";
}

function gameViewModules() {
  return window.HACKME_GAME_VIEW_MODULES || {};
}

function activeGameViewModule() {
  return gameViewModules()[gameSelectedKey] || null;
}

function legacyGameRuntime() {
  return {
    setGameMsg,
    sound(name, options = {}) { playGameSound(name, options); },
    gameRequest,
    loadSelectedGameLeaderboard,
    submitSoloGameScore,
    ensureSoloGameTimer,
    stopSoloGameTimerIfIdle,
    formatSoloGameTime,
    soloRawElapsedMs,
    soloElapsedMs,
    dailyChallenge(gameKey = gameSelectedKey) {
      return window.hackmeGameDailyChallenge?.(gameKey) || null;
    },
    dailyMissions(gameKey = gameSelectedKey) {
      return window.listHackmeGameDailyMissions?.(gameKey) || [];
    },
    achievement(gameKey, id, label, detail = "") {
      const result = window.recordHackmeGameAchievement?.(gameKey, id, label, detail);
      if (result?.unlocked) setGameMsg(`成就解鎖：${result.label}`, true);
      renderGameMetaPanels();
      return result || { unlocked: false };
    },
    mission(gameKey, id, progress, target, label = "") {
      const result = window.recordHackmeGameMissionProgress?.(gameKey, id, progress, target, label);
      renderGameMetaPanels();
      return result || null;
    },
    replay(gameKey, payload) {
      const result = window.recordHackmeGameReplay?.(gameKey, payload);
      renderGameMetaPanels();
      return result;
    },
    renderGameMetaPanels,
  };
}

function dispatchActiveGameViewEvent(type, event) {
  const module = activeGameViewModule();
  if (!module?.dispatch) return false;
  return Boolean(module.dispatch(type, event, legacyGameRuntime()));
}

function formatSoloGameTime(ms) {
  const totalMs = Math.max(0, Number(ms || 0));
  const totalSeconds = Math.floor(totalMs / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  const tenths = Math.floor((totalMs % 1000) / 100);
  return `${minutes}:${String(seconds).padStart(2, "0")}.${tenths}`;
}

function soloRawElapsedMs(state) {
  if (!state?.startedAt) return 0;
  const end = state.completedAt || Date.now();
  return Math.max(1, Math.floor(end - state.startedAt));
}

function soloElapsedMs(state) {
  return soloRawElapsedMs(state) + Number(state?.penaltySeconds || 0) * 1000;
}

function ensureSoloGameTimer() {
  if (soloGameTimer) return;
  soloGameTimer = setInterval(() => {
    const module = activeGameViewModule();
    if (module?.updateStatus) module.updateStatus(legacyGameRuntime());
  }, 250);
}

function stopSoloGameTimerIfIdle() {
  const anyActive = Object.values(gameViewModules()).some((module) => (
    typeof module?.isActive === "function" && module.isActive()
  ));
  if (!anyActive && soloGameTimer) {
    clearInterval(soloGameTimer);
    soloGameTimer = null;
  }
}

function setGameMsg(text, ok) {
  const el = $("game-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = text ? "msg show " + (ok ? "ok" : "err") : "msg";
  if (text) playGameSound(ok ? "uiSuccess" : "uiError", { volume: ok ? 0.16 : 0.2, throttleMs: 350 });
}

function gameSoundsEnabled() {
  try {
    return window.localStorage?.getItem("hackme_game_sounds") !== "off";
  } catch (err) {
    return true;
  }
}

function playGameSound(name, options = {}) {
  const src = GAME_SOUND_ASSETS[name] || GAME_SOUND_ASSETS.uiClick;
  if (!src || typeof Audio === "undefined" || !gameSoundsEnabled()) return;
  const now = Date.now();
  const throttleMs = Number(options.throttleMs ?? 80);
  if (now - Number(gameAudioLastPlayedAt.get(src) || 0) < throttleMs) return;
  gameAudioLastPlayedAt.set(src, now);
  try {
    const pool = gameAudioCache.get(src) || [];
    let audio = pool.find((item) => item.paused || item.ended);
    if (!audio) {
      audio = new Audio(src);
      audio.preload = "auto";
      pool.push(audio);
      gameAudioCache.set(src, pool.slice(-4));
    }
    audio.volume = Math.max(0, Math.min(1, Number(options.volume ?? 0.18)));
    audio.currentTime = 0;
    audio.play?.().catch(() => {});
  } catch (err) {
    // Browsers may block audio until a user gesture; keep gameplay silent instead of failing.
  }
}

function gameIcon(key) {
  if (key === "snake") return "S";
  if (key === "game_2048") return "2";
  if (key === "brick_breaker") return "▤";
  if (key === "rubiks_cube") return "◇";
  if (key === "reversi") return "●";
  if (key === "go") return "○";
  if (key === "gomoku") return "五";
  if (key === "chinese_chess") return "象";
  if (key === "sudoku") return "9";
  if (key === "minesweeper") return "!";
  if (key === "1a2b") return "A";
  if (key === "tetris") return "▦";
  if (key === "real_tetris") return "▧";
  if (key === "space_shooter") return "▲";
  if (key === "fps_arena") return "◎";
  if (key === "open_world") return "市";
  if (key === "racing") return "R";
  if (key === "bullet_hell") return "✦";
  if (key === "stickman_shooter") return "人";
  return "♟";
}

function gameSubtitle(game) {
  if (game.key === "snake") return "手機滑動 / 鍵盤皆可玩";
  if (game.key === "game_2048") return "手機滑動合併數字";
  if (game.key === "brick_breaker") return "手機按鍵控制擋板";
  if (game.key === "rubiks_cube") return "3D 視角拖曳 / 六面轉動解題";
  if (game.key === "reversi") return "線上棋盤黑白棋 / AI 練習";
  if (game.key === "go") return "19 路線上棋盤圍棋 / 目數結算";
  if (game.key === "gomoku") return "15 路線上棋盤五子棋 / AI 練習";
  if (game.key === "chinese_chess") return "9x10 線上棋盤中國象棋 / AI 練習";
  if (game.key === "sudoku") return "單人邏輯解題";
  if (game.key === "minesweeper") return "單人推理挑戰";
  if (game.key === "1a2b") return "單人猜數字";
  if (game.key === "tetris") return "高分消除挑戰";
  if (game.key === "real_tetris") return "剛體物理與 99% 消線";
  if (game.key === "space_shooter") return "高分射擊挑戰";
  if (game.key === "fps_arena") return "3D 射擊訓練 / 合作 / PvP";
  if (game.key === "open_world") return "3D 城市探索 / 駕車任務 / 警戒追逐";
  if (game.key === "racing") return "道具干擾、甩尾與氮氣衝刺";
  if (game.key === "bullet_hell") return "閃避密集彈幕並反擊";
  if (game.key === "stickman_shooter") return "2D 側捲平台射擊 / 合作解謎";
  return game.supports_computer ? "玩家對戰 / 電腦練習" : "玩家對戰";
}

function gameSupportsMultiplayer(key = gameSelectedKey) {
  return Array.isArray(GAME_MULTIPLAYER_MODES[key]) && GAME_MULTIPLAYER_MODES[key].length > 0;
}

function gameMultiplayerModeLabel(mode) {
  if (mode === "pvp") return "PvP 對戰";
  return "合作破關";
}

function gameMultiplayerActiveRoom(key = gameSelectedKey, mode = "") {
  if (!gameSupportsMultiplayer(key)) return null;
  const rooms = Array.isArray(gameState.multiplayer?.rooms) ? gameState.multiplayer.rooms : [];
  const selectedId = Number(gameMultiplayerSelectedRoomByKey[key] || 0);
  const selected = selectedId ? rooms.find((room) => Number(room.id) === selectedId) : null;
  const room = selected || rooms.find((item) => item.status === "active") || rooms.find((item) => item.status === "lobby") || null;
  if (!room) return null;
  if (mode && room.mode !== mode) return null;
  if (!room.guest_user_id) return null;
  return room;
}

function gameMultiplayerPeerId(room) {
  if (!room) return null;
  const me = Number(currentUserId || 0);
  if (Number(room.host_user_id) === me) return Number(room.guest_user_id || 0) || null;
  if (Number(room.guest_user_id) === me) return Number(room.host_user_id || 0) || null;
  return null;
}

function gameMultiplayerPeerState(snapshot, room) {
  const peerId = gameMultiplayerPeerId(room || snapshot?.room);
  const players = Array.isArray(snapshot?.players) ? snapshot.players : [];
  return players.find((player) => Number(player.user_id) === Number(peerId)) || null;
}

function gameExperienceHint(key) {
  const hints = {
    chess: "西洋棋：先按「和電腦練習」或選擇棋局；點棋子後可點亮起的合法目標格，升變會用視窗選擇。",
    sudoku: "數獨：先按開始。手機可直接點格輸入；筆記模式用來暫存候選數，提示會加時。",
    minesweeper: "踩地雷：首次翻格安全。手機請用「插旗模式」在翻格與插旗之間切換。",
    "1a2b": "1A2B：輸入 4 位不重複數字；A 是數字位置都對，B 是數字對但位置不同。",
    tetris: "俄羅斯方塊：方向鍵移動/旋轉，空白鍵落下，C Hold；手機下方按鍵可長按左右與加速。",
    space_shooter: "宇宙戰機：左右移動、空白鍵或發射鍵射擊；敵方閃避預設關閉，想提高難度再打開。",
    fps_arena: "3D 射擊場：WASD 移動、滑鼠/拖曳瞄準，按住右鍵進入狙擊模式；手機按住「前/左/右」會連續移動，不是一步一步點。",
    open_world: "都市開放世界：WASD 或手機搖桿移動，靠近車輛可上車；路面目標光柱是任務方向。",
    racing: "街頭賽車：按住加速與左右換線，煞車+方向可甩尾，空白鍵氮氣、E 或道具鍵可加速自己或干擾對手。",
    bullet_hell: "彈幕遊戲：黃點是受擊核心，藍圈是擦彈範圍；按住「精密」會變慢並縮小受擊範圍。",
    stickman_shooter: "火柴人橫向射擊：按住移動鍵走位，射擊與跳躍要配合掩體；合作模式需要雙方配合機關。",
    real_tetris: "真實版俄羅斯方塊：方塊會旋轉、傾斜與倒塌，目標是讓行覆蓋率達 90% 才消線。",
    snake: "貪食蛇：方向鍵或滑動轉向，手機請滑動棋盤；避免回頭撞到自己。",
    game_2048: "2048：方向鍵或滑動合併，同數字相撞會升級；可用撤銷修正最近幾步。",
    brick_breaker: "打磚塊：按住左右移動擋板，球碰擋板位置會改變反彈角度。",
    rubiks_cube: "3D 魔術方塊：拖曳旋轉視角，點選面與方向轉動方塊，目標是把六面復原。",
    reversi: "黑白棋：落子必須夾住對手棋，合法位置會提示；結算看黑白棋數。",
    go: "圍棋：19 路棋盤，輪流落子與停一手；目前採教育級目數估算，不是職業級死活判定。",
    gomoku: "五子棋：先連成五顆獲勝；可切換 AI 難度與禁手規則。",
    chinese_chess: "中國象棋：點選棋子後再點目標；將帥、馬腿、象眼等規則會由前端限制。",
  };
  return hints[key] || "選擇遊戲後會顯示操作提示。";
}

function updateGameExperienceHint(key = gameSelectedKey) {
  const el = $("game-experience-hint");
  if (!el) return;
  el.textContent = gameExperienceHint(key);
  el.dataset.gameHint = key || "";
}

function switchGameView(key) {
  setGameMsg("", true);
  const previousModule = activeGameViewModule();
  const previousKey = gameSelectedKey;
  if (previousKey && previousKey !== key && typeof previousModule?.suspend === "function") {
    try {
      previousModule.suspend(legacyGameRuntime());
    } catch (err) {
      console.warn("game suspend failed", previousKey, err);
    }
  }
  gameSelectedKey = key || "chess";
  updateGameExperienceHint(gameSelectedKey);
  const select = $("game-select");
  if (select && select.value !== gameSelectedKey) select.value = gameSelectedKey;
  const isLocalGameModule = isLocalGameModuleAvailable(gameSelectedKey);
  const selectedViewModule = activeGameViewModule();
  Object.values(gameViewModules()).forEach((module) => {
    (module.panelIds || []).forEach((id) => {
      const panel = $(id);
      if (panel) panel.style.display = module.key === gameSelectedKey ? "" : "none";
    });
  });
  const localGameModulePanel = $("local-module-game-panel");
  if (localGameModulePanel) localGameModulePanel.style.display = isLocalGameModule ? "" : "none";
  document.querySelectorAll("[data-game-key]").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("data-game-key") === gameSelectedKey);
  });
  if (isLocalGameModule) {
    mountLocalGameModule(gameSelectedKey);
  } else {
    cleanupLocalGameModule();
  }
  if (!isLocalGameModule && selectedViewModule?.ensure) selectedViewModule.ensure(legacyGameRuntime());
  updateGameFullscreenButtons();
  renderGameMultiplayerPanel();
  loadSelectedGameMultiplayer().catch((err) => {
    if (gameSupportsMultiplayer(gameSelectedKey)) setGameMsg(err.message || "多人房間讀取失敗", false);
  });
  loadSelectedGameLeaderboard().catch((err) => setGameMsg(err.message || "排行榜讀取失敗", false));
}

function gameFullscreenTarget() {
  return document.querySelector("#module-games .game-board-panel");
}

function updateGameFullscreenButtons() {
  const target = gameFullscreenTarget();
  const active = Boolean(target && document.fullscreenElement === target);
  document.querySelectorAll("#game-fullscreen-btn, #fps-arena-fullscreen-btn").forEach((button) => {
    button.textContent = active ? "離開全螢幕" : (button.id === "fps-arena-fullscreen-btn" ? "全螢幕" : "全螢幕遊戲");
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

async function toggleGameFullscreen() {
  const target = gameFullscreenTarget();
  if (!target) return;
  try {
    if (document.fullscreenElement === target) {
      await document.exitFullscreen?.();
    } else {
      await target.requestFullscreen?.();
    }
    updateGameFullscreenButtons();
  } catch (err) {
    setGameMsg("此瀏覽器無法切換全螢幕", false);
  }
}

function gameRequestNeedsFreshCsrf(json, res) {
  return res.status === 403 && String(json?.msg || "").toUpperCase().includes("CSRF");
}

async function gameRequest(path, { method = "GET", body = null } = {}) {
  const upperMethod = String(method || "GET").toUpperCase();
  const mutates = upperMethod !== "GET";
  const buildOptions = async () => {
    const csrf = await fetchCsrfToken({ force: mutates });
    const headers = { "X-CSRF-Token": csrf || "" };
    if (mutates) headers["Content-Type"] = "application/json";
    return {
      method: upperMethod,
      credentials: "same-origin",
      cache: "no-store",
      headers,
      body: body ? JSON.stringify(body) : undefined,
    };
  };

  let res = await apiFetch(API + path, await buildOptions());
  let json = await res.json().catch(() => ({}));
  if (gameRequestNeedsFreshCsrf(json, res)) {
    await fetchCsrfToken({ force: true });
    res = await apiFetch(API + path, await buildOptions());
    json = await res.json().catch(() => ({}));
  }
  if (!res.ok || !json.ok) {
    throw new Error(json.msg || `HTTP ${res.status}`);
  }
  if (mutates && typeof _csrfToken !== "undefined") {
    setCsrfToken(null);
  }
  return json;
}
window.hackmeGameRequest = gameRequest;

function cleanupLocalGameModule() {
  if (localGameModuleCleanup) localGameModuleCleanup();
  localGameModuleCleanup = null;
  localGameModuleActiveApi = null;
  localGameModuleMountedKey = "";
  localGameModuleTouchStart = null;
  localGameModulePressedControl = null;
  localGameModuleSwipeMode = "tap";
  const root = $("local-module-game-root");
  if (root) {
    root.onclick = null;
    root.innerHTML = "";
  }
  const actions = $("local-module-game-actions");
  if (actions) actions.innerHTML = "";
  const controls = $("local-module-game-controls");
  if (controls) controls.innerHTML = "";
  const panel = $("local-module-game-panel");
  if (panel) delete panel.dataset.moduleKind;
  const kicker = $("local-module-game-kicker");
  if (kicker) kicker.textContent = "線上遊戲";
}

function normalizeSoloScoreTiming(body = {}) {
  const payload = { ...body };
  const penaltySeconds = Math.max(0, Math.round(Number(payload.penalty_seconds || 0)));
  const rawFromPayload = Number(payload.raw_elapsed_ms || 0);
  const elapsedFromPayload = Number(payload.elapsed_ms || 0);
  const rawElapsed = Math.max(1, Math.round(rawFromPayload > 0 ? rawFromPayload : elapsedFromPayload - penaltySeconds * 1000));
  payload.raw_elapsed_ms = rawElapsed;
  payload.penalty_seconds = penaltySeconds;
  payload.elapsed_ms = rawElapsed + penaltySeconds * 1000;
  return payload;
}

async function submitLocalGameModuleScore(key, body) {
  const payload = normalizeSoloScoreTiming(body);
  try {
    const json = await gameRequest(`/games/${encodeURIComponent(key)}/solo-scores?compact=1`, { method: "POST", body: payload });
    recordGameOutcome(key, payload, json);
    showGameDailyRewardFeedback(json, "成績已送出");
    await loadSelectedGameLeaderboard();
    return json;
  } catch (err) {
    setGameMsg(err.message || "成績送出失敗", false);
    return null;
  }
}

function showGameDailyRewardFeedback(json, fallback) {
  const reward = json?.daily_reward;
  if (reward?.wallet && typeof renderEconomyWallet === "function") renderEconomyWallet(reward.wallet);
  if (reward?.awarded) {
    setGameMsg(`每日任務完成，獲得 ${Number(reward.reward_points || 0).toLocaleString()} 積分`, true);
    return;
  }
  if (reward?.already_claimed) {
    setGameMsg("成績已送出，每日任務獎勵今日已領取", true);
    return;
  }
  if (reward?.error) {
    setGameMsg(`成績已送出；每日任務積分暫時未入帳：${reward.error}`, false);
    return;
  }
  setGameMsg(fallback || "成績已送出", true);
}

function currentGameDailyChallenge(key = gameSelectedKey) {
  return window.hackmeGameDailyChallenge?.(key) || null;
}

function scoreOutcomeFromBody(body = {}, json = {}) {
  const score = Number(body.score || 0);
  const elapsed = Number(body.elapsed_ms || body.raw_elapsed_ms || 0);
  const guessCount = Number(body.guess_count || 0);
  const accuracy = Number(body.accuracy || 0);
  return {
    ...body,
    score,
    elapsed_ms: elapsed,
    guess_count: guessCount,
    accuracy,
    completed: true,
    clean: !Number(body.penalty_seconds || 0),
    ranked: json?.ranked !== false,
  };
}

function recordGameOutcome(gameKey, body = {}, json = {}) {
  const outcome = scoreOutcomeFromBody(body, json);
  window.completeHackmeGameMissionsForResult?.(gameKey, outcome);
  const shareText = window.buildHackmeGameShareText?.(gameKey, outcome) || "";
  window.recordHackmeGameReplay?.(gameKey, {
    ...outcome,
    summary: shareText,
    title: window.hackmeGameByKey?.(gameKey)?.title || gameKey,
  });
  renderGameMetaPanels();
}

function createLocalGameModuleApi(key) {
  const root = $("local-module-game-root");
  const actions = $("local-module-game-actions");
  const controls = $("local-module-game-controls");
  return {
    key,
    root,
    actions,
    controls,
    onAction: null,
    onControl: null,
    onKey: null,
    sound(name, options = {}) { playGameSound(name, options); },
    setTitle(text) { $("local-module-game-title").textContent = text || ""; },
    status(text) { $("local-module-game-status").textContent = text || ""; },
    setActions(html) { actions.innerHTML = html || ""; },
    setControls(html) { controls.innerHTML = html || ""; },
    setSwipeMode(mode) { localGameModuleSwipeMode = mode === "hold" ? "hold" : "tap"; },
    dailyChallenge() { return window.hackmeGameDailyChallenge?.(key) || null; },
    dailyMissions() { return window.listHackmeGameDailyMissions?.(key) || []; },
    users() { return gameState.users || []; },
    multiplayer() { return window.hackmeGameMultiplayer || null; },
    achievement(id, label, detail = "") {
      const result = window.recordHackmeGameAchievement?.(key, id, label, detail);
      if (result?.unlocked) setGameMsg(`成就解鎖：${result.label}`, true);
      renderGameMetaPanels();
      return result || { unlocked: false };
    },
    mission(id, progress, target, label = "") {
      const result = window.recordHackmeGameMissionProgress?.(key, id, progress, target, label);
      renderGameMetaPanels();
      return result || null;
    },
    recordReplay(payload) {
      const result = window.recordHackmeGameReplay?.(key, payload);
      renderGameMetaPanels();
      return result;
    },
    shareSummary(payload) {
      return window.buildHackmeGameShareText?.(key, payload) || "";
    },
    request(path, options) { return gameRequest(path, options); },
    submitScore(body) { return submitLocalGameModuleScore(key, body); },
  };
}

function mountLocalGameModule(key) {
  if (localGameModuleMountedKey === key && localGameModuleActiveApi) return;
  cleanupLocalGameModule();
  const game = window.hackmeGameByKey?.(key) || {};
  const module = window.HACKME_GAME_MODULES?.[key];
  const isBoardGame = ["reversi", "go", "gomoku", "chinese_chess"].includes(key);
  const panel = $("local-module-game-panel");
  const kicker = $("local-module-game-kicker");
  if (panel) panel.dataset.moduleKind = isBoardGame ? "online-board" : "arcade";
  if (kicker) kicker.textContent = isBoardGame ? "線上棋盤" : "線上遊戲";
  $("local-module-game-title").textContent = game.title || "遊戲";
  $("local-module-game-status").textContent = game.subtitle || "準備中";
  if (!module?.mount) {
    $("local-module-game-root").innerHTML = "<div class=\"game-page-empty\"><strong>遊戲模組尚未載入</strong></div>";
    return;
  }
  const api = createLocalGameModuleApi(key);
  localGameModuleActiveApi = api;
  localGameModuleMountedKey = key;
  localGameModuleCleanup = module.mount(api) || null;
}

function renderGameCatalog(games) {
  const wrap = $("game-catalog-list");
  if (!wrap) return;
  const rows = filterAvailableGameCatalog(games);
  if (typeof renderChessPracticeDifficultyOptions === "function") renderChessPracticeDifficultyOptions(rows);
  wrap.innerHTML = rows.length ? `
    <div class="game-select-panel">
      <label for="game-select">選擇遊戲</label>
      <select id="game-select" aria-label="選擇遊戲">
        ${rows.map((game) => `<option value="${sanitize(game.key || "chess")}" ${game.key === gameSelectedKey ? "selected" : ""}>${sanitize(game.title || "西洋棋")} · ${sanitize(gameSubtitle(game))}</option>`).join("")}
      </select>
    </div>
  ` : "<p style=\"color:var(--muted);\">尚未開放遊戲</p>";
  if (!rows.some((game) => game.key === gameSelectedKey)) gameSelectedKey = "chess";
  switchGameView(gameSelectedKey);
}

function renderGameUsers(users) {
  const select = $("game-invite-user");
  if (!select) return;
  const rows = Array.isArray(users) ? users : [];
  if (!rows.length) {
    select.innerHTML = '<option value="">目前沒有可邀請玩家</option>';
    return;
  }
  select.innerHTML = '<option value="">選擇好友玩家</option>' + rows.map((user) => {
    const marks = [user.is_friend ? "好友" : "", user.is_official ? "官方/管理者" : ""].filter(Boolean);
    const label = `${user.username || ""} · ${user.role || "user"}${marks.length ? " · " + marks.join(" · ") : ""}`;
    return `<option value="${sanitize(user.username || "")}">${sanitize(label)}</option>`;
  }).join("");
}

function renderGameMultiplayerUsers(users = gameState.users || []) {
  const select = $("game-multiplayer-user");
  if (!select) return;
  const current = select.value || "";
  const rows = Array.isArray(users) ? users : [];
  if (!rows.length) {
    select.innerHTML = '<option value="">目前沒有可邀請玩家</option>';
    return;
  }
  select.innerHTML = '<option value="">選擇好友玩家</option>' + rows.map((user) => {
    const marks = [user.is_friend ? "好友" : "", user.is_official ? "官方/管理者" : ""].filter(Boolean);
    const label = `${user.username || ""} · ${user.role || "user"}${marks.length ? " · " + marks.join(" · ") : ""}`;
    return `<option value="${sanitize(user.username || "")}">${sanitize(label)}</option>`;
  }).join("");
  if (current && rows.some((user) => user.username === current)) select.value = current;
}

function renderGameMultiplayerPanel() {
  const panel = $("game-multiplayer-panel");
  if (!panel) return;
  const supported = gameSupportsMultiplayer(gameSelectedKey);
  panel.style.display = supported ? "" : "none";
  if (!supported) return;
  const modes = GAME_MULTIPLAYER_MODES[gameSelectedKey] || [];
  const modeSelect = $("game-multiplayer-mode");
  if (modeSelect) {
    const previous = modeSelect.value || modes[0]?.key || "coop";
    modeSelect.innerHTML = modes.map((mode) => `<option value="${sanitize(mode.key)}">${sanitize(mode.label)}</option>`).join("");
    modeSelect.value = modes.some((mode) => mode.key === previous) ? previous : (modes[0]?.key || "coop");
  }
  renderGameMultiplayerUsers();
  const hint = $("game-multiplayer-hint");
  if (hint) {
    hint.textContent = gameSelectedKey === "fps_arena"
      ? "3D 支援合作打殭屍與 PvP；合作也會誤傷，槍聲會提示大致方向。"
      : "火柴人合作需要雙人壓板、開門與同步抵達終點；合作也會誤傷。";
  }
  const list = $("game-multiplayer-list");
  if (!list) return;
  const rooms = Array.isArray(gameState.multiplayer?.rooms) ? gameState.multiplayer.rooms : [];
  const invites = Array.isArray(gameState.multiplayer?.invites) ? gameState.multiplayer.invites : [];
  const selectedId = Number(gameMultiplayerSelectedRoomByKey[gameSelectedKey] || 0);
  const inviteRows = invites.map((invite) => {
    const incoming = Number(invite.invitee_user_id) === Number(currentUserId || 0);
    const title = incoming
      ? `${invite.inviter_username} 邀請你加入 ${gameMultiplayerModeLabel(invite.mode)}`
      : `邀請 ${invite.invitee_username} 加入 ${gameMultiplayerModeLabel(invite.mode)}`;
    const actions = invite.status === "pending" && incoming
      ? `<button class="btn game-mini-btn btn-primary" type="button" data-game-mp-invite="${invite.id}" data-game-mp-action="accept">接受</button>
         <button class="btn game-mini-btn" type="button" data-game-mp-invite="${invite.id}" data-game-mp-action="reject">拒絕</button>`
      : invite.status === "pending"
        ? `<button class="btn game-mini-btn" type="button" data-game-mp-invite="${invite.id}" data-game-mp-action="cancel">取消</button>`
        : "";
    return `
      <div class="drive-file-row game-list-row">
        <div><strong>${sanitize(title)}</strong><small>${sanitize(invite.status)} · ${sanitize(formatChatTime(invite.created_at))}</small></div>
        <div class="drive-file-actions">${actions}</div>
      </div>
    `;
  });
  const roomRows = rooms.map((room) => {
    const active = Number(room.id) === selectedId || (!selectedId && room.status === "active");
    const peer = Number(room.host_user_id) === Number(currentUserId || 0) ? room.guest_username : room.host_username;
    const canStart = room.can_start && room.guest_user_id;
    return `
      <div class="drive-file-row game-list-row game-multiplayer-room ${active ? "active" : ""}">
        <div>
          <strong>${sanitize(gameMultiplayerModeLabel(room.mode))} · ${sanitize(room.room_code || `#${room.id}`)}</strong>
          <small>${sanitize(room.status)} · 隊友 ${sanitize(peer || "等待加入")} · ${sanitize(formatChatTime(room.updated_at))}</small>
        </div>
        <div class="drive-file-actions">
          <span class="game-multiplayer-badge">${sanitize(room.my_role === "host" ? "房主" : "成員")}</span>
          <button class="btn game-mini-btn ${active ? "btn-primary" : ""}" type="button" data-game-mp-room="${room.id}">使用</button>
          ${canStart ? `<button class="btn game-mini-btn" type="button" data-game-mp-start="${room.id}">同步開始</button>` : ""}
        </div>
      </div>
    `;
  });
  list.innerHTML = [...inviteRows, ...roomRows].join("") || "<p style=\"color:var(--muted);\">尚無多人邀請或房間</p>";
}

async function loadSelectedGameMultiplayer() {
  if (!gameSupportsMultiplayer(gameSelectedKey)) {
    gameState.multiplayer = { rooms: [], invites: [], modes: [] };
    renderGameMultiplayerPanel();
    return null;
  }
  const data = await gameRequest(`/games/${encodeURIComponent(gameSelectedKey)}/multiplayer`);
  gameState.multiplayer = {
    rooms: data.rooms || [],
    invites: data.invites || [],
    modes: data.modes || [],
  };
  const selected = gameMultiplayerSelectedRoomByKey[gameSelectedKey];
  if (selected && !(gameState.multiplayer.rooms || []).some((room) => Number(room.id) === Number(selected))) {
    delete gameMultiplayerSelectedRoomByKey[gameSelectedKey];
  }
  renderGameMultiplayerPanel();
  window.dispatchEvent(new CustomEvent("hackme:game-multiplayer-updated", { detail: { gameKey: gameSelectedKey } }));
  return data;
}

async function createGameMultiplayerInvite() {
  if (!gameSupportsMultiplayer(gameSelectedKey)) return;
  const username = $("game-multiplayer-user")?.value || "";
  const mode = $("game-multiplayer-mode")?.value || "coop";
  if (!username) {
    setGameMsg("請先選擇要邀請的玩家。", false);
    return;
  }
  try {
    const json = await gameRequest(`/games/${encodeURIComponent(gameSelectedKey)}/multiplayer/invites`, {
      method: "POST",
      body: { opponent_username: username, mode },
    });
    if (json.room?.id) gameMultiplayerSelectedRoomByKey[gameSelectedKey] = Number(json.room.id);
    await loadSelectedGameMultiplayer();
    setGameMsg("已送出多人邀請", true);
  } catch (err) {
    setGameMsg(err.message || "多人邀請失敗", false);
  }
}

async function reviewGameMultiplayerInvite(inviteId, action) {
  try {
    const json = await gameRequest(`/games/multiplayer/invites/${encodeURIComponent(inviteId)}/${encodeURIComponent(action)}`, {
      method: "POST",
      body: {},
    });
    if (json.room?.game_key) {
      gameMultiplayerSelectedRoomByKey[json.room.game_key] = Number(json.room.id);
    }
    await loadSelectedGameMultiplayer();
    setGameMsg(action === "accept" ? "已加入多人房間" : "多人邀請已更新", true);
  } catch (err) {
    setGameMsg(err.message || "多人邀請處理失敗", false);
  }
}

function gameMultiplayerInviteGameTitle(gameKey) {
  const game = window.hackmeGameByKey?.(gameKey) || {};
  return game.title || (gameKey === "fps_arena" ? "3D 對戰" : gameKey === "stickman_shooter" ? "火柴人橫向射擊" : "多人遊戲");
}

function syncGameMultiplayerInviteSeenUser() {
  const userId = String(currentUserId || "");
  if (gameMultiplayerInviteSeenUserId === userId) return;
  gameMultiplayerInviteSeenUserId = userId;
  gameMultiplayerInviteSeenIds.clear();
  hideGameMultiplayerInviteModal();
}

function setGameMultiplayerInviteModalBusy(busy) {
  gameMultiplayerInviteActionBusy = !!busy;
  ["game-multiplayer-invite-accept-btn", "game-multiplayer-invite-reject-btn"].forEach((id) => {
    const button = $(id);
    if (button) button.disabled = gameMultiplayerInviteActionBusy;
  });
}

function showGameMultiplayerInviteModal(invite) {
  const overlay = $("game-multiplayer-invite-modal");
  const title = $("game-multiplayer-invite-modal-title");
  const body = $("game-multiplayer-invite-modal-body");
  const detail = $("game-multiplayer-invite-modal-detail");
  if (!overlay || !invite) return;
  const gameTitle = gameMultiplayerInviteGameTitle(invite.game_key);
  const modeLabel = gameMultiplayerModeLabel(invite.mode);
  gameMultiplayerInviteModalInvite = invite;
  gameMultiplayerInviteSeenIds.add(String(invite.id));
  if (title) title.textContent = "多人遊戲邀請";
  if (body) {
    body.innerHTML = `<strong>${sanitize(invite.inviter_username || "玩家")}</strong> 邀請你加入 <strong>${sanitize(gameTitle)}</strong>`;
  }
  if (detail) {
    const roomCode = invite.room?.room_code || "";
    detail.textContent = `${modeLabel}${roomCode ? ` · 房間 ${roomCode}` : ""}`;
  }
  setGameMultiplayerInviteModalBusy(false);
  overlay.hidden = false;
  overlay.classList.add("show");
  overlay.setAttribute("aria-hidden", "false");
}

function hideGameMultiplayerInviteModal() {
  const overlay = $("game-multiplayer-invite-modal");
  if (!overlay) return;
  overlay.classList.remove("show");
  overlay.hidden = true;
  overlay.setAttribute("aria-hidden", "true");
  gameMultiplayerInviteModalInvite = null;
  setGameMultiplayerInviteModalBusy(false);
}

async function loadGlobalGameMultiplayerInvites({ force = false } = {}) {
  syncGameMultiplayerInviteSeenUser();
  if (!currentUserId) return { ok: true, invites: [] };
  if (typeof canAccessModule === "function" && !canAccessModule("games")) return { ok: true, invites: [] };
  if (typeof isFeatureEnabledForUi === "function" && !isFeatureEnabledForUi("feature_games_enabled", false)) return { ok: true, invites: [] };
  const data = await gameRequest("/games/multiplayer/invites/pending");
  const invites = Array.isArray(data.invites) ? data.invites : [];
  if (gameMultiplayerInviteModalInvite && !invites.some((invite) => Number(invite.id) === Number(gameMultiplayerInviteModalInvite.id))) {
    hideGameMultiplayerInviteModal();
  }
  if (!$("game-multiplayer-invite-modal")) return data;
  if (gameMultiplayerInviteModalInvite) return data;
  const nextInvite = invites.find((invite) => force || !gameMultiplayerInviteSeenIds.has(String(invite.id)));
  if (nextInvite) showGameMultiplayerInviteModal(nextInvite);
  return data;
}

async function respondGlobalGameMultiplayerInvite(action) {
  const invite = gameMultiplayerInviteModalInvite;
  if (!invite || gameMultiplayerInviteActionBusy) return;
  setGameMultiplayerInviteModalBusy(true);
  try {
    const json = await gameRequest(`/games/multiplayer/invites/${encodeURIComponent(invite.id)}/${encodeURIComponent(action)}`, {
      method: "POST",
      body: {},
    });
    hideGameMultiplayerInviteModal();
    if (action === "accept" && json.room?.game_key) {
      gameMultiplayerSelectedRoomByKey[json.room.game_key] = Number(json.room.id);
      if (typeof switchModuleTab === "function") switchModuleTab("games");
      switchGameView(json.room.game_key);
      setGameMsg("已接受邀請並切換到多人房間。", true);
    } else {
      setGameMsg("已拒絕多人邀請。", true);
      if (gameSupportsMultiplayer(gameSelectedKey)) await loadSelectedGameMultiplayer();
    }
    await loadGlobalGameMultiplayerInvites({ force: true }).catch(() => null);
  } catch (err) {
    const detail = $("game-multiplayer-invite-modal-detail");
    if (detail) detail.textContent = err.message || "多人邀請處理失敗";
    setGameMultiplayerInviteModalBusy(false);
  }
}

function ensureGameMultiplayerInvitePolling({ kickoff = true } = {}) {
  if (gameMultiplayerInvitePollTimer) return;
  if (!currentUserId) return;
  if (typeof canAccessModule === "function" && !canAccessModule("games")) return;
  if (typeof isFeatureEnabledForUi === "function" && !isFeatureEnabledForUi("feature_games_enabled", false)) return;
  const tick = () => {
    if (!currentUserId) {
      stopGameMultiplayerInvitePolling();
      return;
    }
    if (document.hidden && !gameMultiplayerInviteModalInvite) return;
    if (gameMultiplayerInviteKickoffTimer) {
      window.clearTimeout(gameMultiplayerInviteKickoffTimer);
      gameMultiplayerInviteKickoffTimer = null;
    }
    loadGlobalGameMultiplayerInvites().catch(() => null);
  };
  const delay = document.hidden
    ? gameInvitePollMs("hidden")
    : (currentModuleTab === "games" || gameMultiplayerInviteModalInvite)
      ? gameInvitePollMs("active")
      : gameInvitePollMs("idle");
  gameMultiplayerInvitePollTimer = window.setInterval(tick, delay);
  if (kickoff) {
    gameMultiplayerInviteKickoffTimer = window.setTimeout(tick, 1200);
  }
}

function stopGameMultiplayerInvitePolling() {
  if (gameMultiplayerInvitePollTimer) {
    window.clearInterval(gameMultiplayerInvitePollTimer);
    gameMultiplayerInvitePollTimer = null;
  }
  if (gameMultiplayerInviteKickoffTimer) {
    window.clearTimeout(gameMultiplayerInviteKickoffTimer);
    gameMultiplayerInviteKickoffTimer = null;
  }
}

async function startGameMultiplayerRoom(roomId) {
  try {
    const json = await gameRequest(`/games/multiplayer/rooms/${encodeURIComponent(roomId)}/start`, { method: "POST", body: {} });
    if (json.room?.game_key) gameMultiplayerSelectedRoomByKey[json.room.game_key] = Number(json.room.id);
    await loadSelectedGameMultiplayer();
    setGameMsg("多人房間已同步開始", true);
    return json;
  } catch (err) {
    setGameMsg(err.message || "多人房間開始失敗", false);
    return null;
  }
}

function renderGameInvites(invites) {
  const wrap = $("game-invite-list");
  if (!wrap) return;
  const rows = Array.isArray(invites) ? invites : [];
  if (!rows.length) {
    wrap.innerHTML = "<p style=\"color:var(--muted);\">目前沒有邀請</p>";
    return;
  }
  wrap.innerHTML = rows.map((invite) => {
    const incoming = invite.opponent_username === currentUser;
    const title = incoming ? `${invite.inviter_username} 邀請你對戰` : `邀請 ${invite.opponent_username}`;
    const actions = invite.status === "pending" && incoming
      ? `<button class="btn game-mini-btn btn-primary" type="button" data-game-invite="${invite.id}" data-game-invite-action="accept">接受</button>
         <button class="btn game-mini-btn" type="button" data-game-invite="${invite.id}" data-game-invite-action="reject">拒絕</button>`
      : invite.status === "pending"
        ? `<button class="btn game-mini-btn" type="button" data-game-invite="${invite.id}" data-game-invite-action="cancel">取消</button>`
        : "";
    return `
      <div class="drive-file-row game-list-row">
        <div><strong>${sanitize(title)}</strong><small>${sanitize(invite.status)} · ${sanitize(formatChatTime(invite.created_at))}</small></div>
        <div class="drive-file-actions">${actions}</div>
      </div>
    `;
  }).join("");
}


function renderGameLeaderboard(data) {
  const wrap = $("game-leaderboard-list");
  if (!wrap) return;
  const rows = Array.isArray(data?.leaderboard) ? data.leaderboard : [];
  if (!rows.length) {
    const empty = data?.rank_mode === "score_desc"
      ? "本週尚無高分紀錄"
      : data?.rank_mode === "time_asc"
        ? "本週尚無完成時間紀錄"
        : "本週尚無玩家對戰成績";
    wrap.innerHTML = `<p style="color:var(--muted);">${empty}</p>`;
    return;
  }
  if (data?.rank_mode === "score_desc") {
    wrap.innerHTML = rows.map((row) => `
      <div class="drive-file-row game-list-row">
        <div><strong>#${row.rank} ${sanitize(row.username || "-")}</strong><small>${sanitize(data.difficulty || "standard")} · ${row.attempts || 1} 次挑戰 · ${formatSoloGameTime(row.elapsed_ms || 0)}</small></div>
        <strong>${Number(row.score || 0).toLocaleString()}</strong>
      </div>
    `).join("");
    return;
  }
  if (data?.rank_mode === "guesses_then_time") {
    wrap.innerHTML = rows.map((row) => `
      <div class="drive-file-row game-list-row">
        <div><strong>#${row.rank} ${sanitize(row.username || "-")}</strong><small>猜 ${row.guess_count || 0} 次 · 完成 ${row.attempts || 1} 次</small></div>
        <strong>${formatSoloGameTime(row.elapsed_ms || 0)}</strong>
      </div>
    `).join("");
    return;
  }
  if (data?.rank_mode === "time_asc") {
    wrap.innerHTML = rows.map((row) => `
      <div class="drive-file-row game-list-row">
        <div><strong>#${row.rank} ${sanitize(row.username || "-")}</strong><small>${sanitize(data.difficulty || "standard")} · ${row.attempts || 1} 次完成${row.penalty_seconds ? ` · 加時 ${row.penalty_seconds} 秒` : ""}</small></div>
        <strong>${formatSoloGameTime(row.elapsed_ms || 0)}</strong>
      </div>
    `).join("");
    return;
  }
  wrap.innerHTML = rows.map((row) => `
    <div class="drive-file-row game-list-row">
      <div><strong>#${row.rank} ${sanitize(row.username || "-")}</strong><small>${row.wins || 0} 勝 · ${row.draws || 0} 和 · ${row.losses || 0} 敗</small></div>
      <strong>${row.score || 0}</strong>
    </div>
  `).join("");
}

function gameMetricText(value, target) {
  const current = Number(value || 0);
  const goal = Number(target || 1);
  if (goal >= 10000) return `${formatSoloGameTime(Math.min(current, goal))} / ${formatSoloGameTime(goal)}`;
  return `${Math.min(current, goal).toLocaleString()} / ${goal.toLocaleString()}`;
}

function renderGameDailyPanel() {
  const wrap = $("game-daily-panel");
  if (!wrap) return;
  const key = gameSelectedKey || "chess";
  const challenge = currentGameDailyChallenge(key);
  const missions = window.listHackmeGameDailyMissions?.(key, challenge) || [];
  wrap.innerHTML = `
    <div class="drive-card-sub">${sanitize(challenge?.label || "每日挑戰")} · 完成可領每日積分</div>
    <div class="game-daily-missions">
      ${missions.map((mission) => `
        <div class="game-mission-row ${mission.complete ? "complete" : ""}">
          <span class="game-meta-icon" data-game-meta-icon="${mission.complete ? "check" : "target"}">${mission.complete ? "✓" : mission.dailyIndex || "·"}</span>
          <div><strong>${sanitize(mission.label || mission.id)}</strong><small>${gameMetricText(mission.progress, mission.target)}</small></div>
        </div>
      `).join("") || "<p style=\"color:var(--muted);\">今日沒有任務</p>"}
    </div>
  `;
}

function renderGameAchievementsPanel() {
  const wrap = $("game-achievement-list");
  if (!wrap) return;
  const rows = window.listHackmeGameAchievements?.(gameSelectedKey || "") || [];
  if (!rows.length) {
    wrap.innerHTML = "<p style=\"color:var(--muted);\">尚未解鎖成就</p>";
    return;
  }
  wrap.innerHTML = rows.slice(0, 8).map((row) => `
    <div class="game-achievement-row ${row.unlocked || row.unlockedAt ? "unlocked" : ""}">
      <span class="game-meta-icon" data-game-meta-icon="${row.unlocked || row.unlockedAt ? "trophy" : "star"}">${row.unlocked || row.unlockedAt ? "✓" : "○"}</span>
      <div><strong>${sanitize(row.label || row.id)}</strong><small>${sanitize(row.detail || (row.unlockedAt ? formatChatTime(row.unlockedAt) : "尚未解鎖"))}</small></div>
    </div>
  `).join("");
}

function renderGameReplaysPanel() {
  const wrap = $("game-replay-list");
  if (!wrap) return;
  const rows = window.listHackmeGameReplays?.(gameSelectedKey || "") || [];
  if (!rows.length) {
    wrap.innerHTML = "<p style=\"color:var(--muted);\">完成一局後會保留最近回放摘要</p>";
    return;
  }
  wrap.innerHTML = rows.slice(0, 5).map((row) => {
    const share = window.buildHackmeGameShareText?.(row.gameKey, row) || row.summary || "";
    return `
      <div class="drive-file-row game-list-row">
        <div><strong>${sanitize(row.title || row.gameKey)}</strong><small>${sanitize(row.summary || share)} · ${sanitize(formatChatTime(row.createdAt))}</small></div>
        <button class="btn game-mini-btn" type="button" data-game-share-text="${sanitize(share)}">分享</button>
      </div>
    `;
  }).join("");
}

function renderGameMetaPanels() {
  renderGameDailyPanel();
  renderGameAchievementsPanel();
  renderGameReplaysPanel();
}

async function loadSelectedGameDailyLeaderboard() {
  const wrap = $("game-daily-leaderboard-list");
  if (!wrap) return null;
  const key = gameSelectedKey || "chess";
  if (key === "chess") {
    wrap.innerHTML = "<p style=\"color:var(--muted);\">西洋棋以週排行榜與對局分析為主</p>";
    return null;
  }
  const challenge = currentGameDailyChallenge(key);
  if (!challenge?.key) {
    wrap.innerHTML = "<p style=\"color:var(--muted);\">今日挑戰尚未產生</p>";
    return null;
  }
  try {
    const data = await gameRequest(`/games/${encodeURIComponent(key)}/solo-leaderboard?puzzle_id=${encodeURIComponent(challenge.key)}`);
    const rows = Array.isArray(data?.leaderboard) ? data.leaderboard : [];
    if (!rows.length) {
      wrap.innerHTML = "<p style=\"color:var(--muted);\">今日尚無挑戰成績</p>";
    } else {
      wrap.innerHTML = rows.slice(0, 5).map((row) => `
        <div class="drive-file-row game-list-row">
          <div><strong>#${row.rank} ${sanitize(row.username || "-")}</strong><small>${row.attempts || 1} 次挑戰 · ${formatSoloGameTime(row.elapsed_ms || 0)}</small></div>
          <strong>${data.rank_mode === "score_desc" ? Number(row.score || 0).toLocaleString() : formatSoloGameTime(row.elapsed_ms || 0)}</strong>
        </div>
      `).join("");
    }
    return data;
  } catch (err) {
    wrap.innerHTML = `<p style="color:var(--danger);">${sanitize(err.message || "每日排行榜讀取失敗")}</p>`;
    return null;
  }
}


async function loadSelectedGameLeaderboard() {
  const key = gameSelectedKey || "chess";
  const viewModule = activeGameViewModule();
  let path = viewModule?.leaderboardPath ? viewModule.leaderboardPath(legacyGameRuntime()) : "/games/chess/leaderboard";
  if (isLocalGameModuleAvailable(key)) {
    path = `/games/${encodeURIComponent(key)}/solo-leaderboard`;
  }
  const data = await gameRequest(path);
  gameState.leaderboard = data.leaderboard || [];
  renderGameLeaderboard(data);
  const awardBtn = $("game-award-btn");
  if (awardBtn) awardBtn.style.display = currentUser === "root" && key === "chess" ? "" : "none";
  await loadChessRootDashboard().catch(() => {});
  renderGameMetaPanels();
  await loadSelectedGameDailyLeaderboard();
  return data;
}

async function submitSoloGameScore(gameKey, state) {
  if (!state || state.scoreSubmitted) return;
  state.scoreSubmitted = true;
  const rawElapsed = soloRawElapsedMs(state);
  const elapsed = soloElapsedMs(state);
  try {
    const json = await gameRequest(`/games/${encodeURIComponent(gameKey)}/solo-scores?compact=1`, {
      method: "POST",
      body: {
        raw_elapsed_ms: rawElapsed,
        penalty_seconds: Number(state.penaltySeconds || 0),
        elapsed_ms: elapsed,
        difficulty: state.difficulty || "standard",
        puzzle_id: state.puzzleId || "",
        guess_count: Array.isArray(state.guesses) ? state.guesses.length : 0,
        score: Number(state.score || 0),
      },
    });
    recordGameOutcome(gameKey, {
      raw_elapsed_ms: rawElapsed,
      penalty_seconds: Number(state.penaltySeconds || 0),
      elapsed_ms: elapsed,
      difficulty: state.difficulty || "standard",
      puzzle_id: state.puzzleId || "",
      guess_count: Array.isArray(state.guesses) ? state.guesses.length : Number(state.guessCount || 0),
      score: Number(state.score || 0),
      lines: Number(state.lines || 0),
      combo: Number(state.maxCombo || state.combo || 0),
      boss: Number(state.bossDefeated || state.bossCount || 0),
      weapon: Number(state.weaponLevel || 0),
      graze: Number(state.graze || 0),
      wave: Number(state.wave || 0),
      accuracy: Number(state.shots ? Math.round((Number(state.hits || 0) / Number(state.shots || 1)) * 100) : 0),
      survive: Number(state.health || state.lives || 0) > 0 ? 1 : 0,
      clean: !Number(state.penaltySeconds || 0) && !Number(state.mistakes || 0),
      maxTile: Number(state.maxTile || 0),
      moves: Number(state.moves || 0),
      length: Number(state.maxLength || state.snake?.length || 0),
      powerup: Number(state.powerupsCollected || 0),
    }, json);
    showGameDailyRewardFeedback(json, "成績已送出");
    await loadSelectedGameLeaderboard();
  } catch (err) {
    state.scoreSubmitted = false;
    setGameMsg(err.message || "成績送出失敗", false);
  }
}


async function loadGameZone() {
  try {
    const runtimeReady = ensureGameRuntimeScriptsLoaded();
    await fetchCsrfToken();
    const [catalog, usersJson, invitesJson, matchesJson] = await Promise.all([
      gameRequest("/games/catalog"),
      gameRequest("/games/users"),
      gameRequest("/games/chess/invites"),
      gameRequest("/games/chess/matches"),
    ]);
    await runtimeReady;
    gameState = {
      matches: matchesJson.matches || [],
      invites: invitesJson.invites || [],
      leaderboard: [],
      users: usersJson.users || [],
      multiplayer: gameState.multiplayer || { rooms: [], invites: [], modes: [] },
    };
    renderGameCatalog(catalog.games || []);
    renderGameUsers(usersJson.users || []);
    renderGameMultiplayerUsers(usersJson.users || []);
    renderGameInvites(invitesJson.invites || []);
    renderGameMatches(matchesJson.matches || []);
    await loadSelectedGameMultiplayer();
    await loadSelectedGameLeaderboard();
    setGameMsg("", true);
  } catch (err) {
    setGameMsg(err.message || "遊戲區讀取失敗", false);
  }
}

async function refreshGameZoneAfterMutation(successMessage) {
  try {
    await loadGameZone();
    if (successMessage) setGameMsg(successMessage, true);
  } catch (err) {
    if (successMessage) setGameMsg(successMessage, true);
  }
}

window.hackmeGameMultiplayer = {
  activeRoom: gameMultiplayerActiveRoom,
  peerId: gameMultiplayerPeerId,
  peerState: gameMultiplayerPeerState,
  selectedRoomId(gameKey = gameSelectedKey) {
    return Number(gameMultiplayerSelectedRoomByKey[gameKey] || 0);
  },
  selectRoom(gameKey, roomId) {
    if (gameKey && roomId) gameMultiplayerSelectedRoomByKey[gameKey] = Number(roomId);
    renderGameMultiplayerPanel();
  },
  async refresh(gameKey = gameSelectedKey) {
    const previous = gameSelectedKey;
    if (gameKey !== gameSelectedKey) gameSelectedKey = gameKey;
    try {
      return await loadSelectedGameMultiplayer();
    } finally {
      gameSelectedKey = previous;
    }
  },
  async start(roomId) {
    return startGameMultiplayerRoom(roomId);
  },
  async pollRoom(roomId, afterEventId = 0) {
    return gameRequest(`/games/multiplayer/rooms/${encodeURIComponent(roomId)}?after_event_id=${encodeURIComponent(afterEventId || 0)}`);
  },
  async syncRoom(roomId, state, events = [], afterEventId = 0) {
    return gameRequest(`/games/multiplayer/rooms/${encodeURIComponent(roomId)}/state`, {
      method: "POST",
      body: {
        state: state || {},
        events: Array.isArray(events) ? events : [],
        after_event_id: afterEventId || 0,
      },
    });
  },
  async checkInvitesNow() {
    return loadGlobalGameMultiplayerInvites({ force: true });
  },
};


document.addEventListener("click", (event) => {
  if (event.target?.closest?.("#game-multiplayer-invite-accept-btn")) {
    respondGlobalGameMultiplayerInvite("accept");
    return;
  }
  if (event.target?.closest?.("#game-multiplayer-invite-reject-btn")) {
    respondGlobalGameMultiplayerInvite("reject");
    return;
  }
  if (event.target?.closest?.("#game-fullscreen-btn, #fps-arena-fullscreen-btn")) {
    toggleGameFullscreen();
    return;
  }
  const shareBtn = event.target?.closest?.("[data-game-share-text]");
  if (shareBtn) {
    playGameSound("uiClick", { volume: 0.12 });
    const text = shareBtn.dataset.gameShareText || "";
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(
        () => setGameMsg("分享文字已複製", true),
        () => setGameMsg(text, true),
      );
    } else {
      setGameMsg(text, true);
    }
    return;
  }
  const localGameModuleAction = event.target?.closest?.("#local-module-game-panel [data-action]");
  if (localGameModuleAction && localGameModuleActiveApi?.onAction) {
    playGameSound(localGameModuleAction.dataset.action === "pause" ? "uiSwitch" : "uiClick", { volume: 0.12 });
    localGameModuleActiveApi.onAction(localGameModuleAction.dataset.action || "");
    return;
  }
  const multiplayerInviteBtn = event.target?.closest?.("#game-multiplayer-invite-btn");
  if (multiplayerInviteBtn) {
    playGameSound("uiOpen", { volume: 0.12 });
    createGameMultiplayerInvite();
    return;
  }
  const multiplayerInviteAction = event.target?.closest?.("[data-game-mp-invite]");
  if (multiplayerInviteAction) {
    playGameSound(multiplayerInviteAction.dataset.gameMpAction === "reject" ? "uiClose" : "uiClick", { volume: 0.12 });
    reviewGameMultiplayerInvite(multiplayerInviteAction.dataset.gameMpInvite, multiplayerInviteAction.dataset.gameMpAction || "accept");
    return;
  }
  const multiplayerRoomBtn = event.target?.closest?.("[data-game-mp-room]");
  if (multiplayerRoomBtn) {
    playGameSound("uiSelect", { volume: 0.12 });
    gameMultiplayerSelectedRoomByKey[gameSelectedKey] = Number(multiplayerRoomBtn.dataset.gameMpRoom || 0);
    renderGameMultiplayerPanel();
    window.dispatchEvent(new CustomEvent("hackme:game-multiplayer-updated", { detail: { gameKey: gameSelectedKey } }));
    setGameMsg("已選擇多人房間，可在遊戲面板開始多人模式。", true);
    return;
  }
  const multiplayerStartBtn = event.target?.closest?.("[data-game-mp-start]");
  if (multiplayerStartBtn) {
    playGameSound("uiClick", { volume: 0.12 });
    startGameMultiplayerRoom(multiplayerStartBtn.dataset.gameMpStart);
    return;
  }
  const catalogBtn = event.target?.closest?.("[data-game-key]");
  if (catalogBtn) {
    playGameSound("uiSelect", { volume: 0.12 });
    switchGameView(catalogBtn.dataset.gameKey || "chess");
    return;
  }
  if (dispatchActiveGameViewEvent("click", event)) {
    return;
  }
  const rootRefreshBtn = event.target?.closest?.("#game-root-chess-refresh-btn");
  if (rootRefreshBtn) {
    loadChessRootDashboard().catch((err) => setGameMsg(err.message || "dashboard 讀取失敗", false));
    return;
  }
  const rootWarmStartBtn = event.target?.closest?.("#game-root-chess-warm-start-btn");
  if (rootWarmStartBtn) {
    warmStartChessModels();
    return;
  }
  const rootStageBtn = event.target?.closest?.("#game-root-chess-stage-btn");
  if (rootStageBtn) {
    stageChessCandidate();
    return;
  }
  const rootPromoteBtn = event.target?.closest?.("#game-root-chess-promote-btn");
  if (rootPromoteBtn) {
    promoteChessCandidate();
    return;
  }
  const deleteMatchBtn = event.target?.closest?.("[data-game-delete-match]");
  if (deleteMatchBtn) {
    deleteFinishedGame(deleteMatchBtn.dataset.gameDeleteMatch);
    return;
  }
  const offerDrawBtn = event.target?.closest?.("#game-offer-draw-btn");
  if (offerDrawBtn) {
    offerChessDraw();
    return;
  }
  const acceptDrawBtn = event.target?.closest?.("#game-accept-draw-btn");
  if (acceptDrawBtn) {
    respondChessDraw("accept");
    return;
  }
  const rejectDrawBtn = event.target?.closest?.("#game-reject-draw-btn");
  if (rejectDrawBtn) {
    respondChessDraw("reject");
    return;
  }
  const claimDrawBtn = event.target?.closest?.("#game-claim-draw-btn");
  if (claimDrawBtn) {
    claimChessDraw();
    return;
  }
  const inviteBtn = event.target?.closest?.("[data-game-invite]");
  if (inviteBtn) {
    reviewGameInvite(inviteBtn.dataset.gameInvite, inviteBtn.dataset.gameInviteAction || "accept");
    return;
  }
  const matchBtn = event.target?.closest?.("[data-game-match-id]");
  if (matchBtn) {
    gameSelectedMatchId = Number(matchBtn.dataset.gameMatchId || 0);
    gameSelectedSquare = null;
    renderGameMatches(gameState.matches || []);
    return;
  }
  const squareBtn = event.target?.closest?.("[data-chess-square]");
  if (squareBtn) {
    selectChessSquare(squareBtn.dataset.chessSquare || "");
  }
});

document.addEventListener("pointerdown", (event) => {
  const control = event.target?.closest?.("#local-module-game-controls button");
  if (!control || !localGameModuleActiveApi?.onControl) return;
  event.preventDefault();
  playGameSound(control.dataset.hold ? "uiSwitch" : "uiClick", { volume: 0.1, throttleMs: 40 });
  localGameModulePressedControl = control;
  localGameModuleActiveApi.onControl(control, true);
});

document.addEventListener("pointerup", () => {
  if (!localGameModulePressedControl || !localGameModuleActiveApi?.onControl) {
    localGameModulePressedControl = null;
    return;
  }
  if (localGameModulePressedControl.dataset.hold) {
    localGameModuleActiveApi.onControl(localGameModulePressedControl, false);
  }
  localGameModulePressedControl = null;
});

document.addEventListener("pointercancel", () => {
  if (localGameModulePressedControl?.dataset.hold && localGameModuleActiveApi?.onControl) {
    localGameModuleActiveApi.onControl(localGameModulePressedControl, false);
  }
  localGameModulePressedControl = null;
});

document.addEventListener("touchstart", (event) => {
  const root = event.target?.closest?.("#local-module-game-root");
  if (!root) return;
  const touch = event.changedTouches[0];
  localGameModuleTouchStart = touch ? { x: touch.clientX, y: touch.clientY } : null;
}, { passive: true });

document.addEventListener("touchend", (event) => {
  const root = event.target?.closest?.("#local-module-game-root");
  if (!root || !localGameModuleTouchStart || !localGameModuleActiveApi?.onKey) return;
  const touch = event.changedTouches[0];
  if (!touch) return;
  const dx = touch.clientX - localGameModuleTouchStart.x;
  const dy = touch.clientY - localGameModuleTouchStart.y;
  localGameModuleTouchStart = null;
  if (Math.max(Math.abs(dx), Math.abs(dy)) < 24) return;
  event.preventDefault();
  const key = Math.abs(dx) > Math.abs(dy)
    ? (dx > 0 ? "ArrowRight" : "ArrowLeft")
    : (dy > 0 ? "ArrowDown" : "ArrowUp");
  localGameModuleActiveApi.onKey({ key, preventDefault() {} }, true);
  if (localGameModuleSwipeMode === "hold") {
    window.setTimeout(() => {
      if (!localGameModuleActiveApi?.onKey) return;
      localGameModuleActiveApi.onKey({ key, preventDefault() {} }, false);
    }, 90);
  }
}, { passive: false });

document.addEventListener("submit", (event) => {
  const uciForm = event.target?.closest?.("#game-chess-uci-form");
  if (!uciForm) return;
  event.preventDefault();
  submitChessUciMove();
});

document.addEventListener("input", (event) => {
  if (dispatchActiveGameViewEvent("input", event)) {
    return;
  }
  const chessUciInput = event.target?.closest?.("#game-chess-uci-input");
  if (chessUciInput) {
    chessUciInput.value = normalizeChessUciInput(chessUciInput.value || "");
  }
});

document.addEventListener("keydown", (event) => {
  const tag = String(event.target?.tagName || "").toLowerCase();
  const editing = ["input", "textarea", "select"].includes(tag);
  if ((!editing || gameSelectedKey === "1a2b") && dispatchActiveGameViewEvent("keydown", event)) {
    return;
  }
  if (!editing && isLocalGameModuleAvailable(gameSelectedKey) && localGameModuleActiveApi?.onKey) {
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", " "].includes(event.key)) event.preventDefault();
    localGameModuleActiveApi.onKey(event, true);
  }
});

document.addEventListener("keyup", (event) => {
  if (dispatchActiveGameViewEvent("keyup", event)) {
    return;
  }
  if (isLocalGameModuleAvailable(gameSelectedKey) && localGameModuleActiveApi?.onKey) {
    localGameModuleActiveApi.onKey(event, false);
  }
});

document.addEventListener("change", (event) => {
  const gameSelect = event.target?.closest?.("#game-select");
  if (gameSelect) {
    const key = gameSelect.value || "chess";
    playGameSound("uiSelect", { volume: 0.12 });
    switchGameView(key);
    return;
  }
  if (event.target?.closest?.("#game-multiplayer-mode")) {
    playGameSound("uiSwitch", { volume: 0.12 });
    renderGameMultiplayerPanel();
    return;
  }
  dispatchActiveGameViewEvent("change", event);
});

document.addEventListener("contextmenu", (event) => {
  dispatchActiveGameViewEvent("contextmenu", event);
});

document.addEventListener("fullscreenchange", updateGameFullscreenButtons);

document.addEventListener("visibilitychange", () => {
  if (!gameMultiplayerInvitePollTimer || !currentUserId) return;
  stopGameMultiplayerInvitePolling();
  ensureGameMultiplayerInvitePolling({ kickoff: !document.hidden });
});

document.addEventListener("hackme:module-changed", () => {
  if (!gameMultiplayerInvitePollTimer || !currentUserId) return;
  stopGameMultiplayerInvitePolling();
  ensureGameMultiplayerInvitePolling({ kickoff: currentModuleTab === "games" });
});
