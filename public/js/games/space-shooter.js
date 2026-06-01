'use strict';

let spaceShooterState = null;
let spaceShooterLoopTimer = null;
const SPACE_SHOOTER_TOUCH_HOLD_ACTIONS = new Set(["shooter-left", "shooter-right", "shooter-fire"]);
const spaceShooterTouchPointers = new Map();
let spaceShooterSuppressClickUntil = 0;
const SPACE_SHOOTER_ENEMY_TYPES = ["striker", "gunner", "evader"];
const SPACE_SHOOTER_ASSET_SOURCES = Object.freeze({
  extension: {
    name: "Kenney Space Shooter Extension",
    url: "https://kenney.nl/assets/space-shooter-extension",
    license: "Creative Commons CC0",
    usage: "bundled PNG ships, missiles, meteor props and powerup parts with canvas fallback",
  },
});
const SPACE_SHOOTER_ASSET_BASE = "/assets/games/vendor/kenney/space-shooter-extension/";
const SPACE_SHOOTER_IMAGE_ASSETS = Object.freeze({
  player: `${SPACE_SHOOTER_ASSET_BASE}ships/player.png`,
  striker: `${SPACE_SHOOTER_ASSET_BASE}ships/enemy_striker.png`,
  gunner: `${SPACE_SHOOTER_ASSET_BASE}ships/enemy_gunner.png`,
  evader: `${SPACE_SHOOTER_ASSET_BASE}ships/enemy_evader.png`,
  boss: `${SPACE_SHOOTER_ASSET_BASE}ships/boss.png`,
  playerLaser: `${SPACE_SHOOTER_ASSET_BASE}missiles/player_laser.png`,
  enemyLaser: `${SPACE_SHOOTER_ASSET_BASE}missiles/enemy_laser.png`,
  meteor: `${SPACE_SHOOTER_ASSET_BASE}props/meteor.png`,
  powerupWeapon: `${SPACE_SHOOTER_ASSET_BASE}parts/powerup_weapon.png`,
  powerupShield: `${SPACE_SHOOTER_ASSET_BASE}parts/powerup_shield.png`,
});
const SPACE_SHOOTER_IMAGES = {};

function spaceShooterImageFor(key) {
  const src = SPACE_SHOOTER_IMAGE_ASSETS[key];
  if (!src || typeof Image === "undefined") return null;
  if (!SPACE_SHOOTER_IMAGES[key]) {
    const image = new Image();
    image.decoding = "async";
    image.src = src;
    SPACE_SHOOTER_IMAGES[key] = image;
  }
  return SPACE_SHOOTER_IMAGES[key];
}

function spaceShooterImageReady(image) {
  return Boolean(image?.complete && image.naturalWidth > 0);
}

function drawSpaceShooterImage(ctx, key, x, y, w, h, options = {}) {
  const image = spaceShooterImageFor(key);
  if (!spaceShooterImageReady(image)) return false;
  ctx.save();
  ctx.globalAlpha = options.alpha ?? 1;
  ctx.translate(x, y);
  if (options.rotation) ctx.rotate(options.rotation);
  if (options.flipX || options.flipY) ctx.scale(options.flipX ? -1 : 1, options.flipY ? -1 : 1);
  ctx.drawImage(image, -w / 2, -h / 2, w, h);
  ctx.restore();
  return true;
}

function playSpaceShooterSound(name, options = {}) {
  if (typeof playGameSound === "function") playGameSound(name, options);
}

function clearSpaceShooterLoop() {
  if (spaceShooterLoopTimer) {
    clearInterval(spaceShooterLoopTimer);
    spaceShooterLoopTimer = null;
  }
}

function spaceShooterRandom() {
  return typeof spaceShooterState?.rng === "function" ? spaceShooterState.rng() : Math.random();
}

function spaceShooterEnemyDodgeEnabled() {
  return Boolean(document.getElementById("space-shooter-enemy-dodge")?.checked);
}

function recordSpaceShooterAchievement(id, label, detail = "") {
  const result = window.recordHackmeGameAchievement?.("space_shooter", id, label, detail);
  if (result?.unlocked && typeof setGameMsg === "function") setGameMsg(`成就解鎖：${result.label}`, true);
  return result || { unlocked: false };
}

function damageSpaceShooterPlayer(state) {
  if (state.shield > 0) {
    state.shield -= 1;
    playSpaceShooterSound("metalHit", { volume: 0.12, throttleMs: 140 });
    recordSpaceShooterAchievement("shield-save", "護盾救援", "用護盾擋下一次傷害。");
    return;
  }
  state.lives -= 1;
  playSpaceShooterSound("punch", { volume: 0.16, throttleMs: 180 });
}

