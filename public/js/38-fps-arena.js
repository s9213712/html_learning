'use strict';

const FPS_ARENA_MODES = {
  aim: { label: "Aim Trainer", seconds: 60, health: 100 },
  pve: { label: "PvE Arena", seconds: 90, health: 100 },
  bomb: { label: "Bomb Defuse", seconds: 75, health: 100 },
  bot: { label: "Bot Match", seconds: 90, health: 100 },
  coop: { label: "Co-op PvE", seconds: 120, health: 100 },
  pvp: { label: "PvP Duel", seconds: 120, health: 100 },
  br: { label: "Battle Royale", seconds: 180, health: 100 },
};
const FPS_ARENA_WEAPONS = [
  { key: "pistol", label: "Pistol", mag: 12, reserve: 24, delay: 190, recoil: 0.01, damage: 1, spread: 0.0035 },
  { key: "rifle", label: "Rifle", mag: 30, reserve: 90, delay: 130, recoil: 0.012, damage: 1, spread: 0.002 },
  { key: "smg", label: "SMG", mag: 36, reserve: 144, delay: 82, recoil: 0.008, damage: 1, spread: 0.004 },
  { key: "marksman", label: "DMR", mag: 12, reserve: 48, delay: 260, recoil: 0.022, damage: 2, spread: 0.001 },
  { key: "shotgun", label: "Shotgun", mag: 8, reserve: 40, delay: 520, recoil: 0.038, damage: 3, spread: 0.018 },
  { key: "rail", label: "Rail Rifle", mag: 6, reserve: 30, delay: 620, recoil: 0.045, damage: 4, spread: 0.0005 },
];

const FPS_ARENA_LEVELS = [
  {
    key: "warehouse",
    label: "第 1 關 貨櫃倉庫",
    theme: { background: 0x52616f, floor: 0x3f4c5c, grid: 0x38bdf8, fog: 0x6f8494 },
    weapons: ["rifle", "smg"],
    roles: ["raider", "assault", "flanker"],
    spawnInterval: 2700,
    maxEnemies: 7,
    enemyHp: 0,
    enemySpeed: 1,
    boss: { label: "鎮暴隊長", role: "juggernaut", hp: 9, score: 900, atMs: 36000 },
  },
  {
    key: "reactor",
    label: "第 2 關 反應爐中庭",
    theme: { background: 0x385a62, floor: 0x25505a, grid: 0x22d3ee, fog: 0x5f8e97 },
    weapons: ["rifle", "smg", "shotgun"],
    roles: ["assault", "flanker", "marksman", "engineer"],
    spawnInterval: 2250,
    maxEnemies: 8,
    enemyHp: 1,
    enemySpeed: 1.08,
    boss: { label: "反應爐工程兵", role: "engineer", hp: 12, score: 1150, atMs: 33000 },
  },
  {
    key: "subway",
    label: "第 3 關 地鐵月台",
    theme: { background: 0x4a4b50, floor: 0x3e424b, grid: 0xfacc15, fog: 0x77736a },
    weapons: ["rifle", "smg", "marksman", "shotgun"],
    roles: ["flanker", "marksman", "engineer", "raider"],
    spawnInterval: 2050,
    maxEnemies: 9,
    enemyHp: 1,
    enemySpeed: 1.14,
    boss: { label: "月台狙擊手", role: "marksman", hp: 14, score: 1300, atMs: 30000 },
  },
  {
    key: "citadel",
    label: "第 4 關 核心堡壘",
    theme: { background: 0x51445e, floor: 0x433456, grid: 0xc4b5fd, fog: 0x7d6f91 },
    weapons: ["rifle", "smg", "marksman", "shotgun", "rail"],
    roles: ["juggernaut", "engineer", "marksman", "flanker"],
    spawnInterval: 1750,
    maxEnemies: 10,
    enemyHp: 2,
    enemySpeed: 1.2,
    boss: { label: "核心指揮官", role: "juggernaut", hp: 18, score: 1800, atMs: 27000 },
  },
];

const FPS_ARENA_STANCES = {
  stand: { label: "站立", eye: 1.65, speed: 6.2, radius: 0.42, breathe: 1 },
  crouch: { label: "蹲下", eye: 1.18, speed: 3.8, radius: 0.34, breathe: 0.72 },
  prone: { label: "匍匐", eye: 0.72, speed: 1.65, radius: 0.27, breathe: 0.48 },
};

let fpsArenaState = null;
let fpsArenaRaf = null;
let fpsArenaResizeObserver = null;
let fpsArenaPointerDragging = false;
let fpsArenaLastPointer = null;
let fpsArenaTouchPointerId = null;
let fpsArenaTouchMoved = false;
let fpsArenaAudioContext = null;
const FPS_ARENA_NORMAL_FOV = 72;
const FPS_ARENA_SNIPER_FOV = 38;
const FPS_ARENA_SNIPER_LOOK_SCALE = 0.42;
const FPS_ARENA_SNIPER_SPREAD_SCALE = 0.34;
const FPS_ARENA_SCOPE_SWAY = 0.0036;
const FPS_ARENA_BOT_FIRE_RANGE = 18;
const FPS_ARENA_PLAYER_RADIUS = 0.42;
const FPS_ARENA_MULTIPLAYER_SYNC_MS = 180;
const FPS_ARENA_AI_ROLES = {
  raider: {
    enemyColor: 0xf43f5e,
    botColor: 0x38bdf8,
    hpBonus: 0,
    speedScale: 1.22,
    preferredRange: 1.7,
    retreatRange: 0.65,
    fireRange: 7.5,
    fireDelayMin: 920,
    fireDelayMax: 1320,
    projectileSpread: 0.42,
    distanceSpread: 0.026,
    damage: 7,
    coverBias: 0.2,
    flankBias: 0.55,
    canShoot: false,
  },
  assault: {
    enemyColor: 0xfb7185,
    botColor: 0x38bdf8,
    hpBonus: 0,
    speedScale: 1.04,
    preferredRange: 7.5,
    retreatRange: 2.4,
    fireRange: 14.5,
    fireDelayMin: 650,
    fireDelayMax: 1080,
    projectileSpread: 0.27,
    distanceSpread: 0.018,
    damage: 8,
    coverBias: 0.42,
    flankBias: 0.36,
    canShoot: true,
  },
  flanker: {
    enemyColor: 0xfdba74,
    botColor: 0x67e8f9,
    hpBonus: 0,
    speedScale: 1.14,
    preferredRange: 5.8,
    retreatRange: 1.8,
    fireRange: 13.5,
    fireDelayMin: 720,
    fireDelayMax: 1180,
    projectileSpread: 0.34,
    distanceSpread: 0.022,
    damage: 7,
    coverBias: 0.35,
    flankBias: 0.9,
    canShoot: true,
  },
  marksman: {
    enemyColor: 0xbae6fd,
    botColor: 0xbfdbfe,
    hpBonus: -1,
    speedScale: 0.88,
    preferredRange: 13.5,
    retreatRange: 6.5,
    fireRange: FPS_ARENA_BOT_FIRE_RANGE + 3,
    fireDelayMin: 920,
    fireDelayMax: 1480,
    projectileSpread: 0.12,
    distanceSpread: 0.012,
    damage: 12,
    coverBias: 0.86,
    flankBias: 0.18,
    canShoot: true,
  },
  engineer: {
    enemyColor: 0xfde68a,
    botColor: 0xfacc15,
    hpBonus: 1,
    speedScale: 0.92,
    preferredRange: 10.5,
    retreatRange: 4.2,
    fireRange: 16.5,
    fireDelayMin: 760,
    fireDelayMax: 1200,
    projectileSpread: 0.3,
    distanceSpread: 0.02,
    damage: 9,
    coverBias: 0.72,
    flankBias: 0.28,
    canShoot: true,
  },
  juggernaut: {
    enemyColor: 0xc4b5fd,
    botColor: 0xa78bfa,
    hpBonus: 4,
    speedScale: 0.78,
    preferredRange: 5.6,
    retreatRange: 1.2,
    fireRange: 13.2,
    fireDelayMin: 520,
    fireDelayMax: 880,
    projectileSpread: 0.38,
    distanceSpread: 0.018,
    damage: 11,
    coverBias: 0.26,
    flankBias: 0.12,
    canShoot: true,
  },
};

function fpsArenaMode() {
  const selected = $("fps-arena-mode")?.value || "aim";
  return FPS_ARENA_MODES[selected] ? selected : "aim";
}

function fpsArenaModeLabel(mode) {
  return FPS_ARENA_MODES[mode]?.label || "Aim Trainer";
}

function fpsArenaDifficultyKey() {
  return `${fpsArenaLevel().key}-${fpsArenaMode()}`;
}

function fpsArenaLevel() {
  const selected = $("fps-arena-level")?.value || "warehouse";
  return FPS_ARENA_LEVELS.find((level) => level.key === selected) || FPS_ARENA_LEVELS[0];
}

function fpsArenaWeaponByKey(key) {
  return FPS_ARENA_WEAPONS.find((weapon) => weapon.key === key) || FPS_ARENA_WEAPONS[0];
}

function fpsArenaStanceMeta(state) {
  return FPS_ARENA_STANCES[state?.stance] || FPS_ARENA_STANCES.stand;
}

function fpsArenaDesiredStance(state) {
  if (state.keys.x || state.keys.X || state.keys.z || state.keys.Z || state.mobileProne) return "prone";
  if (state.keys.c || state.keys.C || state.keys.Control || state.keys.ctrl || state.mobileCrouch) return "crouch";
  return "stand";
}

function fpsArenaPlayerHitRadius(state) {
  return fpsArenaStanceMeta(state).radius;
}

function isFpsArenaActive() {
  return fpsArenaState?.status === "active";
}

function fpsArenaFormatTime(ms) {
  return formatSoloGameTime(Math.max(0, ms || 0));
}

function fpsArenaScoreLine(state) {
  if (!state) return "選擇模式後開始，最高分列入排行榜。";
  const elapsed = Math.max(0, Date.now() - state.startedAt);
  const remaining = Math.max(0, state.durationMs - elapsed);
  const peer = state.multiplayer?.peer?.username ? ` · 對手/隊友 ${state.multiplayer.peer.username}` : "";
  const boss = state.bossActive && !state.bossDefeated ? ` · Boss ${state.bossLabel || ""}` : "";
  return `${state.level?.label || "第 1 關"} · ${fpsArenaModeLabel(state.mode)}${peer}${boss} · 分數 ${Number(state.score || 0).toLocaleString()} · 命中 ${state.hits}/${state.shots} · 生命 ${Math.max(0, Math.ceil(state.health))} · ${fpsArenaFormatTime(remaining)}`;
}

function updateFpsArenaHud() {
  const hud = $("fps-arena-hud");
  if (!hud) return;
  if (!fpsArenaState) {
    if (hud.textContent !== "尚未開始") hud.textContent = "尚未開始";
    return;
  }
  const state = fpsArenaState;
  const now = performance.now();
  const elapsed = Math.max(0, Date.now() - state.startedAt);
  const remaining = Math.max(0, state.durationMs - elapsed);
  const accuracy = state.shots > 0 ? Math.round((state.hits / state.shots) * 100) : 0;
  const hudHtml = [
    `<span>${sanitize(state.level?.label || "第 1 關")}</span>`,
    `<span>${sanitize(fpsArenaModeLabel(state.mode))}</span>`,
    `<span>分數 ${Number(state.score || 0).toLocaleString()}</span>`,
    `<span>命中率 ${accuracy}%</span>`,
    `<span>生命 ${Math.max(0, Math.ceil(state.health))}</span>`,
    `<span>防具 ${Math.max(0, Math.round(state.armor || 0))}</span>`,
    `<span>${sanitize(state.weapon?.label || "Rifle")} ${state.ammo}/${state.reserve}${state.reloadingUntil > performance.now() ? " 換彈" : ""}</span>`,
    state.sniperMode ? "<span>狙擊模式</span>" : "",
    `<span>體力 ${Math.round(state.stamina ?? 100)}%</span>`,
    `<span>${sanitize(fpsArenaStanceMeta(state).label)}${state.coverState?.active ? " · 掩體" : ""}</span>`,
    `<span>時間 ${fpsArenaFormatTime(remaining)}</span>`,
    state.mode === "br" && state.zone ? `<span>安全區 ${state.zone.radius.toFixed(1)}m</span>` : "",
    state.bossActive && !state.bossDefeated ? `<span>Boss ${sanitize(state.bossLabel || "")}</span>` : "",
    state.mode === "bomb" ? `<span>拆彈 ${Math.min(100, Math.round(state.defuseProgress || 0))}%</span>` : "",
    state.multiplayer ? `<span>${state.mode === "pvp" ? "PvP" : "Co-op"} ${sanitize(state.multiplayer.peer?.username || "同步中")}</span>` : "",
  ].join("");
  if (hudHtml !== state.lastHudHtml && now >= Number(state.nextHudDomAt || 0)) {
    hud.innerHTML = hudHtml;
    state.lastHudHtml = hudHtml;
    state.nextHudDomAt = now + 100;
  }
}

function updateFpsArenaStatus(prefix = "") {
  const status = $("fps-arena-status");
  if (!status) return;
  if (!fpsArenaState) {
    const level = fpsArenaLevel();
    const weapons = level.weapons.map((key) => fpsArenaWeaponByKey(key).label).join(" / ");
    status.textContent = `${level.label} · 解鎖武器 ${weapons} · 選擇模式後開始。`;
    return;
  }
  const state = fpsArenaState;
  const line = fpsArenaScoreLine(state);
  status.textContent = state.status === "finished"
    ? `任務結束 · ${fpsArenaModeLabel(state.mode)} · 分數 ${Number(state.score || 0).toLocaleString()} · 命中 ${state.hits}/${state.shots}`
    : `${prefix ? `${prefix} ` : ""}${line}`;
  updateFpsArenaHud();
}

function disposeFpsArenaScene() {
  if (fpsArenaRaf) {
    cancelAnimationFrame(fpsArenaRaf);
    fpsArenaRaf = null;
  }
  if (fpsArenaResizeObserver) {
    fpsArenaResizeObserver.disconnect();
    fpsArenaResizeObserver = null;
  }
  if (fpsArenaState?.renderer) {
    fpsArenaState.renderer.dispose();
    const canvas = fpsArenaState.renderer.domElement;
    if (canvas?.parentNode) canvas.parentNode.removeChild(canvas);
  }
  const stage = $("fps-arena-stage");
  if (stage) {
    stage.classList.remove("is-sniper-mode");
    stage.dataset.aim = "hip";
  }
}

function fpsArenaMaterial(color, roughness = 0.78, metalness = 0.06) {
  return new THREE.MeshStandardMaterial({ color, roughness, metalness });
}

function fpsArenaGlowMaterial(color, opacity = 1, intensity = 0.65) {
  return new THREE.MeshStandardMaterial({
    color,
    emissive: color,
    emissiveIntensity: intensity,
    roughness: 0.48,
    metalness: 0.16,
    transparent: opacity < 1,
    opacity,
  });
}

