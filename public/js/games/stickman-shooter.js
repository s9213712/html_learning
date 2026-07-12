'use strict';

(function () {
  const { clamp } = window.HACKME_LOCAL_GAME_HELPERS;
  const WIDTH = 720;
  const HEIGHT = 360;
  const GRAVITY = 0.72;
  const PLAYER_W = 22;
  const PLAYER_H = 46;
  const WORLD_ROOMS = 4;
  const ROOM_WIDTH = WIDTH;
  const WORLD_WIDTH = ROOM_WIDTH * WORLD_ROOMS;
  const RELOAD_TICKS = 54;
  const POWERUP_SIZE = 20;
  const MULTIPLAYER_SYNC_TICKS = 12;

  const MODES = [
    { key: "standard", label: "標準", enemyHp: 0, enemyShots: 0, reserve: 45 },
    { key: "rush", label: "突襲", enemyHp: 1, enemyShots: 10, reserve: 36 },
    { key: "survival", label: "生存", enemyHp: 2, enemyShots: 18, reserve: 30 },
    { key: "hazard", label: "陷阱工廠", enemyHp: 1, enemyShots: 14, reserve: 34 },
    { key: "kaizo", label: "即死實驗", enemyHp: 2, enemyShots: 22, reserve: 30 },
  ];

  const STICKMAN_WEAPONS = {
    rifle: { key: "rifle", label: "突擊步槍", mag: 9, reserve: 0, delay: 8, speed: 12.5, damage: 1, spread: [0], pierce: 0, color: "#fef08a" },
    scatter: { key: "scatter", label: "霰彈槍", mag: 6, reserve: -6, delay: 15, speed: 11.8, damage: 1, spread: [-0.14, -0.05, 0.05, 0.14], pierce: 0, color: "#fbbf24" },
    pulse: { key: "pulse", label: "脈衝槍", mag: 12, reserve: 8, delay: 6, speed: 13.8, damage: 1, spread: [-0.035, 0.035], pierce: 1, color: "#67e8f9" },
    rail: { key: "rail", label: "穿甲軌道槍", mag: 5, reserve: -12, delay: 20, speed: 15.5, damage: 3, spread: [0], pierce: 3, color: "#c4b5fd" },
  };

  const STICKMAN_LEVELS = [
    {
      key: "dock",
      label: "第 1 關 貨櫃碼頭",
      weapon: "rifle",
      mechanic: "壓板門",
      bg: ["#07111f", "#12324f"],
      roles: [["rifle", "rusher"], ["rifle", "rusher", "shield"], ["sniper", "rifle", "rusher"], ["shield", "boss", "rifle"]],
      boss: { label: "貨櫃鎮暴者", hp: 22, role: "boss" },
      enemyHp: 0,
      enemyCountBonus: 0,
      trapSpeed: 1,
    },
    {
      key: "reactor",
      label: "第 2 關 反應爐",
      weapon: "scatter",
      mechanic: "電流地板",
      bg: ["#0f172a", "#164e63"],
      roles: [["shield", "rusher"], ["grenadier", "rifle", "shield"], ["sniper", "grenadier", "shield"], ["grenadier", "boss", "sniper"]],
      boss: { label: "反應爐爆破手", hp: 30, role: "grenadier" },
      enemyHp: 1,
      enemyCountBonus: 1,
      trapSpeed: 1.18,
    },
    {
      key: "skyline",
      label: "第 3 關 高架工廠",
      weapon: "pulse",
      mechanic: "巡邏無人機",
      bg: ["#111827", "#312e81"],
      roles: [["ambusher", "rusher"], ["ambusher", "sniper", "shield"], ["grenadier", "sniper", "ambusher"], ["boss", "ambusher", "grenadier"]],
      boss: { label: "高架獵手", hp: 36, role: "ambusher" },
      enemyHp: 2,
      enemyCountBonus: 1,
      trapSpeed: 1.35,
    },
    {
      key: "core",
      label: "第 4 關 核心實驗室",
      weapon: "rail",
      mechanic: "連續雷射與雙 Boss",
      bg: ["#170f1f", "#4c1d95"],
      roles: [["sniper", "ambusher"], ["grenadier", "shield", "sniper"], ["boss", "ambusher", "grenadier"], ["boss", "grenadier", "sniper"]],
      boss: { label: "核心雙子", hp: 42, role: "boss", twins: true },
      enemyHp: 3,
      enemyCountBonus: 2,
      trapSpeed: 1.55,
    },
  ];

  const POWERUP_META = {
    mushroom: { label: "蘑菇", glyph: "M", color: "#fb7185" },
    fireFlower: { label: "火焰花", glyph: "F", color: "#f97316" },
    star: { label: "無敵星", glyph: "★", color: "#fde047" },
    spring: { label: "彈跳鞋", glyph: "↟", color: "#38bdf8" },
    ammo: { label: "彈藥包", glyph: "A", color: "#a3e635" },
    shield: { label: "護盾", glyph: "S", color: "#93c5fd" },
  };

  const STICKMAN_ASSET_SOURCES = Object.freeze({
    platformer: {
      name: "Kenney New Platformer Pack",
      url: "https://kenney.nl/assets/new-platformer-pack",
      license: "Creative Commons CC0",
      usage: "bundled PNG character, enemy, tile, trap, pickup and background assets with canvas fallback",
    },
  });
  const STICKMAN_ASSET_BASE = "/assets/games/vendor/kenney/new-platformer-pack/";
  const STICKMAN_IMAGE_ASSETS = Object.freeze({
    backgroundTrees: `${STICKMAN_ASSET_BASE}backgrounds/trees.png`,
    backgroundHills: `${STICKMAN_ASSET_BASE}backgrounds/hills.png`,
    backgroundDesert: `${STICKMAN_ASSET_BASE}backgrounds/desert.png`,
    backgroundClouds: `${STICKMAN_ASSET_BASE}backgrounds/clouds.png`,
    playerIdle: `${STICKMAN_ASSET_BASE}characters/player_idle.png`,
    playerWalkA: `${STICKMAN_ASSET_BASE}characters/player_walk_a.png`,
    playerWalkB: `${STICKMAN_ASSET_BASE}characters/player_walk_b.png`,
    playerJump: `${STICKMAN_ASSET_BASE}characters/player_jump.png`,
    playerHit: `${STICKMAN_ASSET_BASE}characters/player_hit.png`,
    coopIdle: `${STICKMAN_ASSET_BASE}characters/coop_idle.png`,
    coopWalkA: `${STICKMAN_ASSET_BASE}characters/coop_walk_a.png`,
    enemySlime: `${STICKMAN_ASSET_BASE}enemies/slime_walk_a.png`,
    enemyFireSlime: `${STICKMAN_ASSET_BASE}enemies/slime_fire_walk_a.png`,
    enemyBee: `${STICKMAN_ASSET_BASE}enemies/bee_a.png`,
    enemyFrog: `${STICKMAN_ASSET_BASE}enemies/frog_jump.png`,
    enemyMouse: `${STICKMAN_ASSET_BASE}enemies/mouse_walk_a.png`,
    enemySaw: `${STICKMAN_ASSET_BASE}enemies/saw_a.png`,
    terrainGrass: `${STICKMAN_ASSET_BASE}tiles/terrain_grass_center.png`,
    terrainGrassTop: `${STICKMAN_ASSET_BASE}tiles/terrain_grass_top.png`,
    terrainDirt: `${STICKMAN_ASSET_BASE}tiles/terrain_dirt_center.png`,
    terrainStone: `${STICKMAN_ASSET_BASE}tiles/terrain_stone_center.png`,
    blockBlue: `${STICKMAN_ASSET_BASE}tiles/block_blue.png`,
    blockRed: `${STICKMAN_ASSET_BASE}tiles/block_red.png`,
    blockYellow: `${STICKMAN_ASSET_BASE}tiles/block_yellow.png`,
    blockCoin: `${STICKMAN_ASSET_BASE}tiles/block_coin.png`,
    blockExclamation: `${STICKMAN_ASSET_BASE}tiles/block_exclamation.png`,
    spikes: `${STICKMAN_ASSET_BASE}tiles/spikes.png`,
    saw: `${STICKMAN_ASSET_BASE}tiles/saw.png`,
    spring: `${STICKMAN_ASSET_BASE}tiles/spring.png`,
    coin: `${STICKMAN_ASSET_BASE}tiles/coin_gold.png`,
    heart: `${STICKMAN_ASSET_BASE}tiles/heart.png`,
    gemBlue: `${STICKMAN_ASSET_BASE}tiles/gem_blue.png`,
    gemRed: `${STICKMAN_ASSET_BASE}tiles/gem_red.png`,
    mushroom: `${STICKMAN_ASSET_BASE}tiles/mushroom_red.png`,
    rock: `${STICKMAN_ASSET_BASE}tiles/rock.png`,
    bush: `${STICKMAN_ASSET_BASE}tiles/bush.png`,
  });
  const STICKMAN_IMAGES = {};

  function stickmanImageFor(key) {
    const src = STICKMAN_IMAGE_ASSETS[key];
    if (!src || typeof Image === "undefined") return null;
    if (!STICKMAN_IMAGES[key]) {
      const image = new Image();
      image.decoding = "async";
      image.src = src;
      STICKMAN_IMAGES[key] = image;
    }
    return STICKMAN_IMAGES[key];
  }

  function stickmanImageReady(image) {
    return Boolean(image?.complete && image.naturalWidth > 0);
  }

  function drawStickmanImage(ctx, key, x, y, w, h, options = {}) {
    const image = stickmanImageFor(key);
    if (!stickmanImageReady(image)) return false;
    ctx.save();
    ctx.globalAlpha = options.alpha ?? 1;
    ctx.translate(x + w / 2, y + h / 2);
    if (options.rotation) ctx.rotate(options.rotation);
    if (options.flipX || options.flipY) ctx.scale(options.flipX ? -1 : 1, options.flipY ? -1 : 1);
    ctx.drawImage(image, -w / 2, -h / 2, w, h);
    ctx.restore();
    return true;
  }

  function drawStickmanTiledImage(ctx, key, x, y, w, h, tileW = 24, tileH = tileW, options = {}) {
    const image = stickmanImageFor(key);
    if (!stickmanImageReady(image)) return false;
    ctx.save();
    ctx.globalAlpha = options.alpha ?? 1;
    for (let py = y; py < y + h; py += tileH) {
      for (let px = x; px < x + w; px += tileW) {
        ctx.drawImage(image, px, py, Math.min(tileW, x + w - px), Math.min(tileH, y + h - py));
      }
    }
    ctx.restore();
    return true;
  }

  const STICKMAN_ENEMY_ROLES = {
    rifle: {
      color: "#fda4af",
      accent: "#7f1d1d",
      hpBonus: 0,
      speedScale: 1,
      maxSpeed: 1.18,
      sightRange: 500,
      fireRange: 540,
      preferredRange: 240,
      retreatRange: 92,
      fireDelay: 92,
      spread: 0.035,
      burst: 1,
      flankOffset: 92,
      coverBias: 0.45,
      canShoot: true,
    },
    rusher: {
      color: "#fdba74",
      accent: "#9a3412",
      hpBonus: 0,
      speedScale: 1.22,
      maxSpeed: 1.42,
      sightRange: 420,
      fireRange: 270,
      preferredRange: 64,
      retreatRange: 28,
      fireDelay: 126,
      spread: 0.07,
      burst: 1,
      flankOffset: 64,
      coverBias: 0.15,
      canShoot: true,
    },
    shield: {
      color: "#c4b5fd",
      accent: "#4c1d95",
      hpBonus: 2,
      speedScale: 0.86,
      maxSpeed: 1.02,
      sightRange: 455,
      fireRange: 410,
      preferredRange: 155,
      retreatRange: 44,
      fireDelay: 108,
      spread: 0.052,
      burst: 1,
      flankOffset: 58,
      coverBias: 0.3,
      canShoot: true,
    },
    sniper: {
      color: "#bae6fd",
      accent: "#075985",
      hpBonus: -1,
      speedScale: 0.82,
      maxSpeed: 0.95,
      sightRange: 650,
      fireRange: 650,
      preferredRange: 340,
      retreatRange: 170,
      fireDelay: 138,
      spread: 0.012,
      burst: 1,
      flankOffset: 130,
      coverBias: 0.82,
      canShoot: true,
    },
    ambusher: {
      color: "#86efac",
      accent: "#14532d",
      hpBonus: 0,
      speedScale: 1.18,
      maxSpeed: 1.34,
      sightRange: 560,
      fireRange: 450,
      preferredRange: 118,
      retreatRange: 52,
      fireDelay: 82,
      spread: 0.042,
      burst: 2,
      flankOffset: 150,
      coverBias: 0.62,
      canShoot: true,
    },
    grenadier: {
      color: "#fde68a",
      accent: "#713f12",
      hpBonus: 1,
      speedScale: 0.9,
      maxSpeed: 1.04,
      sightRange: 610,
      fireRange: 560,
      preferredRange: 280,
      retreatRange: 118,
      fireDelay: 118,
      spread: 0.095,
      burst: 1,
      flankOffset: 112,
      coverBias: 0.78,
      canShoot: true,
    },
    boss: {
      color: "#f97316",
      accent: "#7c2d12",
      hpBonus: 0,
      speedScale: 1,
      maxSpeed: 1.45,
      sightRange: 660,
      fireRange: 620,
      preferredRange: 215,
      retreatRange: 82,
      fireDelay: 66,
      spread: 0.08,
      burst: 3,
      flankOffset: 118,
      coverBias: 0.2,
      canShoot: true,
    },
  };

  function stickmanRandom(state) {
    return typeof state?.rng === "function" ? state.rng() : Math.random();
  }

  function currentStickmanLevel(api) {
    return STICKMAN_LEVELS[api?._stickmanLevelIndex || 0] || STICKMAN_LEVELS[0];
  }

  function stickmanLevelFromState(state) {
    return state?.level || STICKMAN_LEVELS[state?.levelIndex || 0] || STICKMAN_LEVELS[0];
  }

  function stickmanWeapon(state) {
    return STICKMAN_WEAPONS[state?.weaponKey] || STICKMAN_WEAPONS.rifle;
  }

  function rectsOverlap(a, b) {
    return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
  }

  function addStickmanParticles(state, x, y, color, count = 10) {
    for (let i = 0; i < count; i += 1) {
      const angle = stickmanRandom(state) * Math.PI * 2;
      const speed = 0.8 + stickmanRandom(state) * 2.8;
      state.particles.push({
        x,
        y,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed - 0.4,
        life: 16 + stickmanRandom(state) * 18,
        color,
      });
    }
  }

  function makeStickmanWorld(level = STICKMAN_LEVELS[0]) {
    const layouts = {
      dock: [
        { x: 130, y: 265, w: 126, h: 12, type: "catwalk" },
        { x: 238, y: 212, w: 92, h: 12, type: "container" },
        { x: 344, y: 236, w: 112, h: 12, type: "cratewalk" },
        { x: 468, y: 192, w: 112, h: 12, type: "crane" },
        { x: 570, y: 278, w: 118, h: 12, type: "catwalk" },
        { x: 724, y: 226, w: 96, h: 12, type: "container" },
        { x: 802, y: 274, w: 136, h: 12, type: "catwalk" },
        { x: 1012, y: 232, w: 128, h: 12, type: "pipe" },
        { x: 1216, y: 276, w: 132, h: 12, type: "catwalk" },
        { x: 1392, y: 210, w: 108, h: 12, type: "container" },
        { x: 1502, y: 258, w: 118, h: 12, type: "catwalk" },
        { x: 1628, y: 188, w: 92, h: 12, type: "crane" },
        { x: 1888, y: 282, w: 160, h: 12, type: "catwalk" },
        { x: 2110, y: 214, w: 122, h: 12, type: "container" },
        { x: 2206, y: 266, w: 150, h: 12, type: "catwalk" },
        { x: 2586, y: 184, w: 96, h: 12, type: "crane" },
        { x: 2660, y: 286, w: 150, h: 12, type: "catwalk" },
      ],
      reactor: [
        { x: 188, y: 210, w: 102, h: 12, type: "pipe" },
        { x: 334, y: 272, w: 148, h: 12, type: "reactor" },
        { x: 488, y: 188, w: 92, h: 12, type: "vent" },
        { x: 706, y: 214, w: 96, h: 12, type: "pipe" },
        { x: 958, y: 176, w: 118, h: 12, type: "reactor" },
        { x: 1164, y: 258, w: 148, h: 12, type: "pipe" },
        { x: 1332, y: 206, w: 112, h: 12, type: "pipe" },
        { x: 1578, y: 176, w: 112, h: 12, type: "reactor" },
        { x: 1786, y: 246, w: 142, h: 12, type: "vent" },
        { x: 2108, y: 218, w: 126, h: 12, type: "pipe" },
        { x: 2264, y: 272, w: 130, h: 12, type: "reactor" },
        { x: 2428, y: 188, w: 104, h: 12, type: "vent" },
        { x: 2688, y: 212, w: 96, h: 12, type: "pipe" },
      ],
      skyline: [
        { x: 116, y: 282, w: 138, h: 12, type: "girder" },
        { x: 280, y: 196, w: 92, h: 12, type: "girder" },
        { x: 470, y: 202, w: 118, h: 12, type: "girder" },
        { x: 762, y: 174, w: 106, h: 12, type: "billboard" },
        { x: 930, y: 262, w: 124, h: 12, type: "girder" },
        { x: 1180, y: 188, w: 138, h: 12, type: "girder" },
        { x: 1452, y: 160, w: 108, h: 12, type: "antenna" },
        { x: 1646, y: 274, w: 132, h: 12, type: "girder" },
        { x: 1970, y: 176, w: 148, h: 12, type: "girder" },
        { x: 2196, y: 146, w: 96, h: 12, type: "antenna" },
        { x: 2398, y: 198, w: 118, h: 12, type: "girder" },
        { x: 2642, y: 172, w: 122, h: 12, type: "billboard" },
      ],
      core: [
        { x: 118, y: 262, w: 112, h: 12, type: "core-rail" },
        { x: 342, y: 206, w: 92, h: 12, type: "core-rail" },
        { x: 610, y: 198, w: 122, h: 12, type: "core-rail" },
        { x: 824, y: 164, w: 92, h: 12, type: "core-node" },
        { x: 1020, y: 178, w: 112, h: 12, type: "core-node" },
        { x: 1286, y: 148, w: 96, h: 12, type: "core-rail" },
        { x: 1510, y: 202, w: 118, h: 12, type: "core-rail" },
        { x: 1648, y: 274, w: 120, h: 12, type: "core-node" },
        { x: 1768, y: 160, w: 102, h: 12, type: "core-node" },
        { x: 2024, y: 184, w: 126, h: 12, type: "core-rail" },
        { x: 2272, y: 154, w: 108, h: 12, type: "core-node" },
        { x: 2508, y: 192, w: 132, h: 12, type: "core-rail" },
        { x: 2706, y: 146, w: 98, h: 12, type: "core-node" },
      ],
    };
    return [
      { x: 0, y: 318, w: WORLD_WIDTH, h: 42, type: "ground" },
      ...(layouts[level.key] || layouts.dock),
    ];
  }

  function makeStickmanScenery(level = STICKMAN_LEVELS[0]) {
    const key = level.key || "dock";
    const common = [
      { kind: "asset-backdrop", x: -180, y: 178, w: 1180, h: 118, parallax: 0.34, variant: key },
      { kind: "asset-backdrop", x: 760, y: 168, w: 1280, h: 132, parallax: 0.34, variant: key },
      { kind: "asset-backdrop", x: 1780, y: 184, w: 1180, h: 112, parallax: 0.34, variant: key },
      { kind: "room-marker", x: 0, label: "A" },
      { kind: "room-marker", x: ROOM_WIDTH, label: "B" },
      { kind: "room-marker", x: ROOM_WIDTH * 2, label: "C" },
      { kind: "room-marker", x: ROOM_WIDTH * 3, label: "BOSS" },
    ];
    if (key === "dock") {
      return [
        ...common,
        { kind: "container-stack", x: 72, y: 256, rows: 2, cols: 3 },
        { kind: "asset-tile-cluster", x: 282, y: 292, rows: 2, cols: 5, tile: "container" },
        { kind: "asset-prop", x: 356, y: 234, w: 54, h: 68, prop: "lamp" },
        { kind: "crane", x: 430, y: 82, w: 210, h: 178 },
        { kind: "ship", x: 880, y: 245, w: 330, h: 58 },
        { kind: "asset-prop", x: 1116, y: 214, w: 72, h: 54, prop: "sign", label: "DOCK" },
        { kind: "container-stack", x: 1288, y: 236, rows: 3, cols: 4 },
        { kind: "crane", x: 1600, y: 66, w: 250, h: 205 },
        { kind: "asset-tile-cluster", x: 1776, y: 286, rows: 2, cols: 6, tile: "crate" },
        { kind: "ship", x: 2056, y: 250, w: 360, h: 54 },
        { kind: "container-stack", x: 2520, y: 248, rows: 2, cols: 5 },
        { kind: "asset-prop", x: 2664, y: 222, w: 68, h: 54, prop: "sign", label: "BOSS" },
      ];
    }
    if (key === "reactor") {
      return [
        ...common,
        { kind: "reactor-core", x: 238, y: 116, w: 118, h: 172 },
        { kind: "asset-prop", x: 404, y: 244, w: 70, h: 60, prop: "hazard" },
        { kind: "pipe-field", x: 642, y: 82, w: 260, h: 224 },
        { kind: "asset-tile-cluster", x: 842, y: 286, rows: 2, cols: 4, tile: "warning" },
        { kind: "reactor-core", x: 1128, y: 92, w: 148, h: 198 },
        { kind: "pipe-field", x: 1518, y: 96, w: 310, h: 204 },
        { kind: "asset-prop", x: 1828, y: 222, w: 84, h: 76, prop: "console" },
        { kind: "reactor-core", x: 2140, y: 110, w: 132, h: 180 },
        { kind: "pipe-field", x: 2466, y: 78, w: 284, h: 218 },
        { kind: "asset-tile-cluster", x: 2648, y: 280, rows: 3, cols: 4, tile: "warning" },
      ];
    }
    if (key === "skyline") {
      return [
        ...common,
        { kind: "tower", x: 96, y: 116, w: 108, h: 202 },
        { kind: "asset-prop", x: 258, y: 222, w: 66, h: 66, prop: "fan" },
        { kind: "billboard", x: 554, y: 88, w: 160, h: 56, label: "RUN" },
        { kind: "tower", x: 916, y: 80, w: 132, h: 238 },
        { kind: "asset-tile-cluster", x: 1038, y: 280, rows: 2, cols: 5, tile: "neon" },
        { kind: "skybridge", x: 1210, y: 118, w: 248, h: 24 },
        { kind: "billboard", x: 1662, y: 76, w: 190, h: 58, label: "CO-OP" },
        { kind: "asset-prop", x: 1806, y: 204, w: 76, h: 74, prop: "antenna" },
        { kind: "tower", x: 2030, y: 58, w: 126, h: 260 },
        { kind: "skybridge", x: 2380, y: 132, w: 266, h: 22 },
        { kind: "asset-prop", x: 2602, y: 212, w: 84, h: 64, prop: "fan" },
      ];
    }
    return [
      ...common,
      { kind: "core-chamber", x: 142, y: 86, w: 170, h: 232 },
      { kind: "asset-prop", x: 348, y: 232, w: 72, h: 70, prop: "coil" },
      { kind: "core-spine", x: 538, y: 58, w: 106, h: 252 },
      { kind: "asset-tile-cluster", x: 702, y: 282, rows: 2, cols: 4, tile: "core" },
      { kind: "core-chamber", x: 926, y: 74, w: 190, h: 244 },
      { kind: "core-spine", x: 1370, y: 40, w: 118, h: 270 },
      { kind: "asset-prop", x: 1548, y: 216, w: 86, h: 82, prop: "console" },
      { kind: "core-chamber", x: 1822, y: 62, w: 198, h: 256 },
      { kind: "core-spine", x: 2260, y: 44, w: 126, h: 274 },
      { kind: "asset-tile-cluster", x: 2366, y: 280, rows: 3, cols: 4, tile: "core" },
      { kind: "core-chamber", x: 2628, y: 54, w: 196, h: 264 },
      { kind: "asset-prop", x: 2740, y: 224, w: 74, h: 72, prop: "coil" },
    ];
  }

  function makeStickmanCover(level = STICKMAN_LEVELS[0]) {
    const cover = [
      { x: 412, y: 282, w: 24, h: 36 },
      { x: 910, y: 238, w: 28, h: 36 },
      { x: 1260, y: 240, w: 30, h: 36 },
      { x: 1836, y: 184, w: 26, h: 36 },
      { x: 2354, y: 230, w: 32, h: 36 },
    ];
    if (level.key === "reactor") cover.push({ x: 1548, y: 166, w: 30, h: 40 }, { x: 2180, y: 182, w: 28, h: 36 });
    if (level.key === "skyline") cover.push({ x: 520, y: 166, w: 28, h: 36 }, { x: 2068, y: 140, w: 30, h: 36 });
    if (level.key === "core") cover.push({ x: 1058, y: 142, w: 30, h: 36 }, { x: 2068, y: 148, w: 30, h: 36 }, { x: 2586, y: 156, w: 30, h: 36 });
    return cover;
  }

  function makeStickmanTraps(level = STICKMAN_LEVELS[0]) {
    const speed = Number(level.trapSpeed || 1);
    const traps = [
      { type: "spikes", x: 284, y: 310, w: 72, h: 8, lethal: true },
      { type: "laser", x: 704, y: 168, w: 12, h: 150, period: Math.round(150 / speed), active: 76, phase: 28, lethal: true },
      { type: "saw", x: 1084, y: 302, r: 16, range: 44, phase: 12, lethal: true },
      { type: "spikes", x: 1362, y: 310, w: 88, h: 8, lethal: true },
      { type: "crusher", x: 1634, y: 108, w: 58, h: 72, drop: 128, period: Math.round(156 / speed), phase: 18, lethal: true },
      { type: "laser", x: 2026, y: 158, w: 12, h: 160, period: Math.round(132 / speed), active: 64, phase: 80, lethal: true },
      { type: "saw", x: 2324, y: 258, r: 15, range: 52, phase: 42, lethal: true },
      { type: "spikes", x: 2558, y: 310, w: 92, h: 8, lethal: true },
    ];
    if (level.key === "reactor" || level.key === "core") {
      traps.push(
        { type: "electric", x: 942, y: 304, w: 124, h: 12, period: Math.round(118 / speed), active: 58, phase: 12, lethal: true },
        { type: "electric", x: 1814, y: 304, w: 112, h: 12, period: Math.round(104 / speed), active: 52, phase: 50, lethal: true },
      );
    }
    if (level.key === "skyline" || level.key === "core") {
      traps.push(
        { type: "drone", x: 1238, y: 154, w: 96, h: 12, range: 88, period: Math.round(132 / speed), active: 82, phase: 32, lethal: true },
        { type: "drone", x: 2268, y: 150, w: 112, h: 12, range: 104, period: Math.round(126 / speed), active: 76, phase: 72, lethal: true },
      );
    }
    if (level.key === "core") {
      traps.push(
        { type: "laser", x: 2768, y: 110, w: 12, h: 208, period: 72, active: 44, phase: 20, lethal: true },
        { type: "crusher", x: 2462, y: 82, w: 70, h: 82, drop: 150, period: 92, phase: 18, lethal: true },
      );
    }
    return traps;
  }

  function makeStickmanCoopPuzzles(level = STICKMAN_LEVELS[0]) {
    const extraPlate = level.key === "core"
      ? [{ id: "gate-c", x: 2306, y: 306, w: 58, h: 12, label: "C" }]
      : [];
    const extraGate = level.key === "core"
      ? [{ id: "gate-c", x: 2408, y: 150, w: 28, h: 168, type: "hold", label: "CORE" }]
      : [];
    return {
      plates: [
        { id: "gate-a", x: 520, y: 306, w: 58, h: 12, label: "A" },
        { id: "gate-b-left", x: 1518, y: 306, w: 54, h: 12, label: "B1" },
        { id: "gate-b-right", x: 1644, y: 306, w: 54, h: 12, label: "B2" },
        ...extraPlate,
      ],
      gates: [
        { id: "gate-a", x: 762, y: 188, w: 24, h: 130, type: "hold", label: "HOLD" },
        { id: "gate-b", x: 1786, y: 180, w: 28, h: 138, type: "dual", label: "DUAL" },
        ...extraGate,
      ],
    };
  }

  function makeStickmanCrates(level = STICKMAN_LEVELS[0]) {
    const crates = [
      { x: 205, y: 239, w: 24, h: 24, hp: 1, power: "mushroom" },
      { x: 384, y: 210, w: 24, h: 24, hp: 1, power: "fireFlower" },
      { x: 836, y: 248, w: 24, h: 24, hp: 1, power: "ammo" },
      { x: 1110, y: 206, w: 24, h: 24, hp: 1, power: "shield" },
      { x: 1540, y: 232, w: 24, h: 24, hp: 1, power: "spring" },
      { x: 1888, y: 256, w: 24, h: 24, hp: 1, power: "star" },
      { x: 2250, y: 240, w: 24, h: 24, hp: 1, power: "fireFlower" },
      { x: 2688, y: 260, w: 24, h: 24, hp: 1, power: "ammo" },
    ];
    if (level.key !== "dock") crates.push({ x: 1472, y: 180, w: 24, h: 24, hp: 1, power: "star" });
    if (level.key === "core") crates.push({ x: 2522, y: 164, w: 24, h: 24, hp: 1, power: "shield" });
    return crates;
  }

  function makeStickmanPowerups(level = STICKMAN_LEVELS[0]) {
    const powerups = [
      { kind: "ammo", x: 620, y: 250, w: POWERUP_SIZE, h: POWERUP_SIZE, life: 99999 },
      { kind: "spring", x: 1282, y: 248, w: POWERUP_SIZE, h: POWERUP_SIZE, life: 99999 },
      { kind: "shield", x: 2384, y: 204, w: POWERUP_SIZE, h: POWERUP_SIZE, life: 99999 },
    ];
    if (level.key === "reactor") powerups.push({ kind: "ammo", x: 1014, y: 186, w: POWERUP_SIZE, h: POWERUP_SIZE, life: 99999 });
    if (level.key === "skyline") powerups.push({ kind: "spring", x: 2050, y: 148, w: POWERUP_SIZE, h: POWERUP_SIZE, life: 99999 });
    if (level.key === "core") powerups.push({ kind: "star", x: 1038, y: 150, w: POWERUP_SIZE, h: POWERUP_SIZE, life: 99999 });
    return powerups;
  }

  function currentStickmanTrapRect(state, trap) {
    if (trap.type === "spikes") return { x: trap.x, y: trap.y, w: trap.w, h: trap.h };
    if (trap.type === "laser") {
      const step = (state.tick + trap.phase) % trap.period;
      if (step >= trap.active) return null;
      return { x: trap.x, y: trap.y, w: trap.w, h: trap.h };
    }
    if (trap.type === "electric") {
      const step = (state.tick + trap.phase) % trap.period;
      if (step >= trap.active) return null;
      return { x: trap.x, y: trap.y, w: trap.w, h: trap.h };
    }
    if (trap.type === "drone") {
      const step = (state.tick + trap.phase) % trap.period;
      if (step >= trap.active) return null;
      const x = trap.x + Math.sin((state.tick + trap.phase) / 34) * trap.range;
      return { x, y: trap.y, w: trap.w, h: trap.h };
    }
    if (trap.type === "saw") {
      const x = trap.x + Math.sin((state.tick + trap.phase) / 42) * trap.range;
      return { x: x - trap.r, y: trap.y - trap.r, w: trap.r * 2, h: trap.r * 2 };
    }
    if (trap.type === "crusher") {
      const step = (state.tick + trap.phase) % trap.period;
      const t = step < 34 ? step / 34 : (step < 92 ? 1 : Math.max(0, 1 - (step - 92) / 64));
      const y = trap.y + trap.drop * t;
      return { x: trap.x, y, w: trap.w, h: trap.h };
    }
    return null;
  }

  function groundYAt(state, x) {
    const candidates = state.platforms.filter((platform) => x >= platform.x - 10 && x <= platform.x + platform.w + 10);
    const best = candidates.sort((a, b) => a.y - b.y).find((platform) => platform.y > 90);
    return best ? best.y : 318;
  }

  function stickmanEnemyRoleMeta(enemy) {
    return STICKMAN_ENEMY_ROLES[enemy?.aiRole] || STICKMAN_ENEMY_ROLES[enemy?.kind] || STICKMAN_ENEMY_ROLES.rifle;
  }

  function stickmanRoleForRoom(state, room, index) {
    const level = stickmanLevelFromState(state);
    const pools = level.roles || [
      ["rifle", "rusher"],
      ["rifle", "rusher", "shield"],
      ["sniper", "rifle", "rusher", "shield"],
      ["sniper", "shield", "rifle", "rusher"],
    ];
    const pool = pools[Math.max(0, Math.min(pools.length - 1, room - 1))];
    return pool[(index + room) % pool.length];
  }

  function stickmanEnemySpawnOffset(level, room, index) {
    const plans = {
      dock: [
        [248, 384, 552, 646],
        [120, 338, 514, 650],
        [156, 310, 510, 638],
        [190, 356, 520, 636],
      ],
      reactor: [
        [168, 352, 548, 632],
        [104, 286, 486, 630],
        [224, 402, 560, 660],
        [148, 336, 516, 628],
      ],
      skyline: [
        [116, 300, 504, 642],
        [202, 430, 604, 668],
        [112, 308, 520, 654],
        [250, 456, 612, 666],
      ],
      core: [
        [142, 332, 518, 648],
        [96, 292, 472, 638],
        [198, 390, 542, 666],
        [150, 320, 500, 642],
      ],
    };
    const levelPlan = plans[level?.key] || plans.dock;
    const roomPlan = levelPlan[Math.max(0, Math.min(levelPlan.length - 1, room - 1))] || levelPlan[0];
    return roomPlan[index % roomPlan.length] + Math.floor(index / roomPlan.length) * 34;
  }

  function stickmanBossSpawnOffset(level, index) {
    const plans = {
      dock: [398, 504],
      reactor: [314, 542],
      skyline: [456, 608],
      core: [308, 548],
    };
    const plan = plans[level?.key] || plans.dock;
    return plan[index % plan.length] + Math.floor(index / plan.length) * 34;
  }

  function pointInStickmanRect(x, y, rect, inflate = 0) {
    return x >= rect.x - inflate && x <= rect.x + rect.w + inflate && y >= rect.y - inflate && y <= rect.y + rect.h + inflate;
  }

  function stickmanLineBlockedByCover(state, fromX, fromY, toX, toY, ignoreCover = null) {
    const covers = state.cover || [];
    const steps = Math.max(12, Math.ceil(Math.hypot(toX - fromX, toY - fromY) / 24));
    for (const cover of covers) {
      if (cover === ignoreCover) continue;
      for (let i = 1; i < steps; i += 1) {
        const t = i / steps;
        if (pointInStickmanRect(fromX + (toX - fromX) * t, fromY + (toY - fromY) * t, cover, 3)) return true;
      }
    }
    return false;
  }

  function stickmanTrapDangerAt(state, enemy, testX) {
    const body = {
      x: testX,
      y: groundYAt(state, testX + enemy.w / 2) - enemy.h,
      w: enemy.w,
      h: enemy.h,
    };
    return (state.traps || []).some((trap) => {
      const rect = currentStickmanTrapRect(state, trap);
      return rect && rectsOverlap(body, { x: rect.x - 8, y: rect.y - 8, w: rect.w + 16, h: rect.h + 16 });
    });
  }

  function stickmanFindCoverTactic(state, enemy, player) {
    const role = stickmanEnemyRoleMeta(enemy);
    const fromX = enemy.x + enemy.w / 2;
    const fromY = enemy.y + enemy.h * 0.42;
    const playerX = player.x + PLAYER_W / 2;
    const playerY = player.y + PLAYER_H * 0.45;
    let best = null;
    for (const cover of state.cover || []) {
      if (Math.abs((cover.x + cover.w / 2) - fromX) > 360) continue;
      const candidates = [
        { x: cover.x - enemy.w - 10, side: -1 },
        { x: cover.x + cover.w + 10, side: 1 },
      ];
      candidates.forEach((candidate) => {
        const targetX = clamp(candidate.x, enemy.roomMin || enemy.patrolMin, enemy.roomMax || enemy.patrolMax);
        if (stickmanTrapDangerAt(state, enemy, targetX)) return;
        const targetY = groundYAt(state, targetX + enemy.w / 2) - enemy.h;
        const bodyX = targetX + enemy.w / 2;
        const protectedByCover = stickmanLineBlockedByCover(state, bodyX, targetY + enemy.h * 0.42, playerX, playerY, null);
        const peekX = targetX + candidate.side * 18;
        const peekClear = !stickmanLineBlockedByCover(state, peekX, targetY + enemy.h * 0.42, playerX, playerY, cover);
        const rangeScore = Math.abs(Math.abs(playerX - bodyX) - role.preferredRange) * 0.35;
        const safetyScore = protectedByCover ? -90 * role.coverBias : 36;
        const peekScore = peekClear ? -18 : 12;
        const score = Math.abs(targetX - enemy.x) + rangeScore + safetyScore + peekScore;
        if (!best || score < best.score) best = { x: targetX, y: targetY, score, cover, peekClear };
      });
    }
    return best;
  }

  function activeStickmanPowerText(state) {
    const rows = [];
    if (state.tick < state.starUntil) rows.push(`無敵 ${Math.ceil((state.starUntil - state.tick) / 60)}s`);
    if (state.tick < state.fireUntil) rows.push(`火力 ${Math.ceil((state.fireUntil - state.tick) / 60)}s`);
    if (state.tick < state.jumpBoostUntil) rows.push(`高跳 ${Math.ceil((state.jumpBoostUntil - state.tick) / 60)}s`);
    if (state.shield > 0) rows.push(`護盾 ${state.shield}`);
    return rows.join(" · ");
  }

  function spawnStickmanPowerup(state, kind, x, y) {
    state.powerups.push({
      kind,
      x: clamp(x, 24, WORLD_WIDTH - 40),
      y: clamp(y, 60, 292),
      w: POWERUP_SIZE,
      h: POWERUP_SIZE,
      life: 900,
      bornAt: state.tick,
    });
  }

  function applyStickmanPowerup(api, state, powerup) {
    const kind = powerup.kind || "ammo";
    const meta = POWERUP_META[kind] || POWERUP_META.ammo;
    const p = state.player;
    api.sound?.("uiDrop", { volume: 0.14, throttleMs: 140 });
    state.powerupsCollected += 1;
    state.lastPickup = meta.label;
    state.lastPickupUntil = state.tick + 120;
    if (kind === "mushroom") {
      p.maxHp = Math.min(8, (p.maxHp || 5) + 1);
      p.hp = Math.min(p.maxHp, p.hp + 2);
      state.shield = Math.min(3, state.shield + 1);
      state.score += 120;
      api.achievement?.("stickman-mushroom", "蘑菇保命", "取得蘑菇並增加容錯。");
    } else if (kind === "fireFlower") {
      state.fireUntil = Math.max(state.fireUntil, state.tick) + 620;
      state.weaponLevel = Math.max(state.weaponLevel, 2);
      state.reserve += 6;
      state.score += 150;
      api.achievement?.("stickman-fire-flower", "火焰花火力", "取得火焰花三向射擊。");
    } else if (kind === "star") {
      state.starUntil = Math.max(state.starUntil, state.tick) + 360;
      state.invulnerableUntil = Math.max(state.invulnerableUntil, state.starUntil);
      state.score += 180;
      api.achievement?.("stickman-star", "短暫無敵", "取得無敵星穿過危險區。");
    } else if (kind === "spring") {
      state.jumpBoostUntil = Math.max(state.jumpBoostUntil, state.tick) + 560;
      state.score += 110;
    } else if (kind === "shield") {
      state.shield = Math.min(3, state.shield + 1);
      state.score += 90;
    } else {
      const weapon = stickmanWeapon(state);
      state.reserve += 18;
      state.ammo = Math.min(weapon.mag, state.ammo + 3);
      state.score += 60;
    }
    addStickmanParticles(state, powerup.x + powerup.w / 2, powerup.y + powerup.h / 2, meta.color, 20);
    api.status(`${meta.label} 已取得。`);
  }

  function damageStickmanPlayer(api, state, amount, reason, options = {}) {
    const p = state.player;
    if (state.tick < state.starUntil) {
      state.score += reason === "trap" ? 24 : 12;
      addStickmanParticles(state, p.x + PLAYER_W / 2, p.y + 20, "#fde047", 8);
      return false;
    }
    if (state.tick <= state.invulnerableUntil) return false;
    if (state.shield > 0) {
      state.shield -= 1;
      state.invulnerableUntil = state.tick + 84;
      p.vy = Math.min(p.vy, -7.2);
      if (Number.isFinite(options.sourceX)) p.vx += p.x < options.sourceX ? -4 : 4;
      api.sound?.("metalHit", { volume: 0.13, throttleMs: 170 });
      addStickmanParticles(state, p.x + PLAYER_W / 2, p.y + 20, "#93c5fd", 18);
      api.achievement?.("stickman-shield-save", "護盾救命", "用護盾擋下一次致命或受傷碰撞。");
      return false;
    }
    if (reason === "trap") state.trapHits += 1;
    state.invulnerableUntil = state.tick + 72;
    if (options.lethal) {
      p.hp = 0;
      state.deathReason = reason;
    } else {
      p.hp -= amount;
      state.deathReason = p.hp <= 0 ? reason : "";
    }
    if (Number.isFinite(options.sourceX)) p.vx += p.x < options.sourceX ? -3.8 : 3.8;
    api.sound?.(reason === "trap" ? "metalHit" : "punch", { volume: 0.16, throttleMs: 170 });
    addStickmanParticles(state, p.x + PLAYER_W / 2, p.y + 20, reason === "trap" ? "#ef4444" : "#fb7185", options.lethal ? 30 : 18);
    return true;
  }

  function maybeDropStickmanPowerup(state, enemy) {
    if (enemy.kind === "boss") {
      spawnStickmanPowerup(state, "star", enemy.x + enemy.w / 2, enemy.y + 4);
      spawnStickmanPowerup(state, "ammo", enemy.x + enemy.w / 2 + 28, enemy.y + 8);
      return;
    }
    const roll = stickmanRandom(state);
    if (roll < 0.09) spawnStickmanPowerup(state, "mushroom", enemy.x, enemy.y + 8);
    else if (roll < 0.17) spawnStickmanPowerup(state, "ammo", enemy.x, enemy.y + 8);
    else if (roll < 0.22) spawnStickmanPowerup(state, "shield", enemy.x, enemy.y + 8);
  }

  function defeatStickmanEnemy(api, state, enemy, x, y) {
    if (enemy.defeated) return;
    enemy.defeated = true;
    state.score += enemy.kind === "boss" ? 950 : 150;
    api.sound?.(enemy.kind === "boss" ? "metalHit" : "hit", { volume: enemy.kind === "boss" ? 0.18 : 0.12, throttleMs: 90 });
    if (enemy.kind === "boss") {
      const remainingBosses = state.enemies.filter((item) => item !== enemy && item.kind === "boss" && item.hp > 0 && !item.defeated).length;
      if (remainingBosses <= 0) {
        state.bossDefeated = 1;
        api.achievement?.(`boss-down-${state.level.key}`, `${state.level.label} Boss 擊破`, `擊破 ${state.bossName || "關卡 Boss"}。`);
      }
    }
    maybeDropStickmanPowerup(state, enemy);
    addStickmanParticles(state, x || enemy.x + enemy.w / 2, y || enemy.y + enemy.h / 2, "#38bdf8", enemy.kind === "boss" ? 48 : 22);
  }

  function spawnStickmanRoom(state, room) {
    if (state.spawnedRooms.has(room)) return;
    state.spawnedRooms.add(room);
    const mode = MODES[state.modeIndex] || MODES[0];
    const level = stickmanLevelFromState(state);
    const baseX = (room - 1) * ROOM_WIDTH;
    const enemyCount = room === WORLD_ROOMS ? 2 + Math.min(1, level.enemyCountBonus || 0) : 2 + Math.min(2, room) + Number(level.enemyCountBonus || 0);
    for (let i = 0; i < enemyCount; i += 1) {
      const aiRole = stickmanRoleForRoom(state, room, i);
      const role = STICKMAN_ENEMY_ROLES[aiRole] || STICKMAN_ENEMY_ROLES.rifle;
      const x = baseX + stickmanEnemySpawnOffset(level, room, i) + (stickmanRandom(state) - 0.5) * 34;
      const y = groundYAt(state, x) - 42;
      const hp = Math.max(1, 3 + Math.floor(room / 2) + mode.enemyHp + Number(level.enemyHp || 0) + role.hpBonus);
      state.enemies.push({
        x,
        y,
        w: 22,
        h: 42,
        vx: i % 2 ? -0.78 : 0.78,
        baseSpeed: (0.78 + room * 0.04) * role.speedScale,
        facing: i % 2 ? -1 : 1,
        patrolMin: Math.max(baseX + 90, x - 82),
        patrolMax: Math.min(baseX + ROOM_WIDTH - 90, x + 96),
        roomMin: baseX + 58,
        roomMax: baseX + ROOM_WIDTH - 64,
        hp,
        maxHp: hp,
        fireAt: 42 + Math.floor(stickmanRandom(state) * 52),
        hurt: 0,
        aiState: "patrol",
        aiRole,
        flankSign: stickmanRandom(state) > 0.5 ? 1 : -1,
        lastSeenX: null,
        lastSeenY: null,
        alertUntil: 0,
        walkCycle: stickmanRandom(state) * Math.PI * 2,
        kind: "grunt",
      });
    }
    if (room === WORLD_ROOMS && !state.bossSpawned) {
      state.bossSpawned = true;
      state.bossIntroUntil = state.tick + 180;
      state.bossName = level.boss?.label || "關卡 Boss";
      const bossCount = level.boss?.twins ? 2 : 1;
      for (let i = 0; i < bossCount; i += 1) {
        const bossRole = level.boss?.role || "boss";
        const bossHp = Number(level.boss?.hp || 22) + mode.enemyHp * 3;
        const x = baseX + stickmanBossSpawnOffset(level, i);
        state.enemies.push({
          x,
          y: groundYAt(state, x + 19) - 64,
          w: 38,
          h: 64,
          vx: i % 2 ? 0.68 : -0.72,
          baseSpeed: 0.72 + Number(level.enemyHp || 0) * 0.05,
          facing: -1,
          patrolMin: baseX + 232,
          patrolMax: baseX + 650,
          roomMin: baseX + 180,
          roomMax: baseX + ROOM_WIDTH - 48,
          hp: bossHp,
          maxHp: bossHp,
          fireAt: 24,
          hurt: 0,
          aiState: "patrol",
          aiRole: bossRole,
          flankSign: i % 2 ? 1 : -1,
          lastSeenX: null,
          lastSeenY: null,
          alertUntil: 0,
          walkCycle: stickmanRandom(state) * Math.PI * 2,
          kind: "boss",
          bossLabel: level.boss?.twins ? `${state.bossName} ${i + 1}` : state.bossName,
        });
      }
    }
  }

  function setStickmanStatus(api, state) {
    const accuracy = state.shots ? Math.round((state.hits / state.shots) * 100) : 0;
    const reload = state.reloadTicks > 0 ? " · 換彈中" : "";
    const power = activeStickmanPowerText(state);
    const pickup = state.tick < state.lastPickupUntil ? ` · 取得 ${state.lastPickup}` : "";
    const coop = stickmanIsCoop(state) ? ` · Co-op ${state.multiplayer?.peer?.username ? `with ${state.multiplayer.peer.username}` : "等待隊友同步"}` : "";
    const weapon = stickmanWeapon(state);
    const boss = state.tick < (state.bossIntroUntil || 0) ? ` · Boss ${state.bossName || ""}` : "";
    api.status(`${state.level.label}${coop} · ${MODES[state.modeIndex].label} · ${weapon.label} · Room ${state.room}/${WORLD_ROOMS}${boss} · 分數 ${Math.round(state.score).toLocaleString()} · HP ${state.player.hp}/${state.player.maxHp || 5} · 彈藥 ${state.ammo}/${state.reserve} · 命中 ${accuracy}%${power ? ` · ${power}` : ""}${pickup}${reload}`);
  }

  function stickmanMultiplayerStatePayload(state) {
    return {
      x: Math.round(state.player.x * 10) / 10,
      y: Math.round(state.player.y * 10) / 10,
      w: PLAYER_W,
      h: PLAYER_H,
      hp: state.player.hp,
      maxHp: state.player.maxHp || 5,
      facing: state.player.facing || 1,
      walkCycle: state.player.walkCycle || 0,
      room: state.room,
      score: Math.round(state.score),
      bossDefeated: state.bossDefeated,
      status: state.status,
      at: Date.now(),
    };
  }

  function applyStickmanMultiplayerSnapshot(api, state, snapshot) {
    if (!stickmanIsCoop(state) || !snapshot?.room) return;
    state.multiplayer.room = snapshot.room;
    const mp = api.multiplayer?.();
    const peer = mp?.peerState(snapshot, snapshot.room);
    if (peer) state.multiplayer.peer = peer;
    const events = Array.isArray(snapshot.events) ? snapshot.events : [];
    events.forEach((event) => {
      const eventId = Number(event.id || 0);
      if (!eventId || state.multiplayer.processedEvents.has(eventId)) return;
      state.multiplayer.processedEvents.add(eventId);
      state.multiplayer.afterEventId = Math.max(state.multiplayer.afterEventId || 0, eventId);
      if (Number(event.sender_user_id) === Number(currentUserId || 0)) return;
      const targetId = Number(event.target_user_id || 0);
      if (targetId && targetId !== Number(currentUserId || 0)) return;
      if (event.event_type === "friendly_fire" || event.event_type === "player_hit") {
        const damage = Math.max(1, Math.min(3, Number(event.payload?.damage || 1)));
        if (damageStickmanPlayer(api, state, damage, "shot", { sourceX: Number(event.payload?.x || state.player.x) })) {
          state.score = Math.max(0, state.score - 40);
          api.status(`${event.sender_username || "隊友"} 誤傷了你。`);
        }
      }
      if (event.event_type === "objective") {
        state.lastPickup = event.payload?.label || "隊友完成目標";
        state.lastPickupUntil = state.tick + 120;
      }
    });
    if (state.multiplayer.processedEvents.size > 160) {
      state.multiplayer.processedEvents = new Set(Array.from(state.multiplayer.processedEvents).slice(-80));
    }
  }

  function syncStickmanMultiplayer(api, state, { force = false } = {}) {
    if (!stickmanIsCoop(state) || state.multiplayer.syncing) return;
    if (!force && state.tick - (state.multiplayer.lastSyncTick || 0) < MULTIPLAYER_SYNC_TICKS) return;
    const roomId = state.multiplayer.roomId;
    if (!roomId) return;
    const mp = api.multiplayer?.();
    if (!mp?.syncRoom) return;
    state.multiplayer.lastSyncTick = state.tick;
    const events = state.multiplayer.pendingEvents.splice(0, 12);
    state.multiplayer.syncing = true;
    mp.syncRoom(roomId, stickmanMultiplayerStatePayload(state), events, state.multiplayer.afterEventId || 0)
      .then((snapshot) => applyStickmanMultiplayerSnapshot(api, state, snapshot))
      .catch((err) => {
        state.multiplayer.pendingEvents.unshift(...events);
        state.multiplayer.lastError = err.message || "同步失敗";
      })
      .finally(() => {
        state.multiplayer.syncing = false;
      });
  }

  function playerRect(state) {
    const p = state.player;
    return { x: p.x, y: p.y, w: PLAYER_W, h: PLAYER_H };
  }

  function stickmanPeerRect(state) {
    const peer = state?.multiplayer?.peer?.state;
    if (!peer || !Number.isFinite(Number(peer.x)) || !Number.isFinite(Number(peer.y))) return null;
    return {
      x: Number(peer.x),
      y: Number(peer.y),
      w: Number(peer.w || PLAYER_W),
      h: Number(peer.h || PLAYER_H),
      hp: Number(peer.hp || 0),
      username: state.multiplayer.peer.username || "隊友",
      facing: Number(peer.facing || 1),
      walkCycle: Number(peer.walkCycle || 0),
    };
  }

  function stickmanIsCoop(state) {
    return state?.multiplayer?.mode === "coop";
  }

  function queueStickmanMultiplayerEvent(state, event) {
    if (!stickmanIsCoop(state)) return;
    state.multiplayer.pendingEvents.push(event);
  }

  function updateStickmanCoopPuzzles(state) {
    if (!stickmanIsCoop(state) || !state.coopPuzzles) return;
    const local = playerRect(state);
    const peer = stickmanPeerRect(state);
    state.coopPuzzles.plates.forEach((plate) => {
      const localPressed = rectsOverlap(local, plate);
      const peerPressed = peer ? rectsOverlap(peer, plate) : false;
      plate.pressed = localPressed || peerPressed;
      plate.pressedBy = [localPressed ? "you" : "", peerPressed ? "peer" : ""].filter(Boolean).join("+");
    });
    const platePressed = (id) => state.coopPuzzles.plates.some((plate) => plate.id === id && plate.pressed);
    state.coopPuzzles.gates.forEach((gate) => {
      gate.open = gate.type === "dual"
        ? platePressed("gate-b-left") && platePressed("gate-b-right")
        : platePressed(gate.id);
    });
  }

  function resolveStickmanCoopGates(state, previousX) {
    if (!stickmanIsCoop(state) || !state.coopPuzzles) return;
    const p = state.player;
    const body = playerRect(state);
    state.coopPuzzles.gates.forEach((gate) => {
      if (gate.open || !rectsOverlap(body, gate)) return;
      if (previousX + PLAYER_W <= gate.x + 4) {
        p.x = gate.x - PLAYER_W - 0.5;
      } else {
        p.x = gate.x + gate.w + 0.5;
      }
      p.vx = 0;
      body.x = p.x;
    });
  }

  function applyStickmanPhysics(state) {
    const p = state.player;
    const prevX = p.x;
    const prevY = p.y;
    p.vy += GRAVITY;
    p.x = clamp(p.x + p.vx, 8, WORLD_WIDTH - PLAYER_W - 8);
    p.y += p.vy;
    p.grounded = false;

    const body = playerRect(state);
    for (const platform of state.platforms) {
      const wasAbove = prevY + PLAYER_H <= platform.y + 2;
      if (p.vy >= 0 && wasAbove && rectsOverlap(body, platform)) {
        p.y = platform.y - PLAYER_H;
        p.vy = 0;
        p.grounded = true;
        p.doubleJumpUsed = false;
        body.y = p.y;
      }
    }
    if (p.y > HEIGHT + 80) {
      p.hp = 0;
    }
    resolveStickmanCoopGates(state, prevX);
  }

  function startStickmanReload(state) {
    const weapon = stickmanWeapon(state);
    if (state.reloadTicks > 0 || state.ammo >= weapon.mag || state.reserve <= 0) return;
    state.reloadTicks = RELOAD_TICKS;
  }

  function finishStickmanReload(state) {
    const weapon = stickmanWeapon(state);
    const needed = weapon.mag - state.ammo;
    const taken = Math.min(needed, state.reserve);
    state.ammo += taken;
    state.reserve -= taken;
    state.reloadTicks = 0;
    return taken > 0;
  }

  function fireStickmanShot(api, state) {
    if (state.status !== "active" || state.paused) return;
    if (state.reloadTicks > 0) return;
    if (state.ammo <= 0) {
      state.emptyReload = true;
      startStickmanReload(state);
      return;
    }
    if (state.tick < state.nextShotAt) return;
    const weapon = stickmanWeapon(state);
    const empowered = state.tick < state.fireUntil || state.tick < state.starUntil;
    const shotSpread = empowered ? [-0.08, 0, 0.08] : weapon.spread;
    state.nextShotAt = state.tick + Math.max(4, weapon.delay - (empowered ? 3 : 0));
    state.ammo -= 1;
    state.shots += 1;
    api.sound?.(weapon.key === "rail" ? "metalHit" : "uiTick", { volume: weapon.key === "rail" ? 0.1 : 0.055, throttleMs: 80 });
    const p = state.player;
    const dir = p.facing || 1;
    shotSpread.forEach((spread) => {
      state.playerShots.push({
        x: p.x + (dir > 0 ? PLAYER_W + 3 : -3),
        y: p.y + 19,
        vx: dir * (empowered ? Math.max(13.6, weapon.speed) : weapon.speed),
        vy: spread * 13 + (stickmanRandom(state) - 0.5) * 0.18,
        w: empowered || weapon.key === "rail" ? 11 : 8,
        h: empowered || weapon.key === "rail" ? 4 : 3,
        life: weapon.key === "rail" ? 92 : (empowered ? 82 : 70),
        damage: empowered ? Math.max(2, weapon.damage) : weapon.damage,
        pierce: empowered ? Math.max(1, weapon.pierce) : weapon.pierce,
        color: empowered ? "#fb923c" : weapon.color,
      });
    });
    addStickmanParticles(state, p.x + (dir > 0 ? PLAYER_W + 5 : -5), p.y + 19, empowered ? "#fb923c" : "#fef08a", empowered ? 8 : 4);
  }

  function enemyFireStickmanShot(state, enemy) {
    const role = stickmanEnemyRoleMeta(enemy);
    if (!role.canShoot) return false;
    const p = state.player;
    const fromX = enemy.x + enemy.w / 2;
    const fromY = enemy.y + enemy.h * 0.42;
    const toX = p.x + PLAYER_W / 2;
    const toY = p.y + PLAYER_H * 0.45;
    if (stickmanLineBlockedByCover(state, fromX, fromY, toX, toY)) return false;
    const angle = Math.atan2(toY - fromY, toX - fromX);
    const distance = Math.hypot(toX - fromX, toY - fromY);
    const speed = enemy.aiRole === "sniper" ? 6.4 : enemy.aiRole === "grenadier" ? 4.2 : enemy.kind === "boss" ? 5.8 : 4.8;
    const miss = role.spread + Math.min(0.06, distance * 0.00005);
    const spread = role.burst > 1
      ? [-miss * 1.35, 0, miss * 1.35]
      : [(stickmanRandom(state) - 0.5) * miss * 2];
    spread.forEach((offset) => {
      state.enemyShots.push({
        x: fromX,
        y: fromY,
        vx: Math.cos(angle + offset) * speed,
        vy: Math.sin(angle + offset) * speed,
        r: enemy.aiRole === "grenadier" ? 5 : enemy.kind === "boss" ? 4 : 3,
        life: enemy.aiRole === "grenadier" ? 140 : 118,
        color: enemy.aiRole === "grenadier" ? "#facc15" : "#fb7185",
      });
    });
    addStickmanParticles(state, fromX, fromY, enemy.aiRole === "grenadier" ? "#facc15" : "#fb7185", 3);
    return true;
  }

  function updateStickmanEnemies(state) {
    const mode = MODES[state.modeIndex] || MODES[0];
    const p = state.player;
    state.enemies.forEach((enemy) => {
      if (enemy.hurt > 0) enemy.hurt -= 1;
      const oldX = enemy.x;
      const role = stickmanEnemyRoleMeta(enemy);
      const dxToPlayer = (p.x + PLAYER_W / 2) - (enemy.x + enemy.w / 2);
      const distance = Math.abs(dxToPlayer);
      const sameLane = Math.abs(p.y - enemy.y) < 118;
      const moveDir = dxToPlayer === 0 ? enemy.facing : Math.sign(dxToPlayer);
      const fromX = enemy.x + enemy.w / 2;
      const fromY = enemy.y + enemy.h * 0.42;
      const playerX = p.x + PLAYER_W / 2;
      const playerY = p.y + PLAYER_H * 0.45;
      const hasLine = !stickmanLineBlockedByCover(state, fromX, fromY, playerX, playerY);
      const seesPlayer = sameLane && distance < role.sightRange && hasLine;
      if (seesPlayer) {
        enemy.lastSeenX = p.x;
        enemy.lastSeenY = p.y;
        enemy.alertUntil = state.tick + 170;
      }
      const aware = seesPlayer || state.tick < (enemy.alertUntil || 0) || (sameLane && distance < 150);
      const baseSpeed = enemy.baseSpeed || (enemy.kind === "boss" ? 0.72 : 0.78);
      const maxSpeed = role.maxSpeed || (enemy.kind === "boss" ? 1.38 : 1.18);
      let desiredX = null;
      const lookAheadDir = Math.sign(enemy.vx || moveDir || 1);
      if (stickmanTrapDangerAt(state, enemy, enemy.x + lookAheadDir * 34)) {
        enemy.aiState = "avoidTrap";
        enemy.flankSign *= -1;
        desiredX = enemy.x - lookAheadDir * 92;
      } else if (aware) {
        const cover = stickmanFindCoverTactic(state, enemy, p);
        const lowHp = enemy.hp <= enemy.maxHp * 0.45;
        if ((lowHp || role.coverBias > 0.7) && cover && Math.abs(cover.x - enemy.x) > 10) {
          enemy.aiState = "seekCover";
          desiredX = cover.x;
        } else if (!hasLine && enemy.lastSeenX !== null) {
          enemy.aiState = "flank";
          desiredX = enemy.lastSeenX + (enemy.flankSign || 1) * role.flankOffset;
          if (stickmanTrapDangerAt(state, enemy, desiredX)) desiredX = enemy.lastSeenX - (enemy.flankSign || 1) * role.flankOffset;
        } else if (distance < role.retreatRange) {
          enemy.aiState = "retreat";
          desiredX = enemy.x - moveDir * Math.max(80, role.preferredRange * 0.55);
        } else if (distance > role.preferredRange + 68) {
          enemy.aiState = enemy.aiRole === "rusher" ? "rush" : "chase";
          desiredX = p.x - moveDir * role.preferredRange;
        } else if (hasLine && role.canShoot) {
          enemy.aiState = "suppress";
          desiredX = enemy.x + Math.sin(state.tick * 0.045 + enemy.walkCycle) * 22;
        } else {
          enemy.aiState = "hold";
          desiredX = enemy.x;
        }
      } else {
        enemy.aiState = "patrol";
        const patrolDir = enemy.vx < 0 ? -1 : 1;
        enemy.vx += patrolDir * 0.018;
      }
      if (desiredX !== null) {
        const tacticalMin = enemy.aiState === "patrol" ? enemy.patrolMin : (enemy.roomMin || enemy.patrolMin);
        const tacticalMax = enemy.aiState === "patrol" ? enemy.patrolMax : (enemy.roomMax || enemy.patrolMax);
        desiredX = clamp(desiredX, tacticalMin, tacticalMax);
        const desiredDir = desiredX > enemy.x + 5 ? 1 : desiredX < enemy.x - 5 ? -1 : 0;
        if (desiredDir === 0) enemy.vx *= enemy.aiState === "suppress" ? 0.78 : 0.62;
        else enemy.vx += desiredDir * (enemy.aiState === "rush" ? 0.078 : 0.055);
      }
      enemy.vx = clamp(enemy.vx, -maxSpeed, maxSpeed);
      if (enemy.aiState === "patrol" && Math.abs(enemy.vx) < baseSpeed) {
        enemy.vx = (enemy.vx < 0 ? -1 : 1) * baseSpeed;
      }
      enemy.x += enemy.vx;
      const clampMin = enemy.aiState === "patrol" ? enemy.patrolMin : (enemy.roomMin || enemy.patrolMin);
      const clampMax = enemy.aiState === "patrol" ? enemy.patrolMax : (enemy.roomMax || enemy.patrolMax);
      if (enemy.x < clampMin) {
        enemy.x = clampMin;
        enemy.vx = Math.abs(enemy.vx || baseSpeed);
      }
      if (enemy.x > clampMax) {
        enemy.x = clampMax;
        enemy.vx = -Math.abs(enemy.vx || baseSpeed);
      }
      enemy.y = groundYAt(state, enemy.x + enemy.w / 2) - enemy.h;
      enemy.facing = dxToPlayer < 0 ? -1 : 1;
      enemy.walkCycle += Math.max(0.08, Math.abs(enemy.x - oldX) * 0.28 + Math.abs(enemy.vx) * 0.06);
      enemy.fireAt -= 1;
      const fireGap = Math.max(30, role.fireDelay - mode.enemyShots - state.room * 2 - Number(stickmanLevelFromState(state).enemyHp || 0) * 4);
      const canFireState = enemy.aiState === "suppress" || enemy.aiState === "hold" || enemy.aiState === "retreat" || enemy.aiState === "seekCover";
      if (enemy.fireAt <= 0 && canFireState && sameLane && distance < role.fireRange && hasLine) {
        enemyFireStickmanShot(state, enemy);
        enemy.fireAt = fireGap + Math.floor(stickmanRandom(state) * 26);
      }
    });
  }

  function updateStickmanBullets(api, state) {
    state.playerShots.forEach((shot) => {
      shot.x += shot.vx;
      shot.y += shot.vy;
      shot.life -= 1;
    });
    state.enemyShots.forEach((shot) => {
      shot.x += shot.vx;
      shot.y += shot.vy;
      shot.life -= 1;
    });

    for (const shot of state.playerShots) {
      if (shot.life <= 0) continue;
      for (const cover of state.cover) {
        if (rectsOverlap({ x: shot.x, y: shot.y, w: shot.w, h: shot.h }, cover)) {
          shot.life = 0;
          addStickmanParticles(state, shot.x, shot.y, "#94a3b8", 5);
        }
      }
      if (shot.life <= 0) continue;
      for (const crate of state.crates) {
        if (crate.hp <= 0 || !rectsOverlap({ x: shot.x, y: shot.y, w: shot.w, h: shot.h }, crate)) continue;
        crate.hp -= shot.damage || 1;
        state.score += 20;
        if (shot.pierce > 0) shot.pierce -= 1;
        else shot.life = 0;
        addStickmanParticles(state, shot.x, shot.y, "#fbbf24", 8);
        if (crate.hp <= 0) {
          state.cratesBroken += 1;
          spawnStickmanPowerup(state, crate.power || "ammo", crate.x + 2, crate.y - 20);
          addStickmanParticles(state, crate.x + crate.w / 2, crate.y + crate.h / 2, "#fde68a", 20);
          api.achievement?.("stickman-question-block", "問號補給", "打破問號補給箱取得道具。");
        }
      }
      if (shot.life <= 0) continue;
      const peer = stickmanPeerRect(state);
      if (peer && peer.hp > 0 && rectsOverlap({ x: shot.x, y: shot.y, w: shot.w, h: shot.h }, peer)) {
        shot.life = 0;
        state.score = Math.max(0, state.score - 35);
        addStickmanParticles(state, shot.x, shot.y, "#f43f5e", 12);
        queueStickmanMultiplayerEvent(state, {
          type: "friendly_fire",
          target_user_id: state.multiplayer?.peer?.user_id,
          payload: {
            damage: Math.max(1, Number(shot.damage || 1)),
            x: shot.x,
            y: shot.y,
            label: "friendly fire",
          },
        });
      }
      if (shot.life <= 0) continue;
      for (const enemy of state.enemies) {
        if (enemy.hp <= 0 || !rectsOverlap({ x: shot.x, y: shot.y, w: shot.w, h: shot.h }, enemy)) continue;
        if (shot.pierce > 0) shot.pierce -= 1;
        else shot.life = 0;
        enemy.hp -= shot.damage || 1;
        enemy.hurt = 9;
        state.hits += 1;
        state.score += enemy.kind === "boss" ? 42 : 28;
        addStickmanParticles(state, shot.x, shot.y, enemy.kind === "boss" ? "#f97316" : "#facc15", enemy.kind === "boss" ? 9 : 6);
        if (enemy.hp <= 0) {
          defeatStickmanEnemy(api, state, enemy, enemy.x + enemy.w / 2, enemy.y + enemy.h / 2);
        }
      }
    }

    const pRect = playerRect(state);
    for (const shot of state.enemyShots) {
      if (shot.life <= 0) continue;
      for (const cover of state.cover) {
        if (shot.x > cover.x && shot.x < cover.x + cover.w && shot.y > cover.y && shot.y < cover.y + cover.h) {
          shot.life = 0;
          addStickmanParticles(state, shot.x, shot.y, "#94a3b8", 5);
        }
      }
      if (
        shot.life > 0 &&
        shot.x > pRect.x &&
        shot.x < pRect.x + pRect.w &&
        shot.y > pRect.y &&
        shot.y < pRect.y + pRect.h
      ) {
        shot.life = 0;
        damageStickmanPlayer(api, state, 1, "shot", { sourceX: shot.x });
      }
    }
    state.playerShots = state.playerShots.filter((shot) => shot.life > 0 && shot.x > state.cameraX - 60 && shot.x < state.cameraX + WIDTH + 120);
    state.enemyShots = state.enemyShots.filter((shot) => shot.life > 0 && shot.x > state.cameraX - 90 && shot.x < state.cameraX + WIDTH + 130 && shot.y > 0 && shot.y < HEIGHT + 30);
    state.enemies = state.enemies.filter((enemy) => enemy.hp > 0);
    state.crates = state.crates.filter((crate) => crate.hp > 0);
  }

  function updateStickmanPlayer(state) {
    const p = state.player;
    const left = state.keys.left ? -1 : 0;
    const right = state.keys.right ? 1 : 0;
    const moving = left + right;
    const sprinting = state.keys.sprint && state.stamina > 8 && moving !== 0;
    const jumpBoost = state.tick < state.jumpBoostUntil ? 0.35 : 0;
    const speed = (sprinting ? 4.5 : 3.15) + jumpBoost;
    p.vx += moving * 0.82;
    p.vx *= p.grounded ? 0.76 : 0.91;
    p.vx = clamp(p.vx, -speed, speed);
    if (moving !== 0) p.facing = moving > 0 ? 1 : -1;
    p.walkCycle = Number(p.walkCycle || 0) + Math.abs(p.vx) * 0.22;
    if (sprinting) state.stamina = Math.max(0, state.stamina - 0.5);
    else state.stamina = Math.min(100, state.stamina + 0.28);
  }

  function jumpStickman(state) {
    if (!state || state.status !== "active" || state.paused) return;
    const boosted = state.tick < state.jumpBoostUntil;
    if (!state.player.grounded) {
      if (!boosted || state.player.doubleJumpUsed) return;
      state.player.doubleJumpUsed = true;
      state.player.vy = -11.4;
      addStickmanParticles(state, state.player.x + PLAYER_W / 2, state.player.y + PLAYER_H, "#38bdf8", 12);
      return;
    }
    state.player.vy = boosted ? -13.8 : -12.2;
    state.player.grounded = false;
  }

  function advanceStickmanRoom(state) {
    const nextRoom = Math.min(WORLD_ROOMS, Math.floor((state.player.x + PLAYER_W / 2) / ROOM_WIDTH) + 1);
    if (nextRoom > state.room) {
      state.room = nextRoom;
      state.wave = nextRoom;
      state.score += 110;
      spawnStickmanRoom(state, nextRoom);
    }
  }

  function updateStickmanPowerups(api, state) {
    const pRect = playerRect(state);
    state.powerups.forEach((powerup) => {
      powerup.life -= 1;
      if (rectsOverlap(pRect, powerup)) {
        powerup.life = 0;
        applyStickmanPowerup(api, state, powerup);
      }
    });
    state.powerups = state.powerups.filter((powerup) => powerup.life > 0);
  }

  function updateStickmanHazards(api, state) {
    const pRect = playerRect(state);
    state.traps.forEach((trap) => {
      const trapRect = currentStickmanTrapRect(state, trap);
      if (!trap.cleared && state.player.x > trap.x + (trap.w || trap.r * 2 || 30) + 44) {
        trap.cleared = true;
        state.trapsPassed += 1;
        state.score += 35;
        if (state.trapsPassed >= 4) api.achievement?.("stickman-trap-runner", "陷阱穿越", "穿過 4 個即死陷阱。");
      }
      if (!trapRect || !rectsOverlap(pRect, trapRect)) return;
      damageStickmanPlayer(api, state, 99, "trap", {
        lethal: Boolean(trap.lethal),
        sourceX: trapRect.x + trapRect.w / 2,
      });
    });
  }

  function updateStickmanContacts(api, state) {
    const pRect = playerRect(state);
    state.enemies.forEach((enemy) => {
      if (enemy.hp <= 0 || !rectsOverlap(pRect, enemy)) return;
      if (state.tick < state.starUntil) {
        enemy.hp = 0;
        state.score += enemy.kind === "boss" ? 160 : 80;
        defeatStickmanEnemy(api, state, enemy, enemy.x + enemy.w / 2, enemy.y + enemy.h / 2);
        return;
      }
      damageStickmanPlayer(api, state, enemy.kind === "boss" ? 2 : 1, "enemy", { sourceX: enemy.x + enemy.w / 2 });
    });
    state.enemies = state.enemies.filter((enemy) => enemy.hp > 0);
  }

  function finishStickmanShooter(api, reason = "complete") {
    const state = api._stickmanShooterState;
    if (!state || state.status === "finished") return;
    state.status = "finished";
    state.completedAt = Date.now();
    api.sound?.(reason === "complete" ? "uiSuccess" : "uiError", { volume: 0.16, throttleMs: 250 });
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
    const accuracy = state.shots ? Math.round((state.hits / state.shots) * 100) : 0;
    const survived = state.player.hp > 0 ? 1 : 0;
    drawStickmanShooter(state);
    const failReason = state.deathReason === "trap" ? "即死陷阱" : "任務失敗";
    api.status(`結束 · 分數 ${Math.round(state.score).toLocaleString()} · 命中 ${accuracy}% · ${reason === "complete" ? "通關" : failReason}`);
    if (state.score > 0) api.achievement?.("first-clear", "火柴人出擊", "完成一局側捲射擊。");
    if (reason === "complete") {
      api.achievement?.(`stickman-clear-${state.level.key}`, `${state.level.label} 通關`, `使用 ${stickmanWeapon(state).label} 通過 ${state.level.mechanic}。`);
      if (state.level.key === "core") api.achievement?.("stickman-campaign-clear", "火柴人全關卡制霸", "完成核心實驗室。");
    }
    if (reason === "complete" && state.trapHits === 0) api.achievement?.("stickman-no-trap-hit", "陷阱零失誤", "通關且沒有被陷阱擊中。");
    api.mission?.("score-1600", state.score, 1600, "火柴人 1600 分");
    api.mission?.("boss", state.bossDefeated, 1, "擊破側捲 Boss");
    api.mission?.("accuracy-40", accuracy, 40, "命中率 40%");
    api.mission?.("powerups-3", state.powerupsCollected, 3, "取得 3 個道具");
    api.mission?.("traps-4", state.trapsPassed, 4, "通過 4 個即死陷阱");
    const penaltySeconds = survived ? 0 : 5;
    const rawElapsedMs = Math.max(1, Date.now() - state.startedAt);
    if (stickmanIsCoop(state)) {
      queueStickmanMultiplayerEvent(state, {
        type: survived ? "finish" : "down",
        payload: { reason, score: Math.round(state.score), hp: state.player.hp },
      });
      syncStickmanMultiplayer(api, state, { force: true });
    }
    api.submitScore({
      score: Math.max(1, Math.round(state.score)),
      difficulty: state.dailyChallenge?.difficulty || `${state.level.key}-${MODES[state.modeIndex].key}`,
      puzzle_id: state.dailyChallenge?.key || `${api.key}-${state.level.key}`,
      raw_elapsed_ms: rawElapsedMs,
      elapsed_ms: rawElapsedMs + penaltySeconds * 1000,
      penalty_seconds: penaltySeconds,
      guess_count: 0,
      accuracy,
      survive: survived,
      wave: state.wave,
      boss: state.bossDefeated,
    });
  }

  function updateStickmanShooter(api) {
    const state = api._stickmanShooterState;
    if (!state || state.status !== "active" || state.paused) return;
    state.tick += 1;
    state.score += 0.08;
    updateStickmanPlayer(state);
    updateStickmanCoopPuzzles(state);
    if (state.keys.fire) fireStickmanShot(api, state);
    if (state.reloadTicks > 0) {
      state.reloadTicks -= 1;
      if (state.reloadTicks <= 0 && finishStickmanReload(state) && state.emptyReload) {
        state.emptyReload = false;
        api.achievement?.("reload-discipline", "冷靜換彈", "彈匣耗盡後完成換彈。");
      }
    }
    applyStickmanPhysics(state);
    updateStickmanCoopPuzzles(state);
    advanceStickmanRoom(state);
    updateStickmanEnemies(state);
    updateStickmanBullets(api, state);
    updateStickmanPowerups(api, state);
    updateStickmanHazards(api, state);
    updateStickmanContacts(api, state);
    syncStickmanMultiplayer(api, state);
    state.cameraX = clamp(state.player.x - WIDTH * 0.38, 0, WORLD_WIDTH - WIDTH);
    state.particles.forEach((particle) => {
      particle.x += particle.vx;
      particle.y += particle.vy;
      particle.vy += 0.05;
      particle.life -= 1;
    });
    state.particles = state.particles.filter((particle) => particle.life > 0);
    if (state.player.hp <= 0) {
      finishStickmanShooter(api, "down");
      return;
    }
    if (state.bossDefeated && state.player.x > WORLD_WIDTH - 100) {
      const peer = stickmanPeerRect(state);
      if (stickmanIsCoop(state) && (!peer || peer.x < WORLD_WIDTH - 150)) {
        state.score += 0.5;
        drawStickmanShooter(state);
        setStickmanStatus(api, state);
        return;
      }
      state.score += 500 + state.player.hp * 120 + state.reserve * 3;
      queueStickmanMultiplayerEvent(state, { type: "objective", payload: { label: "雙人抵達終點" } });
      syncStickmanMultiplayer(api, state, { force: true });
      finishStickmanShooter(api, "complete");
      return;
    }
    drawStickmanShooter(state);
    setStickmanStatus(api, state);
  }

  function stickmanAssetTheme(level) {
    const themes = {
      dock: {
        base: "#334155",
        top: "#f59e0b",
        trim: "#fde68a",
        shadow: "#0f172a",
        glow: "#38bdf8",
        glass: "#bae6fd",
        hazard: "#f97316",
      },
      reactor: {
        base: "#155e75",
        top: "#22d3ee",
        trim: "#a5f3fc",
        shadow: "#082f49",
        glow: "#67e8f9",
        glass: "#cffafe",
        hazard: "#facc15",
      },
      skyline: {
        base: "#4338ca",
        top: "#f472b6",
        trim: "#c4b5fd",
        shadow: "#1e1b4b",
        glow: "#fb7185",
        glass: "#ddd6fe",
        hazard: "#38bdf8",
      },
      core: {
        base: "#581c87",
        top: "#a78bfa",
        trim: "#e9d5ff",
        shadow: "#2e1065",
        glow: "#c084fc",
        glass: "#f5d0fe",
        hazard: "#fb7185",
      },
    };
    return themes[level?.key] || themes.dock;
  }

  function drawStickmanAssetTile(ctx, x, y, w, h, theme, options = {}) {
    const tileSize = options.tileSize || 24;
    if (options.asset && drawStickmanTiledImage(ctx, options.asset, x, y, w, h, tileSize, tileSize, { alpha: options.assetAlpha ?? 1 })) {
      if (options.warning) {
        ctx.fillStyle = "rgba(250,204,21,.42)";
        for (let px = x - h; px < x + w; px += 22) {
          ctx.save();
          ctx.translate(px, y);
          ctx.rotate(-Math.PI / 6);
          ctx.fillRect(0, 0, 8, h * 1.8);
          ctx.restore();
        }
      }
      ctx.strokeStyle = "rgba(15,23,42,.32)";
      ctx.strokeRect(x + 1, y + 1, Math.max(0, w - 2), Math.max(0, h - 2));
      return;
    }
    const gradient = ctx.createLinearGradient(x, y, x, y + h);
    gradient.addColorStop(0, options.fill || theme.base);
    gradient.addColorStop(1, options.shadow || theme.shadow);
    ctx.fillStyle = gradient;
    ctx.fillRect(x, y, w, h);
    ctx.fillStyle = options.top || theme.top;
    ctx.fillRect(x, y, w, Math.min(5, Math.max(3, h * 0.22)));
    ctx.strokeStyle = "rgba(255,255,255,.14)";
    ctx.strokeRect(x + 1, y + 1, Math.max(0, w - 2), Math.max(0, h - 2));
    ctx.strokeStyle = "rgba(15,23,42,.46)";
    for (let px = x + tileSize; px < x + w; px += tileSize) {
      ctx.beginPath();
      ctx.moveTo(px, y + 2);
      ctx.lineTo(px, y + h - 2);
      ctx.stroke();
    }
    for (let py = y + tileSize; py < y + h; py += tileSize) {
      ctx.beginPath();
      ctx.moveTo(x + 2, py);
      ctx.lineTo(x + w - 2, py);
      ctx.stroke();
    }
    if (options.warning) {
      ctx.fillStyle = "rgba(250,204,21,.72)";
      for (let px = x - h; px < x + w; px += 22) {
        ctx.save();
        ctx.translate(px, y);
        ctx.rotate(-Math.PI / 6);
        ctx.fillRect(0, 0, 8, h * 1.8);
        ctx.restore();
      }
    }
    ctx.fillStyle = "rgba(255,255,255,.34)";
    for (let px = x + 8; px < x + w - 4; px += tileSize) {
      ctx.beginPath();
      ctx.arc(px, y + Math.min(10, h - 5), 1.8, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function stickmanBackdropAsset(level) {
    if (level?.key === "reactor") return "backgroundHills";
    if (level?.key === "skyline") return "backgroundTrees";
    if (level?.key === "core") return "backgroundClouds";
    return "backgroundDesert";
  }

  function stickmanTileAsset(type) {
    const map = {
      ground: "terrainGrass",
      container: "blockBlue",
      cratewalk: "blockYellow",
      crane: "blockYellow",
      pipe: "terrainStone",
      reactor: "blockExclamation",
      vent: "terrainStone",
      girder: "blockBlue",
      billboard: "blockRed",
      antenna: "terrainStone",
      "core-rail": "blockRed",
      "core-node": "blockExclamation",
    };
    return map[type] || "terrainStone";
  }

  function stickmanClusterAsset(tile) {
    if (tile === "warning") return "blockExclamation";
    if (tile === "core") return "gemRed";
    if (tile === "neon") return "gemBlue";
    if (tile === "crate") return "blockYellow";
    if (tile === "container") return "blockBlue";
    return "blockBlue";
  }

  function stickmanPowerAsset(kind) {
    const map = {
      mushroom: "mushroom",
      fireFlower: "gemRed",
      star: "coin",
      spring: "spring",
      ammo: "blockCoin",
      shield: "heart",
    };
    return map[kind] || "coin";
  }

  function stickmanEnemyAsset(enemy) {
    const role = enemy?.aiRole || enemy?.kind;
    if (enemy?.kind === "boss") return "enemyFrog";
    if (role === "rusher") return "enemyMouse";
    if (role === "shield") return "enemySlime";
    if (role === "sniper") return "enemyBee";
    if (role === "ambusher") return "enemyFireSlime";
    if (role === "grenadier") return "enemyFrog";
    return "enemySlime";
  }

  function stickmanPlayerAsset(state, figureType, hurt, walkCycle) {
    if (hurt) return "playerHit";
    if (figureType === "coop") return Math.abs(Math.sin(walkCycle || 0)) > 0.38 ? "coopWalkA" : "coopIdle";
    if (!state?.player?.grounded && figureType === "player") return "playerJump";
    if (Math.abs(Math.sin(walkCycle || 0)) > 0.72) return "playerWalkB";
    if (Math.abs(Math.sin(walkCycle || 0)) > 0.28) return "playerWalkA";
    return "playerIdle";
  }

  function drawStickmanAssetBackdrop(ctx, item, x, state, theme) {
    const level = stickmanLevelFromState(state);
    drawStickmanImage(ctx, stickmanBackdropAsset(level), x, item.y - 20, item.w, item.h + 42, { alpha: 0.36 });
    const hill = ctx.createLinearGradient(0, item.y, 0, item.y + item.h);
    hill.addColorStop(0, `${theme.glow}20`);
    hill.addColorStop(1, "rgba(15,23,42,.04)");
    ctx.fillStyle = hill;
    ctx.beginPath();
    ctx.moveTo(x, item.y + item.h);
    for (let i = 0; i <= 8; i += 1) {
      const px = x + (item.w / 8) * i;
      const py = item.y + item.h * 0.54 + Math.sin((state.tick + i * 36) / 120) * 5 - (i % 3) * 12;
      ctx.lineTo(px, py);
    }
    ctx.lineTo(x + item.w, item.y + item.h);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = "rgba(15,23,42,.22)";
    for (let i = 0; i < 12; i += 1) {
      const px = x + 18 + i * 92;
      const h = 34 + (i % 5) * 14;
      ctx.fillRect(px, item.y + item.h - h, 34 + (i % 3) * 12, h);
      ctx.fillStyle = i % 2 ? `${theme.glow}28` : "rgba(226,232,240,.13)";
      ctx.fillRect(px + 8, item.y + item.h - h + 10, 8, 5);
      ctx.fillStyle = "rgba(15,23,42,.22)";
    }
  }

  function drawStickmanAssetProp(ctx, item, x, state, theme) {
    const y = item.y;
    if (item.prop === "lamp") {
      ctx.strokeStyle = "rgba(226,232,240,.44)";
      ctx.lineWidth = 4;
      ctx.beginPath();
      ctx.moveTo(x + 14, y + item.h);
      ctx.lineTo(x + 14, y + 8);
      ctx.lineTo(x + item.w - 8, y + 8);
      ctx.stroke();
      ctx.fillStyle = `${theme.glow}55`;
      ctx.beginPath();
      ctx.arc(x + item.w - 8, y + 14, 18 + Math.sin(state.tick / 18) * 2, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = theme.trim;
      ctx.fillRect(x + item.w - 16, y + 6, 16, 10);
      return;
    }
    if (item.prop === "sign") {
      ctx.fillStyle = "rgba(15,23,42,.76)";
      ctx.fillRect(x + 8, y + 8, item.w - 16, item.h - 18);
      ctx.strokeStyle = theme.top;
      ctx.strokeRect(x + 12, y + 12, item.w - 24, item.h - 26);
      ctx.fillStyle = theme.trim;
      ctx.font = "800 12px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(item.label || "GO", x + item.w / 2, y + item.h / 2 + 2);
      ctx.textAlign = "start";
      ctx.fillStyle = "rgba(148,163,184,.64)";
      ctx.fillRect(x + 18, y + item.h - 10, 4, 18);
      ctx.fillRect(x + item.w - 22, y + item.h - 10, 4, 18);
      return;
    }
    if (item.prop === "hazard") {
      drawStickmanAssetTile(ctx, x, y + 16, item.w, item.h - 16, theme, { fill: "#78350f", top: "#facc15", warning: true, tileSize: 18 });
      ctx.fillStyle = "rgba(251,113,133,.28)";
      ctx.beginPath();
      ctx.arc(x + item.w / 2, y + 18, 18 + Math.sin(state.tick / 20) * 3, 0, Math.PI * 2);
      ctx.fill();
      return;
    }
    if (item.prop === "console") {
      drawStickmanAssetTile(ctx, x + 8, y + 20, item.w - 16, item.h - 20, theme, { fill: "#1f2937", top: theme.glow, tileSize: 18 });
      ctx.fillStyle = `${theme.glow}66`;
      for (let i = 0; i < 4; i += 1) ctx.fillRect(x + 18 + i * 14, y + 30 + (i % 2) * 10, 8, 6);
      ctx.fillStyle = theme.trim;
      ctx.beginPath();
      ctx.arc(x + item.w - 22, y + 38, 6, 0, Math.PI * 2);
      ctx.fill();
      return;
    }
    if (item.prop === "fan") {
      ctx.fillStyle = "rgba(15,23,42,.64)";
      ctx.fillRect(x + 8, y + 8, item.w - 16, item.h - 16);
      ctx.save();
      ctx.translate(x + item.w / 2, y + item.h / 2);
      ctx.rotate(state.tick / 10);
      ctx.fillStyle = theme.glow;
      for (let i = 0; i < 4; i += 1) {
        ctx.rotate(Math.PI / 2);
        ctx.fillRect(0, -3, 24, 6);
      }
      ctx.restore();
      ctx.strokeStyle = "rgba(226,232,240,.32)";
      ctx.strokeRect(x + 12, y + 12, item.w - 24, item.h - 24);
      return;
    }
    if (item.prop === "antenna") {
      ctx.strokeStyle = theme.trim;
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.moveTo(x + item.w / 2, y + item.h);
      ctx.lineTo(x + item.w / 2, y + 6);
      ctx.moveTo(x + item.w / 2, y + 22);
      ctx.lineTo(x + 10, y + 42);
      ctx.moveTo(x + item.w / 2, y + 22);
      ctx.lineTo(x + item.w - 10, y + 42);
      ctx.stroke();
      ctx.fillStyle = `${theme.glow}66`;
      ctx.beginPath();
      ctx.arc(x + item.w / 2, y + 8, 6 + Math.sin(state.tick / 16) * 1.5, 0, Math.PI * 2);
      ctx.fill();
      return;
    }
    if (item.prop === "coil") {
      ctx.strokeStyle = `${theme.glow}88`;
      ctx.lineWidth = 4;
      for (let i = 0; i < 4; i += 1) {
        ctx.beginPath();
        ctx.ellipse(x + item.w / 2, y + 16 + i * 13, item.w * 0.28, 7, 0, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.fillStyle = `${theme.glow}33`;
      ctx.fillRect(x + item.w / 2 - 5, y + 8, 10, item.h - 16);
    }
  }

  function drawStickmanFigure(ctx, x, y, facing, color, accent, hurt = false, scale = 1, walkCycle = 0, assetKey = "") {
    const stride = Math.sin(walkCycle || 0);
    const counter = Math.sin((walkCycle || 0) + Math.PI);
    const legA = stride * 7;
    const legB = counter * 7;
    const armSwing = counter * 4;
    if (assetKey && drawStickmanImage(ctx, assetKey, x - 18 * scale, y + 2, 36 * scale, 48 * scale, { flipX: facing < 0 })) {
      ctx.fillStyle = "rgba(15,23,42,.42)";
      ctx.beginPath();
      ctx.ellipse(x, y + 52 * scale, 15 * scale, 4 * scale, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = accent;
      ctx.fillRect(x + facing * 10 * scale, y + 20 * scale, facing * 26 * scale, 5 * scale);
      ctx.fillStyle = hurt ? "#fef3c7" : color;
      ctx.fillRect(x + facing * 32 * scale, y + 18 * scale, facing * 10 * scale, 4 * scale);
      return;
    }
    ctx.save();
    ctx.translate(x, y);
    ctx.scale(scale, scale);
    const suit = hurt ? "#fef3c7" : color;
    ctx.strokeStyle = suit;
    ctx.fillStyle = accent;
    ctx.lineWidth = 3;
    ctx.lineCap = "round";
    ctx.fillStyle = "rgba(15,23,42,.42)";
    ctx.beginPath();
    ctx.ellipse(0, 52, 15, 4, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = accent;
    ctx.fillRect(-6, 17, 12, 20);
    ctx.fillStyle = `${accent}99`;
    ctx.fillRect(-9, 19, 18, 7);
    ctx.beginPath();
    ctx.arc(0, 7, 7, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = "rgba(255,255,255,.18)";
    ctx.beginPath();
    ctx.arc(0, 7, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#0f172a";
    ctx.fillRect(facing > 0 ? 1 : -9, 5, 8, 3);
    ctx.beginPath();
    ctx.moveTo(0, 15);
    ctx.lineTo(0, 35);
    ctx.moveTo(0, 21);
    ctx.lineTo(facing * 17, 18 + armSwing * 0.25);
    ctx.moveTo(facing * 17, 18 + armSwing * 0.25);
    ctx.lineTo(facing * 27, 18 + armSwing * 0.2);
    ctx.moveTo(0, 25);
    ctx.lineTo(-facing * (12 + armSwing * 0.22), 31 - armSwing * 0.25);
    ctx.moveTo(0, 35);
    ctx.lineTo(-9 + legA, 51);
    ctx.moveTo(0, 35);
    ctx.lineTo(10 + legB, 51);
    ctx.stroke();
    ctx.fillStyle = "#111827";
    ctx.fillRect(facing * 15, 14, facing * 18, 6);
    ctx.fillStyle = suit;
    ctx.fillRect(facing * 27, 12, facing * 10, 4);
    ctx.fillStyle = accent;
    ctx.fillRect(-facing * 12, 20, 6, 18);
    ctx.restore();
  }

  function drawStickmanTraps(ctx, state, cam) {
    state.traps.forEach((trap) => {
      const rect = currentStickmanTrapRect(state, trap);
      const baseX = trap.x - cam;
      if (baseX > WIDTH + 120 || baseX + (trap.w || trap.r * 2 || 80) < -120) return;
      if (trap.type === "spikes") {
        if (drawStickmanTiledImage(ctx, "spikes", baseX, trap.y - 18, trap.w, 26, 24, 26)) return;
        ctx.fillStyle = "#991b1b";
        for (let x = 0; x < trap.w; x += 12) {
          ctx.beginPath();
          ctx.moveTo(baseX + x, trap.y + trap.h);
          ctx.lineTo(baseX + x + 6, trap.y - 16);
          ctx.lineTo(baseX + x + 12, trap.y + trap.h);
          ctx.closePath();
          ctx.fill();
        }
        ctx.fillStyle = "rgba(248,113,113,.42)";
        ctx.fillRect(baseX, trap.y + trap.h - 2, trap.w, 3);
      } else if (trap.type === "laser") {
        const active = Boolean(rect);
        ctx.fillStyle = active ? "rgba(239,68,68,.2)" : "rgba(248,113,113,.08)";
        ctx.fillRect(baseX - 14, trap.y, trap.w + 28, trap.h);
        ctx.fillStyle = active ? "#ef4444" : "#7f1d1d";
        ctx.fillRect(baseX, trap.y, trap.w, trap.h);
        ctx.fillStyle = "#fecaca";
        ctx.fillRect(baseX - 3, trap.y - 6, trap.w + 6, 6);
        ctx.fillRect(baseX - 3, trap.y + trap.h, trap.w + 6, 6);
      } else if (trap.type === "saw" && rect) {
        const cx = rect.x - cam + rect.w / 2;
        const cy = rect.y + rect.h / 2;
        if (drawStickmanImage(ctx, "saw", cx - trap.r - 8, cy - trap.r - 8, trap.r * 2 + 16, trap.r * 2 + 16, { rotation: state.tick / 6 })) return;
        ctx.save();
        ctx.translate(cx, cy);
        ctx.rotate(state.tick / 6);
        ctx.fillStyle = "#cbd5e1";
        for (let i = 0; i < 10; i += 1) {
          ctx.rotate(Math.PI / 5);
          ctx.fillRect(-3, -trap.r - 6, 6, 10);
        }
        ctx.beginPath();
        ctx.arc(0, 0, trap.r, 0, Math.PI * 2);
        ctx.fillStyle = "#64748b";
        ctx.fill();
        ctx.beginPath();
        ctx.arc(0, 0, trap.r * 0.45, 0, Math.PI * 2);
        ctx.fillStyle = "#0f172a";
        ctx.fill();
        ctx.restore();
      } else if (trap.type === "crusher" && rect) {
        ctx.fillStyle = "#7f1d1d";
        ctx.fillRect(rect.x - cam, rect.y, rect.w, rect.h);
        ctx.fillStyle = "#fecaca";
        for (let i = 5; i < rect.w; i += 12) ctx.fillRect(rect.x - cam + i, rect.y + rect.h - 8, 6, 10);
        ctx.strokeStyle = "rgba(226,232,240,.32)";
        ctx.beginPath();
        ctx.moveTo(rect.x - cam + rect.w / 2, 40);
        ctx.lineTo(rect.x - cam + rect.w / 2, rect.y);
        ctx.stroke();
      } else if (trap.type === "electric") {
        const active = Boolean(rect);
        ctx.fillStyle = active ? "rgba(34,211,238,.46)" : "rgba(34,211,238,.12)";
        ctx.fillRect(baseX, trap.y, trap.w, trap.h);
        ctx.strokeStyle = active ? "#67e8f9" : "#155e75";
        ctx.beginPath();
        for (let x = 0; x < trap.w; x += 12) {
          ctx.moveTo(baseX + x, trap.y + trap.h);
          ctx.lineTo(baseX + x + 6, trap.y - 10);
        }
        ctx.stroke();
      } else if (trap.type === "drone" && rect) {
        const x = rect.x - cam;
        ctx.fillStyle = "rgba(248,113,113,.2)";
        ctx.fillRect(x, trap.y, rect.w, rect.h);
        ctx.fillStyle = "#f43f5e";
        ctx.fillRect(x, trap.y, rect.w, rect.h);
        ctx.fillStyle = "#94a3b8";
        ctx.fillRect(x + rect.w / 2 - 12, trap.y - 26, 24, 12);
        ctx.fillStyle = "#e2e8f0";
        ctx.fillRect(x + rect.w / 2 - 20, trap.y - 21, 40, 3);
      }
    });
  }

  function drawStickmanCrates(ctx, state, cam) {
    state.crates.forEach((crate) => {
      const x = crate.x - cam;
      if (x > WIDTH + 40 || x + crate.w < -40) return;
      if (drawStickmanImage(ctx, "blockCoin", x - 3, crate.y - 4, crate.w + 6, crate.h + 8)) return;
      ctx.fillStyle = "#d97706";
      ctx.fillRect(x, crate.y, crate.w, crate.h);
      ctx.strokeStyle = "#fde68a";
      ctx.strokeRect(x + 2, crate.y + 2, crate.w - 4, crate.h - 4);
      ctx.fillStyle = "#fff7ed";
      ctx.font = "700 16px system-ui, sans-serif";
      ctx.fillText("?", x + 7, crate.y + 18);
    });
  }

  function drawStickmanPowerups(ctx, state, cam) {
    state.powerups.forEach((powerup) => {
      const meta = POWERUP_META[powerup.kind] || POWERUP_META.ammo;
      const x = powerup.x - cam;
      if (x > WIDTH + 40 || x + powerup.w < -40) return;
      const bob = Math.sin((state.tick + (powerup.bornAt || 0)) / 18) * 3;
      if (drawStickmanImage(ctx, stickmanPowerAsset(powerup.kind), x - 2, powerup.y - 2 + bob, powerup.w + 4, powerup.h + 4, {
        rotation: powerup.kind === "star" ? state.tick / 16 : 0,
      })) return;
      ctx.beginPath();
      ctx.arc(x + powerup.w / 2, powerup.y + powerup.h / 2 + bob, powerup.w / 2, 0, Math.PI * 2);
      ctx.fillStyle = meta.color;
      ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,.72)";
      ctx.stroke();
      ctx.fillStyle = powerup.kind === "star" ? "#713f12" : "#0f172a";
      ctx.font = "700 12px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(meta.glyph, x + powerup.w / 2, powerup.y + powerup.h / 2 + bob + 4);
      ctx.textAlign = "start";
    });
  }

  function drawStickmanCoopPuzzles(ctx, state, cam) {
    if (!stickmanIsCoop(state) || !state.coopPuzzles) return;
    state.coopPuzzles.plates.forEach((plate) => {
      const x = plate.x - cam;
      if (x > WIDTH + 60 || x + plate.w < -60) return;
      ctx.fillStyle = plate.pressed ? "#22c55e" : "#64748b";
      ctx.fillRect(x, plate.y, plate.w, plate.h);
      ctx.fillStyle = plate.pressed ? "rgba(34,197,94,.22)" : "rgba(148,163,184,.18)";
      ctx.fillRect(x - 4, plate.y - 6, plate.w + 8, plate.h + 12);
      ctx.fillStyle = "#e2e8f0";
      ctx.font = "700 10px system-ui, sans-serif";
      ctx.fillText(plate.label, x + 8, plate.y + 10);
    });
    state.coopPuzzles.gates.forEach((gate) => {
      const x = gate.x - cam;
      if (x > WIDTH + 70 || x + gate.w < -70) return;
      ctx.fillStyle = gate.open ? "rgba(34,197,94,.24)" : "rgba(248,113,113,.78)";
      ctx.fillRect(x, gate.y, gate.w, gate.h);
      ctx.strokeStyle = gate.open ? "#86efac" : "#fecaca";
      ctx.strokeRect(x, gate.y, gate.w, gate.h);
      ctx.save();
      ctx.translate(x + gate.w / 2, gate.y + gate.h / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.fillStyle = "#e2e8f0";
      ctx.font = "700 10px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(gate.open ? "OPEN" : gate.label, 0, 4);
      ctx.restore();
    });
  }

  function drawStickmanScenery(ctx, state, cam) {
    const level = stickmanLevelFromState(state);
    const theme = stickmanAssetTheme(level);
    (state.scenery || []).forEach((item) => {
      const x = item.x - cam * (item.parallax || 1);
      const width = item.w || 120;
      if (x > WIDTH + 220 || x + width < -220) return;
      if (item.kind === "asset-backdrop") {
        drawStickmanAssetBackdrop(ctx, item, x, state, theme);
        return;
      }
      if (item.kind === "asset-tile-cluster") {
        const tileW = 28;
        const tileH = 22;
        for (let row = 0; row < (item.rows || 2); row += 1) {
          for (let col = 0; col < (item.cols || 3); col += 1) {
            const px = x + col * tileW + (row % 2) * 7;
            const py = item.y - row * tileH;
            const warning = item.tile === "warning";
            const core = item.tile === "core";
            const neon = item.tile === "neon";
            drawStickmanAssetTile(ctx, px, py, tileW - 2, tileH - 2, theme, {
              fill: core ? "#3b0764" : neon ? "#312e81" : item.tile === "crate" ? "#78350f" : "#334155",
              top: warning ? "#facc15" : core ? "#c084fc" : neon ? "#f472b6" : theme.top,
              warning,
              tileSize: 14,
              asset: stickmanClusterAsset(item.tile),
            });
          }
        }
        return;
      }
      if (item.kind === "asset-prop") {
        drawStickmanAssetProp(ctx, item, x, state, theme);
        return;
      }
      if (item.kind === "room-marker") {
        const markerX = item.x - cam;
        if (markerX < -80 || markerX > WIDTH + 80) return;
        ctx.fillStyle = "rgba(226,232,240,.16)";
        ctx.fillRect(markerX + 14, 72, 56, 18);
        ctx.fillStyle = "rgba(226,232,240,.72)";
        ctx.font = "700 10px system-ui, sans-serif";
        ctx.fillText(`ZONE ${item.label}`, markerX + 20, 85);
        return;
      }
      if (item.kind === "container-stack") {
        const cols = item.cols || 3;
        const rows = item.rows || 2;
        for (let row = 0; row < rows; row += 1) {
          for (let col = 0; col < cols; col += 1) {
            const px = x + col * 38 + (row % 2) * 9;
            const py = item.y - row * 24;
            drawStickmanAssetTile(ctx, px, py, 36, 22, theme, {
              fill: (row + col) % 2 ? "#1d4ed8" : "#b45309",
              top: (row + col) % 2 ? "#60a5fa" : "#f59e0b",
              tileSize: 18,
              asset: (row + col) % 2 ? "blockBlue" : "blockRed",
            });
          }
        }
        return;
      }
      if (item.kind === "crane") {
        ctx.strokeStyle = "rgba(250,204,21,.42)";
        ctx.lineWidth = 4;
        ctx.beginPath();
        ctx.moveTo(x, item.y + item.h);
        ctx.lineTo(x + 26, item.y);
        ctx.lineTo(x + item.w, item.y + 10);
        ctx.stroke();
        ctx.lineWidth = 1;
        ctx.fillStyle = "rgba(250,204,21,.24)";
        ctx.fillRect(x + item.w - 20, item.y + 10, 10, 64);
        ctx.fillRect(x + item.w - 28, item.y + 74, 26, 14);
        return;
      }
      if (item.kind === "ship") {
        ctx.fillStyle = "rgba(15,23,42,.66)";
        ctx.beginPath();
        ctx.moveTo(x, item.y + item.h * 0.35);
        ctx.lineTo(x + item.w - 34, item.y + item.h * 0.35);
        ctx.lineTo(x + item.w, item.y + item.h);
        ctx.lineTo(x + 26, item.y + item.h);
        ctx.closePath();
        ctx.fill();
        ctx.fillStyle = "rgba(56,189,248,.16)";
        ctx.fillRect(x + 46, item.y + 4, item.w * 0.48, 12);
        ctx.fillStyle = "rgba(226,232,240,.26)";
        for (let px = x + 62; px < x + item.w - 80; px += 34) ctx.fillRect(px, item.y + item.h * 0.44, 12, 8);
        return;
      }
      if (item.kind === "reactor-core" || item.kind === "core-chamber") {
        const glow = item.kind === "reactor-core" ? "#22d3ee" : "#a78bfa";
        ctx.fillStyle = item.kind === "reactor-core" ? "rgba(8,47,73,.58)" : "rgba(46,16,101,.58)";
        ctx.fillRect(x, item.y, item.w, item.h);
        ctx.strokeStyle = glow;
        ctx.strokeRect(x + 8, item.y + 10, item.w - 16, item.h - 20);
        ctx.fillStyle = "rgba(226,232,240,.12)";
        for (let py = item.y + 18; py < item.y + item.h - 18; py += 22) {
          ctx.fillRect(x + 16, py, 10, 8);
          ctx.fillRect(x + item.w - 26, py + 7, 10, 8);
        }
        ctx.fillStyle = `${glow}33`;
        ctx.beginPath();
        ctx.arc(x + item.w / 2, item.y + item.h / 2, Math.min(item.w, item.h) * 0.22 + Math.sin(state.tick / 28) * 4, 0, Math.PI * 2);
        ctx.fill();
        return;
      }
      if (item.kind === "pipe-field") {
        ctx.strokeStyle = "rgba(34,211,238,.22)";
        for (let i = 0; i < 5; i += 1) {
          const py = item.y + 18 + i * 34;
          ctx.beginPath();
          ctx.moveTo(x, py);
          ctx.lineTo(x + item.w, py + Math.sin((state.tick + i * 20) / 40) * 4);
          ctx.stroke();
        }
        ctx.fillStyle = "rgba(14,116,144,.2)";
        ctx.fillRect(x + 8, item.y + item.h - 34, item.w - 16, 18);
        return;
      }
      if (item.kind === "tower") {
        ctx.fillStyle = "rgba(15,23,42,.55)";
        ctx.fillRect(x, item.y, item.w, item.h);
        ctx.fillStyle = "rgba(125,211,252,.18)";
        for (let py = item.y + 12; py < item.y + item.h - 12; py += 22) {
          for (let px = x + 10; px < x + item.w - 10; px += 24) ctx.fillRect(px, py, 9, 7);
        }
        return;
      }
      if (item.kind === "billboard") {
        ctx.fillStyle = "rgba(30,41,59,.76)";
        ctx.fillRect(x, item.y, item.w, item.h);
        ctx.strokeStyle = "rgba(244,114,182,.52)";
        ctx.strokeRect(x + 4, item.y + 4, item.w - 8, item.h - 8);
        ctx.fillStyle = "rgba(244,114,182,.74)";
        ctx.font = "800 18px system-ui, sans-serif";
        ctx.fillText(item.label || level.mechanic || "RUN", x + 16, item.y + item.h / 2 + 6);
        return;
      }
      if (item.kind === "skybridge") {
        ctx.fillStyle = "rgba(71,85,105,.42)";
        ctx.fillRect(x, item.y, item.w, item.h);
        ctx.strokeStyle = "rgba(148,163,184,.3)";
        ctx.strokeRect(x, item.y, item.w, item.h);
        ctx.strokeStyle = "rgba(226,232,240,.18)";
        for (let px = x + 14; px < x + item.w; px += 28) {
          ctx.beginPath();
          ctx.moveTo(px, item.y);
          ctx.lineTo(px + 14, item.y + item.h);
          ctx.stroke();
        }
        return;
      }
      if (item.kind === "core-spine") {
        ctx.fillStyle = "rgba(76,29,149,.46)";
        ctx.fillRect(x, item.y, item.w, item.h);
        ctx.strokeStyle = "rgba(196,181,253,.42)";
        for (let py = item.y + 12; py < item.y + item.h; py += 26) {
          ctx.beginPath();
          ctx.moveTo(x + 10, py);
          ctx.lineTo(x + item.w - 10, py + 12);
          ctx.stroke();
        }
      }
    });
  }

  function drawStickmanPlatform(ctx, platform, x) {
    const palette = {
      ground: ["#263244", "#38bdf8"],
      container: ["#92400e", "#fde68a"],
      cratewalk: ["#78350f", "#fbbf24"],
      crane: ["#854d0e", "#facc15"],
      pipe: ["#155e75", "#67e8f9"],
      reactor: ["#0e7490", "#22d3ee"],
      vent: ["#475569", "#e2e8f0"],
      girder: ["#4338ca", "#a5b4fc"],
      billboard: ["#831843", "#f472b6"],
      antenna: ["#334155", "#7dd3fc"],
      "core-rail": ["#581c87", "#c4b5fd"],
      "core-node": ["#6d28d9", "#d8b4fe"],
    };
    const [fill, top] = palette[platform.type] || ["#334155", "rgba(125,211,252,.18)"];
    drawStickmanAssetTile(ctx, x, platform.y, platform.w, platform.h, {
      base: fill,
      top,
      trim: top,
      shadow: "#0f172a",
      glow: top,
    }, {
      fill,
      top,
      warning: platform.type === "reactor" || platform.type === "core-node",
      tileSize: platform.type === "ground" ? 32 : 24,
      asset: stickmanTileAsset(platform.type),
      assetAlpha: platform.type === "ground" ? 0.92 : 1,
    });
    if (platform.type !== "ground") {
      ctx.strokeStyle = "rgba(15,23,42,.42)";
      for (let px = x + 16; px < x + platform.w; px += 28) {
        ctx.beginPath();
        ctx.moveTo(px, platform.y + platform.h);
        ctx.lineTo(px - 10, platform.y + platform.h + 18);
        ctx.stroke();
      }
    }
  }

  function drawStickmanShooter(state) {
    const ctx = state.ctx;
    ctx.clearRect(0, 0, WIDTH, HEIGHT);
    const cam = state.cameraX;
    const level = stickmanLevelFromState(state);
    const gradient = ctx.createLinearGradient(0, 0, 0, HEIGHT);
    gradient.addColorStop(0, level.bg?.[0] || "#07111f");
    gradient.addColorStop(0.56, level.bg?.[1] || "#132134");
    gradient.addColorStop(1, "#111827");
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, WIDTH, HEIGHT);

    ctx.fillStyle = level.key === "core" ? "rgba(196,181,253,.13)" : level.key === "reactor" ? "rgba(34,211,238,.13)" : "rgba(56,189,248,.12)";
    for (let i = 0; i < 34; i += 1) {
      const x = ((i * 93 - cam * 0.25) % (WIDTH + 80)) - 40;
      const h = 52 + (i % 6) * 14;
      ctx.fillRect(x, 318 - h, 42, h);
    }
    drawStickmanScenery(ctx, state, cam);
    ctx.strokeStyle = "rgba(148,163,184,.18)";
    for (let i = 0; i < 12; i += 1) {
      const y = 66 + i * 22;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(WIDTH, y + Math.sin((state.tick + i * 30) / 50) * 5);
      ctx.stroke();
    }

    state.platforms.forEach((platform) => {
      const x = platform.x - cam;
      if (x > WIDTH + 80 || x + platform.w < -80) return;
      drawStickmanPlatform(ctx, platform, x);
    });
    drawStickmanTraps(ctx, state, cam);
    drawStickmanCrates(ctx, state, cam);
    drawStickmanPowerups(ctx, state, cam);
    drawStickmanCoopPuzzles(ctx, state, cam);
    state.cover.forEach((cover) => {
      const x = cover.x - cam;
      if (x > WIDTH + 50 || x + cover.w < -50) return;
      ctx.fillStyle = "#475569";
      ctx.fillRect(x, cover.y, cover.w, cover.h);
      ctx.fillStyle = "rgba(255,255,255,.12)";
      ctx.fillRect(x + 4, cover.y + 5, cover.w - 8, 4);
    });

    state.playerShots.forEach((shot) => {
      ctx.fillStyle = shot.color || "#fef08a";
      ctx.fillRect(shot.x - cam, shot.y, shot.w, shot.h);
    });
    state.enemyShots.forEach((shot) => {
      ctx.beginPath();
      ctx.arc(shot.x - cam, shot.y, shot.r, 0, Math.PI * 2);
      ctx.fillStyle = shot.color || "#fb7185";
      ctx.fill();
    });
    state.enemies.forEach((enemy) => {
      const x = enemy.x - cam;
      if (x > WIDTH + 60 || x + enemy.w < -60) return;
      const scale = enemy.kind === "boss" ? 1.34 : 1;
      const role = stickmanEnemyRoleMeta(enemy);
      drawStickmanFigure(ctx, x + enemy.w / 2, enemy.y - 6, enemy.facing || 1, role.color, role.accent, enemy.hurt > 0, scale, enemy.walkCycle || 0, stickmanEnemyAsset(enemy));
      ctx.fillStyle = "rgba(15,23,42,.72)";
      ctx.fillRect(x - 4, enemy.y - 12, enemy.w + 8, 4);
      ctx.fillStyle = role.color;
      ctx.fillRect(x - 4, enemy.y - 12, (enemy.w + 8) * Math.max(0, enemy.hp / enemy.maxHp), 4);
      if (enemy.kind === "boss") {
        ctx.fillStyle = "#fecaca";
        ctx.font = "700 10px system-ui, sans-serif";
        ctx.fillText(enemy.bossLabel || state.bossName || "Boss", x - 12, enemy.y - 18);
      }
    });

    state.particles.forEach((particle) => {
      ctx.globalAlpha = clamp(particle.life / 22, 0, 1);
      ctx.fillStyle = particle.color;
      ctx.fillRect(particle.x - cam - 2, particle.y - 2, 4, 4);
      ctx.globalAlpha = 1;
    });

    const flicker = state.tick < state.invulnerableUntil && state.tick % 10 < 5;
    if (!flicker) {
      drawStickmanFigure(ctx, state.player.x - cam + PLAYER_W / 2, state.player.y - 6, state.player.facing || 1, "#e2e8f0", "#38bdf8", false, 1, state.player.walkCycle || 0, stickmanPlayerAsset(state, "player", false, state.player.walkCycle || 0));
    }
    const peer = stickmanPeerRect(state);
    if (peer && peer.hp > 0) {
      drawStickmanFigure(ctx, peer.x - cam + PLAYER_W / 2, peer.y - 6, peer.facing || 1, "#bbf7d0", "#16a34a", false, 1, peer.walkCycle || 0, stickmanPlayerAsset(state, "coop", false, peer.walkCycle || 0));
      ctx.fillStyle = "rgba(15,23,42,.72)";
      ctx.fillRect(peer.x - cam - 4, peer.y - 16, PLAYER_W + 8, 4);
      ctx.fillStyle = "#22c55e";
      ctx.fillRect(peer.x - cam - 4, peer.y - 16, (PLAYER_W + 8) * clamp(peer.hp / 5, 0, 1), 4);
      ctx.fillStyle = "rgba(226,232,240,.82)";
      ctx.font = "10px system-ui, sans-serif";
      ctx.fillText(peer.username || "隊友", peer.x - cam - 2, peer.y - 22);
    }
    ctx.fillStyle = "rgba(15,23,42,.72)";
    ctx.fillRect(14, 14, 398, 52);
    ctx.fillStyle = "#e2e8f0";
    ctx.font = "12px system-ui, sans-serif";
    const accuracy = state.shots ? Math.round((state.hits / state.shots) * 100) : 0;
    const powerText = activeStickmanPowerText(state) || "無";
    ctx.fillText(`${level.label}  weapon ${stickmanWeapon(state).label}`, 24, 29);
    ctx.fillText(`score ${Math.round(state.score).toLocaleString()}  hp ${state.player.hp}/${state.player.maxHp || 5}  ammo ${state.ammo}/${state.reserve}`, 24, 44);
    ctx.fillText(`room ${state.room}/${WORLD_ROOMS}  acc ${accuracy}%  stamina ${Math.round(state.stamina)}  traps ${state.trapsPassed}/${state.traps.length}  power ${powerText}`, 24, 59);
    ctx.fillStyle = "rgba(148,163,184,.26)";
    ctx.fillRect(384, 24, 146, 8);
    ctx.fillStyle = "#38bdf8";
    ctx.fillRect(384, 24, 146 * (state.stamina / 100), 8);

    if (state.tick < (state.bossIntroUntil || 0)) {
      ctx.fillStyle = "rgba(127,29,29,.76)";
      ctx.fillRect(158, 82, WIDTH - 316, 58);
      ctx.textAlign = "center";
      ctx.fillStyle = "#fecaca";
      ctx.font = "800 22px system-ui, sans-serif";
      ctx.fillText(`BOSS · ${state.bossName || "警戒目標"}`, WIDTH / 2, 116);
      ctx.textAlign = "start";
    }

    if (state.status === "finished") {
      ctx.fillStyle = "rgba(7,17,31,.78)";
      ctx.fillRect(126, 126, WIDTH - 252, 112);
      ctx.textAlign = "center";
      ctx.fillStyle = state.bossDefeated ? "#86efac" : "#fb7185";
      ctx.font = "700 28px system-ui, sans-serif";
      ctx.fillText(state.bossDefeated ? "MISSION CLEAR" : (state.deathReason === "trap" ? "INSTANT TRAP" : "MISSION FAILED"), WIDTH / 2, 170);
      ctx.fillStyle = "rgba(226,232,240,.9)";
      ctx.font = "13px system-ui, sans-serif";
      ctx.fillText(`分數 ${Math.round(state.score).toLocaleString()} · 命中 ${accuracy}%`, WIDTH / 2, 198);
      ctx.textAlign = "start";
    }
  }

  function startStickmanShooter(api, options = {}) {
    if (api._stickmanShooterState?.timer) clearInterval(api._stickmanShooterState.timer);
    const wantsCoop = options.multiplayerMode === "coop";
    const multiplayerRoom = wantsCoop ? api.multiplayer?.()?.activeRoom?.("stickman_shooter", "coop") : null;
    if (wantsCoop && !multiplayerRoom) {
      api.status("請先在多人房間邀請並選擇一位隊友。");
      return;
    }
    const canvas = api.root.querySelector("canvas");
    const dailyChallenge = api.dailyChallenge?.() || null;
    const mode = MODES[api._stickmanModeIndex || 0] || MODES[0];
    const level = currentStickmanLevel(api);
    const weapon = STICKMAN_WEAPONS[level.weapon] || STICKMAN_WEAPONS.rifle;
    const state = {
      canvas,
      ctx: canvas.getContext("2d"),
      status: "active",
      paused: false,
      startedAt: Date.now(),
      completedAt: null,
      tick: 0,
      score: 0,
      shots: 0,
      hits: 0,
      wave: 1,
      room: 1,
      modeIndex: api._stickmanModeIndex || 0,
      levelIndex: api._stickmanLevelIndex || 0,
      level,
      weaponKey: weapon.key,
      cameraX: 0,
      stamina: 100,
      ammo: weapon.mag,
      reserve: Math.max(12, mode.reserve + Number(weapon.reserve || 0)),
      reloadTicks: 0,
      emptyReload: false,
      nextShotAt: 0,
      invulnerableUntil: 80,
      fireUntil: 0,
      starUntil: 0,
      jumpBoostUntil: 0,
      shield: 0,
      weaponLevel: 1,
      powerupsCollected: 0,
      cratesBroken: 0,
      trapsPassed: 0,
      trapHits: 0,
      lastPickup: "",
      lastPickupUntil: 0,
      deathReason: "",
      bossSpawned: false,
      bossDefeated: 0,
      bossIntroUntil: 0,
      bossName: level.boss?.label || "",
      player: { x: 38, y: 318 - PLAYER_H, vx: 0, vy: 0, hp: 5, maxHp: 5, grounded: true, facing: 1, walkCycle: 0, doubleJumpUsed: false },
      keys: { left: false, right: false, fire: false, sprint: false },
      platforms: makeStickmanWorld(level),
      scenery: makeStickmanScenery(level),
      cover: makeStickmanCover(level),
      traps: makeStickmanTraps(level),
      coopPuzzles: wantsCoop ? makeStickmanCoopPuzzles(level) : null,
      crates: makeStickmanCrates(level),
      powerups: makeStickmanPowerups(level),
      spawnedRooms: new Set(),
      enemies: [],
      playerShots: [],
      enemyShots: [],
      particles: [],
      multiplayer: wantsCoop ? {
        room: multiplayerRoom,
        roomId: multiplayerRoom.id,
        mode: "coop",
        peer: null,
        afterEventId: 0,
        pendingEvents: [],
        processedEvents: new Set(),
        syncing: false,
        lastSyncTick: -999,
        lastError: "",
      } : null,
      dailyChallenge,
      rng: dailyChallenge?.seed ? window.createHackmeGameSeededRandom?.(dailyChallenge.seed) : null,
      timer: null,
    };
    api._stickmanShooterState = state;
    spawnStickmanRoom(state, 1);
    drawStickmanShooter(state);
    setStickmanStatus(api, state);
    if (wantsCoop) {
      api.multiplayer?.()?.start?.(multiplayerRoom.id).catch((err) => {
        window.reportFrontendFailure?.("stickman-multiplayer-start", err);
        api.status(err?.message || "多人連線啟動失敗。");
      });
      syncStickmanMultiplayer(api, state, { force: true });
    }
    state.timer = setInterval(() => updateStickmanShooter(api), 16);
  }

  function showStickmanShooterReady(api) {
    if (api._stickmanShooterState?.timer) clearInterval(api._stickmanShooterState.timer);
    api._stickmanShooterState = null;
    const canvas = api.root.querySelector("canvas");
    const ctx = canvas?.getContext("2d");
    if (!ctx) return;
    ctx.fillStyle = "#07111f";
    ctx.fillRect(0, 0, WIDTH, HEIGHT);
    const gradient = ctx.createLinearGradient(0, 0, WIDTH, HEIGHT);
    gradient.addColorStop(0, "rgba(56,189,248,.18)");
    gradient.addColorStop(1, "rgba(249,115,22,.16)");
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, WIDTH, HEIGHT);
    ctx.fillStyle = "rgba(148,163,184,.16)";
    for (let i = 0; i < 18; i += 1) ctx.fillRect(i * 44, 250 - (i % 5) * 18, 28, 72 + (i % 4) * 12);
    ctx.fillStyle = "#263244";
    ctx.fillRect(0, 318, WIDTH, 42);
    ctx.fillStyle = "#991b1b";
    for (let x = 282; x < 354; x += 12) {
      ctx.beginPath();
      ctx.moveTo(x, 318);
      ctx.lineTo(x + 6, 294);
      ctx.lineTo(x + 12, 318);
      ctx.closePath();
      ctx.fill();
    }
    ctx.fillStyle = "#d97706";
    ctx.fillRect(364, 244, 28, 28);
    ctx.fillStyle = "#fff7ed";
    ctx.font = "700 18px system-ui, sans-serif";
    ctx.fillText("?", 373, 265);
    ctx.beginPath();
    ctx.arc(430, 258, 12, 0, Math.PI * 2);
    ctx.fillStyle = POWERUP_META.star.color;
    ctx.fill();
    ctx.fillStyle = "#713f12";
    ctx.fillText("★", 424, 264);
    drawStickmanFigure(ctx, 120, 266, 1, "#e2e8f0", "#38bdf8", false, 1, 0, "playerIdle");
    drawStickmanFigure(ctx, 520, 266, -1, "#fda4af", "#7f1d1d", false, 1, 0, "enemySlime");
    ctx.textAlign = "center";
    ctx.fillStyle = "#e2e8f0";
    ctx.font = "700 28px system-ui, sans-serif";
    ctx.fillText("火柴人橫向射擊", WIDTH / 2, 132);
    ctx.font = "14px system-ui, sans-serif";
    ctx.fillStyle = "rgba(226,232,240,.82)";
    ctx.fillText("按開始後才會計時；即死陷阱、問號補給與短效道具會一起進場", WIDTH / 2, 160);
    ctx.textAlign = "start";
    const mode = MODES[api._stickmanModeIndex || 0] || MODES[0];
    const level = currentStickmanLevel(api);
    const weapon = STICKMAN_WEAPONS[level.weapon] || STICKMAN_WEAPONS.rifle;
    api.status(`待機 · ${level.label} · 機關：${level.mechanic} · 初始武器：${weapon.label} · 模式：${mode.label}`);
  }

  function setStickmanInput(state, name, pressed) {
    if (!state) return;
    if (name === "left") state.keys.left = pressed;
    if (name === "right") state.keys.right = pressed;
    if (name === "fire") state.keys.fire = pressed;
    if (name === "sprint") state.keys.sprint = pressed;
  }

  function handleStickmanKey(api, event, pressed) {
    const state = api._stickmanShooterState;
    const key = event.key;
    if (["ArrowLeft", "ArrowRight", "ArrowUp", " ", "a", "A", "d", "D", "w", "W", "j", "J", "r", "R", "Shift"].includes(key)) {
      event.preventDefault?.();
    }
    if (key === "ArrowLeft" || key === "a" || key === "A") setStickmanInput(state, "left", pressed);
    if (key === "ArrowRight" || key === "d" || key === "D") setStickmanInput(state, "right", pressed);
    if (key === "Shift") setStickmanInput(state, "sprint", pressed);
    if ((key === "ArrowUp" || key === "w" || key === "W" || key === " ") && pressed) jumpStickman(state);
    if (key === "j" || key === "J") setStickmanInput(state, "fire", pressed);
    if ((key === "r" || key === "R") && pressed) startStickmanReload(state);
  }

  function updateStickmanActions(api) {
    const mode = MODES[api._stickmanModeIndex || 0] || MODES[0];
    const level = currentStickmanLevel(api);
    api.setActions(`
      <button class="btn game-mini-btn btn-primary" type="button" data-action="new">開始</button>
      <button class="btn game-mini-btn" type="button" data-action="coop">合作開始</button>
      <button class="btn game-mini-btn" type="button" data-action="pause">暫停</button>
      <button class="btn game-mini-btn" type="button" data-action="level">關卡：${level.label}</button>
      <button class="btn game-mini-btn" type="button" data-action="mode">模式：${mode.label}</button>
      <button class="btn game-mini-btn" type="button" data-action="finish">結算</button>
    `);
  }

  window.registerHackmeLocalGameModule("stickman_shooter", {
    mount(api) {
      api._stickmanModeIndex = api._stickmanModeIndex || 0;
      api._stickmanLevelIndex = api._stickmanLevelIndex || 0;
      api.setTitle("火柴人橫向射擊");
      api.setSwipeMode?.("hold");
      api.root.innerHTML = `<div class="stickman-shooter-shell"><canvas class="stickman-shooter-canvas" width="${WIDTH}" height="${HEIGHT}" aria-label="火柴人橫向射擊"></canvas></div>`;
      updateStickmanActions(api);
      api.setControls(`
        <button class="btn game-mini-btn" type="button" data-hold="left">左</button>
        <button class="btn game-mini-btn" type="button" data-hold="right">右</button>
        <button class="btn game-mini-btn" type="button" data-jump="1">跳</button>
        <button class="btn game-mini-btn btn-primary" type="button" data-hold="fire">射擊</button>
        <button class="btn game-mini-btn" type="button" data-reload="1">換彈</button>
        <button class="btn game-mini-btn" type="button" data-hold="sprint">衝刺</button>
      `);
      api.onAction = (action) => {
        if (action === "new") startStickmanShooter(api);
        if (action === "coop") startStickmanShooter(api, { multiplayerMode: "coop" });
        if (action === "pause" && api._stickmanShooterState?.status === "active") {
          api._stickmanShooterState.paused = !api._stickmanShooterState.paused;
          api.status(api._stickmanShooterState.paused ? "暫停中。" : "繼續。");
        }
        if (action === "mode") {
          if (api._stickmanShooterState?.timer) clearInterval(api._stickmanShooterState.timer);
          api._stickmanModeIndex = ((api._stickmanModeIndex || 0) + 1) % MODES.length;
          updateStickmanActions(api);
          showStickmanShooterReady(api);
        }
        if (action === "level") {
          if (api._stickmanShooterState?.timer) clearInterval(api._stickmanShooterState.timer);
          api._stickmanLevelIndex = ((api._stickmanLevelIndex || 0) + 1) % STICKMAN_LEVELS.length;
          updateStickmanActions(api);
          showStickmanShooterReady(api);
        }
        if (action === "finish") finishStickmanShooter(api, "manual");
      };
      api.onControl = (target, pressed) => {
        const state = api._stickmanShooterState;
        if (target.dataset.jump && pressed) jumpStickman(state);
        if (target.dataset.reload && pressed) startStickmanReload(state);
        setStickmanInput(state, target.dataset.hold || "", pressed);
      };
      api.onKey = (event, pressed) => handleStickmanKey(api, event, pressed);
      showStickmanShooterReady(api);
      return () => {
        if (api._stickmanShooterState?.timer) clearInterval(api._stickmanShooterState.timer);
        api._stickmanShooterState = null;
      };
    },
  });
}());