function fireSpaceShooterShots(state) {
  const level = Math.max(1, Math.min(4, Number(state.weaponLevel || 1)));
  const spreads = level === 1
    ? [{ x: 0, vx: 0 }]
    : level === 2
      ? [{ x: -7, vx: -0.35 }, { x: 7, vx: 0.35 }]
      : level === 3
        ? [{ x: -11, vx: -0.55 }, { x: 0, vx: 0 }, { x: 11, vx: 0.55 }]
        : [{ x: -15, vx: -0.8 }, { x: -5, vx: -0.25 }, { x: 5, vx: 0.25 }, { x: 15, vx: 0.8 }];
  spreads.forEach((shot) => {
    state.bullets.push({ x: state.playerX + shot.x, y: 448, vx: shot.vx, vy: -12.5, damage: 1 });
  });
}

function spawnSpaceShooterPowerup(state, x, y, type = "") {
  const roll = spaceShooterRandom();
  state.powerups.push({
    x,
    y,
    type: type || (roll < 0.62 ? "weapon" : roll < 0.86 ? "shield" : "life"),
    vy: 2.5,
    phase: spaceShooterRandom() * Math.PI * 2,
  });
}

function collectSpaceShooterPowerup(state, powerup) {
  playSpaceShooterSound("uiDrop", { volume: 0.14, throttleMs: 160 });
  if (powerup.type === "weapon") {
    state.weaponLevel = Math.min(4, state.weaponLevel + 1);
    state.score += 75;
    if (state.weaponLevel >= 4) recordSpaceShooterAchievement("weapon-max", "滿載火力", "宇宙戰機武器升級到最高階。");
    return;
  }
  if (powerup.type === "shield") {
    state.shield = Math.min(3, state.shield + 1);
    state.score += 50;
    return;
  }
  state.lives = Math.min(5, state.lives + 1);
  state.score += 100;
}

function spawnSpaceShooterEnemy(state) {
  const roll = spaceShooterRandom();
  const type = SPACE_SHOOTER_ENEMY_TYPES[roll < 0.42 ? 0 : roll < 0.74 ? 1 : 2];
  const hpBonus = state.score > 900 ? 1 : 0;
  state.enemies.push({
    x: 24 + spaceShooterRandom() * 312,
    y: -18,
    vx: (spaceShooterRandom() - 0.5) * (type === "evader" ? 2.1 : 1.1),
    hp: (type === "gunner" ? 2 : 1) + hpBonus + Math.floor(state.bossDefeated / 3),
    type,
    phase: spaceShooterRandom() * Math.PI * 2,
    fireAt: state.tick + (type === "gunner" ? 34 : 46) + Math.floor(spaceShooterRandom() * 20),
    dodgeUntil: 0,
    dodgeVx: 0,
  });
}

function fireSpaceShooterEnemyAttack(state, enemy) {
  const baseSpeed = 3.4 + Math.min(1.8, state.score / 2600);
  const toPlayer = Math.atan2(450 - enemy.y, state.playerX - enemy.x);
  if (enemy.type === "gunner") {
    [-0.22, 0, 0.22].forEach((spread) => {
      const angle = toPlayer + spread;
      state.enemyBullets.push({
        x: enemy.x,
        y: enemy.y + 18,
        vx: Math.cos(angle) * (baseSpeed * 0.9),
        vy: Math.sin(angle) * (baseSpeed * 0.9),
        color: "#fbbf24",
      });
    });
    enemy.fireAt = state.tick + Math.max(28, 56 - Math.floor(state.score / 550));
    return;
  }
  const angle = enemy.type === "evader" ? toPlayer + Math.sin(enemy.phase) * 0.18 : toPlayer;
  state.enemyBullets.push({
    x: enemy.x,
    y: enemy.y + 16,
    vx: Math.cos(angle) * baseSpeed,
    vy: Math.sin(angle) * baseSpeed,
    color: enemy.type === "evader" ? "#a78bfa" : "#f97316",
  });
  enemy.fireAt = state.tick + Math.max(32, (enemy.type === "evader" ? 72 : 62) - Math.floor(state.score / 700));
}

function updateSpaceShooterEnemyDodge(state, enemy) {
  if (!state.enemyDodgeEnabled) return;
  if (enemy.type !== "evader" && enemy.type !== "gunner") return;
  if (state.tick < Number(enemy.dodgeUntil || 0)) return;
  const threat = state.bullets.find((bullet) => (
    bullet.vy < 0
    && bullet.y < enemy.y + 42
    && bullet.y > enemy.y - 110
    && Math.abs(bullet.x - enemy.x) < (enemy.type === "evader" ? 32 : 18)
  ));
  if (!threat) return;
  const direction = threat.x <= enemy.x ? 1 : -1;
  enemy.dodgeVx = direction * (enemy.type === "evader" ? 4.8 : 3.2);
  enemy.dodgeUntil = state.tick + (enemy.type === "evader" ? 16 : 10);
}