function fpsArenaAddBox(scene, x, y, z, sx, sy, sz, color) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(sx, sy, sz), fpsArenaMaterial(color));
  mesh.position.set(x, y, z);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  scene.add(mesh);
  return mesh;
}

function fpsArenaAddCylinder(scene, x, y, z, radius, height, color) {
  const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, height, 20), fpsArenaMaterial(color));
  mesh.position.set(x, y, z);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  scene.add(mesh);
  return mesh;
}

function fpsArenaPickSpawnPoint(state) {
  const points = Array.isArray(state?.spawnPoints) ? state.spawnPoints : [];
  if (points.length) {
    const farPoints = points.filter((point) => !state.player || Math.hypot(point.x - state.player.x, point.z - state.player.z) > 7);
    const pool = farPoints.length ? farPoints : points;
    return pool[Math.floor(Math.random() * pool.length)];
  }
  return { x: Math.random() * 12 - 6, z: -7 - Math.random() * 16 };
}

function fpsArenaAiRoleMeta(role) {
  return FPS_ARENA_AI_ROLES[role] || FPS_ARENA_AI_ROLES.assault;
}

function fpsArenaPickCombatRole(state, kind, requestedRole = null) {
  if (kind === "target") return "target";
  if (requestedRole && FPS_ARENA_AI_ROLES[requestedRole]) return requestedRole;
  const levelRoles = Array.isArray(state?.level?.roles) && state.level.roles.length ? state.level.roles : null;
  const sequence = kind === "bot"
    ? ["assault", "marksman", "flanker", "assault", "marksman"]
    : (levelRoles || ["raider", "assault", "flanker", "assault", "raider"]);
  const index = Number(state.aiSpawnCounter || 0);
  state.aiSpawnCounter = index + 1;
  return sequence[index % sequence.length];
}

function fpsArenaRegisterHumanPart(state, root, mesh, part, damage = 1, scoreBonus = 0) {
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  mesh.userData = {
    root,
    kind: root.userData.kind,
    part,
    damage,
    scoreBonus,
  };
  root.add(mesh);
  state.hittables.push(mesh);
  return mesh;
}

function fpsArenaAttachModelPart(root, mesh) {
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  root.add(mesh);
  return mesh;
}

function fpsArenaAddTarget(state, kind, options = {}) {
  const aiRole = fpsArenaPickCombatRole(state, kind, options.role);
  const role = fpsArenaAiRoleMeta(aiRole);
  const torsoColor = kind === "bot" ? role.botColor : kind === "enemy" ? role.enemyColor : 0x22c55e;
  const limbColor = kind === "bot" ? 0x0f5f9e : kind === "enemy" ? 0x7f1d1d : 0x166534;
  const headColor = kind === "bot" ? 0xbfdbfe : kind === "enemy" ? 0xfecaca : 0xdcfce7;
  const root = new THREE.Group();
  const spawn = options.x !== undefined || options.z !== undefined
    ? { x: options.x ?? (Math.random() * 12 - 6), z: options.z ?? (-7 - Math.random() * 16) }
    : fpsArenaPickSpawnPoint(state);
  root.position.set(spawn.x, options.y ?? 1.05, spawn.z);
  const baseHp = options.hp ?? (kind === "target" ? 1 : 2);
  const hp = kind === "target" ? baseHp : Math.max(1, baseHp + role.hpBonus + Number(state.level?.enemyHp || 0));
  root.userData = {
    kind,
    role: aiRole,
    boss: Boolean(options.boss),
    bossLabel: options.bossLabel || "",
    hp,
    maxHp: hp,
    score: options.score || (kind === "target" ? 120 : 180),
    speed: (options.speed ?? 1) * (kind === "target" ? 1 : role.speedScale) * Number(state.level?.enemySpeed || 1),
    aiState: kind === "target" ? "target" : "advance",
    aiTarget: null,
    lastKnownPlayer: null,
    lastSeenAt: 0,
    nextThinkAt: 0,
    coverPoint: null,
    strafeSign: Math.random() > 0.5 ? 1 : -1,
    phase: Math.random() * Math.PI * 2,
    breathPhase: Math.random() * Math.PI * 2,
    baseY: root.position.y,
    lastAttack: 0,
    fireDelay: role.fireDelayMin + Math.random() * (role.fireDelayMax - role.fireDelayMin),
  };
  const torsoMaterial = fpsArenaMaterial(torsoColor, 0.7, 0.04);
  const limbMaterial = fpsArenaMaterial(limbColor, 0.82, 0.02);
  const headMaterial = fpsArenaMaterial(headColor, 0.66, 0.02);
  const armorMaterial = fpsArenaMaterial(kind === "bot" ? 0x0f172a : kind === "enemy" ? 0x1f2937 : 0x14532d, 0.58, 0.16);
  const visorMaterial = fpsArenaMaterial(kind === "bot" ? 0x7dd3fc : kind === "enemy" ? 0xfca5a5 : 0xbbf7d0, 0.42, 0.2);
  const darkMaterial = fpsArenaMaterial(0x020617, 0.74, 0.08);
  fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.SphereGeometry(0.24, 20, 14), headMaterial), "head", 2, 80).position.set(0, 0.92, 0);
  fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.CapsuleGeometry(0.24, 0.65, 4, 12), torsoMaterial), "torso", 1, 0).position.set(0, 0.24, 0);
  const leftArm = fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.CapsuleGeometry(0.075, 0.62, 3, 8), limbMaterial), "arm");
  leftArm.position.set(-0.35, 0.25, 0);
  leftArm.rotation.z = 0.22;
  const rightArm = fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.CapsuleGeometry(0.075, 0.62, 3, 8), limbMaterial), "arm");
  rightArm.position.set(0.35, 0.25, 0);
  rightArm.rotation.z = -0.22;
  const leftLeg = fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.CapsuleGeometry(0.09, 0.72, 3, 8), limbMaterial), "leg");
  leftLeg.position.set(-0.14, -0.55, 0);
  leftLeg.rotation.z = -0.08;
  const rightLeg = fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.CapsuleGeometry(0.09, 0.72, 3, 8), limbMaterial), "leg");
  rightLeg.position.set(0.14, -0.55, 0);
  rightLeg.rotation.z = 0.08;
  const shoulder = fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.BoxGeometry(0.78, 0.12, 0.18), torsoMaterial), "torso");
  shoulder.position.set(0, 0.55, 0);
  fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.56, 0.2, 0.34), armorMaterial)).position.set(0, 0.43, -0.03);
  fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.46, 0.18, 0.32), armorMaterial)).position.set(0, 0.14, -0.04);
  fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.1, 0.2), darkMaterial)).position.set(0, 1.05, -0.02);
  fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.07, 0.08), visorMaterial)).position.set(0, 0.96, -0.2);
  const leftPad = fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.24, 0.12, 0.25), armorMaterial));
  leftPad.position.set(-0.46, 0.56, 0);
  leftPad.rotation.z = 0.22;
  const rightPad = fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.24, 0.12, 0.25), armorMaterial));
  rightPad.position.set(0.46, 0.56, 0);
  rightPad.rotation.z = -0.22;
  fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.18, 0.18), armorMaterial)).position.set(-0.33, 0.03, -0.02);
  fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.18, 0.18), armorMaterial)).position.set(0.33, 0.03, -0.02);
  const weaponMesh = fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.12, 0.72), darkMaterial));
  weaponMesh.position.set(0.34, 0.23, -0.42);
  weaponMesh.rotation.x = -0.1;
  const weaponBarrel = fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.08, 0.42), darkMaterial));
  weaponBarrel.position.set(0.34, 0.25, -0.96);
  if (options.boss) {
    fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.68, 0.28, 0.38), armorMaterial)).position.set(0, 0.58, -0.05);
    fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.36, 0.22), darkMaterial)).position.set(0, 0.22, 0.28);
  }
  if (options.scale) root.scale.setScalar(options.scale);
  if (options.bossLabel) sceneLabelBillboard(state, root, options.bossLabel);
  state.scene.add(root);
  state.targets.push(root);
  return root;
}

function fpsArenaCreateRemotePlayer(state, peer) {
  const root = new THREE.Group();
  root.position.set(Number(peer?.state?.x || 0), 1.05, Number(peer?.state?.z || -2));
  root.userData = {
    kind: "remote_player",
    userId: Number(peer?.user_id || 0),
    username: peer?.username || "玩家",
    hp: Number(peer?.state?.health || 100),
    maxHp: 100,
    dead: false,
  };
  const bodyMaterial = fpsArenaMaterial(0x22c55e, 0.68, 0.04);
  const limbMaterial = fpsArenaMaterial(0x166534, 0.82, 0.02);
  const headMaterial = fpsArenaMaterial(0xbbf7d0, 0.62, 0.02);
  fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.SphereGeometry(0.24, 20, 14), headMaterial), "head", 2, 80).position.set(0, 0.92, 0);
  fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.CapsuleGeometry(0.24, 0.65, 4, 12), bodyMaterial), "torso", 1, 0).position.set(0, 0.24, 0);
  const leftArm = fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.CapsuleGeometry(0.075, 0.62, 3, 8), limbMaterial), "arm");
  leftArm.position.set(-0.35, 0.25, 0);
  leftArm.rotation.z = 0.22;
  const rightArm = fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.CapsuleGeometry(0.075, 0.62, 3, 8), limbMaterial), "arm");
  rightArm.position.set(0.35, 0.25, 0);
  rightArm.rotation.z = -0.22;
  const leftLeg = fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.CapsuleGeometry(0.09, 0.72, 3, 8), limbMaterial), "leg");
  leftLeg.position.set(-0.14, -0.55, 0);
  leftLeg.rotation.z = -0.08;
  const rightLeg = fpsArenaRegisterHumanPart(state, root, new THREE.Mesh(new THREE.CapsuleGeometry(0.09, 0.72, 3, 8), limbMaterial), "leg");
  rightLeg.position.set(0.14, -0.55, 0);
  sceneLabelBillboard(state, root, root.userData.username);
  state.scene.add(root);
  state.remotePlayer = root;
  return root;
}

function sceneLabelBillboard(state, root, label) {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 64;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "rgba(15,23,42,.72)";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#bbf7d0";
  ctx.font = "700 28px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(String(label || "玩家").slice(0, 12), canvas.width / 2, 42);
  const texture = new THREE.CanvasTexture(canvas);
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true });
  const sprite = new THREE.Sprite(material);
  sprite.position.set(0, 1.38, 0);
  sprite.scale.set(1.5, 0.38, 1);
  root.add(sprite);
}

function fpsArenaPickupLabel(pickup) {
  if (pickup.type === "medkit") return "醫藥箱";
  if (pickup.type === "ammo") return "子彈";
  if (pickup.type === "armor") return "防具";
  if (pickup.type === "weapon") return fpsArenaWeaponByKey(pickup.weaponKey).label;
  return "物資";
}

function fpsArenaCreatePickup(state, type, x, z, options = {}) {
  const root = new THREE.Group();
  root.position.set(x, 0.18, z);
  root.userData = { kind: "pickup", type, weaponKey: options.weaponKey || "", ammoKey: options.ammoKey || "" };
  const color = type === "medkit" ? 0xf8fafc : type === "ammo" ? 0xfacc15 : type === "armor" ? 0x60a5fa : 0x94a3b8;
  const accent = type === "medkit" ? 0xdc2626 : type === "ammo" ? 0x713f12 : type === "armor" ? 0x1e3a8a : 0x111827;
  const body = fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(type === "weapon" ? 0.88 : 0.46, 0.16, type === "weapon" ? 0.18 : 0.38), fpsArenaMaterial(color, 0.64, 0.08)));
  body.position.set(0, 0, 0);
  if (type === "medkit") {
    fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.03, 0.08), fpsArenaMaterial(accent, 0.55, 0.04))).position.set(0, 0.1, 0);
    fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.03, 0.28), fpsArenaMaterial(accent, 0.55, 0.04))).position.set(0, 0.105, 0);
  } else if (type === "armor") {
    fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.28, 0.26, 0.08), fpsArenaMaterial(accent, 0.58, 0.16))).position.set(0, 0.12, 0);
  } else if (type === "weapon") {
    fpsArenaAttachModelPart(root, new THREE.Mesh(new THREE.BoxGeometry(0.42, 0.07, 0.1), fpsArenaMaterial(accent, 0.72, 0.12))).position.set(0.36, 0.08, 0);
  }
  sceneLabelBillboard(state, root, fpsArenaPickupLabel(root.userData));
  root.children.forEach((child) => {
    if (child.isSprite) child.position.y = 0.72;
  });
  state.scene.add(root);
  state.pickups.push(root);
  return root;
}

function fpsArenaSpawnLoot(state) {
  const points = [
    { x: -5.2, z: -6.4 }, { x: 4.8, z: -7.2 }, { x: -7.4, z: -13.8 }, { x: 7.2, z: -15.8 },
    { x: -4.6, z: -22.4 }, { x: 4.9, z: -24.6 }, { x: -1.2, z: -29.2 }, { x: 7.6, z: -29.5 },
  ];
  const guns = (state.level?.weapons || ["rifle", "smg"]).filter((key) => key !== "pistol");
  points.forEach((point, index) => {
    if (index % 4 === 0) fpsArenaCreatePickup(state, "medkit", point.x, point.z);
    else if (index % 4 === 1) fpsArenaCreatePickup(state, "ammo", point.x, point.z, { ammoKey: guns[index % Math.max(1, guns.length)] || "rifle" });
    else if (index % 4 === 2) fpsArenaCreatePickup(state, "armor", point.x, point.z);
    else fpsArenaCreatePickup(state, "weapon", point.x, point.z, { weaponKey: guns[index % Math.max(1, guns.length)] || "rifle" });
  });
}

function fpsArenaCollectPickup(state, pickup) {
  const data = pickup.userData || {};
  const label = fpsArenaPickupLabel(data);
  if (data.type === "medkit") {
    state.health = Math.min(100, state.health + 38);
  } else if (data.type === "armor") {
    state.armor = Math.min(100, Number(state.armor || 0) + 45);
  } else if (data.type === "ammo") {
    const weapon = state.weapon || FPS_ARENA_WEAPONS[0];
    state.reserve += Math.max(18, Math.round(weapon.mag * 1.5));
  } else if (data.type === "weapon") {
    const weapon = fpsArenaWeaponByKey(data.weaponKey || "rifle");
    if (!state.weaponPool.some((item) => item.key === weapon.key)) state.weaponPool.push(weapon);
    state.weaponIndex = state.weaponPool.findIndex((item) => item.key === weapon.key);
    state.weapon = weapon;
    state.ammo = weapon.mag;
    state.reserve = Math.max(state.reserve, weapon.reserve);
  }
  state.score += 45;
  window.recordHackmeGameAchievement?.("fps_arena", "loot-first", "戰場搜刮", "拾取地面物資。");
  updateFpsArenaStatus(`拾取 ${label}。`);
}

function fpsArenaUpdatePickups(state, now) {
  for (let i = state.pickups.length - 1; i >= 0; i -= 1) {
    const pickup = state.pickups[i];
    pickup.rotation.y += 0.85 * (1 / 60);
    pickup.position.y = 0.18 + Math.sin(now * 0.003 + i) * 0.035;
    if (Math.hypot(state.player.x - pickup.position.x, state.player.z - pickup.position.z) > 1.05) continue;
    fpsArenaCollectPickup(state, pickup);
    state.scene.remove(pickup);
    fpsArenaDisposeObject(pickup);
    state.pickups.splice(i, 1);
  }
}

function fpsArenaCreateBattleRoyaleZone(state) {
  const geometry = new THREE.RingGeometry(0.96, 1, 96);
  const material = new THREE.MeshBasicMaterial({ color: 0x60a5fa, transparent: true, opacity: 0.36, side: THREE.DoubleSide });
  const ring = new THREE.Mesh(geometry, material);
  ring.rotation.x = -Math.PI / 2;
  ring.position.set(0, 0.045, -15.5);
  state.scene.add(ring);
  state.zone = {
    center: new THREE.Vector3(0, 0, -15.5),
    radius: 18,
    startRadius: 18,
    endRadius: 4.2,
    shrinkStart: 18000,
    shrinkEnd: Math.max(60000, state.durationMs - 18000),
    mesh: ring,
    lastWarningAt: 0,
  };
}

function fpsArenaUpdateBattleRoyaleZone(state, dt) {
  if (state.mode !== "br" || !state.zone) return;
  const elapsed = Date.now() - state.startedAt;
  const progress = Math.max(0, Math.min(1, (elapsed - state.zone.shrinkStart) / Math.max(1, state.zone.shrinkEnd - state.zone.shrinkStart)));
  const eased = progress * progress * (3 - 2 * progress);
  state.zone.radius = state.zone.startRadius + (state.zone.endRadius - state.zone.startRadius) * eased;
  if (state.zone.mesh) state.zone.mesh.scale.setScalar(state.zone.radius);
  const distance = Math.hypot(state.player.x - state.zone.center.x, state.player.z - state.zone.center.z);
  if (distance > state.zone.radius) {
    const damage = (4 + progress * 10) * dt;
    state.health -= damage;
    if (elapsed - state.zone.lastWarningAt > 1600) {
      state.zone.lastWarningAt = elapsed;
      updateFpsArenaStatus(`毒圈外，正在扣血。安全區半徑 ${state.zone.radius.toFixed(1)}m。`);
    }
  }
}

function fpsArenaUpdateRemotePlayer(state, peer) {
  if (!state?.multiplayer || !peer?.state) return;
  const remote = state.remotePlayer || fpsArenaCreateRemotePlayer(state, peer);
  const p = peer.state || {};
  remote.userData.userId = Number(peer.user_id || remote.userData.userId || 0);
  remote.userData.username = peer.username || remote.userData.username || "玩家";
  remote.userData.hp = Number(p.health ?? remote.userData.hp ?? 100);
  remote.position.set(Number(p.x || 0), 1.05, Number(p.z || -2));
  remote.rotation.y = Number(p.yaw || 0);
  remote.visible = p.status !== "finished" && remote.userData.hp > 0;
}

function fpsArenaCreateBomb(state) {
  const site = Math.random() > 0.5 ? { x: -4.5, z: -14 } : { x: 4.8, z: -18 };
  const bomb = fpsArenaAddBox(state.scene, site.x, 0.45, site.z, 0.9, 0.65, 0.9, 0xfacc15);
  bomb.userData = { kind: "bomb", hp: 999, score: 0 };
  state.bomb = bomb;
  state.hittables.push(bomb);
  const beacon = new THREE.Mesh(new THREE.CylinderGeometry(0.14, 0.14, 2.4, 18), fpsArenaMaterial(0xfacc15, 0.35, 0.12));
  beacon.position.set(site.x, 1.7, site.z);
  state.scene.add(beacon);
}

function fpsArenaBotMuzzlePosition(bot) {
  const forward = new THREE.Vector3(0, 0, 1).applyQuaternion(bot.quaternion).normalize();
  const right = new THREE.Vector3(1, 0, 0).applyQuaternion(bot.quaternion).normalize();
  return bot.position.clone()
    .add(new THREE.Vector3(0, 0.45, 0))
    .add(forward.multiplyScalar(0.34))
    .add(right.multiplyScalar(0.18));
}

function fpsArenaLineOfSightClear(state, from, to) {
  if (!state.cover?.length) return true;
  const direction = to.clone().sub(from);
  const distance = direction.length();
  if (distance <= 0.001) return true;
  direction.normalize();
  const raycaster = new THREE.Raycaster(from, direction, 0.05, distance - 0.2);
  return raycaster.intersectObjects(state.cover, false).length === 0;
}

function fpsArenaAddTracer(state, from, to, hit) {
  const geometry = new THREE.BufferGeometry().setFromPoints([from, to]);
  const material = new THREE.LineBasicMaterial({
    color: hit ? 0xf97316 : 0xfacc15,
    transparent: true,
    opacity: hit ? 0.98 : 0.72,
  });
  const line = new THREE.Line(geometry, material);
  state.scene.add(line);
  state.botTracers.push({ object: line, expiresAt: performance.now() + 140 });
}

function fpsArenaCameraForward(state) {
  return new THREE.Vector3(0, 0, -1).applyQuaternion(state.camera.quaternion).normalize();
}

function fpsArenaPlayerMuzzlePosition(state) {
  const forward = fpsArenaCameraForward(state);
  const right = new THREE.Vector3(1, 0, 0).applyQuaternion(state.camera.quaternion).normalize();
  const down = new THREE.Vector3(0, -1, 0);
  return state.camera.position.clone()
    .add(forward.multiplyScalar(0.44))
    .add(right.multiplyScalar(0.16))
    .add(down.multiplyScalar(0.12));
}

function fpsArenaAddImpactSpark(state, position, color = 0xfef08a) {
  const spark = new THREE.Mesh(
    new THREE.SphereGeometry(0.08, 8, 6),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.95 })
  );
  spark.position.copy(position);
  state.scene.add(spark);
  state.impactEffects.push({ object: spark, expiresAt: performance.now() + 120 });
}

function fpsArenaDisposeObject(object) {
  object?.traverse?.((child) => {
    child.geometry?.dispose?.();
    child.material?.dispose?.();
  });
  object?.geometry?.dispose?.();
  object?.material?.dispose?.();
}

function fpsArenaAddBloodSplatter(state, position, count = 16) {
  if (!position) return;
  for (let i = 0; i < count; i += 1) {
    const droplet = new THREE.Mesh(
      new THREE.SphereGeometry(0.025 + Math.random() * 0.035, 7, 5),
      new THREE.MeshBasicMaterial({ color: 0xb91c1c, transparent: true, opacity: 0.9 })
    );
    droplet.position.copy(position);
    state.scene.add(droplet);
    state.bloodEffects.push({
      object: droplet,
      velocity: new THREE.Vector3(
        (Math.random() - 0.5) * 3.2,
        1.2 + Math.random() * 2.6,
        (Math.random() - 0.5) * 3.2
      ),
      bornAt: performance.now(),
      expiresAt: performance.now() + 640 + Math.random() * 420,
    });
  }
}

function fpsArenaAddPlayerFireEffects(state, hitPoint, hitTarget = false) {
  const muzzle = fpsArenaPlayerMuzzlePosition(state);
  const endpoint = hitPoint || state.camera.position.clone().add(fpsArenaCameraForward(state).multiplyScalar(34));
  fpsArenaAddTracer(state, muzzle, endpoint, hitTarget);
  fpsArenaAddMuzzleFlash(state, muzzle, 0x93c5fd);
  if (hitPoint) fpsArenaAddImpactSpark(state, hitPoint, hitTarget ? 0xf97316 : 0xfef08a);
}

function fpsArenaAddBotProjectile(state, from, to, bot, dist) {
  const role = fpsArenaAiRoleMeta(bot.userData?.role);
  const spread = role.projectileSpread + dist * role.distanceSpread;
  const endpoint = to.clone().add(new THREE.Vector3(
    (Math.random() - 0.5) * spread,
    (Math.random() - 0.5) * spread * 0.7,
    (Math.random() - 0.5) * spread
  ));
  const velocity = endpoint.sub(from).normalize().multiplyScalar(18 + Math.random() * 7);
  const projectile = new THREE.Mesh(
    new THREE.SphereGeometry(0.065, 10, 8),
    new THREE.MeshBasicMaterial({ color: 0xfacc15, transparent: true, opacity: 0.95 })
  );
  projectile.position.copy(from);
  state.scene.add(projectile);
  state.botProjectiles.push({
    object: projectile,
    velocity,
    previous: from.clone(),
    damage: role.damage + Math.random() * 4,
    owner: bot,
    expiresAt: performance.now() + 1800,
  });
}

function fpsArenaAddMuzzleFlash(state, position, color = 0xfef08a) {
  const flash = new THREE.Mesh(
    new THREE.SphereGeometry(0.09, 10, 8),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9 })
  );
  flash.position.copy(position);
  state.scene.add(flash);
  state.botMuzzleFlashes.push({ object: flash, expiresAt: performance.now() + 90 });
}

function fpsArenaEnsureAudio() {
  try {
    const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextCtor) return null;
    if (!fpsArenaAudioContext) fpsArenaAudioContext = new AudioContextCtor();
    if (fpsArenaAudioContext.state === "suspended") fpsArenaAudioContext.resume?.();
    return fpsArenaAudioContext;
  } catch {
    return null;
  }
}

function fpsArenaPlaySound(type) {
  const audio = fpsArenaEnsureAudio();
  if (!audio) return;
  const now = audio.currentTime;
  const gain = audio.createGain();
  const osc = audio.createOscillator();
  const settings = {
    fire: { type: "square", start: 160, end: 58, gain: 0.07, duration: 0.08 },
    botFire: { type: "sawtooth", start: 220, end: 85, gain: 0.045, duration: 0.07 },
    hit: { type: "triangle", start: 520, end: 260, gain: 0.04, duration: 0.09 },
    damage: { type: "sawtooth", start: 90, end: 44, gain: 0.08, duration: 0.12 },
    defuse: { type: "sine", start: 740, end: 980, gain: 0.035, duration: 0.12 },
    scream: { type: "sawtooth", start: 720, end: 115, gain: 0.075, duration: 0.34 },
  }[type] || { type: "square", start: 160, end: 70, gain: 0.05, duration: 0.08 };
  osc.type = settings.type;
  osc.frequency.setValueAtTime(settings.start, now);
  osc.frequency.exponentialRampToValueAtTime(Math.max(1, settings.end), now + settings.duration);
  gain.gain.setValueAtTime(0.0001, now);
  gain.gain.exponentialRampToValueAtTime(settings.gain, now + 0.008);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + settings.duration);
  osc.connect(gain);
  gain.connect(audio.destination);
  osc.start(now);
  osc.stop(now + settings.duration + 0.02);
}

function fpsArenaIsMultiplayer(state) {
  return state?.multiplayer && (state.mode === "coop" || state.mode === "pvp");
}

function fpsArenaQueueMultiplayerEvent(state, event) {
  if (!fpsArenaIsMultiplayer(state)) return;
  state.multiplayer.pendingEvents.push(event);
}

function fpsArenaMultiplayerStatePayload(state) {
  return {
    x: Math.round(state.player.x * 100) / 100,
    y: Math.round(state.player.y * 100) / 100,
    z: Math.round(state.player.z * 100) / 100,
    yaw: Math.round(state.yaw * 1000) / 1000,
    pitch: Math.round(state.pitch * 1000) / 1000,
    stance: state.stance || "stand",
    health: Math.max(0, Math.round(state.health)),
    score: Math.round(state.score || 0),
    shots: state.shots,
    hits: state.hits,
    mode: state.mode,
    status: state.status,
    at: Date.now(),
  };
}

function fpsArenaPlayRemoteGunshot(state, payload = {}, senderName = "對手") {
  const audio = fpsArenaEnsureAudio();
  const dx = Number(payload.x || 0) - state.player.x;
  const dz = Number(payload.z || 0) - state.player.z;
  const distance = Math.max(1, Math.hypot(dx, dz));
  const pan = Math.max(-1, Math.min(1, dx / 12));
  if (audio) {
    const now = audio.currentTime;
    const gain = audio.createGain();
    const osc = audio.createOscillator();
    const panner = audio.createStereoPanner ? audio.createStereoPanner() : null;
    osc.type = "square";
    osc.frequency.setValueAtTime(150, now);
    osc.frequency.exponentialRampToValueAtTime(48, now + 0.09);
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(Math.max(0.012, 0.09 / distance), now + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.16);
    osc.connect(gain);
    if (panner) {
      panner.pan.setValueAtTime(pan, now);
      gain.connect(panner);
      panner.connect(audio.destination);
    } else {
      gain.connect(audio.destination);
    }
    osc.start(now);
    osc.stop(now + 0.18);
  }
  const side = Math.abs(dx) < 2 ? "正前/正後" : (dx > 0 ? "右側" : "左側");
  updateFpsArenaStatus(`聽到 ${senderName} 槍聲：${side}，約 ${Math.round(distance)}m。`);
}

function applyFpsArenaMultiplayerSnapshot(state, snapshot) {
  if (!fpsArenaIsMultiplayer(state) || !snapshot?.room) return;
  const mp = window.hackmeGameMultiplayer;
  state.multiplayer.room = snapshot.room;
  const peer = mp?.peerState?.(snapshot, snapshot.room);
  if (peer) {
    state.multiplayer.peer = peer;
    fpsArenaUpdateRemotePlayer(state, peer);
  }
  const events = Array.isArray(snapshot.events) ? snapshot.events : [];
  events.forEach((event) => {
    const eventId = Number(event.id || 0);
    if (!eventId || state.multiplayer.processedEvents.has(eventId)) return;
    state.multiplayer.processedEvents.add(eventId);
    state.multiplayer.afterEventId = Math.max(state.multiplayer.afterEventId || 0, eventId);
    if (Number(event.sender_user_id) === Number(currentUserId || 0)) return;
    if (event.event_type === "gunshot") {
      fpsArenaPlayRemoteGunshot(state, event.payload || {}, event.sender_username || "對手");
      return;
    }
    const targetId = Number(event.target_user_id || 0);
    if (targetId && targetId !== Number(currentUserId || 0)) return;
    if (event.event_type === "player_hit" || event.event_type === "friendly_fire") {
      const damage = Math.max(1, Math.min(40, Number(event.payload?.damage || 10)));
      fpsArenaApplyIncomingDamage(
        state,
        damage,
        new THREE.Vector3(Number(event.payload?.x || 0), 1.1, Number(event.payload?.z || -4)),
        `${event.sender_username || "對手"} 命中你`
      );
      if (state.health <= 0) finishFpsArenaGame(state.mode === "pvp" ? "pvp_down" : "health");
    }
  });
  if (state.multiplayer.processedEvents.size > 180) {
    state.multiplayer.processedEvents = new Set(Array.from(state.multiplayer.processedEvents).slice(-90));
  }
}