function updateSpaceShooterEnemies(state) {
  const fallSpeed = 2.85 + Math.min(3, state.score / 700);
  state.enemies.forEach((enemy) => {
    enemy.phase = Number(enemy.phase || 0) + 0.09;
    updateSpaceShooterEnemyDodge(state, enemy);
    const weaving = Math.sin(enemy.phase) * (enemy.type === "striker" ? 0.55 : enemy.type === "gunner" ? 0.82 : 1.2);
    const dodge = state.tick < Number(enemy.dodgeUntil || 0) ? Number(enemy.dodgeVx || 0) : 0;
    if (!dodge) enemy.dodgeVx *= 0.86;
    enemy.x += Number(enemy.vx || 0) + weaving + dodge;
    enemy.y += fallSpeed * (enemy.type === "gunner" ? 0.86 : enemy.type === "evader" ? 0.95 : 1.08);
    if (enemy.x < 24 || enemy.x > 336) {
      enemy.x = Math.max(24, Math.min(336, enemy.x));
      enemy.vx = -Number(enemy.vx || 0);
      enemy.dodgeVx = -Number(enemy.dodgeVx || 0) * 0.45;
    }
    if (state.tick >= Number(enemy.fireAt || 0) && enemy.y > 12 && enemy.y < 390) fireSpaceShooterEnemyAttack(state, enemy);
  });
}

function spawnSpaceShooterBoss(state) {
  if (state.boss) return;
  const maxHp = 26 + state.bossCount * 10;
  state.boss = {
    x: 180,
    y: 70,
    vx: 3.1 + Math.min(2, state.bossCount * 0.35),
    hp: maxHp,
    maxHp,
    fireAt: state.tick + 12,
  };
  state.bossCount += 1;
  state.nextBossScore += 850 + state.bossCount * 180;
}

function startSpaceShooterGame() {
  clearSpaceShooterLoop();
  const dailyChallenge = window.hackmeGameDailyChallenge?.("space_shooter") || null;
  spaceShooterState = {
    status: "active",
    startedAt: Date.now(),
    completedAt: null,
    penaltySeconds: 0,
    scoreSubmitted: false,
    difficulty: dailyChallenge?.difficulty || "standard",
    puzzleId: dailyChallenge?.key || "space-shooter-standard",
    dailyChallenge,
    rng: dailyChallenge?.seed ? window.createHackmeGameSeededRandom?.(dailyChallenge.seed) : null,
    score: 0,
    lives: 3,
    shield: 0,
    weaponLevel: 1,
    tick: 0,
    playerX: 180,
    bullets: [],
    enemies: [],
    enemyBullets: [],
    powerups: [],
    boss: null,
    bossCount: 0,
    bossDefeated: 0,
    nextBossScore: 450,
    keys: {},
    lastShotTick: -20,
    autoFire: true,
    enemyDodgeEnabled: spaceShooterEnemyDodgeEnabled(),
  };
  renderSpaceShooterBoard();
  ensureSoloGameTimer();
  spaceShooterLoopTimer = setInterval(tickSpaceShooterGame, 50);
  updateSpaceShooterStatus(`${dailyChallenge?.label || "出擊"}。方向鍵或 A/D 移動，空白鍵射擊。`);
}

function finishSpaceShooterGame() {
  if (!spaceShooterState || spaceShooterState.status === "finished") return;
  spaceShooterState.status = "finished";
  spaceShooterState.completedAt = Date.now();
  clearSpaceShooterLoop();
  renderSpaceShooterBoard();
  updateSpaceShooterStatus();
  stopSoloGameTimerIfIdle();
  if (Number(spaceShooterState.score || 0) > 0) {
    recordSpaceShooterAchievement("score-posted", "完成出擊", "完成一局宇宙戰機。");
    submitSoloGameScore("space_shooter", spaceShooterState);
  }
  setGameMsg(`宇宙戰機結束，分數 ${Number(spaceShooterState.score || 0).toLocaleString()}`, true);
}

function suspendSpaceShooterGame() {
  if (!spaceShooterState || spaceShooterState.status !== "active") return;
  spaceShooterState.status = "paused";
  spaceShooterState.keys = {};
  spaceShooterTouchPointers.clear();
  clearSpaceShooterLoop();
  renderSpaceShooterBoard();
  updateSpaceShooterStatus("已暫停；按開始可重新出擊。");
}