function syncFpsArenaMultiplayer(state, now, { force = false } = {}) {
  if (!fpsArenaIsMultiplayer(state) || state.multiplayer.syncing) return;
  if (!force && now - (state.multiplayer.lastSyncAt || 0) < FPS_ARENA_MULTIPLAYER_SYNC_MS) return;
  const roomId = state.multiplayer.roomId;
  if (!roomId || !window.hackmeGameMultiplayer?.syncRoom) return;
  state.multiplayer.lastSyncAt = now;
  const events = state.multiplayer.pendingEvents.splice(0, 12);
  state.multiplayer.syncing = true;
  window.hackmeGameMultiplayer.syncRoom(roomId, fpsArenaMultiplayerStatePayload(state), events, state.multiplayer.afterEventId || 0)
    .then((snapshot) => applyFpsArenaMultiplayerSnapshot(state, snapshot))
    .catch((err) => {
      state.multiplayer.pendingEvents.unshift(...events);
      state.multiplayer.lastError = err.message || "同步失敗";
    })
    .finally(() => {
      state.multiplayer.syncing = false;
    });
}

function fpsArenaAddShake(state, strength = 1, duration = 130) {
  state.shakeUntil = Math.max(state.shakeUntil || 0, performance.now() + duration);
  state.shakeDuration = Math.max(state.shakeDuration || 0, duration);
  state.shakeStrength = Math.max(state.shakeStrength || 0, strength);
  if (navigator.vibrate && strength >= 1.1) navigator.vibrate(Math.min(45, Math.round(duration / 4)));
}

function fpsArenaUpdateWeaponView(state, now, stage) {
  if (!stage) return;
  const moving = state.keys.w || state.keys.a || state.keys.s || state.keys.d || state.keys.ArrowUp || state.keys.ArrowDown || state.keys.ArrowLeft || state.keys.ArrowRight;
  const stance = state.stance || "stand";
  const bobScale = state.sniperMode ? 0.12 : state.running ? 1.6 : moving ? 1 : 0.28;
  const stanceLift = stance === "prone" ? -18 : stance === "crouch" ? -8 : 0;
  const recoilLift = Math.min(34, (state.recoilKick || 0) * 430);
  const t = now * 0.006;
  stage.dataset.weapon = state.weapon?.key || "rifle";
  stage.dataset.stance = stance;
  stage.dataset.aim = state.sniperMode ? "sniper" : "hip";
  stage.classList.toggle("is-sniper-mode", Boolean(state.sniperMode));
  stage.classList.toggle("is-covered", Boolean(state.coverState?.active));
  stage.style.setProperty("--fps-weapon-x", `${Math.sin(t) * 4.5 * bobScale}px`);
  stage.style.setProperty("--fps-weapon-y", `${stanceLift + Math.abs(Math.cos(t * 0.72)) * 5.5 * bobScale - recoilLift}px`);
  stage.style.setProperty("--fps-weapon-rot", `${Math.sin(t * 0.82) * 1.6 * bobScale - recoilLift * 0.03}deg`);
  stage.style.setProperty("--fps-weapon-scale", stance === "prone" ? "1.08" : stance === "crouch" ? "1.03" : "1");
}

function fpsArenaUpdateSniperMode(state, dt) {
  if (!state?.camera) return;
  const targetFov = state.sniperMode ? FPS_ARENA_SNIPER_FOV : FPS_ARENA_NORMAL_FOV;
  state.cameraFov = Number.isFinite(state.cameraFov) ? state.cameraFov : state.camera.fov || FPS_ARENA_NORMAL_FOV;
  const nextFov = state.cameraFov + (targetFov - state.cameraFov) * Math.min(1, dt * 12);
  if (Math.abs(nextFov - state.camera.fov) > 0.03) {
    state.camera.fov = nextFov;
    state.camera.updateProjectionMatrix();
  }
  state.cameraFov = nextFov;
}

function fpsArenaUpdateFeedback(state, now) {
  const stage = $("fps-arena-stage");
  const remainingShake = Math.max(0, state.shakeUntil - now);
  if (remainingShake > 0) {
    const falloff = remainingShake / Math.max(1, state.shakeDuration || 180);
    const strength = (state.shakeStrength || 0) * falloff;
    state.shakeVector.set(
      Math.sin(now * 0.055 + state.shakePhase) * strength,
      Math.cos(now * 0.071 + state.shakePhase) * strength
    );
  } else {
    state.shakeVector.set(0, 0);
    state.shakeStrength = 0;
  }
  if (stage) {
    state.coverState = fpsArenaCoverProtection(state);
    fpsArenaUpdateWeaponView(state, now, stage);
    stage.style.setProperty("--fps-shake-x", `${state.shakeVector.x * 4.8}px`);
    stage.style.setProperty("--fps-shake-y", `${state.shakeVector.y * 3.2}px`);
    const remainingDamage = Math.max(0, state.damageFlashUntil - now);
    stage.style.setProperty("--fps-damage-opacity", `${Math.min(0.75, remainingDamage / 360)}`);
    stage.style.setProperty("--fps-damage-angle", `${state.damageSourceAngle || 0}rad`);
  }
}

function fpsArenaSetDamageCue(state, from) {
  state.damageFlashUntil = performance.now() + 360;
  const angleToSource = Math.atan2(from.x - state.player.x, state.player.z - from.z);
  state.damageSourceAngle = angleToSource - state.yaw;
  fpsArenaAddShake(state, 1.45, 260);
  fpsArenaPlaySound("damage");
}

function fpsArenaUpdateBotFire(state, bot, dist, now) {
  const role = fpsArenaAiRoleMeta(bot.userData?.role);
  const fireRange = Math.max(role.fireRange || FPS_ARENA_BOT_FIRE_RANGE, bot.userData.kind === "bot" ? FPS_ARENA_BOT_FIRE_RANGE : 0);
  if (!role.canShoot || dist > fireRange || now - bot.userData.lastAttack < bot.userData.fireDelay) return;
  if (!["peekShoot", "suppress", "flank", "advance"].includes(bot.userData.aiState)) return;
  const muzzle = fpsArenaBotMuzzlePosition(bot);
  const aimPoint = state.player.clone().add(new THREE.Vector3(0, -0.15, 0));
  if (!fpsArenaLineOfSightClear(state, muzzle, aimPoint)) return;
  bot.userData.lastAttack = now;
  bot.userData.fireDelay = role.fireDelayMin + Math.random() * (role.fireDelayMax - role.fireDelayMin);
  fpsArenaAddBotProjectile(state, muzzle, aimPoint, bot, dist);
  fpsArenaAddTracer(state, muzzle, aimPoint, false);
  fpsArenaAddMuzzleFlash(state, muzzle);
  fpsArenaPlaySound("botFire");
}

function fpsArenaPointSegmentDistance(point, start, end) {
  const segment = end.clone().sub(start);
  const lengthSq = segment.lengthSq();
  if (lengthSq <= 0.0001) return point.distanceTo(start);
  const t = Math.max(0, Math.min(1, point.clone().sub(start).dot(segment) / lengthSq));
  return point.distanceTo(start.clone().add(segment.multiplyScalar(t)));
}

function fpsArenaProjectileHitsCover(state, start, end) {
  const direction = end.clone().sub(start);
  const distance = direction.length();
  if (distance <= 0.001 || !state.cover?.length) return false;
  direction.normalize();
  const raycaster = new THREE.Raycaster(start, direction, 0, distance);
  return raycaster.intersectObjects(state.cover, false).length > 0;
}

function fpsArenaCoverProtection(state, incoming = null) {
  if (!state?.player || !state.cover?.length) return { active: false, near: false, hard: false, damageScale: 1 };
  const stance = state.stance || "stand";
  let near = false;
  for (const object of state.cover) {
    const box = new THREE.Box3().setFromObject(object);
    const closestX = Math.max(box.min.x, Math.min(state.player.x, box.max.x));
    const closestZ = Math.max(box.min.z, Math.min(state.player.z, box.max.z));
    const distance = Math.hypot(state.player.x - closestX, state.player.z - closestZ);
    if (distance < (stance === "prone" ? 1.45 : 1.05) && box.max.y > 0.48) {
      near = true;
      break;
    }
  }
  const hard = incoming ? fpsArenaProjectileHitsCover(state, incoming, fpsArenaPlayerAimPoint(state)) : false;
  const active = hard || (near && stance !== "stand");
  let damageScale = 1;
  if (hard) damageScale = stance === "prone" ? 0.2 : stance === "crouch" ? 0.32 : 0.48;
  else if (near && stance === "prone") damageScale = 0.45;
  else if (near && stance === "crouch") damageScale = 0.62;
  return { active, near, hard, damageScale };
}

function fpsArenaApplyIncomingDamage(state, amount, source, label = "受到攻擊") {
  const protection = fpsArenaCoverProtection(state, source);
  let finalDamage = Math.max(1, Math.round(Number(amount || 0) * protection.damageScale));
  let armorText = "";
  if (Number(state.armor || 0) > 0) {
    const absorbed = Math.min(Number(state.armor || 0), Math.ceil(finalDamage * 0.45));
    state.armor = Math.max(0, Number(state.armor || 0) - absorbed);
    finalDamage = Math.max(1, finalDamage - Math.round(absorbed * 0.8));
    armorText = absorbed > 0 ? "（防具吸收）" : "";
  }
  state.health -= finalDamage;
  if (source) fpsArenaSetDamageCue(state, source);
  const coverText = protection.active && finalDamage < amount ? "（掩體減傷）" : "";
  updateFpsArenaStatus(`${label}${coverText}${armorText}，-${Math.round(finalDamage)} HP。`);
  return finalDamage;
}

function fpsArenaUpdateBotProjectiles(state, dt, now) {
  const playerCenter = fpsArenaPlayerAimPoint(state);
  const hitRadius = fpsArenaPlayerHitRadius(state);
  for (let i = state.botProjectiles.length - 1; i >= 0; i -= 1) {
    const projectile = state.botProjectiles[i];
    const object = projectile.object;
    projectile.previous.copy(object.position);
    object.position.add(projectile.velocity.clone().multiplyScalar(dt));
    if (fpsArenaProjectileHitsCover(state, projectile.previous, object.position)) {
      fpsArenaAddImpactSpark(state, object.position, 0xfacc15);
      state.scene.remove(object);
      object.geometry?.dispose?.();
      object.material?.dispose?.();
      state.botProjectiles.splice(i, 1);
      continue;
    }
    if (fpsArenaPointSegmentDistance(playerCenter, projectile.previous, object.position) < hitRadius) {
      fpsArenaApplyIncomingDamage(state, projectile.damage, projectile.previous, "敵人命中你");
      state.scene.remove(object);
      object.geometry?.dispose?.();
      object.material?.dispose?.();
      state.botProjectiles.splice(i, 1);
      continue;
    }
    if (projectile.expiresAt <= now) {
      state.scene.remove(object);
      object.geometry?.dispose?.();
      object.material?.dispose?.();
      state.botProjectiles.splice(i, 1);
    }
  }
}

function fpsArenaUpdateCombatEffects(state, now) {
  const removeExpired = (items) => {
    for (let i = items.length - 1; i >= 0; i -= 1) {
      if (items[i].expiresAt > now) continue;
      const object = items[i].object;
      state.scene.remove(object);
      object.geometry?.dispose?.();
      object.material?.dispose?.();
      items.splice(i, 1);
    }
  };
  removeExpired(state.botTracers);
  removeExpired(state.botMuzzleFlashes);
  removeExpired(state.impactEffects);
  for (let i = state.bloodEffects.length - 1; i >= 0; i -= 1) {
    const item = state.bloodEffects[i];
    const dt = Math.min(0.04, Math.max(0.001, (now - (item.lastAt || now)) / 1000));
    item.lastAt = now;
    item.object.position.add(item.velocity.clone().multiplyScalar(dt));
    item.velocity.y -= 6.6 * dt;
    if (item.object.material) {
      item.object.material.opacity = Math.max(0, (item.expiresAt - now) / 720);
    }
    if (item.object.position.y < 0.04) {
      item.object.position.y = 0.04;
      item.velocity.y *= -0.18;
      item.velocity.x *= 0.78;
      item.velocity.z *= 0.78;
    }
    if (item.expiresAt > now) continue;
    state.scene.remove(item.object);
    fpsArenaDisposeObject(item.object);
    state.bloodEffects.splice(i, 1);
  }
  for (let i = state.deadBodies.length - 1; i >= 0; i -= 1) {
    const body = state.deadBodies[i];
    const progress = Math.min(1, (now - body.startedAt) / body.duration);
    body.object.rotation.x = body.baseRotation.x + body.fallDirection * progress * 1.34;
    body.object.rotation.z = body.baseRotation.z + body.sideRoll * progress * 0.64;
    body.object.position.y = Math.max(0.32, body.baseY - progress * 0.66);
    if (body.expiresAt > now) continue;
    state.scene.remove(body.object);
    fpsArenaDisposeObject(body.object);
    state.deadBodies.splice(i, 1);
  }
}

function fpsArenaResizeRenderer() {
  const state = fpsArenaState;
  const stage = $("fps-arena-stage");
  if (!state?.renderer || !state?.camera || !stage) return;
  const width = Math.max(320, Math.floor(stage.clientWidth || 640));
  const height = Math.max(240, Math.floor(stage.clientHeight || 360));
  state.renderer.setSize(width, height, false);
  state.camera.aspect = width / height;
  state.camera.updateProjectionMatrix();
}

function fpsArenaBuildCombatMap(scene, level = FPS_ARENA_LEVELS[0]) {
  const cover = [];
  const trackCover = (mesh, options = {}) => {
    mesh.userData = {
      ...mesh.userData,
      kind: "cover",
      blocksPlayer: options.blocksPlayer !== false,
    };
    cover.push(mesh);
    return mesh;
  };
  const addCoverBox = (...args) => trackCover(fpsArenaAddBox(scene, ...args));
  const addCoverCylinder = (...args) => trackCover(fpsArenaAddCylinder(scene, ...args));
  const addDecorBox = (x, y, z, sx, sy, sz, color, options = {}) => {
    const mesh = fpsArenaAddBox(scene, x, y, z, sx, sy, sz, color);
    mesh.userData = { ...mesh.userData, kind: "set_dressing", blocksPlayer: false };
    if (options.glow) mesh.material = fpsArenaGlowMaterial(color, options.opacity ?? 1, options.intensity ?? 0.58);
    mesh.castShadow = options.shadow !== false;
    mesh.receiveShadow = options.receiveShadow !== false;
    if (options.rotationY) mesh.rotation.y = options.rotationY;
    return mesh;
  };
  const addDecorCylinder = (x, y, z, radius, height, color, options = {}) => {
    const mesh = fpsArenaAddCylinder(scene, x, y, z, radius, height, color);
    mesh.userData = { ...mesh.userData, kind: "set_dressing", blocksPlayer: false };
    if (options.glow) mesh.material = fpsArenaGlowMaterial(color, options.opacity ?? 1, options.intensity ?? 0.6);
    if (options.rotationX) mesh.rotation.x = options.rotationX;
    if (options.rotationZ) mesh.rotation.z = options.rotationZ;
    mesh.castShadow = options.shadow !== false;
    mesh.receiveShadow = options.receiveShadow !== false;
    return mesh;
  };
  const addFloorMark = (x, z, sx, sz, color) => {
    const mark = fpsArenaAddBox(scene, x, 0.015, z, sx, 0.03, sz, color);
    mark.castShadow = false;
    return mark;
  };

  trackCover(fpsArenaAddBox(scene, -11.2, 1.5, -13, 0.35, 3, 42, 0x24324a));
  trackCover(fpsArenaAddBox(scene, 11.2, 1.5, -13, 0.35, 3, 42, 0x24324a));
  trackCover(fpsArenaAddBox(scene, 0, 1.5, -34.2, 22, 3, 0.35, 0x24324a));
  addFloorMark(0, -13, 0.12, 41, 0x1d4ed8);
  addFloorMark(-4.8, -18, 2.8, 0.12, 0xfacc15);
  addFloorMark(4.8, -22, 2.8, 0.12, 0xfacc15);

  addCoverBox(-5.6, 0.68, -5.3, 4.2, 1.35, 0.55, 0x334155);
  addCoverBox(5.6, 0.68, -5.3, 4.2, 1.35, 0.55, 0x334155);
  addCoverBox(-7.1, 0.95, -11.4, 1.45, 1.9, 6.3, 0x1f2937);
  addCoverBox(7.1, 0.95, -12.6, 1.45, 1.9, 6.8, 0x1f2937);
  addCoverBox(-2.1, 0.62, -12.8, 3.2, 1.24, 0.62, 0x475569);
  addCoverBox(2.4, 0.62, -16.2, 3.5, 1.24, 0.62, 0x475569);
  addCoverBox(-5.2, 0.82, -21.2, 4.4, 1.64, 1.15, 0x293548);
  addCoverBox(5.5, 0.82, -24.6, 4.7, 1.64, 1.15, 0x293548);
  addCoverBox(0, 0.58, -27.9, 5.4, 1.16, 0.62, 0x475569);
  addCoverBox(-8.9, 0.65, -28.6, 2.6, 1.3, 1.2, 0x334155);
  addCoverBox(8.8, 0.65, -8.4, 2.4, 1.3, 1.2, 0x334155);

  for (const x of [-8.5, 8.5]) {
    for (const z of [-8.3, -18.5, -29.2]) {
      addCoverCylinder(x, 1.15, z, 0.42, 2.3, 0x334155);
    }
  }
  [
    [-3.8, -8.8], [-2.7, -9.7], [3.4, -9.4], [4.5, -10.3],
    [-1.3, -19.8], [0.1, -20.7], [1.4, -19.8], [-7.1, -24.4], [7.2, -19.6],
  ].forEach(([x, z], index) => {
    addCoverBox(x, 0.52, z, 1.0, 1.04, 1.0, index % 2 ? 0x3f3f46 : 0x334155);
  });

  fpsArenaAddBox(scene, 0, 2.85, -14.8, 18, 0.22, 0.24, 0x1e293b);
  fpsArenaAddBox(scene, 0, 2.85, -25.8, 18, 0.22, 0.24, 0x1e293b);
  fpsArenaAddBox(scene, -4.8, 0.04, -14, 2.8, 0.04, 2.8, 0x78350f);
  fpsArenaAddBox(scene, 4.8, 0.04, -18, 2.8, 0.04, 2.8, 0x78350f);

  if (level.key === "warehouse") {
    [-9.8, 9.8].forEach((x, sideIndex) => {
      [-7.8, -14.5, -21.2, -27.9].forEach((z, rowIndex) => {
        const color = (sideIndex + rowIndex) % 3 === 0 ? 0x1d4ed8 : (rowIndex % 2 ? 0xea580c : 0x475569);
        addDecorBox(x, 1.1, z, 1.7, 2.2, 4.6, color);
        if (rowIndex % 2 === 0) addDecorBox(x, 3.35, z + 0.35, 1.65, 2.1, 3.8, color === 0x475569 ? 0x334155 : color);
      });
    });
    addDecorBox(0, 4.15, -16.5, 18.5, 0.28, 0.34, 0xfacc15);
    addDecorBox(-3.2, 3.55, -16.5, 0.18, 1.1, 0.18, 0xfacc15);
    addDecorCylinder(-3.2, 2.85, -16.5, 0.16, 0.44, 0xf97316, { rotationX: Math.PI / 2 });
    addFloorMark(0, -6.2, 8.5, 0.16, 0xf97316);
  } else if (level.key === "reactor") {
    addFloorMark(0, -20, 16, 0.16, 0x22d3ee);
    addCoverCylinder(-2.8, 1.25, -18.8, 0.7, 2.5, 0x155e75);
    addCoverCylinder(2.8, 1.25, -18.8, 0.7, 2.5, 0x155e75);
    addCoverBox(0, 0.5, -23.1, 7.2, 1.0, 0.44, 0x0f766e);
    addCoverBox(-8.5, 0.7, -15.5, 1.2, 1.4, 4.2, 0x164e63);
    addDecorCylinder(0, 1.85, -18.8, 1.15, 3.7, 0x22d3ee, { glow: true, opacity: 0.82, intensity: 0.9 });
    addDecorCylinder(0, 3.95, -18.8, 1.55, 0.28, 0x67e8f9, { glow: true, opacity: 0.68, intensity: 0.7 });
    addDecorCylinder(0, 0.28, -18.8, 1.7, 0.32, 0x0e7490);
    [-7.7, 7.7].forEach((x) => {
      addDecorCylinder(x, 2.15, -20.3, 0.18, 17.5, 0x0e7490, { rotationX: Math.PI / 2 });
      addDecorBox(x, 2.18, -11.8, 1.0, 0.35, 0.9, 0x22d3ee, { glow: true, opacity: 0.78 });
    });
    addDecorBox(0, 4.55, -18.8, 6.2, 0.18, 6.2, 0x164e63);
  } else if (level.key === "subway") {
    addFloorMark(-6.2, -18.5, 0.2, 26, 0xfacc15);
    addFloorMark(6.2, -18.5, 0.2, 26, 0xfacc15);
    addCoverBox(0, 0.56, -9.8, 8.2, 1.12, 0.52, 0x374151);
    addCoverBox(0, 0.56, -20.5, 8.2, 1.12, 0.52, 0x374151);
    addCoverBox(-8.3, 0.82, -25.2, 2.0, 1.64, 3.8, 0x4b5563);
    addCoverBox(8.3, 0.82, -12.5, 2.0, 1.64, 3.8, 0x4b5563);
    [-3.2, 3.2].forEach((x) => {
      addFloorMark(x, -19, 0.14, 28.5, 0x020617);
      addDecorBox(x, 0.08, -19, 0.2, 0.12, 28.5, 0x0f172a, { shadow: false });
    });
    [-6.8, 6.8].forEach((x, index) => {
      addDecorBox(x, 1.32, -21.8 + index * 5.2, 1.1, 2.25, 11.8, 0x2563eb);
      addDecorBox(x, 2.02, -21.8 + index * 5.2, 1.16, 0.28, 11.9, 0x0f172a);
      for (let z = -26.4 + index * 5.2; z <= -17.4 + index * 5.2; z += 2.2) {
        addDecorBox(x - Math.sign(x) * 0.58, 1.45, z, 0.05, 0.58, 0.82, 0xbfdbfe, { glow: true, opacity: 0.82, intensity: 0.42 });
      }
    });
    addDecorBox(0, 3.25, -13.7, 7.2, 0.22, 0.9, 0xfacc15, { glow: true, opacity: 0.9, intensity: 0.45 });
    addDecorBox(0, 3.04, -13.7, 4.7, 0.58, 0.08, 0x111827);
  } else if (level.key === "citadel") {
    addFloorMark(0, -18.4, 18, 0.18, 0xc4b5fd);
    addCoverBox(0, 0.88, -18.4, 2.2, 1.76, 7.2, 0x4c1d95);
    addCoverBox(-7.8, 1.08, -15.2, 1.5, 2.16, 5.8, 0x312e81);
    addCoverBox(7.8, 1.08, -22.2, 1.5, 2.16, 5.8, 0x312e81);
    addCoverCylinder(-3.8, 1.36, -28.8, 0.58, 2.72, 0x6d28d9);
    addCoverCylinder(3.8, 1.36, -28.8, 0.58, 2.72, 0x6d28d9);
    [-9.0, 9.0].forEach((x) => {
      addDecorCylinder(x, 2.25, -8.5, 0.72, 4.5, 0x312e81);
      addDecorCylinder(x, 2.25, -29.2, 0.72, 4.5, 0x312e81);
      addDecorBox(x, 4.62, -18.8, 1.05, 0.5, 22.6, 0x4c1d95);
    });
    addDecorBox(0, 4.65, -9.0, 12.5, 0.55, 0.72, 0x4c1d95);
    addDecorBox(0, 3.2, -9.0, 3.2, 2.5, 0.65, 0x111827);
    addDecorCylinder(0, 2.1, -27.8, 0.92, 4.2, 0xa78bfa, { glow: true, opacity: 0.72, intensity: 0.86 });
    addDecorCylinder(0, 4.42, -27.8, 1.28, 0.22, 0xc4b5fd, { glow: true, opacity: 0.74, intensity: 0.7 });
    addFloorMark(0, -27.8, 6.2, 6.2, 0x6d28d9);
  }

  const coverPoints = [
    { x: -6.9, z: -4.4, peekX: -4.2, peekZ: -5.1 }, { x: 6.9, z: -4.4, peekX: 4.2, peekZ: -5.1 },
    { x: -5.4, z: -10.2, peekX: -4.2, peekZ: -11.8 }, { x: 5.4, z: -11.2, peekX: 4.3, peekZ: -13.0 },
    { x: -3.9, z: -13.8, peekX: -1.9, peekZ: -12.1 }, { x: 4.4, z: -17.3, peekX: 2.4, peekZ: -15.2 },
    { x: -6.8, z: -20.0, peekX: -4.6, peekZ: -21.5 }, { x: 6.9, z: -23.2, peekX: 4.9, peekZ: -24.8 },
    { x: -7.3, z: -29.8, peekX: -5.9, peekZ: -27.3 }, { x: 7.3, z: -27.2, peekX: 5.6, peekZ: -28.9 },
    { x: -1.8, z: -29.0, peekX: 0.0, peekZ: -27.0 }, { x: 1.8, z: -29.0, peekX: 0.0, peekZ: -27.0 },
  ];
  const navPoints = [
    { x: -8.0, z: -6.8 }, { x: 0.0, z: -7.3 }, { x: 8.0, z: -8.2 },
    { x: -8.2, z: -14.2 }, { x: -1.2, z: -16.2 }, { x: 8.2, z: -16.4 },
    { x: -7.8, z: -22.8 }, { x: 0.0, z: -22.2 }, { x: 7.8, z: -23.4 },
    { x: -7.4, z: -30.2 }, { x: 0.0, z: -31.0 }, { x: 7.4, z: -29.8 },
  ];

  if (level.key === "reactor") {
    coverPoints.push({ x: 0, z: -23.4, peekX: 0, peekZ: -21.4 }, { x: -8.4, z: -15.2, peekX: -6.7, peekZ: -15.2 });
    navPoints.push({ x: 0, z: -18.4 }, { x: -6.2, z: -18.0 }, { x: 6.2, z: -18.0 });
  } else if (level.key === "subway") {
    coverPoints.push({ x: 0, z: -9.4, peekX: -2.4, peekZ: -10.8 }, { x: 0, z: -20.2, peekX: 2.4, peekZ: -21.6 });
    navPoints.push({ x: -6.2, z: -12.0 }, { x: 6.2, z: -25.0 });
  } else if (level.key === "citadel") {
    coverPoints.push({ x: -7.8, z: -15.2, peekX: -5.8, peekZ: -15.2 }, { x: 7.8, z: -22.2, peekX: 5.8, peekZ: -22.2 }, { x: 0, z: -18.4, peekX: 2.1, peekZ: -17.0 });
    navPoints.push({ x: -4.2, z: -27.6 }, { x: 4.2, z: -27.6 }, { x: 0, z: -18.4 });
  }

  return {
    cover,
    coverPoints,
    navPoints,
    spawnPoints: [
      { x: -7.8, z: -7.2 }, { x: -3.4, z: -8.8 }, { x: 3.2, z: -9.2 }, { x: 8.2, z: -10.8 },
      { x: -8.1, z: -17.6 }, { x: -2.2, z: -18.6 }, { x: 3.1, z: -20.2 }, { x: 8.1, z: -22.4 },
      { x: -6.9, z: -27.2 }, { x: 0.1, z: -30.6 }, { x: 6.7, z: -28.6 },
    ],
  };
}