function tickSpaceShooterGame() {
  const state = spaceShooterState;
  if (!state || state.status !== "active") return;
  state.tick += 1;
  const movingLeft = state.keys.ArrowLeft || state.keys.a || state.keys.A;
  const movingRight = state.keys.ArrowRight || state.keys.d || state.keys.D;
  if (movingLeft) state.playerX = Math.max(18, state.playerX - 7);
  if (movingRight) state.playerX = Math.min(342, state.playerX + 7);
  if ((state.autoFire !== false || state.keys[" "] || state.keys.Spacebar) && state.tick - state.lastShotTick >= 5) {
    fireSpaceShooterShots(state);
    playSpaceShooterSound("uiTick", { volume: 0.04, throttleMs: 120 });
    state.lastShotTick = state.tick;
  }
  if (!state.boss && state.score >= state.nextBossScore) spawnSpaceShooterBoss(state);
  if (state.tick % Math.max(12, 34 - Math.floor(state.score / 250)) === 0) {
    spawnSpaceShooterEnemy(state);
  }
  state.bullets.forEach((bullet) => {
    bullet.x += bullet.vx || 0;
    bullet.y += bullet.vy || -12;
  });
  updateSpaceShooterEnemies(state);
  if (state.boss) {
    state.boss.x += state.boss.vx;
    if (state.boss.x < 54 || state.boss.x > 306) state.boss.vx *= -1;
    if (state.tick >= state.boss.fireAt) {
      const toPlayer = Math.atan2(446 - state.boss.y, state.playerX - state.boss.x);
      for (let i = -1; i <= 1; i += 1) {
        const angle = toPlayer + i * 0.24;
        state.enemyBullets.push({
          x: state.boss.x,
          y: state.boss.y + 30,
          vx: Math.cos(angle) * 4.2,
          vy: Math.sin(angle) * 4.2,
        });
      }
      state.boss.fireAt = state.tick + Math.max(10, 18 - state.bossCount);
    }
  }
  state.enemyBullets.forEach((bullet) => {
    bullet.x += bullet.vx;
    bullet.y += bullet.vy;
  });
  state.powerups.forEach((powerup) => {
    powerup.phase += 0.12;
    powerup.y += powerup.vy;
  });
  state.bullets = state.bullets.filter((bullet) => bullet.y > -12 && bullet.x > -18 && bullet.x < 378);
  if (state.boss) {
    state.bullets.forEach((bullet) => {
      if (bullet.y < -20 || Math.abs(bullet.x - state.boss.x) > 48 || Math.abs(bullet.y - state.boss.y) > 34) return;
      bullet.y = -100;
      state.boss.hp -= bullet.damage || 1;
      state.score += 12;
    });
    if (state.boss.hp <= 0) {
      const defeated = state.boss;
      state.score += 650 + state.bossCount * 120;
      playSpaceShooterSound("metalHit", { volume: 0.18, throttleMs: 180 });
      state.boss = null;
      state.bossDefeated += 1;
      recordSpaceShooterAchievement("boss-down", "旗艦擊破", "擊破宇宙戰機 Boss。");
      spawnSpaceShooterPowerup(state, defeated.x - 22, defeated.y + 22, "weapon");
      spawnSpaceShooterPowerup(state, defeated.x + 22, defeated.y + 22, "shield");
    }
  }
  const remainingEnemies = [];
  state.enemies.forEach((enemy) => {
    let destroyed = false;
    state.bullets.forEach((bullet) => {
      if (destroyed || bullet.y < -20) return;
      if (Math.abs(bullet.x - enemy.x) < 18 && Math.abs(bullet.y - enemy.y) < 18) {
        bullet.y = -100;
        enemy.hp -= bullet.damage || 1;
        state.score += 10;
        if (enemy.hp <= 0) {
          destroyed = true;
          state.score += 25;
          playSpaceShooterSound("hit", { volume: 0.09, throttleMs: 70 });
          if (spaceShooterRandom() < 0.12) spawnSpaceShooterPowerup(state, enemy.x, enemy.y);
        }
      }
    });
    if (destroyed) return;
    if (Math.abs(state.playerX - enemy.x) < 24 && enemy.y > 420) {
      damageSpaceShooterPlayer(state);
      return;
    }
    if (enemy.y > 540) {
      damageSpaceShooterPlayer(state);
      return;
    }
    remainingEnemies.push(enemy);
  });
  state.enemies = remainingEnemies;
  state.enemyBullets = state.enemyBullets.filter((bullet) => {
    if (Math.abs(bullet.x - state.playerX) < 16 && Math.abs(bullet.y - 450) < 22) {
      damageSpaceShooterPlayer(state);
      return false;
    }
    return bullet.x > -20 && bullet.x < 380 && bullet.y > -20 && bullet.y < 540;
  });
  state.powerups = state.powerups.filter((powerup) => {
    if (Math.abs(powerup.x - state.playerX) < 26 && Math.abs(powerup.y - 448) < 30) {
      collectSpaceShooterPowerup(state, powerup);
      return false;
    }
    return powerup.y < 540;
  });
  if (state.lives <= 0) {
    finishSpaceShooterGame();
    return;
  }
  renderSpaceShooterBoard();
  updateSpaceShooterStatus();
}