function createFpsArenaWorld(mode) {
  const stage = $("fps-arena-stage");
  if (!stage || typeof THREE === "undefined") return null;
  const dailyChallenge = window.hackmeGameDailyChallenge?.("fps_arena") || null;
  const level = fpsArenaLevel();
  const weaponPool = (mode === "br" ? ["pistol", ...level.weapons] : level.weapons).map((key) => fpsArenaWeaponByKey(key)).filter(Boolean);
  const startWeapon = mode === "br" ? fpsArenaWeaponByKey("pistol") : (weaponPool[0] || FPS_ARENA_WEAPONS[0]);
  stage.querySelectorAll("canvas").forEach((canvas) => canvas.remove());
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(level.theme?.background || 0x08111f);
  scene.fog = new THREE.Fog(level.theme?.fog || 0x6f8494, 24, 72);
  const camera = new THREE.PerspectiveCamera(FPS_ARENA_NORMAL_FOV, 16 / 9, 0.1, 100);
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance", preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.shadowMap.enabled = true;
  stage.prepend(renderer.domElement);

  scene.add(new THREE.HemisphereLight(0xf8fafc, 0x64748b, 1.65));
  const key = new THREE.DirectionalLight(0xffffff, 2.25);
  key.position.set(4, 8, 5);
  key.castShadow = true;
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xdbeafe, 0.82);
  fill.position.set(-6, 4.5, -8);
  scene.add(fill);

  const floor = new THREE.Mesh(new THREE.PlaneGeometry(22, 42), fpsArenaMaterial(level.theme?.floor || 0x162033, 0.9, 0.02));
  floor.rotation.x = -Math.PI / 2;
  floor.position.z = -13;
  floor.receiveShadow = true;
  scene.add(floor);
  const map = fpsArenaBuildCombatMap(scene, level);

  const state = {
    status: "active",
    mode,
    level,
    scene,
    camera,
    renderer,
    hittables: [],
    targets: [],
    cover: map.cover,
    coverPoints: map.coverPoints,
    navPoints: map.navPoints,
    spawnPoints: map.spawnPoints,
    aiSpawnCounter: 0,
    kills: 0,
    bossActive: false,
    bossDefeated: false,
    bossLabel: level.boss?.label || "",
    bossIntroUntil: 0,
    botTracers: [],
    botMuzzleFlashes: [],
    botProjectiles: [],
    impactEffects: [],
    bloodEffects: [],
    deadBodies: [],
    pickups: [],
    remotePlayer: null,
    multiplayer: null,
    startedAt: Date.now(),
    completedAt: null,
    durationMs: (FPS_ARENA_MODES[mode].seconds + Math.min(30, FPS_ARENA_LEVELS.indexOf(level) * 8)) * 1000,
    penaltySeconds: 0,
    scoreSubmitted: false,
    difficulty: `${level.key}-${mode}`,
    puzzleId: dailyChallenge?.key || `fps-arena-${level.key}-${mode}`,
    dailyChallenge,
    score: 0,
    health: FPS_ARENA_MODES[mode].health,
    armor: mode === "br" ? 0 : 25,
    shots: 0,
    hits: 0,
    yaw: 0,
    pitch: -0.03,
    sniperMode: false,
    cameraFov: FPS_ARENA_NORMAL_FOV,
    breathPhase: Math.random() * Math.PI * 2,
    breathOffset: new THREE.Vector2(0, 0),
    player: new THREE.Vector3(0, 1.65, 1.5),
    velocity: new THREE.Vector3(),
    keys: {},
    stamina: 100,
    stance: "stand",
    eyeHeight: 1.65,
    coverState: { active: false, near: false, hard: false, damageScale: 1 },
    mobileSprint: false,
    mobileCrouch: false,
    mobileProne: false,
    running: false,
    lastFrame: performance.now(),
    lastSpawnAt: 0,
    lastShotAt: 0,
    lastPlayerShotAt: 0,
    lastPlayerShotPosition: null,
    weaponIndex: 0,
    weaponPool,
    weapon: startWeapon,
    ammo: startWeapon.mag,
    reserve: startWeapon.reserve,
    reloadingUntil: 0,
    recoilKick: 0,
    damageFlashUntil: 0,
    damageSourceAngle: 0,
    shakeUntil: 0,
    shakeDuration: 180,
    shakeStrength: 0,
    shakePhase: Math.random() * Math.PI * 2,
    shakeVector: new THREE.Vector2(0, 0),
    defuseProgress: 0,
    bomb: null,
    zone: null,
  };
  fpsArenaState = state;
  fpsArenaResizeRenderer();
  fpsArenaResizeObserver = new ResizeObserver(fpsArenaResizeRenderer);
  fpsArenaResizeObserver.observe(stage);
  if (mode === "aim") {
    for (let i = 0; i < 7; i += 1) fpsArenaAddTarget(state, "target");
  } else if (mode === "pve" || mode === "coop") {
    for (let i = 0; i < 4; i += 1) fpsArenaAddTarget(state, "enemy", { hp: 2, speed: 0.9 + i * 0.08 });
  } else if (mode === "bomb") {
    fpsArenaCreateBomb(state);
    for (let i = 0; i < 3; i += 1) fpsArenaAddTarget(state, "enemy", { hp: 2, speed: 0.85 });
  } else if (mode === "bot") {
    for (let i = 0; i < 5; i += 1) fpsArenaAddTarget(state, "bot", { hp: 2, speed: 1.1, score: 220 });
  } else if (mode === "pvp") {
    for (let i = 0; i < 3; i += 1) fpsArenaAddTarget(state, "enemy", { hp: 2, speed: 0.86 + i * 0.08 });
  } else if (mode === "br") {
    fpsArenaSpawnLoot(state);
    fpsArenaCreateBattleRoyaleZone(state);
    for (let i = 0; i < 7; i += 1) fpsArenaAddTarget(state, "enemy", { hp: 2, speed: 0.9 + i * 0.04, score: 260 });
  }
  return state;
}

function renderFpsArenaBoard() {
  const stage = $("fps-arena-stage");
  if (!stage) return;
  if (typeof THREE === "undefined") {
    stage.querySelectorAll("canvas").forEach((canvas) => canvas.remove());
    const hud = $("fps-arena-hud");
    if (hud) hud.textContent = "3D 引擎載入中";
    if (typeof ensureThreeJsLoaded === "function") {
      ensureThreeJsLoaded()
        .then(() => renderFpsArenaBoard())
        .catch((err) => {
          reportFrontendFailure("fps-arena-three-load", err);
          if (hud) hud.textContent = "Three.js 載入失敗，無法啟動 3D 模式";
        });
    } else if (hud) {
      hud.textContent = "Three.js 載入失敗，無法啟動 3D 模式";
    }
    return;
  }
  if (!fpsArenaState) {
    updateFpsArenaHud();
  } else {
    fpsArenaResizeRenderer();
  }
}

async function startFpsArenaGame() {
  if (typeof THREE === "undefined" && typeof ensureThreeJsLoaded === "function") {
    updateFpsArenaStatus("3D 引擎載入中。");
    try {
      await ensureThreeJsLoaded();
    } catch (_) {
      setGameMsg("Three.js 載入失敗，無法啟動 3D 模式", false);
      updateFpsArenaStatus("3D 引擎載入失敗。");
      return;
    }
  }
  if (typeof THREE === "undefined") {
    setGameMsg("Three.js 尚未載入，無法啟動 3D 模式", false);
    updateFpsArenaStatus("3D 引擎尚未載入。");
    return;
  }
  disposeFpsArenaScene();
  const mode = fpsArenaMode();
  const multiplayerMode = mode === "coop" || mode === "pvp";
  const multiplayerRoom = multiplayerMode ? window.hackmeGameMultiplayer?.activeRoom?.("fps_arena", mode) : null;
  if (multiplayerMode && !multiplayerRoom) {
    setGameMsg("請先在多人房間邀請並選擇玩家，再開始 3D 多人模式。", false);
    updateFpsArenaStatus("多人模式等待房間。");
    return;
  }
  const state = createFpsArenaWorld(mode);
  if (!state) {
    setGameMsg("3D 射擊場初始化失敗", false);
    return;
  }
  if (multiplayerRoom) {
    state.multiplayer = {
      room: multiplayerRoom,
      roomId: multiplayerRoom.id,
      mode,
      peer: null,
      afterEventId: 0,
      pendingEvents: [],
      processedEvents: new Set(),
      syncing: false,
      lastSyncAt: 0,
      lastError: "",
    };
    window.hackmeGameMultiplayer?.start?.(multiplayerRoom.id).catch((err) => {
      window.reportFrontendFailure?.("fps-multiplayer-start", err);
      updateFpsArenaStatus(err?.message || "多人連線啟動失敗。", false);
    });
    syncFpsArenaMultiplayer(state, performance.now(), { force: true });
  }
  updateFpsArenaStatus("任務開始。");
  if (typeof ensureSoloGameTimer === "function") ensureSoloGameTimer();
  fpsArenaLoop(performance.now());
}

function finishFpsArenaGame(reason = "time") {
  const state = fpsArenaState;
  if (!state || state.status === "finished") return;
  if (state.sniperMode) setFpsArenaSniperMode(false, { immediate: true });
  state.status = "finished";
  state.completedAt = Date.now();
  if (reason === "defused") state.score += 900 + Math.ceil(state.health * 3);
  if (reason === "time" && state.health > 0) state.score += Math.ceil(state.health);
  if (fpsArenaIsMultiplayer(state)) {
    fpsArenaQueueMultiplayerEvent(state, {
      type: state.health > 0 ? "finish" : "down",
      payload: { reason, score: Math.round(state.score || 0), health: Math.max(0, Math.round(state.health || 0)) },
    });
    syncFpsArenaMultiplayer(state, performance.now(), { force: true });
  }
  updateFpsArenaStatus();
  updateFpsArenaHud();
  if (!fpsArenaIsMultiplayer(state) && Number(state.score || 0) > 0 && typeof submitSoloGameScore === "function") {
    const accuracy = state.shots > 0 ? Math.round((state.hits / state.shots) * 100) : 0;
    if (accuracy >= 45) window.recordHackmeGameAchievement?.("fps_arena", "accuracy", "穩定射手", "命中率達 45%。");
    if (reason === "defused") window.recordHackmeGameAchievement?.("fps_arena", "defuse", "拆彈成功", "完成 Bomb Defuse。");
    if (state.bossDefeated) window.recordHackmeGameAchievement?.("fps_arena", `clear-${state.level.key}`, `${state.level.label} 完成`, "在關卡中擊敗 Boss 並存活到結束。");
    if (state.mode === "br" && state.health > 0) window.recordHackmeGameAchievement?.("fps_arena", "br-survivor", "大逃殺倖存者", "在 Battle Royale 模式存活到結束。");
    submitSoloGameScore("fps_arena", state);
  }
  if (typeof stopSoloGameTimerIfIdle === "function") stopSoloGameTimerIfIdle();
  const label = reason === "defused" ? "拆彈成功" : reason === "health" ? "任務失敗" : reason === "pvp_down" ? "PvP 戰敗" : "任務結束";
  setGameMsg(`${label}，分數 ${Number(state.score || 0).toLocaleString()}`, reason !== "health");
}

function suspendFpsArenaGame() {
  const state = fpsArenaState;
  if (!state || state.status !== "active") return;
  if (state.sniperMode) setFpsArenaSniperMode(false, { immediate: true });
  state.status = "paused";
  state.keys = {};
  if (fpsArenaRaf) {
    cancelAnimationFrame(fpsArenaRaf);
    fpsArenaRaf = null;
  }
  updateFpsArenaStatus("已暫停；按開始可重新進入任務。");
}

function fpsArenaApplyCamera(state) {
  state.camera.position.copy(state.player);
  state.camera.position.x += (state.shakeVector?.x || 0) * 0.025;
  state.camera.position.y += (state.shakeVector?.y || 0) * 0.018;
  state.camera.rotation.order = "YXZ";
  state.camera.rotation.y = state.yaw + (state.breathOffset?.x || 0);
  state.camera.rotation.x = state.pitch + (state.breathOffset?.y || 0) - (state.recoilKick || 0);
  state.camera.rotation.z = (state.shakeVector?.x || 0) * 0.01;
}

function fpsArenaUpdateBreathing(state, now) {
  const t = now * 0.0017 + state.breathPhase;
  const moving = state.keys.w || state.keys.a || state.keys.s || state.keys.d || state.keys.ArrowUp || state.keys.ArrowDown || state.keys.ArrowLeft || state.keys.ArrowRight;
  const stanceScale = fpsArenaStanceMeta(state).breathe;
  const runScale = (state.running ? 2.85 : moving ? 1.8 : 1) * stanceScale;
  const sniperScale = state.sniperMode ? 0.46 : 1;
  const intensity = FPS_ARENA_SCOPE_SWAY * runScale * sniperScale;
  state.breathOffset.set(Math.sin(t) * intensity, Math.cos(t * 0.72) * intensity * 0.78);
  const stage = $("fps-arena-stage");
  if (stage) {
    stage.style.setProperty("--fps-breathe-x", `${Math.sin(t) * (state.running ? 9.2 : moving ? 5.5 : 3.2) * stanceScale * sniperScale}px`);
    stage.style.setProperty("--fps-breathe-y", `${Math.cos(t * 0.72) * (state.running ? 7.2 : moving ? 4.4 : 2.4) * stanceScale * sniperScale}px`);
    stage.style.setProperty("--fps-breathe-rot", `${Math.sin(t * 0.48) * (state.running ? 2.8 : moving ? 1.8 : 1.1) * stanceScale * sniperScale}deg`);
    stage.style.setProperty("--fps-breathe-scale", `${1 + Math.sin(t * 1.16) * (state.running ? 0.06 : moving ? 0.042 : 0.026) * stanceScale * sniperScale}`);
  }
}

function fpsArenaClampPlayerToMap(state, position) {
  position.x = Math.max(-9.8, Math.min(9.8, position.x));
  position.z = Math.max(-31.2, Math.min(2.8, position.z));
  return position;
}

function fpsArenaPositionBlocked(state, position) {
  const cover = state.cover || [];
  for (const object of cover) {
    if (object.userData?.blocksPlayer === false) continue;
    const box = new THREE.Box3().setFromObject(object);
    if (position.y < box.min.y - 0.2 || position.y > box.max.y + 2.2) continue;
    const closestX = Math.max(box.min.x, Math.min(position.x, box.max.x));
    const closestZ = Math.max(box.min.z, Math.min(position.z, box.max.z));
    const dx = position.x - closestX;
    const dz = position.z - closestZ;
    if ((dx * dx) + (dz * dz) < FPS_ARENA_PLAYER_RADIUS * FPS_ARENA_PLAYER_RADIUS) return true;
  }
  return false;
}

function fpsArenaMoveWithCollision(state, delta) {
  const next = fpsArenaClampPlayerToMap(state, state.player.clone().add(delta));
  if (!fpsArenaPositionBlocked(state, next)) {
    state.player.copy(next);
    return;
  }
  const xOnly = fpsArenaClampPlayerToMap(state, state.player.clone().add(new THREE.Vector3(delta.x, 0, 0)));
  if (!fpsArenaPositionBlocked(state, xOnly)) state.player.copy(xOnly);
  const zOnly = fpsArenaClampPlayerToMap(state, state.player.clone().add(new THREE.Vector3(0, 0, delta.z)));
  if (!fpsArenaPositionBlocked(state, zOnly)) state.player.copy(zOnly);
}

function fpsArenaMovePlayer(state, dt) {
  state.stance = fpsArenaDesiredStance(state);
  const stance = fpsArenaStanceMeta(state);
  state.eyeHeight = Number(state.eyeHeight || state.player.y || stance.eye);
  state.eyeHeight += (stance.eye - state.eyeHeight) * Math.min(1, dt * 10);
  state.player.y = state.eyeHeight;
  const forward = new THREE.Vector3(Math.sin(state.yaw), 0, Math.cos(state.yaw) * -1);
  const right = new THREE.Vector3(Math.cos(state.yaw), 0, Math.sin(state.yaw));
  const move = new THREE.Vector3();
  if (state.keys.w || state.keys.ArrowUp) move.add(forward);
  if (state.keys.s || state.keys.ArrowDown) move.sub(forward);
  if (state.keys.d || state.keys.ArrowRight) move.add(right);
  if (state.keys.a || state.keys.ArrowLeft) move.sub(right);
  const moving = move.lengthSq() > 0;
  const sprintKey = state.keys.Shift || state.keys.shift || state.mobileSprint;
  const running = moving && !state.sniperMode && sprintKey && state.stance === "stand" && state.stamina > 4;
  state.running = running;
  if (moving) {
    const speed = (running ? 9.2 : stance.speed) * (state.sniperMode ? 0.55 : 1);
    move.normalize().multiplyScalar(speed * dt);
    fpsArenaMoveWithCollision(state, move);
  }
  if (running) {
    state.stamina = Math.max(0, state.stamina - 34 * dt);
  } else {
    state.stamina = Math.min(100, state.stamina + (moving ? 14 : 22) * dt);
  }
  if (state.stamina <= 0) state.mobileSprint = false;
}