function nudgeSpaceShooter(dx) {
  const state = spaceShooterState;
  if (!state || state.status !== "active") return;
  state.playerX = Math.max(18, Math.min(342, state.playerX + dx));
  renderSpaceShooterBoard();
}

function shootSpaceShooter() {
  const state = spaceShooterState;
  if (!state || state.status !== "active") return;
  if (state.tick - state.lastShotTick < 2) return;
  fireSpaceShooterShots(state);
  state.lastShotTick = state.tick;
  renderSpaceShooterBoard();
}

function setSpaceShooterTouchAction(action, pressed) {
  const state = spaceShooterState;
  if (!state || state.status !== "active") return;
  if (action === "shooter-left") state.keys.ArrowLeft = Boolean(pressed);
  if (action === "shooter-right") state.keys.ArrowRight = Boolean(pressed);
  if (action === "shooter-fire") state.keys[" "] = Boolean(pressed);
}

function spaceShooterCanvasX(event, canvas) {
  const rect = canvas.getBoundingClientRect();
  if (!rect.width) return 180;
  return Math.max(18, Math.min(342, ((event.clientX - rect.left) / rect.width) * canvas.width));
}

function moveSpaceShooterToEvent(event) {
  const state = spaceShooterState;
  const canvas = $("space-shooter-board");
  if (!state || state.status !== "active" || !canvas) return;
  state.playerX = spaceShooterCanvasX(event, canvas);
  renderSpaceShooterBoard();
}

function releaseSpaceShooterTouch(pointerId) {
  const action = spaceShooterTouchPointers.get(pointerId);
  if (!action) return;
  spaceShooterTouchPointers.delete(pointerId);
  setSpaceShooterTouchAction(action, false);
}

function bindSpaceShooterCanvasTouch() {
  const canvas = $("space-shooter-board");
  if (!canvas || canvas.dataset.touchBound === "1") return;
  canvas.dataset.touchBound = "1";
  canvas.style.touchAction = "none";
  canvas.addEventListener("pointerdown", (event) => {
    if (!spaceShooterState || spaceShooterState.status !== "active") return;
    event.preventDefault();
    spaceShooterSuppressClickUntil = Date.now() + 260;
    try {
      canvas.setPointerCapture?.(event.pointerId);
    } catch (_) {}
    moveSpaceShooterToEvent(event);
  }, { passive: false });
  canvas.addEventListener("pointermove", (event) => {
    if (!spaceShooterState || spaceShooterState.status !== "active") return;
    event.preventDefault();
    moveSpaceShooterToEvent(event);
  }, { passive: false });
}

function bindSpaceShooterTouchHold() {
  bindSpaceShooterCanvasTouch();
  if (bindSpaceShooterTouchHold.bound) return;
  bindSpaceShooterTouchHold.bound = true;
  document.addEventListener("pointerdown", (event) => {
    const button = event.target?.closest?.("#space-shooter-game-panel [data-game-touch]");
    const action = button?.dataset.gameTouch || "";
    if (!SPACE_SHOOTER_TOUCH_HOLD_ACTIONS.has(action)) return;
    event.preventDefault();
    spaceShooterSuppressClickUntil = Date.now() + 420;
    spaceShooterTouchPointers.set(event.pointerId, action);
    button.classList.add("is-held");
    try {
      button.setPointerCapture?.(event.pointerId);
    } catch (_) {}
    setSpaceShooterTouchAction(action, true);
  }, { passive: false });
  ["pointerup", "pointercancel", "lostpointercapture"].forEach((type) => {
    document.addEventListener(type, (event) => {
      const button = event.target?.closest?.("#space-shooter-game-panel [data-game-touch]");
      if (button) button.classList.remove("is-held");
      releaseSpaceShooterTouch(event.pointerId);
    });
  });
}

function drawSpaceShooterBackdrop(ctx, canvas, state) {
  const tick = Number(state?.tick || 0);
  const gradient = ctx.createLinearGradient(0, 0, 0, canvas.height);
  gradient.addColorStop(0, "#06111f");
  gradient.addColorStop(0.55, "#10183a");
  gradient.addColorStop(1, "#07111f");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  const planetX = 52 + Math.sin(tick / 180) * 10;
  const planetY = 84 + (tick * 0.16) % 520;
  ctx.fillStyle = "rgba(56,189,248,.16)";
  ctx.beginPath();
  ctx.arc(planetX, planetY % (canvas.height + 120) - 60, 42, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = "rgba(147,197,253,.16)";
  ctx.lineWidth = 5;
  ctx.beginPath();
  ctx.ellipse(planetX, planetY % (canvas.height + 120) - 60, 58, 16, -0.22, 0, Math.PI * 2);
  ctx.stroke();
  ctx.lineWidth = 1;

  ctx.fillStyle = "rgba(255,255,255,.2)";
  for (let i = 0; i < 72; i += 1) {
    const x = (i * 67 + tick * (1.2 + (i % 3) * 0.35)) % canvas.width;
    const y = (i * 41 + tick * (2.1 + (i % 4) * 0.24)) % canvas.height;
    ctx.fillRect(x, y, i % 5 === 0 ? 3 : 2, i % 7 === 0 ? 3 : 2);
  }

  for (let i = 0; i < 10; i += 1) {
    const x = (i * 91 + tick * 0.7) % (canvas.width + 80) - 40;
    const y = 80 + ((i * 53 + tick * 0.9) % 250);
    if (drawSpaceShooterImage(ctx, "meteor", x, y, 24 + (i % 3) * 6, 18 + (i % 2) * 4, {
      alpha: 0.34,
      rotation: i * 0.7 + tick / 130,
    })) continue;
    ctx.fillStyle = i % 2 ? "rgba(148,163,184,.18)" : "rgba(100,116,139,.22)";
    ctx.beginPath();
    ctx.ellipse(x, y, 8 + (i % 3) * 3, 5 + (i % 2) * 2, i, 0, Math.PI * 2);
    ctx.fill();
  }
}

function drawSpaceShooterPlayerShip(ctx, x, y, tick = 0) {
  if (drawSpaceShooterImage(ctx, "player", x, y, 58, 70)) {
    ctx.fillStyle = `rgba(34,197,94,${0.34 + Math.sin(tick / 4) * 0.12})`;
    ctx.beginPath();
    ctx.moveTo(x - 10, y + 27);
    ctx.lineTo(x - 4, y + 46);
    ctx.lineTo(x + 2, y + 27);
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(x + 10, y + 27);
    ctx.lineTo(x + 4, y + 46);
    ctx.lineTo(x - 2, y + 27);
    ctx.fill();
    return;
  }
  ctx.save();
  ctx.translate(x, y);
  ctx.fillStyle = "rgba(59,130,246,.28)";
  ctx.beginPath();
  ctx.moveTo(0, 46);
  ctx.lineTo(-26, 18);
  ctx.lineTo(0, 27);
  ctx.lineTo(26, 18);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = "#4d7dff";
  ctx.beginPath();
  ctx.moveTo(0, -22);
  ctx.lineTo(-18, 18);
  ctx.lineTo(-9, 36);
  ctx.lineTo(0, 27);
  ctx.lineTo(9, 36);
  ctx.lineTo(18, 18);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = "#93c5fd";
  ctx.beginPath();
  ctx.moveTo(0, -8);
  ctx.lineTo(-7, 11);
  ctx.lineTo(0, 17);
  ctx.lineTo(7, 11);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = "#22c55e";
  ctx.fillRect(-18, 17, 7, 16);
  ctx.fillRect(11, 17, 7, 16);
  ctx.fillStyle = `rgba(34,197,94,${0.34 + Math.sin(tick / 4) * 0.12})`;
  ctx.beginPath();
  ctx.moveTo(-13, 35);
  ctx.lineTo(-7, 49);
  ctx.lineTo(-2, 35);
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(13, 35);
  ctx.lineTo(7, 49);
  ctx.lineTo(2, 35);
  ctx.fill();
  ctx.restore();
}

function drawSpaceShooterEnemyShip(ctx, enemy, tick = 0) {
  const imageKey = enemy.type === "gunner" ? "gunner" : enemy.type === "evader" ? "evader" : "striker";
  if (drawSpaceShooterImage(ctx, imageKey, enemy.x, enemy.y, 44, 44, {
    rotation: Math.PI + Math.sin((enemy.phase || 0) + tick / 24) * 0.08,
  })) {
    if (tick < Number(enemy.dodgeUntil || 0)) {
      ctx.strokeStyle = "rgba(125,211,252,.72)";
      ctx.beginPath();
      ctx.arc(enemy.x, enemy.y + 1, 22, 0, Math.PI * 2);
      ctx.stroke();
    }
    return;
  }
  const tone = enemy.type === "gunner" ? "#f97316" : enemy.type === "evader" ? "#a78bfa" : "#ff4f6d";
  const trim = enemy.type === "gunner" ? "#fed7aa" : enemy.type === "evader" ? "#ddd6fe" : "#fecdd3";
  ctx.save();
  ctx.translate(enemy.x, enemy.y);
  ctx.rotate(Math.sin((enemy.phase || 0) + tick / 24) * 0.08);
  ctx.fillStyle = tone;
  ctx.beginPath();
  ctx.moveTo(0, 20);
  ctx.lineTo(-18, -9);
  ctx.lineTo(-7, -17);
  ctx.lineTo(0, -7);
  ctx.lineTo(7, -17);
  ctx.lineTo(18, -9);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = trim;
  ctx.fillRect(-11, -6, 22, 6);
  ctx.fillStyle = "rgba(15,23,42,.42)";
  ctx.fillRect(-4, 1, 8, 16);
  if (enemy.type === "gunner") {
    ctx.fillStyle = "#111827";
    ctx.fillRect(-19, 4, 6, 14);
    ctx.fillRect(13, 4, 6, 14);
  }
  ctx.restore();
  if (tick < Number(enemy.dodgeUntil || 0)) {
    ctx.strokeStyle = "rgba(125,211,252,.72)";
    ctx.beginPath();
    ctx.arc(enemy.x, enemy.y + 1, 22, 0, Math.PI * 2);
    ctx.stroke();
  }
}

function drawSpaceShooterBossShip(ctx, boss) {
  if (drawSpaceShooterImage(ctx, "boss", boss.x, boss.y, 116, 86, { rotation: Math.PI })) return;
  ctx.save();
  ctx.translate(boss.x, boss.y);
  ctx.fillStyle = "#db2777";
  ctx.beginPath();
  ctx.moveTo(0, 40);
  ctx.lineTo(-54, -8);
  ctx.lineTo(-30, -34);
  ctx.lineTo(-10, -20);
  ctx.lineTo(0, -36);
  ctx.lineTo(10, -20);
  ctx.lineTo(30, -34);
  ctx.lineTo(54, -8);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = "#f472b6";
  ctx.fillRect(-33, -8, 66, 9);
  ctx.fillStyle = "#fef3c7";
  ctx.fillRect(-20, 8, 40, 8);
  ctx.fillStyle = "rgba(15,23,42,.46)";
  ctx.fillRect(-45, 4, 16, 22);
  ctx.fillRect(29, 4, 16, 22);
  ctx.restore();
}

function drawSpaceShooterPowerup(ctx, powerup) {
  ctx.save();
  ctx.translate(powerup.x, powerup.y);
  ctx.rotate(powerup.phase);
  const color = powerup.type === "weapon" ? "#86efac" : powerup.type === "shield" ? "#93c5fd" : "#fef08a";
  const imageKey = powerup.type === "shield" ? "powerupShield" : "powerupWeapon";
  if (drawSpaceShooterImage(ctx, imageKey, 0, 0, 28, 28)) {
    ctx.rotate(-powerup.phase);
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.arc(0, 0, 16, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
    return;
  }
  ctx.fillStyle = "rgba(15,23,42,.62)";
  ctx.fillRect(-11, -11, 22, 22);
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(0, 0, 9, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = "rgba(255,255,255,.72)";
  ctx.strokeRect(-7, -7, 14, 14);
  ctx.fillStyle = "#0f172a";
  ctx.font = "700 9px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(powerup.type === "weapon" ? "W" : powerup.type === "shield" ? "S" : "L", 0, 1);
  ctx.restore();
}

function renderSpaceShooterBoard() {
  const canvas = $("space-shooter-board");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  drawSpaceShooterBackdrop(ctx, canvas, spaceShooterState);
  if (!spaceShooterState) {
    ctx.fillStyle = "rgba(214,226,240,.75)";
    ctx.font = "16px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("按「開始」後出擊", canvas.width / 2, canvas.height / 2);
    return;
  }
  const state = spaceShooterState;
  if (state.shield > 0) {
    ctx.strokeStyle = "rgba(147,197,253,.7)";
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.arc(state.playerX, 452, 28 + Math.sin(state.tick * 0.18) * 2, 0, Math.PI * 2);
    ctx.stroke();
    ctx.lineWidth = 1;
  }
  drawSpaceShooterPlayerShip(ctx, state.playerX, 452, state.tick);
  state.bullets.forEach((bullet) => {
    ctx.fillStyle = "#86efac";
    ctx.beginPath();
    ctx.arc(bullet.x, bullet.y, 3.2, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,.86)";
    ctx.beginPath();
    ctx.arc(bullet.x, bullet.y, 1.15, 0, Math.PI * 2);
    ctx.fill();
  });
  state.enemyBullets.forEach((bullet) => {
    ctx.fillStyle = bullet.color || "#facc15";
    ctx.beginPath();
    ctx.arc(bullet.x, bullet.y, 4.2, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,.88)";
    ctx.beginPath();
    ctx.arc(bullet.x, bullet.y, 1.35, 0, Math.PI * 2);
    ctx.fill();
  });
  state.enemies.forEach((enemy) => drawSpaceShooterEnemyShip(ctx, enemy, state.tick));
  if (state.boss) {
    drawSpaceShooterBossShip(ctx, state.boss);
    ctx.fillStyle = "rgba(15,23,42,.78)";
    ctx.fillRect(52, 28, canvas.width - 104, 8);
    ctx.fillStyle = "#fb7185";
    ctx.fillRect(52, 28, (canvas.width - 104) * Math.max(0, state.boss.hp / state.boss.maxHp), 8);
  }
  state.powerups.forEach((powerup) => drawSpaceShooterPowerup(ctx, powerup));
  ctx.fillStyle = "rgba(226,232,240,.84)";
  ctx.font = "12px sans-serif";
  ctx.textAlign = "start";
  ctx.fillText(`weapon ${state.weaponLevel} shield ${state.shield}`, 12, 18);
  if (state.status === "finished") {
    ctx.fillStyle = "rgba(7,17,31,.72)";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#ff4f6d";
    ctx.font = "bold 36px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("GAME OVER", canvas.width / 2, canvas.height / 2 - 18);
    ctx.fillStyle = "rgba(214,226,240,.85)";
    ctx.font = "15px sans-serif";
    ctx.fillText(`分數 ${Number(state.score || 0).toLocaleString()}`, canvas.width / 2, canvas.height / 2 + 18);
  }
}

function updateSpaceShooterStatus(prefix = "") {
  const status = $("space-shooter-status");
  if (!status) return;
  if (!spaceShooterState) {
    status.textContent = "按開始後出擊，最高分列入排行榜。";
    return;
  }
  const score = Number(spaceShooterState.score || 0).toLocaleString();
  const time = formatSoloGameTime(soloElapsedMs(spaceShooterState));
  if (spaceShooterState.status === "finished") {
    status.textContent = `任務結束 · 分數 ${score} · 武器 ${spaceShooterState.weaponLevel} · 時間 ${time}`;
  } else {
    status.textContent = `${prefix ? `${prefix} ` : ""}分數 ${score} · 生命 ${spaceShooterState.lives} · 護盾 ${spaceShooterState.shield} · 武器 ${spaceShooterState.weaponLevel} · 敵方閃避 ${spaceShooterState.enemyDodgeEnabled ? "on" : "off"} · 時間 ${time}`;
  }
}

(function () {
  window.registerHackmeGameViewModule?.({
    key: "space_shooter",
    panelIds: ["space-shooter-game-panel"],
    ensure() {
      bindSpaceShooterTouchHold();
      if (!spaceShooterState) {
        renderSpaceShooterBoard();
        updateSpaceShooterStatus();
      }
    },
    updateStatus() {
      bindSpaceShooterTouchHold();
      updateSpaceShooterStatus();
    },
    isActive() {
      return !!spaceShooterState && spaceShooterState.status === "active";
    },
    suspend() {
      suspendSpaceShooterGame();
    },
    leaderboardPath() {
      return "/games/space_shooter/solo-leaderboard";
    },
    dispatch(type, event) {
      if (type === "click" && event.target?.closest?.("#space-shooter-new-btn")) {
        startSpaceShooterGame();
        bindSpaceShooterTouchHold();
        return true;
      }
      if (type === "change" && event.target?.closest?.("#space-shooter-enemy-dodge")) {
        if (spaceShooterState?.status === "active") {
          spaceShooterState.enemyDodgeEnabled = spaceShooterEnemyDodgeEnabled();
          spaceShooterState.enemies.forEach((enemy) => {
            enemy.dodgeUntil = 0;
            enemy.dodgeVx = 0;
          });
          updateSpaceShooterStatus("難度設定已更新。");
        } else {
          updateSpaceShooterStatus();
        }
        return true;
      }
      const touchBtn = type === "click" ? event.target?.closest?.("[data-game-touch]") : null;
      const action = touchBtn?.dataset.gameTouch || "";
      if (touchBtn && Date.now() < spaceShooterSuppressClickUntil && SPACE_SHOOTER_TOUCH_HOLD_ACTIONS.has(action)) return true;
      if (action === "shooter-left") { nudgeSpaceShooter(-34); return true; }
      if (action === "shooter-right") { nudgeSpaceShooter(34); return true; }
      if (action === "shooter-fire") { shootSpaceShooter(); return true; }
      if (type === "keydown" && spaceShooterState?.status === "active" && ["ArrowLeft", "ArrowRight", " ", "a", "A", "d", "D"].includes(event.key)) {
        event.preventDefault();
        spaceShooterState.keys[event.key] = true;
        return true;
      }
      if (type === "keyup" && spaceShooterState?.keys && ["ArrowLeft", "ArrowRight", " ", "a", "A", "d", "D"].includes(event.key)) {
        spaceShooterState.keys[event.key] = false;
        return true;
      }
      return false;
    },
  });
}());