function fpsArenaRemoveObject(state, object) {
  const root = object?.userData?.root || object;
  state.scene.remove(root);
  state.targets = state.targets.filter((item) => item !== root);
  state.hittables = state.hittables.filter((item) => item !== root && item.userData?.root !== root);
  fpsArenaDisposeObject(root);
}

function fpsArenaKillTarget(state, target, hitPoint) {
  const root = target?.userData?.root || target;
  if (!root || root.userData.dead) return;
  const now = performance.now();
  root.userData.dead = true;
  state.targets = state.targets.filter((item) => item !== root);
  state.hittables = state.hittables.filter((item) => item !== root && item.userData?.root !== root);
  fpsArenaAddBloodSplatter(state, hitPoint || root.position.clone().add(new THREE.Vector3(0, 0.55, 0)), 24);
  fpsArenaAddShake(state, 1.05, 190);
  fpsArenaPlaySound("scream");
  state.deadBodies.push({
    object: root,
    startedAt: now,
    duration: 520,
    expiresAt: now + 4200,
    baseY: root.position.y,
    baseRotation: root.rotation.clone(),
    fallDirection: Math.random() > 0.5 ? 1 : -1,
    sideRoll: Math.random() - 0.5,
  });
}

function shootFpsArena() {
  const state = fpsArenaState;
  if (!state || state.status !== "active") return;
  const now = performance.now();
  const weapon = state.weapon || FPS_ARENA_WEAPONS[0];
  if (state.reloadingUntil && now < state.reloadingUntil) return;
  if (state.ammo <= 0) {
    reloadFpsArena();
    return;
  }
  if (now - state.lastShotAt < weapon.delay) return;
  state.lastShotAt = now;
  state.lastPlayerShotAt = now;
  state.lastPlayerShotPosition = state.player.clone();
  fpsArenaQueueMultiplayerEvent(state, {
    type: "gunshot",
    payload: {
      x: state.player.x,
      y: state.player.y,
      z: state.player.z,
      yaw: state.yaw,
    },
  });
  state.ammo -= 1;
  state.shots += 1;
  fpsArenaAddShake(state, 0.85, 150);
  fpsArenaPlaySound("fire");
  state.pitch = Math.max(-1.25, state.pitch - weapon.recoil);
  state.recoilKick = Math.min(0.08, (state.recoilKick || 0) + weapon.recoil * 0.55);
  const raycaster = new THREE.Raycaster();
  const stanceSpread = state.stance === "prone" ? -0.0007 : state.stance === "crouch" ? -0.00035 : 0;
  const aimSpreadScale = state.sniperMode ? FPS_ARENA_SNIPER_SPREAD_SCALE : 1;
  const spread = Math.max(0.0002, (weapon.spread + stanceSpread) * aimSpreadScale + (state.running ? 0.006 : 0) + Math.min(0.01, (state.recoilKick || 0) * 0.12));
  raycaster.setFromCamera(new THREE.Vector2((Math.random() - 0.5) * spread, (Math.random() - 0.5) * spread), state.camera);
  const hit = raycaster.intersectObjects([...state.hittables, ...state.cover], false)[0];
  const hitCover = Boolean(hit && state.cover.includes(hit.object));
  fpsArenaAddPlayerFireEffects(state, hit?.point, Boolean(hit && !hitCover));
  if (!hit || hitCover) {
    updateFpsArenaStatus();
    return;
  }
  const mesh = hit.object;
  const target = mesh.userData.root || mesh;
  const data = target.userData || mesh.userData;
  if (data.kind === "remote_player" || mesh.userData.kind === "remote_player") {
    const peerId = state.multiplayer?.peer?.user_id || data.userId || mesh.userData.userId;
    const damage = (mesh.userData.damage || 1) * (weapon.damage || 1) * (mesh.userData.part === "head" ? 18 : 11);
    state.hits += 1;
    state.score += state.mode === "pvp" ? 160 + Number(mesh.userData.scoreBonus || 0) : 20;
    fpsArenaPlaySound("hit");
    fpsArenaAddBloodSplatter(state, hit.point, 10);
    fpsArenaQueueMultiplayerEvent(state, {
      type: state.mode === "coop" ? "friendly_fire" : "player_hit",
      target_user_id: peerId,
      payload: {
        damage,
        x: state.player.x,
        y: state.player.y,
        z: state.player.z,
        part: mesh.userData.part || "",
      },
    });
    updateFpsArenaStatus(state.mode === "coop" ? "誤傷隊友。" : "命中對手。");
    return;
  }
  if (data.kind === "bomb" || mesh.userData.kind === "bomb") {
    fpsArenaPlaySound("defuse");
    attemptFpsArenaDefuse(true);
    return;
  }
  state.hits += 1;
  if (mesh.userData.part === "head") window.recordHackmeGameAchievement?.("fps_arena", "headshot", "爆頭訓練", "命中頭部。");
  fpsArenaPlaySound("hit");
  fpsArenaAddBloodSplatter(state, hit.point, data.hp <= 1 ? 18 : 7);
  data.hp -= (mesh.userData.damage || 1) * (weapon.damage || 1);
  state.score += Math.max(20, Math.round((data.score || 100) / 3)) + Number(mesh.userData.scoreBonus || 0);
  target.scale.multiplyScalar(0.94);
  if (data.hp <= 0) {
    state.score += data.score || 100;
    const kind = data.kind;
    state.kills = Number(state.kills || 0) + 1;
    if (data.boss) {
      state.bossDefeated = true;
      state.bossActive = false;
      window.recordHackmeGameAchievement?.("fps_arena", `boss-${state.level.key}`, `${state.level.label} Boss 擊破`, `擊倒 ${data.bossLabel || state.bossLabel || "Boss"}。`);
      if (state.level.key === "citadel") window.recordHackmeGameAchievement?.("fps_arena", "fps-campaign-clear", "3D 關卡制霸", "擊倒核心堡壘 Boss。");
    }
    fpsArenaKillTarget(state, target, hit.point);
    if (kind === "target") fpsArenaAddTarget(state, "target");
  }
  updateFpsArenaStatus();
}

function reloadFpsArena() {
  const state = fpsArenaState;
  if (!state || state.status !== "active") return;
  const weapon = state.weapon || FPS_ARENA_WEAPONS[0];
  if (state.ammo >= weapon.mag || state.reserve <= 0 || (state.reloadingUntil && performance.now() < state.reloadingUntil)) return;
  state.reloadingUntil = performance.now() + 980;
  window.setTimeout(() => {
    const current = fpsArenaState;
    if (!current || current !== state || current.status !== "active") return;
    const need = weapon.mag - current.ammo;
    const loaded = Math.min(need, current.reserve);
    current.ammo += loaded;
    current.reserve -= loaded;
    current.reloadingUntil = 0;
    updateFpsArenaStatus("換彈完成。");
  }, 990);
  updateFpsArenaStatus("換彈中。");
}

function switchFpsArenaWeapon() {
  const state = fpsArenaState;
  if (!state || state.status !== "active") return;
  const pool = Array.isArray(state.weaponPool) && state.weaponPool.length ? state.weaponPool : FPS_ARENA_WEAPONS;
  state.weaponIndex = (Number(state.weaponIndex || 0) + 1) % pool.length;
  state.weapon = pool[state.weaponIndex];
  state.ammo = state.weapon.mag;
  state.reserve = state.weapon.reserve;
  state.reloadingUntil = 0;
  updateFpsArenaStatus(`切換武器：${state.weapon.label}`);
}

function attemptFpsArenaDefuse(fromShot = false) {
  const state = fpsArenaState;
  if (!state || state.status !== "active" || state.mode !== "bomb" || !state.bomb) return;
  const distance = state.player.distanceTo(state.bomb.position);
  if (distance > 3.2) {
    if (!fromShot) setGameMsg("距離炸彈太遠", false);
    return;
  }
  state.defuseProgress += fromShot ? 18 : 28;
  state.score += 35;
  updateFpsArenaStatus("正在拆彈。");
  if (state.defuseProgress >= 100) finishFpsArenaGame("defused");
}

function fpsArenaPlayerAimPoint(state) {
  return state.player.clone().add(new THREE.Vector3(0, -0.15, 0));
}

function fpsArenaBotEyePosition(bot) {
  return bot.position.clone().add(new THREE.Vector3(0, 0.45, 0));
}

function fpsArenaClampAiPosition(position) {
  position.x = Math.max(-9.5, Math.min(9.5, position.x));
  position.z = Math.max(-32, Math.min(1.8, position.z));
  return position;
}

function fpsArenaMoveAiWithCollision(state, mesh, delta) {
  if (delta.lengthSq() <= 0.000001) return;
  const next = fpsArenaClampAiPosition(mesh.position.clone().add(delta));
  if (!fpsArenaPositionBlocked(state, next)) {
    mesh.position.copy(next);
    return;
  }
  const xOnly = fpsArenaClampAiPosition(mesh.position.clone().add(new THREE.Vector3(delta.x, 0, 0)));
  if (!fpsArenaPositionBlocked(state, xOnly)) {
    mesh.position.copy(xOnly);
    return;
  }
  const zOnly = fpsArenaClampAiPosition(mesh.position.clone().add(new THREE.Vector3(0, 0, delta.z)));
  if (!fpsArenaPositionBlocked(state, zOnly)) mesh.position.copy(zOnly);
}

function fpsArenaPickCoverPoint(state, mesh, role, wantsLineOfSight = true) {
  const points = Array.isArray(state.coverPoints) ? state.coverPoints : [];
  const playerAim = fpsArenaPlayerAimPoint(state);
  let best = null;
  for (const point of points) {
    const body = new THREE.Vector3(point.x, mesh.position.y, point.z);
    if (fpsArenaPositionBlocked(state, body)) continue;
    const eye = body.clone().add(new THREE.Vector3(0, 0.45, 0));
    const peek = new THREE.Vector3(point.peekX ?? point.x, mesh.position.y + 0.45, point.peekZ ?? point.z);
    const bodyLos = fpsArenaLineOfSightClear(state, eye, playerAim);
    const peekLos = fpsArenaLineOfSightClear(state, peek, playerAim);
    const botDistance = body.distanceTo(mesh.position);
    const playerDistance = Math.hypot(body.x - state.player.x, body.z - state.player.z);
    if (playerDistance < 1.8) continue;
    const rangePenalty = Math.abs(playerDistance - role.preferredRange) * 0.75;
    const safetyScore = bodyLos ? role.coverBias * 42 : role.coverBias * -68;
    const peekScore = wantsLineOfSight ? (peekLos ? -26 : 58) : (peekLos ? 18 : -22);
    const score = botDistance + rangePenalty + safetyScore + peekScore;
    if (!best || score < best.score) {
      best = {
        score,
        point,
        position: body,
        peekPosition: new THREE.Vector3(point.peekX ?? point.x, mesh.position.y, point.peekZ ?? point.z),
        peekLos,
        bodyLos,
      };
    }
  }
  return best;
}

function fpsArenaPickFlankPoint(state, mesh, role) {
  const points = Array.isArray(state.navPoints) ? state.navPoints : [];
  const side = mesh.userData.strafeSign || 1;
  const playerAim = fpsArenaPlayerAimPoint(state);
  let best = null;
  for (const point of points) {
    const position = new THREE.Vector3(point.x, mesh.position.y, point.z);
    if (fpsArenaPositionBlocked(state, position)) continue;
    const playerDistance = Math.hypot(position.x - state.player.x, position.z - state.player.z);
    if (playerDistance < 2.2 || playerDistance > 16.5) continue;
    const sideScore = side * (position.x - state.player.x) > 0 ? -28 * role.flankBias : 22 * role.flankBias;
    const rangeScore = Math.abs(playerDistance - role.preferredRange) * 0.62;
    const lineScore = fpsArenaLineOfSightClear(state, position.clone().add(new THREE.Vector3(0, 0.45, 0)), playerAim) ? -10 : 12;
    const score = position.distanceTo(mesh.position) + rangeScore + sideScore + lineScore;
    if (!best || score < best.score) best = { score, position };
  }
  return best;
}

function fpsArenaRetreatPoint(state, mesh, role) {
  const cover = fpsArenaPickCoverPoint(state, mesh, role, false);
  if (cover) return cover.position;
  const away = mesh.position.clone().sub(state.player).setY(0);
  if (away.lengthSq() <= 0.0001) away.set(mesh.userData.strafeSign || 1, 0, 0);
  return fpsArenaClampAiPosition(mesh.position.clone().add(away.normalize().multiplyScalar(4.5)));
}

function fpsArenaThinkTacticalState(state, mesh, dist, hasLineOfSight, now) {
  const data = mesh.userData;
  const role = fpsArenaAiRoleMeta(data.role);
  if (hasLineOfSight) {
    data.lastKnownPlayer = state.player.clone();
    data.lastSeenAt = now;
  } else if (state.lastPlayerShotAt && now - state.lastPlayerShotAt < 2400 && state.lastPlayerShotPosition) {
    data.lastKnownPlayer = state.lastPlayerShotPosition.clone();
  }
  if (now < (data.nextThinkAt || 0)) return;
  data.nextThinkAt = now + 260 + Math.random() * 240;
  const lowHealth = data.hp <= Math.max(1, (data.maxHp || 2) * 0.45);
  const cover = fpsArenaPickCoverPoint(state, mesh, role, hasLineOfSight);
  if (lowHealth && cover) {
    const safeCover = fpsArenaPickCoverPoint(state, mesh, role, false) || cover;
    data.aiState = "seekCover";
    data.coverPoint = safeCover.point;
    data.aiTarget = safeCover.position.clone();
    return;
  }
  if (dist < role.retreatRange) {
    data.aiState = "retreat";
    data.aiTarget = fpsArenaRetreatPoint(state, mesh, role);
    return;
  }
  if (hasLineOfSight && role.canShoot && dist <= role.fireRange) {
    if (cover && role.coverBias > 0.55 && mesh.position.distanceTo(cover.position) > 0.8) {
      data.aiState = "seekCover";
      data.coverPoint = cover.point;
      data.aiTarget = cover.position.clone();
    } else {
      data.aiState = role.coverBias > 0.55 ? "peekShoot" : "suppress";
      data.aiTarget = null;
    }
    return;
  }
  const flank = fpsArenaPickFlankPoint(state, mesh, role);
  if ((!hasLineOfSight && data.lastKnownPlayer) || (flank && role.flankBias > 0.7 && dist < 17)) {
    data.aiState = "flank";
    data.aiTarget = flank?.position || data.lastKnownPlayer?.clone() || null;
    return;
  }
  data.aiState = "advance";
  const target = (data.lastKnownPlayer || state.player).clone();
  const fromPlayer = mesh.position.clone().sub(target).setY(0);
  if (fromPlayer.lengthSq() > 0.0001) target.add(fromPlayer.normalize().multiplyScalar(role.preferredRange));
  data.aiTarget = fpsArenaClampAiPosition(target);
}

function fpsArenaUpdateTargets(state, dt, now) {
  state.targets.forEach((mesh) => {
    const kind = mesh.userData.kind;
    if (kind === "target") {
      mesh.userData.phase += dt * 1.8;
      mesh.position.x += Math.sin(mesh.userData.phase) * dt * 1.2;
      mesh.position.y = mesh.userData.baseY + Math.sin(mesh.userData.phase * 1.7) * 0.25;
      mesh.rotation.y += dt * 1.2;
    } else if (kind === "enemy" || kind === "bot") {
      const role = fpsArenaAiRoleMeta(mesh.userData.role);
      const toPlayerRaw = new THREE.Vector3(state.player.x - mesh.position.x, 0, state.player.z - mesh.position.z);
      const dist = Math.max(0.001, toPlayerRaw.length());
      const hasLineOfSight = fpsArenaLineOfSightClear(state, fpsArenaBotEyePosition(mesh), fpsArenaPlayerAimPoint(state));
      fpsArenaThinkTacticalState(state, mesh, dist, hasLineOfSight, now);
      const speed = (kind === "bot" ? 1.35 : 1.08) * (mesh.userData.speed || 1);
      const move = new THREE.Vector3();
      const target = mesh.userData.aiTarget;
      if (target) {
        const toTarget = target.clone().sub(mesh.position).setY(0);
        if (toTarget.lengthSq() > 0.08) move.add(toTarget.normalize().multiplyScalar(speed * dt));
      }
      const toPlayer = toPlayerRaw.clone().normalize();
      const strafe = new THREE.Vector3(-toPlayer.z, 0, toPlayer.x)
        .multiplyScalar((mesh.userData.strafeSign || 1) * Math.sin(now * 0.0025 + mesh.userData.phase) * 0.78 * dt);
      if (["peekShoot", "suppress", "flank"].includes(mesh.userData.aiState)) move.add(strafe);
      if (dist < role.retreatRange && mesh.userData.aiState !== "flank") move.add(toPlayer.clone().multiplyScalar(-speed * 0.85 * dt));
      fpsArenaMoveAiWithCollision(state, mesh, move);
      const lookPoint = mesh.userData.lastKnownPlayer || state.player;
      mesh.lookAt(lookPoint.x, mesh.position.y, lookPoint.z);
      const newDist = Math.hypot(state.player.x - mesh.position.x, state.player.z - mesh.position.z);
      if (newDist < 1.1) {
        state.health -= kind === "bot" ? 16 * dt : 24 * dt;
      }
      fpsArenaUpdateBotFire(state, mesh, newDist, now);
    }
    mesh.userData.breathPhase += dt * 2.2;
    mesh.scale.y = 1 + Math.sin(mesh.userData.breathPhase) * 0.018;
  });
  if (state.bomb) state.bomb.rotation.y += dt * 1.6;
}

function fpsArenaMaybeSpawn(state, now) {
  if (state.mode === "aim") return;
  const level = state.level || FPS_ARENA_LEVELS[0];
  const bossAt = Number(level.boss?.atMs || 36000);
  if (state.mode !== "pvp" && !state.bossActive && !state.bossDefeated && Date.now() - state.startedAt > bossAt) {
    const boss = level.boss || FPS_ARENA_LEVELS[0].boss;
    state.bossActive = true;
    state.bossIntroUntil = now + 2600;
    state.bossLabel = boss.label;
    fpsArenaAddTarget(state, "enemy", {
      role: boss.role,
      hp: boss.hp,
      score: boss.score,
      speed: 0.82,
      boss: true,
      bossLabel: boss.label,
      scale: 1.38,
    });
    updateFpsArenaStatus(`Boss 出現：${boss.label}`);
    window.recordHackmeGameAchievement?.("fps_arena", `boss-encounter-${level.key}`, `${level.label} Boss 橋段`, `遭遇 ${boss.label}。`);
    return;
  }
  const interval = state.mode === "bot" ? 4200 : state.mode === "bomb" ? 5200 : Number(level.spawnInterval || 2600);
  if (now - state.lastSpawnAt < interval) return;
  state.lastSpawnAt = now;
  const count = state.targets.filter((mesh) => mesh.userData.kind === "enemy" || mesh.userData.kind === "bot").length;
  const maxCount = state.mode === "bot" ? 6 : state.mode === "bomb" ? 5 : Number(level.maxEnemies || 8);
  if (count >= maxCount) return;
  fpsArenaAddTarget(state, state.mode === "bot" ? "bot" : "enemy", { hp: state.mode === "bot" ? 2 : 1 + Math.floor(Math.random() * 2) });
}

function fpsArenaLoop(now) {
  const state = fpsArenaState;
  if (!state?.renderer || !state?.scene || !state?.camera) return;
  const dt = Math.min(0.04, Math.max(0.001, (now - state.lastFrame) / 1000));
  state.lastFrame = now;
  if (state.status === "active") {
    fpsArenaMovePlayer(state, dt);
    fpsArenaUpdateBreathing(state, now);
    fpsArenaUpdateFeedback(state, now);
    state.recoilKick = Math.max(0, (state.recoilKick || 0) - dt * 0.08);
    fpsArenaUpdateSniperMode(state, dt);
    fpsArenaApplyCamera(state);
    fpsArenaUpdateTargets(state, dt, now);
    fpsArenaUpdateBotProjectiles(state, dt, now);
    fpsArenaUpdateCombatEffects(state, now);
    fpsArenaUpdatePickups(state, now);
    fpsArenaUpdateBattleRoyaleZone(state, dt);
    fpsArenaMaybeSpawn(state, now);
    syncFpsArenaMultiplayer(state, now);
    if (state.keys[" "] || state.keys.Spacebar) shootFpsArena();
    if (state.keys.e || state.keys.E) attemptFpsArenaDefuse();
    if (Date.now() - state.startedAt >= state.durationMs) finishFpsArenaGame("time");
    if (state.health <= 0) finishFpsArenaGame("health");
    updateFpsArenaHud();
  }
  state.renderer.render(state.scene, state.camera);
  fpsArenaRaf = requestAnimationFrame(fpsArenaLoop);
}

function handleFpsArenaKey(event, pressed) {
  const state = fpsArenaState;
  if (!state || state.status !== "active") return;
    if (["w", "a", "s", "d", "W", "A", "S", "D", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", " ", "Spacebar", "e", "E", "r", "R", "q", "Q", "c", "C", "x", "X", "z", "Z", "Control", "Shift"].includes(event.key)) {
      event.preventDefault();
      state.keys[event.key.length === 1 ? event.key.toLowerCase() : event.key] = pressed;
    }
  if (pressed && (event.key === "r" || event.key === "R")) reloadFpsArena();
  if (pressed && (event.key === "q" || event.key === "Q")) switchFpsArenaWeapon();
}

function handleFpsArenaTouch(action) {
  const state = fpsArenaState;
  if (!state || state.status !== "active") return;
  if (action === "fps-fire") return shootFpsArena();
  if (action === "fps-reload") return reloadFpsArena();
  if (action === "fps-weapon") return switchFpsArenaWeapon();
  if (action === "fps-sprint") {
    state.mobileSprint = !state.mobileSprint;
    if (state.mobileSprint) {
      state.mobileCrouch = false;
      state.mobileProne = false;
    }
    updateFpsArenaStatus(state.mobileSprint ? "衝刺啟動。" : "衝刺關閉。");
    return;
  }
  if (action === "fps-crouch") {
    state.mobileCrouch = !state.mobileCrouch;
    if (state.mobileCrouch) {
      state.mobileProne = false;
      state.mobileSprint = false;
    }
    updateFpsArenaStatus(state.mobileCrouch ? "蹲下，適合利用低掩體。" : "恢復站立。");
    return;
  }
  if (action === "fps-prone") {
    state.mobileProne = !state.mobileProne;
    if (state.mobileProne) {
      state.mobileCrouch = false;
      state.mobileSprint = false;
    }
    updateFpsArenaStatus(state.mobileProne ? "匍匐前進，受彈面積最低。" : "恢復站立。");
    return;
  }
  state.stance = fpsArenaDesiredStance(state);
  const impulse = state.stance === "prone" ? 0.28 : state.stance === "crouch" ? 0.52 : 0.82;
  const forward = new THREE.Vector3(Math.sin(state.yaw), 0, Math.cos(state.yaw) * -1);
  const right = new THREE.Vector3(Math.cos(state.yaw), 0, Math.sin(state.yaw));
  if (action === "fps-forward") fpsArenaMoveWithCollision(state, forward.multiplyScalar(impulse));
  if (action === "fps-back") fpsArenaMoveWithCollision(state, forward.multiplyScalar(-impulse));
  if (action === "fps-right") fpsArenaMoveWithCollision(state, right.multiplyScalar(impulse));
  if (action === "fps-left") fpsArenaMoveWithCollision(state, right.multiplyScalar(-impulse));
}

function handleFpsArenaTouchHold(action, pressed) {
  const state = fpsArenaState;
  if (!state || state.status !== "active") return;
  const keyMap = {
    "fps-forward": "w",
    "fps-back": "s",
    "fps-left": "a",
    "fps-right": "d",
    "fps-fire": " ",
  };
  const key = keyMap[action];
  if (key) {
    state.keys[key] = Boolean(pressed);
    return;
  }
  if (action === "fps-sprint") state.mobileSprint = Boolean(pressed);
}

function fpsArenaApplyLookDelta(state, dx, dy) {
  if (!state || (!dx && !dy)) return;
  const sensitivity = 0.0024 * (state.sniperMode ? FPS_ARENA_SNIPER_LOOK_SCALE : 1);
  state.yaw -= dx * sensitivity;
  state.pitch = Math.max(-1.25, Math.min(1.05, state.pitch - dy * sensitivity));
}

function setFpsArenaSniperMode(enabled, options = {}) {
  const state = fpsArenaState;
  if (!state || state.status !== "active") return;
  const next = Boolean(enabled);
  if (state.sniperMode === next) return;
  state.sniperMode = next;
  if (next) state.mobileSprint = false;
  const stage = $("fps-arena-stage");
  if (stage) {
    stage.classList.toggle("is-sniper-mode", next);
    stage.dataset.aim = next ? "sniper" : "hip";
  }
  if (options.immediate && state.camera) {
    state.cameraFov = next ? FPS_ARENA_SNIPER_FOV : FPS_ARENA_NORMAL_FOV;
    state.camera.fov = state.cameraFov;
    state.camera.updateProjectionMatrix();
  }
  if (options.announce) updateFpsArenaStatus(next ? "狙擊模式。" : "恢復一般瞄準。");
}

function handleFpsArenaPointerMove(event) {
  const state = fpsArenaState;
  if (!state || state.status !== "active") return;
  let dx = event.movementX || 0;
  let dy = event.movementY || 0;
  if (!document.pointerLockElement && fpsArenaPointerDragging && fpsArenaLastPointer) {
    dx = event.clientX - fpsArenaLastPointer.x;
    dy = event.clientY - fpsArenaLastPointer.y;
    fpsArenaLastPointer = { x: event.clientX, y: event.clientY };
  }
  if (!dx && !dy) return;
  fpsArenaApplyLookDelta(state, dx, dy);
}

function handleFpsArenaTouchPointerDown(event) {
  if (event.pointerType === "mouse") return;
  const stage = event.target?.closest?.("#fps-arena-stage");
  if (!stage || !fpsArenaState || fpsArenaState.status !== "active") return;
  event.preventDefault();
  fpsArenaTouchPointerId = event.pointerId;
  fpsArenaTouchMoved = false;
  fpsArenaLastPointer = { x: event.clientX, y: event.clientY };
  try {
    stage.setPointerCapture?.(event.pointerId);
  } catch (err) {}
}

function handleFpsArenaTouchPointerMove(event) {
  if (event.pointerType === "mouse" || fpsArenaTouchPointerId !== event.pointerId || !fpsArenaLastPointer) return;
  const state = fpsArenaState;
  if (!state || state.status !== "active") return;
  event.preventDefault();
  const dx = event.clientX - fpsArenaLastPointer.x;
  const dy = event.clientY - fpsArenaLastPointer.y;
  if (Math.hypot(dx, dy) > 3) fpsArenaTouchMoved = true;
  fpsArenaLastPointer = { x: event.clientX, y: event.clientY };
  fpsArenaApplyLookDelta(state, dx, dy);
}

function handleFpsArenaTouchPointerEnd(event) {
  if (event.pointerType === "mouse" || fpsArenaTouchPointerId !== event.pointerId) return;
  const stage = event.target?.closest?.("#fps-arena-stage");
  event.preventDefault();
  if (event.type === "pointerup" && !fpsArenaTouchMoved) shootFpsArena();
  try {
    stage?.releasePointerCapture?.(event.pointerId);
  } catch (err) {}
  fpsArenaTouchPointerId = null;
  fpsArenaTouchMoved = false;
  fpsArenaLastPointer = null;
}

document.addEventListener("mousemove", handleFpsArenaPointerMove);
document.addEventListener("mouseup", (event) => {
  if (event.button === 2 && fpsArenaState?.sniperMode) setFpsArenaSniperMode(false);
  fpsArenaPointerDragging = false;
  fpsArenaLastPointer = null;
});
document.addEventListener("mousedown", (event) => {
  const stage = event.target?.closest?.("#fps-arena-stage");
  if (!stage || !fpsArenaState || fpsArenaState.status !== "active") return;
  event.preventDefault();
  fpsArenaPointerDragging = true;
  fpsArenaLastPointer = { x: event.clientX, y: event.clientY };
  if (stage.requestPointerLock && !document.pointerLockElement) stage.requestPointerLock();
  if (event.button === 0) shootFpsArena();
  if (event.button === 2) setFpsArenaSniperMode(true, { announce: true });
});
document.addEventListener("contextmenu", (event) => {
  if (!event.target?.closest?.("#fps-arena-stage")) return;
  event.preventDefault();
});
window.addEventListener("blur", () => setFpsArenaSniperMode(false));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) setFpsArenaSniperMode(false);
});
document.addEventListener("pointerdown", handleFpsArenaTouchPointerDown);
document.addEventListener("pointermove", handleFpsArenaTouchPointerMove);
document.addEventListener("pointerup", handleFpsArenaTouchPointerEnd);
document.addEventListener("pointercancel", handleFpsArenaTouchPointerEnd);

window.addEventListener("resize", fpsArenaResizeRenderer);
window.currentFpsArenaMode = fpsArenaMode;
window.currentFpsArenaDifficulty = fpsArenaDifficultyKey;
window.isFpsArenaActive = isFpsArenaActive;
window.renderFpsArenaBoard = renderFpsArenaBoard;
window.updateFpsArenaStatus = updateFpsArenaStatus;
window.startFpsArenaGame = startFpsArenaGame;
window.finishFpsArenaGame = finishFpsArenaGame;
window.suspendFpsArenaGame = suspendFpsArenaGame;
window.handleFpsArenaKey = handleFpsArenaKey;
window.handleFpsArenaTouch = handleFpsArenaTouch;
window.handleFpsArenaTouchHold = handleFpsArenaTouchHold;
