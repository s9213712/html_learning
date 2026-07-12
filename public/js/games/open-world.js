'use strict';

(function () {
  const helpers = window.HACKME_LOCAL_GAME_HELPERS || {};
  const clamp = helpers.clamp || ((value, min, max) => Math.max(min, Math.min(max, value)));
  const WORLD_SIZE = 168;
  const ROAD_WIDTH = 9;
  const PLAYER_RADIUS = 1.05;
  const VEHICLE_RADIUS = 2.1;
  const OPEN_WORLD_ROADS = [-72, -48, -24, 0, 24, 48, 72];
  const OPEN_WORLD_BLOCK_CENTERS = [-60, -36, -12, 12, 36, 60];
  const OPEN_WORLD_PLAYER_START = { x: 14, z: 14, angle: Math.PI * 0.25 };
  const OPEN_WORLD_DIAGONAL_ROADS = [
    { x1: -82, z1: -34, x2: -16, z2: -82, width: 10.5, color: "#3b4250" },
    { x1: -84, z1: 18, x2: 30, z2: 78, width: 10, color: "#343a46" },
    { x1: 18, z1: -82, x2: 82, z2: -16, width: 10.5, color: "#2f3542" },
  ];
  const OPEN_WORLD_ALLEY_ROADS = [
    { x1: -78, z1: -12, x2: -40, z2: 7, width: 3.6, color: "#46515f", noCenterLine: true },
    { x1: -39, z1: 34, x2: -8, z2: 51, width: 3.2, color: "#4b5563", noCenterLine: true },
    { x1: 13, z1: 16, x2: 52, z2: 33, width: 3.4, color: "#414957", noCenterLine: true },
    { x1: 39, z1: -71, x2: 67, z2: -42, width: 3.5, color: "#475569", noCenterLine: true },
  ];
  const OPEN_WORLD_SCENIC_ZONES = Object.freeze([
    { key: "harbor", label: "碼頭水岸", x: -88, z: -8, w: 12, d: 174, color: "#0e7490" },
    { key: "park", label: "中央公園", x: 55, z: 55, w: 28, d: 24, color: "#3f8f57" },
    { key: "hospital", label: "醫療區", x: 58, z: 68, w: 21, d: 14, color: "#e0f2fe" },
    { key: "industrial", label: "工業倉儲", x: 46, z: -58, w: 31, d: 24, color: "#64748b" },
    { key: "market", label: "夜市街", x: -33, z: 45, w: 22, d: 15, color: "#f59e0b" },
    { key: "oldtown", label: "老城墓園", x: -60, z: -60, w: 28, d: 23, color: "#7dd3fc" },
  ]);
  const ENTER_VEHICLE_DISTANCE = 6.5;
  const TRAFFIC_HIT_DISTANCE = 6.2;
  const TRAFFIC_COLLISION_DISTANCE = 4.2;
  const PEDESTRIAN_TRAFFIC_COLLISION_DISTANCE = 2.7;
  const TRAFFIC_COLLISION_COOLDOWN_MS = 650;
  const PATROL_DAMAGE_DISTANCE = 14;
  const PICKUP_DISTANCE = 3;
  const TAIL_GADGET_HIT_DISTANCE = 3;
  const PATROL_GADGET_HIT_DISTANCE = 3;
  const OPEN_WORLD_VEHICLE_SPAWNS = [
    { x: -63, z: -48, angle: Math.PI / 2, type: "sedan", color: "#ef4444", maxSpeed: 31, accel: 26, handling: 1.85 },
    { x: -24, z: 66, angle: 0, type: "van", color: "#f97316", maxSpeed: 24, accel: 18, handling: 1.35 },
    { x: 42, z: -24, angle: -Math.PI / 2, type: "coupe", color: "#22c55e", maxSpeed: 36, accel: 30, handling: 2.05 },
    { x: 72, z: 24, angle: Math.PI, type: "sports", color: "#a855f7", maxSpeed: 42, accel: 34, handling: 2.25 },
    { x: -48, z: 0, angle: Math.PI / 2, type: "taxi", color: "#facc15", maxSpeed: 30, accel: 25, handling: 1.75 },
  ];
  const OPEN_WORLD_PICKUP_SPAWNS = [
    { type: "health", x: 54, z: 54, color: "#ef4444", label: "醫藥箱" },
    { type: "armor", x: -66, z: 66, color: "#38bdf8", label: "防具" },
    { type: "fuel", x: 72, z: -48, color: "#facc15", label: "燃料" },
    { type: "ammo", x: -36, z: -72, color: "#a78bfa", label: "干擾器" },
  ];
  const OPEN_WORLD_MISSIONS = [
    {
      key: "courier",
      label: "快遞路線",
      district: "舊港碼頭",
      reward: 900,
      timeLimit: 150,
      color: "#38bdf8",
      start: { x: -69, z: 54 },
      target: { x: 66, z: -58 },
    },
    {
      key: "race",
      label: "環城競速",
      district: "中央環線",
      reward: 1180,
      timeLimit: 135,
      color: "#f59e0b",
      gates: [
        { x: -70, z: -70 },
        { x: 4, z: -72 },
        { x: 73, z: -46 },
        { x: 70, z: 38 },
        { x: 10, z: 72 },
        { x: -70, z: 58 },
      ],
    },
    {
      key: "rescue",
      label: "城市救援",
      district: "醫療區",
      reward: 980,
      timeLimit: 145,
      color: "#22c55e",
      start: { x: 57, z: 67 },
      target: { x: -56, z: -66 },
    },
    {
      key: "tail",
      label: "追蹤干擾車",
      district: "工業外環",
      reward: 1260,
      timeLimit: 160,
      color: "#ef4444",
      target: { x: 69, z: 5 },
    },
  ];

  const OPEN_WORLD_ASSET_SOURCES = Object.freeze({
    graveyard: {
      name: "Kenney Graveyard Kit",
      url: "https://kenney.nl/assets/graveyard-kit",
      license: "Creative Commons CC0",
      usage: "low-poly street props, fences, lamps and landmark silhouettes rebuilt with local Three.js primitives",
    },
    blaster: {
      name: "Kenney Blaster Kit",
      url: "https://kenney.nl/assets/blaster-kit",
      license: "Creative Commons CC0",
      usage: "pickup crates, ammo packs and gadget silhouettes rebuilt with local Three.js primitives",
    },
    blockyCharacters: {
      name: "Kenney Blocky Characters",
      url: "https://kenney.nl/assets/blocky-characters",
      license: "Creative Commons CC0",
      usage: "blocky player and pedestrian proportions rebuilt with local Three.js primitives",
    },
    platformerTextures: {
      name: "Kenney New Platformer Pack",
      url: "https://kenney.nl/assets/new-platformer-pack",
      license: "Creative Commons CC0",
      usage: "bundled PNG terrain textures for city ground and street props",
    },
    puzzleTextures: {
      name: "Kenney Puzzle Pack 2",
      url: "https://kenney.nl/assets/puzzle-pack-2",
      license: "Creative Commons CC0",
      usage: "bundled PNG tile textures for asphalt-like roads and supply crates",
    },
  });
  const OPEN_WORLD_TEXTURE_ASSETS = Object.freeze({
    grass: "/assets/games/vendor/kenney/new-platformer-pack/tiles/terrain_grass_center.png",
    road: "/assets/games/vendor/kenney/puzzle-pack-2/tiles/black_01.png",
    supply: "/assets/games/vendor/kenney/puzzle-pack-2/tiles/blue_01.png",
  });

  function openWorldRandom(state) {
    return typeof state?.rng === "function" ? state.rng() : Math.random();
  }

  function distance2(x1, z1, x2, z2) {
    const dx = x1 - x2;
    const dz = z1 - z2;
    return dx * dx + dz * dz;
  }

  function distance(x1, z1, x2, z2) {
    return Math.sqrt(distance2(x1, z1, x2, z2));
  }

  function withinDistance2(x1, z1, x2, z2, radius) {
    return distance2(x1, z1, x2, z2) <= radius * radius;
  }

  function angleTo(fromX, fromZ, toX, toZ) {
    return Math.atan2(toX - fromX, toZ - fromZ);
  }

  function normalizeAngle(angle) {
    let next = angle;
    while (next > Math.PI) next -= Math.PI * 2;
    while (next < -Math.PI) next += Math.PI * 2;
    return next;
  }

  function colorFromHex(THREE, value) {
    return new THREE.Color(value || "#ffffff");
  }

  function makeMaterial(THREE, color, roughness = 0.78, metalness = 0.05) {
    return new THREE.MeshStandardMaterial({
      color: colorFromHex(THREE, color),
      roughness,
      metalness,
    });
  }

  function createOpenWorldImageTexture(THREE, src, repeatX = 1, repeatY = 1) {
    if (!THREE.TextureLoader || !THREE.RepeatWrapping) return null;
    const texture = new THREE.TextureLoader().load(src);
    texture.wrapS = THREE.RepeatWrapping;
    texture.wrapT = THREE.RepeatWrapping;
    texture.repeat.set(repeatX, repeatY);
    texture.anisotropy = 4;
    if (THREE.SRGBColorSpace) texture.colorSpace = THREE.SRGBColorSpace;
    return texture;
  }

  function applyOpenWorldMaterialTexture(THREE, material, src, repeatX = 1, repeatY = 1) {
    const texture = createOpenWorldImageTexture(THREE, src, repeatX, repeatY);
    if (!texture || !material) return false;
    material.map = texture;
    material.needsUpdate = true;
    return true;
  }

  function createBox(THREE, width, height, depth, color) {
    const mesh = new THREE.Mesh(
      new THREE.BoxGeometry(width, height, depth),
      makeMaterial(THREE, color),
    );
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    return mesh;
  }

  function createCylinder(THREE, radius, height, color, segments = 18) {
    const mesh = new THREE.Mesh(
      new THREE.CylinderGeometry(radius, radius, height, segments),
      makeMaterial(THREE, color),
    );
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    return mesh;
  }

  function createOpenWorldCanvasTexture(THREE, width, height, painter) {
    if (typeof document === "undefined" || !THREE.CanvasTexture) return null;
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    painter?.(ctx, width, height);
    const texture = new THREE.CanvasTexture(canvas);
    texture.wrapS = THREE.RepeatWrapping;
    texture.wrapT = THREE.RepeatWrapping;
    texture.anisotropy = 4;
    texture.needsUpdate = true;
    if (THREE.SRGBColorSpace) texture.colorSpace = THREE.SRGBColorSpace;
    return texture;
  }

  function createOpenWorldGroundMaterial(THREE) {
    const texture = createOpenWorldCanvasTexture(THREE, 256, 256, (ctx, w, h) => {
      ctx.fillStyle = "#6aa37a";
      ctx.fillRect(0, 0, w, h);
      ctx.fillStyle = "rgba(22,101,52,.18)";
      for (let y = 0; y < h; y += 24) ctx.fillRect(0, y, w, 3);
      ctx.fillStyle = "rgba(226,232,240,.12)";
      for (let i = 0; i < 48; i += 1) {
        const x = (i * 47) % w;
        const y = (i * 31) % h;
        ctx.fillRect(x, y, 2 + (i % 3), 2);
      }
      ctx.fillStyle = "rgba(15,23,42,.08)";
      for (let i = 0; i < 10; i += 1) {
        ctx.beginPath();
        ctx.arc((i * 67) % w, (i * 41) % h, 8 + (i % 4), 0, Math.PI * 2);
        ctx.fill();
      }
    });
    const material = makeMaterial(THREE, "#6aa37a", 0.9, 0);
    if (applyOpenWorldMaterialTexture(THREE, material, OPEN_WORLD_TEXTURE_ASSETS.grass, 34, 34)) return material;
    if (texture) {
      texture.repeat.set(14, 14);
      material.map = texture;
      material.needsUpdate = true;
    }
    return material;
  }

  function createOpenWorldRoadMaterial(THREE, color = "#2f3542") {
    const texture = createOpenWorldCanvasTexture(THREE, 192, 192, (ctx, w, h) => {
      ctx.fillStyle = color;
      ctx.fillRect(0, 0, w, h);
      ctx.fillStyle = "rgba(255,255,255,.035)";
      for (let y = 10; y < h; y += 34) ctx.fillRect(0, y, w, 1);
      ctx.fillStyle = "rgba(15,23,42,.16)";
      for (let i = 0; i < 90; i += 1) {
        ctx.fillRect((i * 37) % w, (i * 53) % h, 1 + (i % 2), 1);
      }
    });
    const material = makeMaterial(THREE, color, 0.94, 0);
    if (texture) {
      texture.repeat.set(6, 14);
      material.map = texture;
      material.needsUpdate = true;
    }
    return material;
  }

  function createOpenWorldSidewalkMaterial(THREE) {
    const texture = createOpenWorldCanvasTexture(THREE, 192, 192, (ctx, w, h) => {
      ctx.fillStyle = "#9aa6b6";
      ctx.fillRect(0, 0, w, h);
      ctx.strokeStyle = "rgba(15,23,42,.18)";
      ctx.lineWidth = 1;
      for (let x = 0; x <= w; x += 24) {
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, h);
        ctx.stroke();
      }
      for (let y = 0; y <= h; y += 24) {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      }
      ctx.fillStyle = "rgba(248,250,252,.12)";
      for (let i = 0; i < 32; i += 1) ctx.fillRect((i * 31) % w, (i * 47) % h, 2, 2);
    });
    const material = makeMaterial(THREE, "#9aa6b6", 0.86, 0);
    if (texture) {
      texture.repeat.set(12, 12);
      material.map = texture;
      material.needsUpdate = true;
    }
    return material;
  }

  function createOpenWorldWaterMaterial(THREE) {
    const texture = createOpenWorldCanvasTexture(THREE, 256, 256, (ctx, w, h) => {
      const gradient = ctx.createLinearGradient(0, 0, w, h);
      gradient.addColorStop(0, "#0f766e");
      gradient.addColorStop(0.52, "#0284c7");
      gradient.addColorStop(1, "#0e7490");
      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, w, h);
      ctx.strokeStyle = "rgba(224,242,254,.24)";
      ctx.lineWidth = 2;
      for (let y = 20; y < h; y += 34) {
        ctx.beginPath();
        for (let x = -20; x <= w + 20; x += 18) {
          const waveY = y + Math.sin((x + y) * 0.04) * 4;
          if (x === -20) ctx.moveTo(x, waveY);
          else ctx.lineTo(x, waveY);
        }
        ctx.stroke();
      }
    });
    const material = makeMaterial(THREE, "#0e7490", 0.66, 0.02);
    if (texture) {
      texture.repeat.set(4, 18);
      material.map = texture;
      material.needsUpdate = true;
    }
    return material;
  }

  function createOpenWorldPlane(THREE, width, depth, material, x, z, y = 0.02) {
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(width, depth), material);
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.set(x, y, z);
    mesh.receiveShadow = true;
    return mesh;
  }

  function createOpenWorldEllipsePlane(THREE, width, depth, material, x, z, y = 0.02) {
    const mesh = new THREE.Mesh(new THREE.CircleGeometry(1, 48), material);
    mesh.rotation.x = -Math.PI / 2;
    mesh.scale.set(width / 2, depth / 2, 1);
    mesh.position.set(x, y, z);
    mesh.receiveShadow = true;
    return mesh;
  }

  function openWorldRoadProfile(coord) {
    const abs = Math.abs(coord);
    if (coord === 0) {
      return { width: 11.6, sidewalk: 18.4, asphalt: "#29313d", median: true, dashLength: 6.2, gap: 6.8 };
    }
    if (abs === 72) {
      return { width: 8.2, sidewalk: 12.6, asphalt: "#252c35", dashLength: 5.2, gap: 8.4 };
    }
    if (abs === 48) {
      return { width: 9.6, sidewalk: 15.2, asphalt: "#303846", dashLength: 5.8, gap: 7.2 };
    }
    return { width: 8.8, sidewalk: 13.8, asphalt: "#333b48", dashLength: 4.4, gap: 8.8 };
  }

  function addOpenWorldLaneDashes(THREE, group, axis, coord, offset, material, dashLength, gap) {
    const start = -WORLD_SIZE / 2 + 7;
    const end = WORLD_SIZE / 2 - 7;
    for (let pos = start; pos <= end; pos += dashLength + gap) {
      const dash = new THREE.Mesh(
        axis === "z"
          ? new THREE.PlaneGeometry(0.28, dashLength)
          : new THREE.PlaneGeometry(dashLength, 0.28),
        material,
      );
      dash.rotation.x = -Math.PI / 2;
      dash.position.set(axis === "z" ? coord + offset : pos, 0.072, axis === "z" ? pos : coord + offset);
      group.add(dash);
    }
  }

  function createOpenWorldStraightRoad(THREE, axis, coord, materials) {
    const profile = openWorldRoadProfile(coord);
    const group = new THREE.Group();
    const length = WORLD_SIZE + 8;
    const roadMaterial = createOpenWorldRoadMaterial(THREE, profile.asphalt);
    const isNorthSouth = axis === "z";
    group.add(createOpenWorldPlane(
      THREE,
      isNorthSouth ? profile.sidewalk : length,
      isNorthSouth ? length : profile.sidewalk,
      materials.sidewalk,
      isNorthSouth ? coord : 0,
      isNorthSouth ? 0 : coord,
      0.018,
    ));
    group.add(createOpenWorldPlane(
      THREE,
      isNorthSouth ? profile.width : length,
      isNorthSouth ? length : profile.width,
      roadMaterial,
      isNorthSouth ? coord : 0,
      isNorthSouth ? 0 : coord,
      0.036,
    ));

    const curbA = createOpenWorldPlane(
      THREE,
      isNorthSouth ? 0.2 : length,
      isNorthSouth ? length : 0.2,
      materials.curb,
      isNorthSouth ? coord - profile.width / 2 : 0,
      isNorthSouth ? 0 : coord - profile.width / 2,
      0.082,
    );
    const curbB = createOpenWorldPlane(
      THREE,
      isNorthSouth ? 0.2 : length,
      isNorthSouth ? length : 0.2,
      materials.curb,
      isNorthSouth ? coord + profile.width / 2 : 0,
      isNorthSouth ? 0 : coord + profile.width / 2,
      0.082,
    );
    group.add(curbA, curbB);

    if (profile.median) {
      const median = createOpenWorldPlane(
        THREE,
        isNorthSouth ? 1.25 : length,
        isNorthSouth ? length : 1.25,
        materials.median,
        isNorthSouth ? coord : 0,
        isNorthSouth ? 0 : coord,
        0.088,
      );
      group.add(median);
      addOpenWorldLaneDashes(THREE, group, axis, coord, -2.35, materials.line, profile.dashLength, profile.gap);
      addOpenWorldLaneDashes(THREE, group, axis, coord, 2.35, materials.line, profile.dashLength, profile.gap);
    } else {
      addOpenWorldLaneDashes(THREE, group, axis, coord, 0, materials.line, profile.dashLength, profile.gap);
    }
    return group;
  }

  function createOpenWorldBuildingMesh(THREE, width, height, depth, color, options = {}) {
    const group = new THREE.Group();
    const body = createBox(THREE, width, height, depth, color);
    body.position.y = height / 2;
    group.add(body);

    const trimColor = options.trim || "#e2e8f0";
    const glassColor = options.glass || "#bae6fd";
    const windowMat = new THREE.MeshStandardMaterial({
      color: colorFromHex(THREE, glassColor),
      emissive: colorFromHex(THREE, options.lit ? "#60a5fa" : "#0f172a"),
      emissiveIntensity: options.lit ? 0.32 : 0.08,
      roughness: 0.38,
      metalness: 0.02,
    });
    const floors = clamp(Math.floor(height / 1.75), 2, 9);
    const frontCols = clamp(Math.floor(width / 1.7), 2, 5);
    const sideCols = clamp(Math.floor(depth / 1.9), 2, 4);
    for (let floor = 0; floor < floors; floor += 1) {
      const y = 1.2 + floor * ((height - 1.6) / floors);
      for (let col = 0; col < frontCols; col += 1) {
        const offset = ((col + 0.5) / frontCols - 0.5) * width * 0.72;
        [-1, 1].forEach((side) => {
          const win = new THREE.Mesh(new THREE.BoxGeometry(0.64, 0.42, 0.06), windowMat);
          win.position.set(offset, y, side * (depth / 2 + 0.035));
          win.castShadow = false;
          group.add(win);
        });
      }
      for (let col = 0; col < sideCols; col += 1) {
        const offset = ((col + 0.5) / sideCols - 0.5) * depth * 0.68;
        [-1, 1].forEach((side) => {
          const win = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.4, 0.58), windowMat);
          win.position.set(side * (width / 2 + 0.035), y, offset);
          win.castShadow = false;
          group.add(win);
        });
      }
    }

    const roof = createBox(THREE, width * 0.78, 0.28, depth * 0.78, "#111827");
    roof.position.y = height + 0.18;
    group.add(roof);
    if (options.sign) {
      const sign = createBox(THREE, Math.min(width * 0.82, 5.8), 0.68, 0.12, options.signColor || "#f59e0b");
      sign.position.set(0, Math.min(height - 0.9, 3.8), depth / 2 + 0.1);
      group.add(sign);
    }
    if (options.waterTower) {
      const tank = createCylinder(THREE, 0.7, 1.25, trimColor, 16);
      tank.position.set(width * 0.22, height + 0.92, depth * -0.16);
      const stand = createBox(THREE, 0.18, 1.1, 0.18, "#334155");
      stand.position.set(width * 0.22, height + 0.42, depth * -0.16);
      group.add(stand, tank);
    }
    return group;
  }

  function createOpenWorldStreetPropMesh(THREE, type, options = {}) {
    const group = new THREE.Group();
    if (type === "streetlight") {
      const pole = createCylinder(THREE, 0.08, 3.2, "#475569", 10);
      pole.position.y = 1.6;
      const arm = createBox(THREE, 1.05, 0.08, 0.08, "#475569");
      arm.position.set(0.42, 3.12, 0);
      const lamp = createBox(THREE, 0.38, 0.18, 0.28, "#fde68a");
      lamp.position.set(0.92, 3.04, 0);
      const glow = new THREE.Mesh(
        new THREE.SphereGeometry(0.42, 12, 8),
        new THREE.MeshStandardMaterial({
          color: 0xfef3c7,
          emissive: 0xf59e0b,
          emissiveIntensity: 0.35,
          transparent: true,
          opacity: 0.34,
        }),
      );
      glow.position.set(0.92, 2.84, 0);
      group.add(pole, arm, lamp, glow);
    } else if (type === "tree") {
      const trunk = createCylinder(THREE, 0.18, 1.25, "#854d0e", 10);
      trunk.position.y = 0.62;
      const crown = new THREE.Mesh(new THREE.ConeGeometry(0.95, 2.1, 8), makeMaterial(THREE, options.color || "#166534", 0.88, 0));
      crown.position.y = 2.05;
      crown.castShadow = true;
      group.add(trunk, crown);
    } else if (type === "barrier") {
      const base = createBox(THREE, 2.2, 0.42, 0.42, "#f97316");
      base.position.y = 0.34;
      const stripeA = createBox(THREE, 0.18, 0.48, 0.48, "#f8fafc");
      stripeA.position.set(-0.46, 0.38, 0);
      const stripeB = createBox(THREE, 0.18, 0.48, 0.48, "#f8fafc");
      stripeB.position.set(0.46, 0.38, 0);
      group.add(base, stripeA, stripeB);
    } else if (type === "bus-stop") {
      const back = createBox(THREE, 2.6, 1.5, 0.14, "#0f172a");
      back.position.y = 0.9;
      const roof = createBox(THREE, 2.9, 0.16, 1.05, "#38bdf8");
      roof.position.set(0, 1.76, 0.36);
      const bench = createBox(THREE, 1.8, 0.24, 0.42, "#f59e0b");
      bench.position.set(0, 0.42, 0.32);
      group.add(back, roof, bench);
    } else if (type === "supply-crate") {
      const crate = createBox(THREE, 1.25, 0.92, 1.25, options.color || "#7c3aed");
      applyOpenWorldMaterialTexture(THREE, crate.material, OPEN_WORLD_TEXTURE_ASSETS.supply, 1, 1);
      crate.position.y = 0.46;
      const lid = createBox(THREE, 1.38, 0.14, 1.38, "#e2e8f0");
      lid.position.y = 0.98;
      group.add(crate, lid);
    } else {
      const plinth = createBox(THREE, 1.1, 0.22, 1.1, "#64748b");
      plinth.position.y = 0.12;
      const stone = createBox(THREE, 0.78, 1.1, 0.22, "#94a3b8");
      stone.position.y = 0.76;
      group.add(plinth, stone);
    }
    group.userData.assetKit = "Kenney CC0 procedural proxy";
    return group;
  }

  function createOpenWorldStreetProps() {
    const props = [];
    OPEN_WORLD_ROADS.forEach((road, index) => {
      OPEN_WORLD_BLOCK_CENTERS.forEach((cell, cellIndex) => {
        if ((index + cellIndex) % 2 === 0) props.push({ type: "streetlight", x: road + 6.4, z: cell, angle: Math.PI / 2 });
        if ((index + cellIndex) % 3 === 0) props.push({ type: "tree", x: cell, z: road - 6.6, angle: 0, color: cellIndex % 2 ? "#14532d" : "#166534" });
      });
    });
    [
      { type: "bus-stop", x: -30, z: 7, angle: Math.PI / 2 },
      { type: "bus-stop", x: 30, z: -31, angle: -Math.PI / 2 },
      { type: "barrier", x: -70, z: 36, angle: 0 },
      { type: "barrier", x: 58, z: -70, angle: Math.PI / 2 },
      { type: "supply-crate", x: -52, z: 54, angle: 0, color: "#7c3aed" },
      { type: "supply-crate", x: 48, z: -52, angle: 0, color: "#059669" },
      { type: "grave-marker", x: -58, z: -54, angle: 0 },
      { type: "grave-marker", x: -64, z: -60, angle: 0.2 },
    ].forEach((prop) => props.push(prop));
    return props.filter((prop) => !pointOnDiagonalRoad(prop.x, prop.z, 1.5));
  }

  function createOpenWorldPickupMesh(THREE, pickup) {
    const type = pickup.type || "ammo";
    const group = new THREE.Group();
    const color = pickup.color || "#a78bfa";
    const crate = createOpenWorldStreetPropMesh(THREE, "supply-crate", { color });
    crate.scale.set(0.78, 0.78, 0.78);
    group.add(crate);
    const badge = createBox(THREE, 0.72, 0.08, 0.72, type === "health" ? "#fecaca" : type === "armor" ? "#bae6fd" : "#fef3c7");
    badge.position.set(0, 0.98, 0);
    group.add(badge);
    if (type === "health") {
      const crossA = createBox(THREE, 0.52, 0.1, 0.12, "#ef4444");
      const crossB = createBox(THREE, 0.12, 0.1, 0.52, "#ef4444");
      crossA.position.y = 1.06;
      crossB.position.y = 1.07;
      group.add(crossA, crossB);
    }
    return group;
  }

  function createCityCarMesh(THREE, color = "#ef4444", options = {}) {
    const group = new THREE.Group();
    const length = options.length || 4.2;
    const width = options.width || 2.25;
    const body = createBox(THREE, width, 0.9, length, color);
    body.position.y = 0.72;
    const cabin = createBox(THREE, width * 0.74, 0.76, length * 0.42, options.cabin || "#dbeafe");
    cabin.position.set(0, 1.34, -0.08);
    cabin.material.transparent = true;
    cabin.material.opacity = 0.72;
    const front = createBox(THREE, width * 0.58, 0.28, 0.16, "#fef3c7");
    front.position.set(0, 0.83, length / 2 + 0.05);
    const rear = createBox(THREE, width * 0.54, 0.24, 0.14, "#fb7185");
    rear.position.set(0, 0.82, -length / 2 - 0.04);
    const hood = createBox(THREE, width * 0.78, 0.08, length * 0.32, "#111827");
    hood.position.set(0, 1.19, length * 0.2);
    hood.material.transparent = true;
    hood.material.opacity = 0.22;
    const bumper = createBox(THREE, width * 0.78, 0.18, 0.22, "#111827");
    bumper.position.set(0, 0.54, length / 2 + 0.14);
    const sideStripe = createBox(THREE, 0.08, 0.2, length * 0.7, options.stripe || "#f8fafc");
    sideStripe.position.set(width / 2 + 0.04, 0.92, -0.08);
    const sideStripeB = createBox(THREE, 0.08, 0.2, length * 0.7, options.stripe || "#f8fafc");
    sideStripeB.position.set(-width / 2 - 0.04, 0.92, -0.08);
    const wheelMat = makeMaterial(THREE, "#111827", 0.92, 0.16);
    [-width * 0.58, width * 0.58].forEach((x) => {
      [-length * 0.35, length * 0.35].forEach((z) => {
        const wheel = new THREE.Mesh(new THREE.CylinderGeometry(0.34, 0.34, 0.3, 14), wheelMat);
        wheel.rotation.z = Math.PI / 2;
        wheel.position.set(x, 0.32, z);
        wheel.castShadow = true;
        group.add(wheel);
      });
    });
    group.add(body, cabin, front, rear, hood, bumper, sideStripe, sideStripeB);
    if (options.lightbar) {
      const bar = createBox(THREE, 0.95, 0.16, 0.28, "#e0f2fe");
      bar.position.set(0, 1.82, 0);
      const red = createBox(THREE, 0.34, 0.18, 0.32, "#ef4444");
      red.position.set(-0.24, 1.92, 0);
      const blue = createBox(THREE, 0.34, 0.18, 0.32, "#2563eb");
      blue.position.set(0.24, 1.92, 0);
      group.add(bar, red, blue);
    }
    return group;
  }

  function createPlayerMesh(THREE) {
    const group = new THREE.Group();
    const groundShadow = new THREE.Mesh(
      new THREE.CircleGeometry(0.72, 24),
      new THREE.MeshBasicMaterial({
        color: 0x000000,
        transparent: true,
        opacity: 0.28,
        depthWrite: false,
      }),
    );
    groundShadow.rotation.x = -Math.PI / 2;
    groundShadow.position.y = 0.026;
    const leftLeg = createBox(THREE, 0.28, 0.84, 0.28, "#1f2937");
    leftLeg.position.set(-0.18, 0.42, 0);
    const rightLeg = createBox(THREE, 0.28, 0.84, 0.28, "#1f2937");
    rightLeg.position.set(0.18, 0.42, 0);
    const torso = createBox(THREE, 0.86, 0.98, 0.42, "#2563eb");
    torso.position.y = 1.28;
    const vest = createBox(THREE, 0.92, 0.58, 0.12, "#0f172a");
    vest.position.set(0, 1.3, 0.28);
    vest.material.transparent = true;
    vest.material.opacity = 0.62;
    const armL = createBox(THREE, 0.22, 0.82, 0.22, "#f8d0a8");
    armL.position.set(-0.62, 1.25, 0.08);
    armL.rotation.z = -0.18;
    const armR = createBox(THREE, 0.22, 0.82, 0.22, "#f8d0a8");
    armR.position.set(0.62, 1.25, 0.08);
    armR.rotation.z = 0.18;
    const head = new THREE.Mesh(
      new THREE.SphereGeometry(0.36, 18, 12),
      makeMaterial(THREE, "#f8d0a8"),
    );
    head.position.y = 2.05;
    const helmet = createBox(THREE, 0.76, 0.22, 0.52, "#111827");
    helmet.position.set(0, 2.28, 0);
    const visor = createBox(THREE, 0.36, 0.08, 0.09, "#0f172a");
    visor.position.set(0, 2.08, 0.33);
    const backpack = createBox(THREE, 0.48, 0.72, 0.22, "#0f766e");
    backpack.position.set(0, 1.34, -0.32);
    group.add(groundShadow, leftLeg, rightLeg, torso, vest, armL, armR, head, helmet, visor, backpack);
    group.userData.walkParts = { leftLeg, rightLeg, armL, armR, torso, head, groundShadow };
    return group;
  }

  function createMissionMarker(THREE) {
    const group = new THREE.Group();
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(2.35, 0.16, 10, 34),
      new THREE.MeshStandardMaterial({
        color: 0x38bdf8,
        emissive: 0x0ea5e9,
        emissiveIntensity: 0.4,
        roughness: 0.48,
      }),
    );
    ring.rotation.x = Math.PI / 2;
    ring.position.y = 0.16;
    const beam = new THREE.Mesh(
      new THREE.CylinderGeometry(0.38, 0.38, 5.8, 18, 1, true),
      new THREE.MeshStandardMaterial({
        color: 0x38bdf8,
        emissive: 0x0ea5e9,
        emissiveIntensity: 0.18,
        transparent: true,
        opacity: 0.28,
      }),
    );
    beam.position.y = 2.9;
    group.add(ring, beam);
    group.userData.ring = ring;
    group.userData.beam = beam;
    return group;
  }

  function createGadgetProjectileMesh(THREE) {
    const mesh = new THREE.Mesh(
      new THREE.SphereGeometry(0.22, 12, 8),
      new THREE.MeshStandardMaterial({
        color: 0xa78bfa,
        emissive: 0x7c3aed,
        emissiveIntensity: 0.8,
        roughness: 0.42,
      }),
    );
    mesh.castShadow = true;
    return mesh;
  }

  function disposeOpenWorldObject(object) {
    object?.geometry?.dispose?.();
    const materials = Array.isArray(object?.material)
      ? object.material
      : object?.material
        ? [object.material]
        : [];
    materials.forEach((material) => material.dispose?.());
    object?.removeFromParent?.();
  }

  function setObjectPose(object, x, z, angle = 0, y = 0) {
    object.position.set(x, y, z);
    object.rotation.y = angle;
    object.rotation.z = 0;
  }

  function animatePlayerWalk(mesh, cycle = 0, intensity = 0) {
    const parts = mesh?.userData?.walkParts;
    if (!parts) return;
    const clamped = clamp(intensity, 0, 1);
    const swing = Math.sin(cycle) * clamped;
    const counter = Math.sin(cycle + Math.PI) * clamped;
    parts.leftLeg.rotation.x = swing * 0.5;
    parts.rightLeg.rotation.x = counter * 0.5;
    parts.armL.rotation.x = counter * 0.42;
    parts.armR.rotation.x = swing * 0.42;
    parts.torso.rotation.x = Math.abs(Math.sin(cycle * 2)) * 0.035 * clamped;
    parts.head.position.y = 2.05 + Math.abs(Math.sin(cycle * 2)) * 0.035 * clamped;
    const shadowScale = 1 + clamped * 0.08 - Math.abs(Math.sin(cycle * 2)) * 0.05 * clamped;
    parts.groundShadow.scale.set(shadowScale, shadowScale, 1);
  }

  function roadSegmentLength(road) {
    return Math.hypot(road.x2 - road.x1, road.z2 - road.z1);
  }

  function roadSegmentAngle(road, dir = 1) {
    const fromX = dir >= 0 ? road.x1 : road.x2;
    const fromZ = dir >= 0 ? road.z1 : road.z2;
    const toX = dir >= 0 ? road.x2 : road.x1;
    const toZ = dir >= 0 ? road.z2 : road.z1;
    return angleTo(fromX, fromZ, toX, toZ);
  }

  function pointOnRoadSegment(road, t, laneOffset = 0) {
    const clampedT = ((t % 1) + 1) % 1;
    const dx = road.x2 - road.x1;
    const dz = road.z2 - road.z1;
    const length = Math.max(1, Math.hypot(dx, dz));
    const nx = -dz / length;
    const nz = dx / length;
    return {
      x: road.x1 + dx * clampedT + nx * laneOffset,
      z: road.z1 + dz * clampedT + nz * laneOffset,
    };
  }

  function distancePointToRoadSegment(x, z, road) {
    const dx = road.x2 - road.x1;
    const dz = road.z2 - road.z1;
    const length2 = dx * dx + dz * dz;
    if (length2 <= 0.001) return distance(x, z, road.x1, road.z1);
    const t = clamp(((x - road.x1) * dx + (z - road.z1) * dz) / length2, 0, 1);
    return distance(x, z, road.x1 + dx * t, road.z1 + dz * t);
  }

  function pointOnDiagonalRoad(x, z, padding = 0) {
    return OPEN_WORLD_DIAGONAL_ROADS.some((road) => (
      distancePointToRoadSegment(x, z, road) <= (road.width || ROAD_WIDTH) * 0.5 + padding
    ));
  }

  function rectTouchesDiagonalCorridor(x, z, w, d, padding = 0) {
    const halfW = w / 2;
    const halfD = d / 2;
    const probes = [
      { x, z },
      { x: x - halfW, z: z - halfD },
      { x: x + halfW, z: z - halfD },
      { x: x - halfW, z: z + halfD },
      { x: x + halfW, z: z + halfD },
      { x: x - halfW, z },
      { x: x + halfW, z },
      { x, z: z - halfD },
      { x, z: z + halfD },
    ];
    return probes.some((point) => pointOnDiagonalRoad(point.x, point.z, padding));
  }

  function isOnRoad(value) {
    return OPEN_WORLD_ROADS.some((road) => Math.abs(value - road) <= ROAD_WIDTH * 0.56);
  }

  function circleHitsRect(x, z, radius, rect) {
    const halfW = rect.w / 2;
    const halfD = rect.d / 2;
    const closestX = clamp(x, rect.x - halfW, rect.x + halfW);
    const closestZ = clamp(z, rect.z - halfD, rect.z + halfD);
    return distance2(x, z, closestX, closestZ) < radius * radius;
  }

  function openWorldReservedPoints() {
    const missionPoints = OPEN_WORLD_MISSIONS.flatMap((mission) => {
      if (mission.gates) return mission.gates;
      return [mission.start, mission.target].filter(Boolean);
    });
    return [
      { x: OPEN_WORLD_PLAYER_START.x, z: OPEN_WORLD_PLAYER_START.z, radius: 7 },
      ...missionPoints.map((point) => ({ x: point.x, z: point.z, radius: 7 })),
      ...OPEN_WORLD_VEHICLE_SPAWNS.map((point) => ({ x: point.x, z: point.z, radius: 6 })),
      ...OPEN_WORLD_PICKUP_SPAWNS.map((point) => ({ x: point.x, z: point.z, radius: 5 })),
    ];
  }

  function blocksReservedPoint(x, z, w, d) {
    return openWorldReservedPoints().some((point) => (
      circleHitsRect(point.x, point.z, point.radius, { x, z, w, d })
    ));
  }

  function createOpenWorldRoadSegment(THREE, road, roadMaterial, lineMaterial) {
    const group = new THREE.Group();
    const length = roadSegmentLength(road);
    const angle = Math.atan2(road.z2 - road.z1, road.x2 - road.x1);
    const roadMesh = new THREE.Mesh(new THREE.PlaneGeometry(length, road.width || ROAD_WIDTH), roadMaterial);
    roadMesh.rotation.x = -Math.PI / 2;
    roadMesh.rotation.z = angle;
    roadMesh.position.y = 0.035;
    roadMesh.receiveShadow = true;
    const line = new THREE.Mesh(new THREE.PlaneGeometry(length, 0.32), lineMaterial);
    line.rotation.x = -Math.PI / 2;
    line.rotation.z = angle;
    line.position.y = 0.055;
    group.add(roadMesh);
    if (!road.noCenterLine) group.add(line);
    group.position.set((road.x1 + road.x2) / 2, 0, (road.z1 + road.z2) / 2);
    return group;
  }

  function addOpenWorldCrosswalks(THREE, scene, material) {
    const majorRoads = OPEN_WORLD_ROADS.filter((coord) => coord === 0 || Math.abs(coord) === 48);
    majorRoads.forEach((x) => {
      majorRoads.forEach((z) => {
        [-1, 1].forEach((side) => {
          for (let i = -2; i <= 2; i += 1) {
            const northSouthStripe = new THREE.Mesh(new THREE.PlaneGeometry(ROAD_WIDTH * 0.82, 0.34), material);
            northSouthStripe.rotation.x = -Math.PI / 2;
            northSouthStripe.position.set(x, 0.096, z + side * (ROAD_WIDTH * 0.72) + i * 0.78);
            scene.add(northSouthStripe);
            const eastWestStripe = new THREE.Mesh(new THREE.PlaneGeometry(0.34, ROAD_WIDTH * 0.82), material);
            eastWestStripe.rotation.x = -Math.PI / 2;
            eastWestStripe.position.set(x + side * (ROAD_WIDTH * 0.72) + i * 0.78, 0.098, z);
            scene.add(eastWestStripe);
          }
        });
      });
    });
  }

  function addOpenWorldSmallCollider(state, x, z, w, d) {
    if (blocksReservedPoint(x, z, w, d)) return false;
    if (rectTouchesDiagonalCorridor(x, z, w, d, 1.1)) return false;
    state.colliders.push({ x, z, w, d });
    return true;
  }

  function addOpenWorldAmbientDistricts(THREE, scene, state) {
    const waterMaterial = createOpenWorldWaterMaterial(THREE);
    const boardwalkMaterial = makeMaterial(THREE, "#8b6f47", 0.88, 0);
    const plazaMaterial = makeMaterial(THREE, "#a7b4c4", 0.82, 0);
    const parkMaterial = makeMaterial(THREE, "#3f8f57", 0.92, 0);
    const hospitalMaterial = makeMaterial(THREE, "#dbeafe", 0.78, 0);
    const industrialMaterial = makeMaterial(THREE, "#64748b", 0.86, 0.02);
    const marketMaterial = makeMaterial(THREE, "#b7791f", 0.82, 0);
    const oldtownMaterial = makeMaterial(THREE, "#75a7b8", 0.86, 0);
    const zoneMaterials = {
      harbor: waterMaterial,
      park: parkMaterial,
      hospital: hospitalMaterial,
      industrial: industrialMaterial,
      market: marketMaterial,
      oldtown: oldtownMaterial,
    };
    OPEN_WORLD_SCENIC_ZONES.forEach((zone) => {
      scene.add(createOpenWorldPlane(THREE, zone.w, zone.d, zoneMaterials[zone.key] || plazaMaterial, zone.x, zone.z, zone.key === "harbor" ? 0.012 : 0.022));
    });
    scene.add(createOpenWorldPlane(THREE, 5.4, 158, boardwalkMaterial, -80.5, -8, 0.034));

    [-58, -34, -10, 16, 42, 66].forEach((z, index) => {
      const dock = createBox(THREE, 8.5, 0.32, 2.1, index % 2 ? "#a16207" : "#92400e");
      dock.position.set(-79.5, 0.16, z);
      scene.add(dock);
      addOpenWorldSmallCollider(state, -79.5, z, 8.7, 2.3);
    });
    [-62, -55, -48].forEach((z, index) => {
      const container = createBox(THREE, 2.4, 1.55, 6.2, ["#2563eb", "#dc2626", "#f59e0b"][index]);
      container.position.set(39 + index * 4.2, 0.78, z);
      container.rotation.y = index % 2 ? 0.08 : -0.05;
      scene.add(container);
      addOpenWorldSmallCollider(state, 39 + index * 4.2, z, 2.8, 6.6);
    });
    [50, 58, 66].forEach((x, index) => {
      const container = createBox(THREE, 6.4, 1.35, 2.3, ["#0f766e", "#7c3aed", "#ea580c"][index]);
      container.position.set(x, 0.68, -50 + index * 4.8);
      scene.add(container);
      addOpenWorldSmallCollider(state, x, -50 + index * 4.8, 6.8, 2.7);
    });

    const pond = new THREE.Mesh(
      new THREE.CircleGeometry(5.2, 36),
      createOpenWorldWaterMaterial(THREE),
    );
    pond.rotation.x = -Math.PI / 2;
    pond.scale.set(1.35, 0.72, 1);
    pond.position.set(55, 0.094, 55);
    pond.receiveShadow = true;
    scene.add(pond);
    const civicStatue = { x: 13, z: 13 };
    const statueBase = createCylinder(THREE, 1.25, 0.55, "#475569", 18);
    statueBase.position.set(civicStatue.x, 0.28, civicStatue.z);
    const statue = createCylinder(THREE, 0.48, 2.4, "#94a3b8", 14);
    statue.position.set(civicStatue.x, 1.48, civicStatue.z);
    scene.add(statueBase, statue);
    addOpenWorldSmallCollider(state, civicStatue.x, civicStatue.z, 2.8, 2.8);

    [
      { x: 49, z: 45 }, { x: 62, z: 45 }, { x: 68, z: 56 }, { x: 45, z: 63 },
      { x: -45, z: 42 }, { x: -24, z: 52 }, { x: -66, z: -51 }, { x: -51, z: -70 },
    ].forEach((tree, index) => {
      const mesh = createOpenWorldStreetPropMesh(THREE, "tree", { color: index % 2 ? "#14532d" : "#15803d" });
      mesh.position.set(tree.x, 0, tree.z);
      scene.add(mesh);
    });

    const hospitalPad = createOpenWorldPlane(THREE, 9, 9, makeMaterial(THREE, "#f8fafc", 0.75, 0), 58, 68, 0.105);
    scene.add(hospitalPad);
    const hA = createBox(THREE, 1.1, 0.08, 5.6, "#ef4444");
    const hB = createBox(THREE, 5.6, 0.08, 1.1, "#ef4444");
    hA.position.set(58, 0.18, 68);
    hB.position.set(58, 0.19, 68);
    scene.add(hA, hB);

    [-41, -35, -29, -23].forEach((x, index) => {
      const stall = createBox(THREE, 3.4, 1.25, 2.4, ["#ef4444", "#22c55e", "#3b82f6", "#f59e0b"][index]);
      stall.position.set(x, 0.62, 45 + (index % 2) * 3.3);
      scene.add(stall);
      addOpenWorldSmallCollider(state, x, 45 + (index % 2) * 3.3, 3.8, 2.8);
    });
  }

  function openWorldBlocked(state, x, z, radius) {
    const edge = WORLD_SIZE / 2 - radius - 1.2;
    if (x < -edge || x > edge || z < -edge || z > edge) return true;
    return state.colliders.some((rect) => circleHitsRect(x, z, radius + 0.2, rect));
  }

  function addOpenWorldHeat(state, amount, reason = "") {
    state.heat = clamp(Number(state.heat || 0) + Number(amount || 0), 0, 100);
    state.lastHeatReason = reason || state.lastHeatReason || "";
    state.lastHeatAt = performance.now();
  }

  function formatOpenWorldTime(ms) {
    const total = Math.max(0, Math.ceil(Number(ms || 0) / 1000));
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  function activeMission(state) {
    return OPEN_WORLD_MISSIONS[state.missionIndex % OPEN_WORLD_MISSIONS.length] || OPEN_WORLD_MISSIONS[0];
  }

  function currentMissionTarget(state) {
    const mission = activeMission(state);
    const missionState = state.missionState || {};
    if (mission.key === "race") {
      return mission.gates?.[missionState.gateIndex || 0] || mission.gates?.[0] || { x: 0, z: 0 };
    }
    if (mission.key === "tail") return missionState.target || mission.target || { x: 0, z: 0 };
    if (missionState.stage === "dropoff") return mission.target || { x: 0, z: 0 };
    return mission.start || mission.target || { x: 0, z: 0 };
  }

  function chooseTailWaypoint(state) {
    const roadX = OPEN_WORLD_ROADS[Math.floor(openWorldRandom(state) * OPEN_WORLD_ROADS.length)] || 0;
    const roadZ = OPEN_WORLD_ROADS[Math.floor(openWorldRandom(state) * OPEN_WORLD_ROADS.length)] || 0;
    return {
      x: clamp(roadX + (openWorldRandom(state) - 0.5) * 6, -76, 76),
      z: clamp(roadZ + (openWorldRandom(state) - 0.5) * 6, -76, 76),
    };
  }

  function updateMissionMarker(state) {
    if (!state.missionMarker) return;
    const mission = activeMission(state);
    const target = currentMissionTarget(state);
    setObjectPose(state.missionMarker, target.x, target.z, 0, 0.08);
    const color = colorFromHex(state.THREE, mission.color);
    state.missionMarker.userData.ring.material.color.copy(color);
    state.missionMarker.userData.ring.material.emissive.copy(color);
    state.missionMarker.userData.beam.material.color.copy(color);
    state.missionMarker.userData.beam.material.emissive.copy(color);
  }

  function prepareOpenWorldMission(state) {
    const mission = activeMission(state);
    state.missionState = {
      stage: mission.key === "race" ? "race" : mission.key === "tail" ? "tail" : "pickup",
      gateIndex: 0,
      tailSeconds: 0,
      tailWaypoint: null,
      startedAt: Date.now(),
      expiresAt: Date.now() + Number(mission.timeLimit || 150) * 1000,
      target: mission.target ? { ...mission.target } : null,
    };
    if (mission.key === "tail") {
      if (!state.tailTarget) {
        const mesh = createCityCarMesh(state.THREE, "#7f1d1d", { cabin: "#fecaca", length: 4.7, width: 2.35 });
        state.scene.add(mesh);
        state.tailTarget = {
          x: mission.target.x,
          z: mission.target.z,
          angle: -Math.PI / 2,
          speed: 13,
          mesh,
        };
      }
      state.tailTarget.x = mission.target.x;
      state.tailTarget.z = mission.target.z;
      state.tailTarget.angle = -Math.PI / 2;
      state.tailTarget.mesh.visible = true;
      state.missionState.target = { x: state.tailTarget.x, z: state.tailTarget.z };
      state.missionState.tailWaypoint = chooseTailWaypoint(state);
    } else if (state.tailTarget) {
      state.tailTarget.mesh.visible = false;
    }
    updateMissionMarker(state);
  }

  function openWorldMissionProgressText(state) {
    const mission = activeMission(state);
    const missionState = state.missionState || {};
    if (mission.key === "courier") return missionState.stage === "dropoff" ? "送往目的地" : "領取包裹";
    if (mission.key === "rescue") return missionState.stage === "dropoff" ? "送往醫療區" : "接應求助者";
    if (mission.key === "race") return `檢查點 ${(missionState.gateIndex || 0) + 1}/${mission.gates.length}`;
    if (mission.key === "tail") return `保持跟車 ${Math.floor(missionState.tailSeconds || 0)}/10 秒`;
    return "探索城市";
  }

  function completeOpenWorldMission(api, state) {
    const mission = activeMission(state);
    const remaining = Math.max(0, Number(state.missionState?.expiresAt || Date.now()) - Date.now());
    const timeBonus = Math.round(remaining / 100);
    const heatBonus = state.heat < 20 ? 180 : state.heat < 45 ? 80 : 0;
    state.score += mission.reward + timeBonus + heatBonus;
    state.cash += Math.round((mission.reward + timeBonus) / 12);
    state.missionsCompleted += 1;
    state.combo += 1;
    state.missionFinishedAt = Date.now();
    api.achievement?.(`mission-${mission.key}`, `${mission.label}完成`, `完成 ${mission.district} 的開放世界任務。`);
    if (state.missionsCompleted >= 3) api.achievement?.("city-runner", "城市跑者", "同一局完成三個城市任務。");
    if (state.heat >= 60) api.achievement?.("high-heat-clear", "高警戒收工", "在高警戒狀態完成任務。");
    state.missionIndex = (state.missionIndex + 1) % OPEN_WORLD_MISSIONS.length;
    prepareOpenWorldMission(state);
    api.status(`任務完成：${mission.label} · 分數 ${Number(state.score).toLocaleString()}`);
  }

  function cycleOpenWorldMission(api, state) {
    if (!state) return;
    if (state.status === "active") {
      state.score = Math.max(0, state.score - 120);
      addOpenWorldHeat(state, 5, "任務臨時改派");
    }
    state.missionIndex = (state.missionIndex + 1) % OPEN_WORLD_MISSIONS.length;
    prepareOpenWorldMission(state);
    api.status(`已切換任務：${activeMission(state).label}`);
  }

  function buildOpenWorldCity(api, state) {
    const THREE = state.THREE;
    const stage = state.stage;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x8ec5d6);
    scene.fog = new THREE.Fog(0x8ec5d6, 78, 210);
    const camera = new THREE.PerspectiveCamera(58, 16 / 9, 0.1, 420);
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    renderer.shadowMap.enabled = true;
    renderer.domElement.className = "open-world-canvas";
    stage.appendChild(renderer.domElement);
    state.scene = scene;
    state.camera = camera;
    state.renderer = renderer;

    const hemi = new THREE.HemisphereLight(0xf8fafc, 0x475569, 1.25);
    scene.add(hemi);
    const sun = new THREE.DirectionalLight(0xffffff, 1.8);
    sun.position.set(42, 90, 30);
    sun.castShadow = true;
    sun.shadow.camera.near = 1;
    sun.shadow.camera.far = 220;
    sun.shadow.camera.left = -95;
    sun.shadow.camera.right = 95;
    sun.shadow.camera.top = 95;
    sun.shadow.camera.bottom = -95;
    scene.add(sun);

    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(WORLD_SIZE + 18, WORLD_SIZE + 18),
      createOpenWorldGroundMaterial(THREE),
    );
    ground.rotation.x = -Math.PI / 2;
    ground.receiveShadow = true;
    scene.add(ground);
    addOpenWorldAmbientDistricts(THREE, scene, state);

    const lineMaterial = makeMaterial(THREE, "#f8fafc", 0.55, 0);
    const roadMaterials = {
      sidewalk: createOpenWorldSidewalkMaterial(THREE),
      curb: makeMaterial(THREE, "#d9e1ea", 0.72, 0),
      median: makeMaterial(THREE, "#2f7d4e", 0.9, 0),
      line: lineMaterial,
    };
    OPEN_WORLD_ROADS.forEach((x) => {
      scene.add(createOpenWorldStraightRoad(THREE, "z", x, roadMaterials));
    });
    OPEN_WORLD_ROADS.forEach((z) => {
      scene.add(createOpenWorldStraightRoad(THREE, "x", z, roadMaterials));
    });
    OPEN_WORLD_DIAGONAL_ROADS.forEach((road) => {
      const segment = createOpenWorldRoadSegment(
        THREE,
        road,
        makeMaterial(THREE, road.color || "#343a46", 0.92, 0),
        lineMaterial,
      );
      scene.add(segment);
    });
    OPEN_WORLD_ALLEY_ROADS.forEach((road) => {
      const segment = createOpenWorldRoadSegment(
        THREE,
        road,
        makeMaterial(THREE, road.color || "#475569", 0.9, 0),
        lineMaterial,
      );
      scene.add(segment);
    });
    addOpenWorldCrosswalks(THREE, scene, lineMaterial);

    const districtPads = [
      { x: 13, z: 13, w: 14, d: 10, color: "#a7b4c4", angle: 0.18 },
      { x: 55, z: 55, w: 22, d: 16, color: "#5fa76b", angle: -0.1 },
      { x: -59, z: -61, w: 20, d: 15, color: "#8fc6d6", angle: 0.22 },
      { x: -33, z: 45, w: 18, d: 11, color: "#b7791f", angle: -0.16 },
      { x: 48, z: -58, w: 24, d: 16, color: "#7a8798", angle: 0.08 },
      { x: -80, z: -8, w: 5, d: 154, color: "#8b6f47", angle: 0 },
    ];
    districtPads.forEach((pad) => {
      const mesh = createOpenWorldEllipsePlane(THREE, pad.w, pad.d, makeMaterial(THREE, pad.color, 0.82, 0), pad.x, pad.z, 0.029);
      mesh.rotation.z = pad.angle || 0;
      scene.add(mesh);
    });

    const buildingColors = ["#b45309", "#475569", "#9ca3af", "#facc15", "#64748b", "#c084fc"];
    OPEN_WORLD_BLOCK_CENTERS.forEach((x) => {
      OPEN_WORLD_BLOCK_CENTERS.forEach((z) => {
        if (Math.abs(x) < 16 && Math.abs(z) < 16) return;
        if (x > 42 && z > 42) return;
        if (x > 30 && z < -42) return;
        if (x < -46 && z < -48) return;
        const h = 4.5 + ((Math.abs(x * 13 + z * 7) % 10) * 1.15);
        const w = 7 + (Math.abs(x + z) % 3);
        const d = 7 + (Math.abs(x - z) % 4);
        if (rectTouchesDiagonalCorridor(x, z, w + 2.4, d + 2.4, 3.2)) return;
        if ((Math.abs(x * 17 - z * 11) % 13) === 0) return;
        const collider = { x, z, w: w + 1.2, d: d + 1.2 };
        if (blocksReservedPoint(collider.x, collider.z, collider.w, collider.d)) return;
        const colorIndex = Math.abs(Math.floor((x + 91) * 3 + z)) % buildingColors.length;
        const color = buildingColors[colorIndex];
        const building = createOpenWorldBuildingMesh(THREE, w, h, d, color, {
          glass: colorIndex % 2 ? "#bfdbfe" : "#fde68a",
          lit: (Math.abs(x * 5 + z * 9) % 3) === 0,
          sign: (Math.abs(x + z) % 5) === 0,
          signColor: colorIndex % 2 ? "#38bdf8" : "#f97316",
          waterTower: (Math.abs(x * 11 - z * 7) % 6) === 0,
        });
        building.position.set(x, 0, z);
        scene.add(building);
        state.colliders.push(collider);
      });
    });

    createOpenWorldStreetProps().forEach((prop) => {
      if (blocksReservedPoint(prop.x, prop.z, 2.4, 2.4)) return;
      const mesh = createOpenWorldStreetPropMesh(THREE, prop.type, prop);
      mesh.position.set(prop.x, 0, prop.z);
      mesh.rotation.y = prop.angle || 0;
      scene.add(mesh);
      if (prop.type === "barrier" || prop.type === "bus-stop" || prop.type === "supply-crate") {
        state.colliders.push({ x: prop.x, z: prop.z, w: prop.type === "bus-stop" ? 3 : 2.4, d: prop.type === "bus-stop" ? 1.4 : 1.4 });
      }
    });

    const landmarkTower = createOpenWorldBuildingMesh(THREE, 8, 24, 8, "#0f766e", {
      glass: "#99f6e4",
      lit: true,
      sign: true,
      signColor: "#14b8a6",
      waterTower: true,
    });
    landmarkTower.position.set(-18, 0, 18);
    scene.add(landmarkTower);
    state.colliders.push({ x: -18, z: 18, w: 9, d: 9 });

    const playerMesh = createPlayerMesh(THREE);
    scene.add(playerMesh);
    state.player.mesh = playerMesh;

    state.vehicles = OPEN_WORLD_VEHICLE_SPAWNS.map((vehicle) => {
      const mesh = createCityCarMesh(THREE, vehicle.color, { cabin: vehicle.type === "taxi" ? "#fef9c3" : "#dbeafe" });
      setObjectPose(mesh, vehicle.x, vehicle.z, vehicle.angle);
      scene.add(mesh);
      return { ...vehicle, mesh, speed: 0, occupied: false };
    });

    state.traffic = [];
    for (let i = 0; i < 16; i += 1) {
      const axis = i % 2 ? "x" : "z";
      const road = OPEN_WORLD_ROADS[(i * 3) % OPEN_WORLD_ROADS.length];
      const lane = (i % 4 < 2 ? -1 : 1) * 1.85;
      const mesh = createCityCarMesh(THREE, i % 3 ? "#0ea5e9" : "#f43f5e", { cabin: "#e0f2fe" });
      const row = {
        axis,
        road,
        lane,
        dir: i % 4 < 2 ? 1 : -1,
        speed: 8.5 + (i % 5) * 1.2,
        x: axis === "x" ? -76 + ((i * 19) % 152) : road + lane,
        z: axis === "z" ? -76 + ((i * 23) % 152) : road + lane,
        mesh,
      };
      row.angle = axis === "x" ? Math.PI / 2 * row.dir : row.dir > 0 ? 0 : Math.PI;
      setObjectPose(mesh, row.x, row.z, row.angle);
      scene.add(mesh);
      state.traffic.push(row);
    }
    OPEN_WORLD_DIAGONAL_ROADS.forEach((route, routeIndex) => {
      [-1, 1].forEach((laneSign, laneIndex) => {
        const mesh = createCityCarMesh(THREE, laneIndex ? "#14b8a6" : "#f97316", { cabin: "#e0f2fe" });
        const dir = laneIndex ? 1 : -1;
        const t = (routeIndex * 0.31 + laneIndex * 0.47 + 0.18) % 1;
        const row = {
          route,
          t,
          dir,
          lane: laneSign * 1.75,
          speed: 9.4 + routeIndex * 1.1,
          mesh,
        };
        const point = pointOnRoadSegment(route, row.t, row.lane);
        row.x = point.x;
        row.z = point.z;
        row.angle = roadSegmentAngle(route, row.dir);
        setObjectPose(mesh, row.x, row.z, row.angle);
        scene.add(mesh);
        state.traffic.push(row);
      });
    });

    state.patrols = [];
    for (let i = 0; i < 4; i += 1) {
      const mesh = createCityCarMesh(THREE, "#f8fafc", { cabin: "#bfdbfe", lightbar: true, length: 4.6 });
      const patrol = {
        x: [-72, 72, -72, 72][i],
        z: [-72, -72, 72, 72][i],
        angle: i < 2 ? 0 : Math.PI,
        speed: 0,
        mesh,
        stunnedUntil: 0,
      };
      setObjectPose(mesh, patrol.x, patrol.z, patrol.angle);
      scene.add(mesh);
      state.patrols.push(patrol);
    }

    state.pickups = OPEN_WORLD_PICKUP_SPAWNS.map((pickup) => {
      const mesh = createOpenWorldPickupMesh(THREE, pickup);
      mesh.position.set(pickup.x, 0, pickup.z);
      scene.add(mesh);
      return { ...pickup, mesh, active: true, spin: openWorldRandom(state) * Math.PI * 2 };
    });

    state.missionMarker = createMissionMarker(THREE);
    scene.add(state.missionMarker);
    prepareOpenWorldMission(state);

    resizeOpenWorldRenderer(state);
    updateOpenWorldCamera(state);
    renderer.render(scene, camera);
  }

  function resizeOpenWorldRenderer(state) {
    if (!state?.renderer || !state?.stage || !state?.camera) return;
    const rect = state.stage.getBoundingClientRect();
    const width = Math.max(320, Math.floor(rect.width || 760));
    const height = Math.max(240, Math.floor(rect.height || width * 0.56));
    state.renderer.setSize(width, height, false);
    state.camera.aspect = width / height;
    state.camera.updateProjectionMatrix();
  }

  function updateOpenWorldCamera(state) {
    const player = state.player;
    const speedRatio = Math.min(1, Math.abs(player.speed || 0) / 35);
    const chase = player.inVehicle ? 16 + speedRatio * 7 : 11;
    const height = player.inVehicle ? 8.2 + speedRatio * 1.6 : 6.2;
    const lookY = player.inVehicle ? 1.2 : 1.6;
    const cameraX = player.x - Math.sin(player.angle) * chase;
    const cameraZ = player.z - Math.cos(player.angle) * chase;
    if (!state.cameraReady) {
      state.camera.position.set(cameraX, height, cameraZ);
      state.cameraReady = true;
    } else {
      state.camera.position.lerp(new state.THREE.Vector3(cameraX, height, cameraZ), 0.18);
    }
    state.camera.lookAt(player.x, lookY, player.z);
  }

  function setOpenWorldInput(state, name, pressed) {
    if (!state) return;
    state.keys[name] = Boolean(pressed);
  }

  function nearestOpenWorldVehicle(state) {
    if (state.player.inVehicle) return state.player.inVehicle;
    let best = null;
    let bestD = Infinity;
    state.vehicles.forEach((vehicle) => {
      if (vehicle.occupied) return;
      const d = distance2(state.player.x, state.player.z, vehicle.x, vehicle.z);
      if (d < bestD) {
        bestD = d;
        best = vehicle;
      }
    });
    return bestD <= ENTER_VEHICLE_DISTANCE * ENTER_VEHICLE_DISTANCE ? best : null;
  }

  function toggleOpenWorldVehicle(api, state) {
    if (!state || state.status !== "active") {
      api.status("開始後才能上車。");
      return;
    }
    const player = state.player;
    if (player.inVehicle) {
      const vehicle = player.inVehicle;
      const exitX = vehicle.x - Math.cos(vehicle.angle) * 2.7;
      const exitZ = vehicle.z + Math.sin(vehicle.angle) * 2.7;
      if (openWorldBlocked(state, exitX, exitZ, PLAYER_RADIUS)) {
        api.status("旁邊沒有安全下車空間。");
        return;
      }
      vehicle.occupied = false;
      vehicle.speed = player.speed * 0.2;
      player.inVehicle = null;
      player.x = exitX;
      player.z = exitZ;
      player.speed = 0;
      player.mesh.visible = true;
      setObjectPose(player.mesh, player.x, player.z, player.angle);
      api.achievement?.("first-exit", "街頭切換", "完成一次上下車切換。");
      return;
    }
    const vehicle = nearestOpenWorldVehicle(state);
    if (!vehicle) {
      api.status("附近沒有可駕駛車輛。");
      return;
    }
    vehicle.occupied = true;
    player.inVehicle = vehicle;
    player.x = vehicle.x;
    player.z = vehicle.z;
    player.angle = vehicle.angle;
    player.speed = vehicle.speed || 0;
    player.mesh.visible = false;
    api.achievement?.("first-drive", "城市駕駛", "第一次進入車輛。");
  }

  function fireOpenWorldGadget(api, state) {
    if (!state || state.status !== "active" || state.gadgetAmmo <= 0 || state.fireCooldown > 0) return;
    state.gadgetAmmo -= 1;
    state.fireCooldown = 0.42;
    const mesh = createGadgetProjectileMesh(state.THREE);
    state.scene.add(mesh);
    state.projectiles.push({
      x: state.player.x + Math.sin(state.player.angle) * 2,
      z: state.player.z + Math.cos(state.player.angle) * 2,
      angle: state.player.angle,
      life: 0.9,
      speed: 42,
      mesh,
    });
    addOpenWorldHeat(state, 3, "市區使用干擾器");
    if (api.status) api.status("干擾器已發射。");
  }

  function updateOpenWorldMovement(state, dt) {
    const player = state.player;
    const turn = (state.keys.left ? 1 : 0) - (state.keys.right ? 1 : 0);
    const throttle = (state.keys.up ? 1 : 0) - (state.keys.down ? 1 : 0);
    const sprint = Boolean(state.keys.sprint);
    if (player.inVehicle) {
      const vehicle = player.inVehicle;
      const maxSpeed = vehicle.maxSpeed || 30;
      const accel = vehicle.accel || 22;
      const handling = vehicle.handling || 1.7;
      player.speed += throttle * accel * dt;
      if (!throttle) player.speed *= Math.pow(0.12, dt);
      if (sprint && throttle > 0) {
        player.speed += accel * 0.55 * dt;
        state.fuel = clamp(state.fuel - dt * 3.2, 0, 100);
      }
      if (state.fuel <= 0 && player.speed > maxSpeed * 0.5) player.speed = maxSpeed * 0.5;
      player.speed = clamp(player.speed, -maxSpeed * 0.48, maxSpeed * (sprint && state.fuel > 0 ? 1.18 : 1));
      const turnScale = (Math.abs(player.speed) / maxSpeed) * 0.72 + 0.18;
      player.angle += turn * handling * turnScale * dt * (player.speed < -0.4 ? -1 : 1);
      const nextX = player.x + Math.sin(player.angle) * player.speed * dt;
      const nextZ = player.z + Math.cos(player.angle) * player.speed * dt;
      if (openWorldBlocked(state, nextX, nextZ, VEHICLE_RADIUS)) {
        player.speed *= -0.24;
        state.health = clamp(state.health - 5, 0, 100);
        addOpenWorldHeat(state, 7, "碰撞事故");
        state.score = Math.max(0, state.score - 45);
      } else {
        player.x = nextX;
        player.z = nextZ;
        state.distanceDriven += Math.abs(player.speed) * dt;
      }
      vehicle.x = player.x;
      vehicle.z = player.z;
      vehicle.angle = player.angle;
      vehicle.speed = player.speed;
      setObjectPose(vehicle.mesh, vehicle.x, vehicle.z, vehicle.angle);
      return;
    }
    const walkSpeed = sprint ? 8.6 : 5.2;
    const targetSpeed = throttle * walkSpeed;
    const accelRate = throttle ? 9.5 : 13.5;
    player.speed += (targetSpeed - player.speed) * Math.min(1, dt * accelRate);
    if (!throttle && Math.abs(player.speed) < 0.04) player.speed = 0;
    const turnIntent = turn * (Math.abs(player.speed) > 0.25 ? 1 : 0.52);
    player.angle += turnIntent * 2.15 * dt * (player.speed < -0.2 ? -0.72 : 1);
    const nextX = player.x + Math.sin(player.angle) * player.speed * dt;
    const nextZ = player.z + Math.cos(player.angle) * player.speed * dt;
    if (!openWorldBlocked(state, nextX, nextZ, PLAYER_RADIUS)) {
      player.x = nextX;
      player.z = nextZ;
      state.distanceWalked += Math.abs(player.speed) * dt;
    } else {
      player.speed *= -0.18;
    }
    const walkRatio = Math.min(1, Math.abs(player.speed) / walkSpeed);
    player.walkCycle = Number(player.walkCycle || 0) + Math.abs(player.speed) * dt * (sprint ? 3.1 : 2.45);
    const bob = Math.abs(Math.sin(player.walkCycle * 2)) * 0.045 * walkRatio;
    const lean = -turn * 0.1 * walkRatio;
    setObjectPose(player.mesh, player.x, player.z, player.angle, bob);
    player.mesh.rotation.z = lean;
    animatePlayerWalk(player.mesh, player.walkCycle, walkRatio);
  }

  function syncOpenWorldPlayerPose(state) {
    const player = state.player;
    if (player.inVehicle) {
      const vehicle = player.inVehicle;
      vehicle.x = player.x;
      vehicle.z = player.z;
      vehicle.angle = player.angle;
      vehicle.speed = player.speed;
      setObjectPose(vehicle.mesh, vehicle.x, vehicle.z, vehicle.angle);
    } else {
      setObjectPose(player.mesh, player.x, player.z, player.angle);
    }
  }

  function pushOpenWorldPlayerAway(state, fromX, fromZ, radius, impulse) {
    const player = state.player;
    const dx = player.x - fromX;
    const dz = player.z - fromZ;
    const length = Math.max(0.001, Math.hypot(dx, dz));
    let nx = dx / length;
    let nz = dz / length;
    if (length < 0.08) {
      nx = Math.sin(player.angle);
      nz = Math.cos(player.angle);
    }
    const overlap = Math.max(0, radius - length);
    const push = Math.min(player.inVehicle ? 2.4 : 1.65, overlap + impulse);
    const attempts = [
      { x: player.x + nx * push, z: player.z + nz * push },
      { x: player.x + nz * push * 0.75, z: player.z - nx * push * 0.75 },
      { x: player.x - nz * push * 0.75, z: player.z + nx * push * 0.75 },
    ];
    const bodyRadius = player.inVehicle ? VEHICLE_RADIUS : PLAYER_RADIUS;
    const edge = WORLD_SIZE / 2 - bodyRadius - 1.6;
    const target = attempts.find((point) => (
      !openWorldBlocked(state, clamp(point.x, -edge, edge), clamp(point.z, -edge, edge), bodyRadius)
    ));
    if (!target) return false;
    player.x = clamp(target.x, -edge, edge);
    player.z = clamp(target.z, -edge, edge);
    syncOpenWorldPlayerPose(state);
    return true;
  }

  function absorbOpenWorldDamage(state, damage) {
    let remaining = Number(damage || 0);
    if (state.armor > 0) {
      const absorbed = Math.min(state.armor, remaining * 0.55);
      state.armor = clamp(state.armor - absorbed, 0, 100);
      remaining -= absorbed * 0.7;
    }
    state.health = clamp(state.health - Math.max(0, remaining), 0, 100);
  }

  function handleOpenWorldTrafficCollision(state, car, dt) {
    const player = state.player;
    const inVehicle = Boolean(player.inVehicle);
    const collisionRadius = inVehicle ? TRAFFIC_COLLISION_DISTANCE : PEDESTRIAN_TRAFFIC_COLLISION_DISTANCE;
    if (!withinDistance2(car.x, car.z, player.x, player.z, collisionRadius)) return;
    pushOpenWorldPlayerAway(state, car.x, car.z, collisionRadius, inVehicle ? 0.42 : 0.72);
    const now = performance.now();
    if (now <= Number(state.nextTrafficHitAt || 0)) return;
    const relativeSpeed = Math.abs(Number(player.speed || 0)) + Math.abs(Number(car.speed || 0));
    const damage = inVehicle
      ? 6 + Math.min(16, relativeSpeed * 0.26)
      : 15 + Math.min(20, Number(car.speed || 0) * 0.7);
    absorbOpenWorldDamage(state, damage);
    if (inVehicle) {
      player.speed *= -0.3;
      player.inVehicle.speed = player.speed;
    } else {
      player.speed = 0;
    }
    state.score = Math.max(0, state.score - (inVehicle ? 55 : 85));
    addOpenWorldHeat(state, inVehicle ? 8 : 5, inVehicle ? "交通撞擊" : "行人事故");
    state.nextTrafficHitAt = now + TRAFFIC_COLLISION_COOLDOWN_MS;
  }

  function updateOpenWorldTraffic(state, dt) {
    state.traffic.forEach((car) => {
      if (car.route) {
        car.t += (car.speed * car.dir * dt) / Math.max(1, roadSegmentLength(car.route));
        car.t = ((car.t % 1) + 1) % 1;
        const point = pointOnRoadSegment(car.route, car.t, car.lane);
        car.x = point.x;
        car.z = point.z;
        car.angle = roadSegmentAngle(car.route, car.dir);
      } else {
        const delta = car.speed * car.dir * dt;
        if (car.axis === "x") {
          car.x += delta;
          if (car.x > 83) car.x = -83;
          if (car.x < -83) car.x = 83;
        } else {
          car.z += delta;
          if (car.z > 83) car.z = -83;
          if (car.z < -83) car.z = 83;
        }
        car.angle = car.axis === "x" ? Math.PI / 2 * car.dir : car.dir > 0 ? 0 : Math.PI;
      }
      setObjectPose(car.mesh, car.x, car.z, car.angle);
      handleOpenWorldTrafficCollision(state, car, dt);
      if (withinDistance2(car.x, car.z, state.player.x, state.player.z, TRAFFIC_HIT_DISTANCE)) {
        if (state.player.inVehicle && Math.abs(state.player.speed) > 9) {
          state.health = clamp(state.health - dt * 12, 0, 100);
          addOpenWorldHeat(state, dt * 8, "交通擦撞");
        }
      }
    });
  }

  function updateOpenWorldPatrols(state, dt) {
    const now = performance.now();
    state.patrols.forEach((patrol, index) => {
      const chasing = state.heat > 16 && now > Number(patrol.stunnedUntil || 0);
      let targetX = [-72, 72, -72, 72][index];
      let targetZ = [-72, -72, 72, 72][index];
      let desiredSpeed = 8;
      if (chasing) {
        targetX = state.player.x;
        targetZ = state.player.z;
        desiredSpeed = 14 + Math.min(12, state.heat * 0.13);
      }
      const desiredAngle = angleTo(patrol.x, patrol.z, targetX, targetZ);
      patrol.angle = normalizeAngle(patrol.angle + clamp(normalizeAngle(desiredAngle - patrol.angle), -2.4 * dt, 2.4 * dt));
      patrol.speed += (desiredSpeed - patrol.speed) * Math.min(1, dt * 1.8);
      const nextX = patrol.x + Math.sin(patrol.angle) * patrol.speed * dt;
      const nextZ = patrol.z + Math.cos(patrol.angle) * patrol.speed * dt;
      if (openWorldBlocked(state, nextX, nextZ, VEHICLE_RADIUS)) {
        patrol.angle += Math.PI * 0.35;
        patrol.speed *= 0.4;
      } else {
        patrol.x = nextX;
        patrol.z = nextZ;
      }
      setObjectPose(patrol.mesh, patrol.x, patrol.z, patrol.angle);
      if (chasing && withinDistance2(patrol.x, patrol.z, state.player.x, state.player.z, PATROL_DAMAGE_DISTANCE)) {
        state.health = clamp(state.health - dt * 7.5, 0, 100);
        state.heat = clamp(state.heat + dt * 2.8, 0, 100);
      }
    });
  }

  function updateOpenWorldPickups(state, dt) {
    state.pickups.forEach((pickup) => {
      if (!pickup.active) return;
      pickup.spin += dt * 2.6;
      pickup.mesh.rotation.y = pickup.spin;
      pickup.mesh.position.y = 0.4 + Math.sin(pickup.spin * 1.7) * 0.16;
      if (!withinDistance2(pickup.x, pickup.z, state.player.x, state.player.z, PICKUP_DISTANCE)) return;
      pickup.active = false;
      pickup.mesh.visible = false;
      state.powerupsCollected += 1;
      if (pickup.type === "health") state.health = clamp(state.health + 32, 0, 100);
      if (pickup.type === "armor") state.armor = clamp(state.armor + 45, 0, 100);
      if (pickup.type === "fuel") state.fuel = clamp(state.fuel + 42, 0, 100);
      if (pickup.type === "ammo") state.gadgetAmmo += 8;
      state.score += 110;
    });
  }

  function updateOpenWorldTailTarget(state, dt) {
    const target = state.tailTarget;
    if (!target?.mesh?.visible) return;
    if (!state.missionState) return;
    const playerDist = distance(target.x, target.z, state.player.x, state.player.z);
    let waypoint = state.missionState?.tailWaypoint;
    if (!waypoint || withinDistance2(target.x, target.z, waypoint.x, waypoint.z, 6)) {
      waypoint = chooseTailWaypoint(state);
      state.missionState.tailWaypoint = waypoint;
    }
    const evadeAngle = playerDist < 32
      ? angleTo(state.player.x, state.player.z, target.x, target.z)
      : angleTo(target.x, target.z, waypoint.x, waypoint.z);
    target.angle = normalizeAngle(target.angle + clamp(normalizeAngle(evadeAngle - target.angle), -1.7 * dt, 1.7 * dt));
    const nextX = target.x + Math.sin(target.angle) * target.speed * dt;
    const nextZ = target.z + Math.cos(target.angle) * target.speed * dt;
    if (openWorldBlocked(state, nextX, nextZ, VEHICLE_RADIUS)) {
      target.angle += Math.PI * 0.5;
    } else {
      target.x = clamp(nextX, -78, 78);
      target.z = clamp(nextZ, -78, 78);
    }
    setObjectPose(target.mesh, target.x, target.z, target.angle);
    if (state.missionState) state.missionState.target = { x: target.x, z: target.z };
  }

  function updateOpenWorldProjectiles(api, state, dt) {
    state.fireCooldown = Math.max(0, Number(state.fireCooldown || 0) - dt);
    state.projectiles.forEach((shot) => {
      shot.x += Math.sin(shot.angle) * shot.speed * dt;
      shot.z += Math.cos(shot.angle) * shot.speed * dt;
      shot.life -= dt;
      shot.mesh?.position.set(shot.x, 1.1, shot.z);
      const target = state.tailTarget;
      if (target?.mesh?.visible && withinDistance2(shot.x, shot.z, target.x, target.z, TAIL_GADGET_HIT_DISTANCE)) {
        shot.life = 0;
        state.gadgetHits += 1;
        state.score += 180;
        if (state.missionState) state.missionState.tailSeconds = Math.min(10, Number(state.missionState.tailSeconds || 0) + 2.6);
      }
      state.patrols.forEach((patrol) => {
        if (shot.life <= 0 || !withinDistance2(shot.x, shot.z, patrol.x, patrol.z, PATROL_GADGET_HIT_DISTANCE)) return;
        shot.life = 0;
        patrol.stunnedUntil = performance.now() + 2200;
        state.evades += 1;
        state.score += 90;
        state.heat = clamp(state.heat + 7, 0, 100);
      });
    });
    state.projectiles = state.projectiles.filter((shot) => {
      const alive = shot.life > 0 && Math.abs(shot.x) < 90 && Math.abs(shot.z) < 90;
      if (!alive) disposeOpenWorldObject(shot.mesh);
      return alive;
    });
  }

  function updateOpenWorldMission(api, state, dt) {
    const mission = activeMission(state);
    const missionState = state.missionState || {};
    if (!missionState.expiresAt) return;
    if (Date.now() > missionState.expiresAt) {
      state.score = Math.max(0, state.score - 160);
      prepareOpenWorldMission(state);
      api.status(`任務逾時，已重新安排：${activeMission(state).label}`);
      return;
    }
    const target = currentMissionTarget(state);
    const d = distance(state.player.x, state.player.z, target.x, target.z);
    if (mission.key === "race" && d < 5.2) {
      missionState.gateIndex += 1;
      state.score += 120 + missionState.gateIndex * 12;
      if (missionState.gateIndex >= mission.gates.length) completeOpenWorldMission(api, state);
      else updateMissionMarker(state);
      return;
    }
    if ((mission.key === "courier" || mission.key === "rescue") && d < 4.8) {
      if (missionState.stage !== "dropoff") {
        missionState.stage = "dropoff";
        state.score += 120;
        updateMissionMarker(state);
      } else {
        completeOpenWorldMission(api, state);
      }
      return;
    }
    if (mission.key === "tail") {
      const targetVehicle = state.tailTarget;
      if (targetVehicle?.mesh?.visible) {
        const followDist = distance(state.player.x, state.player.z, targetVehicle.x, targetVehicle.z);
        if (followDist < 18 && state.player.inVehicle) {
          missionState.tailSeconds += dt;
          state.score += dt * 18;
        } else if (followDist > 40) {
          missionState.tailSeconds = Math.max(0, missionState.tailSeconds - dt * 0.6);
        }
        if (missionState.tailSeconds >= 10) completeOpenWorldMission(api, state);
      }
      updateMissionMarker(state);
    }
  }

  function updateOpenWorldHeat(state, dt) {
    const player = state.player;
    const onStreet = isOnRoad(player.x) || isOnRoad(player.z) || pointOnDiagonalRoad(player.x, player.z, VEHICLE_RADIUS);
    if (player.inVehicle && Math.abs(player.speed) > 37 && !onStreet) {
      addOpenWorldHeat(state, dt * 7.5, "危險駕駛");
    }
    const nearPatrol = state.patrols.some((patrol) => withinDistance2(patrol.x, patrol.z, player.x, player.z, 24));
    if (!nearPatrol || state.heat < 18) state.heat = clamp(state.heat - dt * (nearPatrol ? 1.8 : 5.4), 0, 100);
    const oldStars = state.stars;
    state.stars = Math.min(5, Math.ceil(state.heat / 20));
    if (oldStars >= 3 && state.stars <= 1) state.evades += 1;
  }

  function updateOpenWorldHud(api, state) {
    if (!state.hud || !state.minimap) return;
    if (state.titleCard) state.titleCard.style.display = state.status === "ready" ? "" : "none";
    const now = performance.now();
    const mission = activeMission(state);
    const target = currentMissionTarget(state);
    const missionTime = Math.max(0, Number(state.missionState?.expiresAt || Date.now()) - Date.now());
    const mode = state.player.inVehicle ? `車輛 ${state.player.inVehicle.type}` : "步行";
    const hudHtml = `
      <div class="open-world-hud-row">
        <strong>${mission.label}</strong>
        <span>${openWorldMissionProgressText(state)}</span>
      </div>
      <div class="open-world-hud-grid">
        <span>分數 ${Math.round(state.score).toLocaleString()}</span>
        <span>現金 ${state.cash.toLocaleString()}</span>
        <span>生命 ${Math.round(state.health)}</span>
        <span>防具 ${Math.round(state.armor)}</span>
        <span>燃料 ${Math.round(state.fuel)}</span>
        <span>警戒 ${"★".repeat(state.stars) || "0"}</span>
        <span>${mode}</span>
        <span>${formatOpenWorldTime(missionTime)}</span>
      </div>
      <div class="open-world-hud-row open-world-hud-sub">
        <span>${mission.district}</span>
        <span>距離 ${Math.round(distance(state.player.x, state.player.z, target.x, target.z))}m · 干擾器 ${state.gadgetAmmo}</span>
      </div>
    `;
    if (state.lastHudHtml !== hudHtml) {
      state.hud.innerHTML = hudHtml;
      state.lastHudHtml = hudHtml;
    }
    if (now >= Number(state.nextMinimapAt || 0)) {
      drawOpenWorldMinimap(state);
      state.nextMinimapAt = now + 125;
    }
    const statusText = `${mission.label} · ${openWorldMissionProgressText(state)} · ${mode} · 警戒 ${state.stars}`;
    if (
      state.status === "active" &&
      (statusText !== state.lastStatusText || now > Number(state.nextStatusAt || 0))
    ) {
      api.status(statusText);
      state.lastStatusText = statusText;
      state.nextStatusAt = now + 700;
    }
  }

  function drawOpenWorldMinimap(state) {
    const canvas = state.minimap;
    const ctx = canvas.getContext("2d");
    const size = canvas.width;
    ctx.clearRect(0, 0, size, size);
    ctx.fillStyle = "#102a1d";
    ctx.fillRect(0, 0, size, size);
    const scale = size / WORLD_SIZE;
    const toX = (x) => (x + WORLD_SIZE / 2) * scale;
    const toY = (z) => (z + WORLD_SIZE / 2) * scale;
    OPEN_WORLD_SCENIC_ZONES.forEach((zone) => {
      ctx.fillStyle = zone.key === "harbor"
        ? "rgba(14,116,144,.82)"
        : zone.key === "park"
          ? "rgba(63,143,87,.74)"
          : zone.key === "hospital"
            ? "rgba(224,242,254,.72)"
            : zone.key === "industrial"
              ? "rgba(100,116,139,.72)"
              : zone.key === "market"
                ? "rgba(245,158,11,.68)"
                : "rgba(125,211,252,.62)";
      ctx.fillRect(toX(zone.x - zone.w / 2), toY(zone.z - zone.d / 2), zone.w * scale, zone.d * scale);
    });
    OPEN_WORLD_ROADS.forEach((road) => {
      const profile = openWorldRoadProfile(road);
      ctx.strokeStyle = profile.asphalt || "#64748b";
      ctx.lineWidth = Math.max(2, profile.width * scale);
      ctx.beginPath();
      ctx.moveTo(toX(road), 0);
      ctx.lineTo(toX(road), size);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(0, toY(road));
      ctx.lineTo(size, toY(road));
      ctx.stroke();
    });
    ctx.strokeStyle = "rgba(226,232,240,.82)";
    ctx.lineWidth = Math.max(1, 0.5 * scale);
    OPEN_WORLD_ROADS.forEach((road) => {
      ctx.beginPath();
      ctx.moveTo(toX(road), 0);
      ctx.lineTo(toX(road), size);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(0, toY(road));
      ctx.lineTo(size, toY(road));
      ctx.stroke();
    });
    OPEN_WORLD_DIAGONAL_ROADS.forEach((road) => {
      ctx.strokeStyle = road.color || "#64748b";
      ctx.lineWidth = Math.max(2, (road.width || ROAD_WIDTH) * scale);
      ctx.beginPath();
      ctx.moveTo(toX(road.x1), toY(road.z1));
      ctx.lineTo(toX(road.x2), toY(road.z2));
      ctx.stroke();
    });
    OPEN_WORLD_ALLEY_ROADS.forEach((road) => {
      ctx.strokeStyle = road.color || "#475569";
      ctx.lineWidth = Math.max(1, (road.width || 3.4) * scale);
      ctx.beginPath();
      ctx.moveTo(toX(road.x1), toY(road.z1));
      ctx.lineTo(toX(road.x2), toY(road.z2));
      ctx.stroke();
    });
    ctx.fillStyle = "rgba(15,23,42,.6)";
    state.colliders.slice(0, 120).forEach((rect) => {
      ctx.fillRect(toX(rect.x - rect.w / 2), toY(rect.z - rect.d / 2), rect.w * scale, rect.d * scale);
    });
    const mission = activeMission(state);
    const target = currentMissionTarget(state);
    ctx.fillStyle = mission.color;
    ctx.beginPath();
    ctx.arc(toX(target.x), toY(target.z), 4.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#f8fafc";
    ctx.save();
    ctx.translate(toX(state.player.x), toY(state.player.z));
    ctx.rotate(state.player.angle);
    ctx.beginPath();
    ctx.moveTo(0, -7);
    ctx.lineTo(5, 6);
    ctx.lineTo(-5, 6);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  function renderOpenWorldFrame(state) {
    if (!state?.renderer) return;
    const t = performance.now() * 0.001;
    if (state.missionMarker) {
      state.missionMarker.rotation.y = t * 1.3;
      state.missionMarker.userData.beam.scale.y = 0.9 + Math.sin(t * 2.8) * 0.08;
    }
    state.renderer.render(state.scene, state.camera);
  }

  function disposeOpenWorldState(state) {
    if (!state) return;
    if (state.raf) cancelAnimationFrame(state.raf);
    state.raf = null;
    if (state.resizeHandler) window.removeEventListener("resize", state.resizeHandler);
    state.resizeHandler = null;
    state.projectiles?.forEach?.((shot) => disposeOpenWorldObject(shot.mesh));
    state.projectiles = [];
    state.scene?.traverse?.((object) => {
      object.geometry?.dispose?.();
      const materials = Array.isArray(object.material)
        ? object.material
        : object.material
          ? [object.material]
          : [];
      materials.forEach((material) => material.dispose?.());
    });
    state.renderer?.dispose?.();
    state.renderer?.forceContextLoss?.();
  }

  function tickOpenWorld(api, now) {
    const state = api._openWorldState;
    if (!state || state.status !== "active") return;
    const last = state.lastFrameAt || now;
    const dt = Math.min(0.05, Math.max(0.001, (now - last) / 1000));
    state.lastFrameAt = now;
    updateOpenWorldMovement(state, dt);
    updateOpenWorldTraffic(state, dt);
    updateOpenWorldTailTarget(state, dt);
    updateOpenWorldPatrols(state, dt);
    updateOpenWorldPickups(state, dt);
    updateOpenWorldProjectiles(api, state, dt);
    updateOpenWorldMission(api, state, dt);
    updateOpenWorldHeat(state, dt);
    if (state.armor > 0 && state.health < 100) {
      const repair = Math.min(state.armor, dt * 0.8);
      state.armor -= repair;
      state.health = clamp(state.health + repair * 0.45, 0, 100);
    }
    if (state.health <= 0) {
      finishOpenWorld(api, "城市行動失敗");
      return;
    }
    updateOpenWorldCamera(state);
    updateOpenWorldHud(api, state);
    renderOpenWorldFrame(state);
    state.raf = requestAnimationFrame((frameNow) => tickOpenWorld(api, frameNow));
  }

  function finishOpenWorld(api, reason = "結算") {
    const state = api._openWorldState;
    if (!state || state.status === "finished") return;
    if (state.status !== "active" || !state.startedAt) {
      api.status("尚未開始，按開始後才會計時與送出成績。");
      return;
    }
    state.status = "finished";
    state.completedAt = Date.now();
    if (state.raf) cancelAnimationFrame(state.raf);
    state.raf = null;
    updateOpenWorldHud(api, state);
    renderOpenWorldFrame(state);
    const score = Math.max(1, Math.round(state.score + state.missionsCompleted * 300 + state.evades * 90));
    api.status(`${reason} · 分數 ${score.toLocaleString()} · 任務 ${state.missionsCompleted}`);
    if (state.missionsCompleted > 0) api.achievement?.("score-posted", "城市紀錄", "完成一局 3D 都市開放世界。");
    if (state.evades > 0) api.achievement?.("clean-escape", "甩開追逐", "成功降低警戒並脫離追逐。");
    api.recordReplay?.({
      title: "都市開放世界",
      score,
      difficulty: "open-world-city",
      elapsed_ms: Math.max(1, state.completedAt - state.startedAt),
      summary: `任務 ${state.missionsCompleted} · 警戒 ${state.stars} · 駕駛 ${Math.round(state.distanceDriven)}m`,
      moves: [
        `missions=${state.missionsCompleted}`,
        `evades=${state.evades}`,
        `distance=${Math.round(state.distanceDriven + state.distanceWalked)}`,
      ],
    });
    api.submitScore?.({
      raw_elapsed_ms: Math.max(1, state.completedAt - state.startedAt),
      penalty_seconds: 0,
      elapsed_ms: Math.max(1, state.completedAt - state.startedAt),
      difficulty: "open-world-city",
      puzzle_id: state.dailyChallenge?.key || "open-world-city",
      score,
      guess_count: 0,
      missions: Number(state.missionsCompleted || 0),
      evasion: Number(state.evades || 0),
      drive: Math.round(state.distanceDriven || 0),
      powerup: Number(state.powerupsCollected || 0),
      weapon: Number(state.gadgetHits || 0),
      survive: state.health > 0 ? 1 : 0,
    });
  }

  function createOpenWorldState(api) {
    const THREE = window.THREE;
    const dailyChallenge = api.dailyChallenge?.() || null;
    const stage = api.root.querySelector(".open-world-stage");
    const state = {
      THREE,
      stage,
      titleCard: api.root.querySelector(".open-world-title-card"),
      hud: api.root.querySelector(".open-world-hud"),
      minimap: api.root.querySelector(".open-world-minimap"),
      status: "ready",
      startedAt: 0,
      completedAt: null,
      score: 0,
      cash: 0,
      health: 100,
      armor: 0,
      fuel: 100,
      heat: 0,
      stars: 0,
      combo: 0,
      evades: 0,
      missionsCompleted: 0,
      missionIndex: 0,
      missionState: null,
      missionFinishedAt: 0,
      gadgetAmmo: 8,
      gadgetHits: 0,
      fireCooldown: 0,
      powerupsCollected: 0,
      distanceDriven: 0,
      distanceWalked: 0,
      player: {
        x: OPEN_WORLD_PLAYER_START.x,
        z: OPEN_WORLD_PLAYER_START.z,
        angle: OPEN_WORLD_PLAYER_START.angle,
        speed: 0,
        walkCycle: 0,
        mesh: null,
        inVehicle: null,
      },
      keys: {},
      colliders: [],
      vehicles: [],
      traffic: [],
      patrols: [],
      pickups: [],
      projectiles: [],
      tailTarget: null,
      dailyChallenge,
      rng: dailyChallenge?.seed ? window.createHackmeGameSeededRandom?.(dailyChallenge.seed) : null,
      raf: null,
      lastFrameAt: 0,
      lastStatusText: "",
      nextStatusAt: 0,
      nextTrafficHitAt: 0,
      cameraReady: false,
      resizeHandler: null,
    };
    buildOpenWorldCity(api, state);
    state.resizeHandler = () => {
      resizeOpenWorldRenderer(state);
      renderOpenWorldFrame(state);
    };
    window.addEventListener("resize", state.resizeHandler);
    return state;
  }

  function showOpenWorldReady(api) {
    disposeOpenWorldState(api._openWorldState);
    api._openWorldState = null;
    if (!window.THREE) {
      api.root.innerHTML = `<div class="game-page-empty"><strong>3D 引擎尚未載入</strong><span>需要 three.min.js 才能遊玩都市開放世界。</span></div>`;
      api.status("Three.js 尚未載入。");
      return;
    }
    api.root.innerHTML = `
      <div class="open-world-shell">
        <div class="open-world-stage" aria-label="都市開放世界遊戲畫面">
          <div class="open-world-title-card">
            <strong>都市開放世界</strong>
            <span>按開始後才會計時、產生警戒與送出成績</span>
          </div>
          <div class="open-world-hud" aria-live="polite"></div>
          <canvas class="open-world-minimap" width="168" height="168" aria-label="都市小地圖"></canvas>
        </div>
      </div>
    `;
    const state = createOpenWorldState(api);
    api._openWorldState = state;
    updateOpenWorldHud(api, state);
    renderOpenWorldFrame(state);
    api.status("待機 · 選擇任務後開始城市探索。");
  }

  function startOpenWorld(api) {
    if (!window.THREE && typeof ensureThreeJsLoaded === "function") {
      api.status("3D 引擎載入中。");
      ensureThreeJsLoaded()
        .then(() => startOpenWorld(api))
        .catch((err) => {
          reportFrontendFailure("open-world-three-load", err);
          showOpenWorldReady(api);
          api.status("3D 引擎載入失敗，請重新整理後再試。", false);
        });
      return;
    }
    if (!window.THREE) {
      showOpenWorldReady(api);
      return;
    }
    if (!api._openWorldState || api._openWorldState.status === "finished") showOpenWorldReady(api);
    const state = api._openWorldState || createOpenWorldState(api);
    api._openWorldState = state;
    state.status = "active";
    state.startedAt = Date.now();
    state.completedAt = null;
    state.score = 0;
    state.cash = 0;
    state.health = 100;
    state.armor = 0;
    state.fuel = 100;
    state.heat = 0;
    state.stars = 0;
    state.evades = 0;
    state.missionsCompleted = 0;
    state.distanceDriven = 0;
    state.distanceWalked = 0;
    state.gadgetAmmo = 8;
    state.gadgetHits = 0;
    state.powerupsCollected = 0;
    state.lastStatusText = "";
    state.nextStatusAt = 0;
    state.nextTrafficHitAt = 0;
    state.projectiles.forEach((shot) => disposeOpenWorldObject(shot.mesh));
    state.projectiles = [];
    state.player.x = OPEN_WORLD_PLAYER_START.x;
    state.player.z = OPEN_WORLD_PLAYER_START.z;
    state.player.angle = OPEN_WORLD_PLAYER_START.angle;
    state.player.speed = 0;
    if (state.player.inVehicle) {
      state.player.inVehicle.occupied = false;
      state.player.inVehicle = null;
    }
    state.player.mesh.visible = true;
    setObjectPose(state.player.mesh, state.player.x, state.player.z, state.player.angle);
    state.pickups.forEach((pickup) => {
      pickup.active = true;
      pickup.mesh.visible = true;
    });
    prepareOpenWorldMission(state);
    updateOpenWorldHud(api, state);
    if (state.raf) cancelAnimationFrame(state.raf);
    state.lastFrameAt = performance.now();
    state.raf = requestAnimationFrame((now) => tickOpenWorld(api, now));
    api.status(`${activeMission(state).label} 開始。`);
  }

  function handleOpenWorldKey(api, event, pressed) {
    const state = api._openWorldState;
    if (!state) return;
    const key = event.key;
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "a", "A", "d", "D", "w", "W", "s", "S", "Shift", " ", "e", "E", "m", "M"].includes(key)) {
      event.preventDefault?.();
    }
    if (key === "ArrowLeft" || key === "a" || key === "A") setOpenWorldInput(state, "left", pressed);
    if (key === "ArrowRight" || key === "d" || key === "D") setOpenWorldInput(state, "right", pressed);
    if (key === "ArrowUp" || key === "w" || key === "W") setOpenWorldInput(state, "up", pressed);
    if (key === "ArrowDown" || key === "s" || key === "S") setOpenWorldInput(state, "down", pressed);
    if (key === "Shift") setOpenWorldInput(state, "sprint", pressed);
    if ((key === "e" || key === "E") && pressed) toggleOpenWorldVehicle(api, state);
    if ((key === "m" || key === "M") && pressed) cycleOpenWorldMission(api, state);
    if (key === " " && pressed) fireOpenWorldGadget(api, state);
  }

  function resetOpenWorldJoystick(api, stick) {
    const state = api?._openWorldState;
    if (state) {
      ["left", "right", "up", "down", "sprint"].forEach((key) => setOpenWorldInput(state, key, false));
    }
    const knob = stick?.querySelector?.(".game-virtual-stick-knob");
    if (knob) knob.style.transform = "translate(-50%, -50%)";
    if (stick) stick.classList.remove("is-active");
  }

  function updateOpenWorldJoystick(api, stick, clientX, clientY) {
    const state = api?._openWorldState;
    if (!state || !stick) return;
    const rect = stick.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const radius = Math.max(1, Math.min(rect.width, rect.height) / 2);
    const dx = clamp((clientX - cx) / radius, -1, 1);
    const dy = clamp((clientY - cy) / radius, -1, 1);
    const dead = 0.18;
    setOpenWorldInput(state, "left", dx < -dead);
    setOpenWorldInput(state, "right", dx > dead);
    setOpenWorldInput(state, "up", dy < -dead);
    setOpenWorldInput(state, "down", dy > dead);
    setOpenWorldInput(state, "sprint", Math.hypot(dx, dy) > 0.78);
    const knob = stick.querySelector(".game-virtual-stick-knob");
    if (knob) knob.style.transform = `translate(calc(-50% + ${Math.round(dx * 34)}px), calc(-50% + ${Math.round(dy * 34)}px))`;
    stick.classList.add("is-active");
  }

  function bindOpenWorldJoystick(api) {
    const stick = api.controls?.querySelector?.("[data-open-world-stick]");
    if (!stick) return () => {};
    let pointerId = null;
    const down = (event) => {
      pointerId = event.pointerId;
      event.preventDefault();
      try {
        stick.setPointerCapture?.(pointerId);
      } catch (_) {}
      updateOpenWorldJoystick(api, stick, event.clientX, event.clientY);
    };
    const move = (event) => {
      if (pointerId !== event.pointerId) return;
      event.preventDefault();
      updateOpenWorldJoystick(api, stick, event.clientX, event.clientY);
    };
    const up = (event) => {
      if (pointerId !== event.pointerId) return;
      pointerId = null;
      resetOpenWorldJoystick(api, stick);
    };
    stick.addEventListener("pointerdown", down, { passive: false });
    stick.addEventListener("pointermove", move, { passive: false });
    stick.addEventListener("pointerup", up);
    stick.addEventListener("pointercancel", up);
    stick.addEventListener("lostpointercapture", up);
    return () => {
      stick.removeEventListener("pointerdown", down);
      stick.removeEventListener("pointermove", move);
      stick.removeEventListener("pointerup", up);
      stick.removeEventListener("pointercancel", up);
      stick.removeEventListener("lostpointercapture", up);
      resetOpenWorldJoystick(api, stick);
    };
  }

  window.registerHackmeLocalGameModule("open_world", {
    mount(api) {
      let disposed = false;
      api.setTitle("都市開放世界");
      api.setSwipeMode?.("hold");
      api.setActions(`
        <button class="btn game-mini-btn btn-primary" type="button" data-action="new">開始</button>
        <button class="btn game-mini-btn" type="button" data-action="mission">換任務</button>
        <button class="btn game-mini-btn" type="button" data-action="vehicle">上/下車</button>
        <button class="btn game-mini-btn" type="button" data-action="finish">結算</button>
      `);
      api.setControls(`
        <div class="game-virtual-stick" data-open-world-stick aria-label="移動搖桿" role="application">
          <span class="game-virtual-stick-label">移動</span>
          <span class="game-virtual-stick-knob"></span>
        </div>
        <button class="btn game-mini-btn" type="button" data-hold="left">左</button>
        <button class="btn game-mini-btn" type="button" data-hold="up">前進</button>
        <button class="btn game-mini-btn" type="button" data-hold="down">後退</button>
        <button class="btn game-mini-btn" type="button" data-hold="right">右</button>
        <button class="btn game-mini-btn" type="button" data-hold="sprint">加速</button>
        <button class="btn game-mini-btn" type="button" data-open-world-control="vehicle">上車</button>
        <button class="btn game-mini-btn" type="button" data-open-world-control="gadget">干擾器</button>
        <button class="btn game-mini-btn" type="button" data-open-world-control="mission">任務</button>
      `);
      const cleanupJoystick = bindOpenWorldJoystick(api);
      api.onAction = (action) => {
        const state = api._openWorldState;
        if (action === "new") startOpenWorld(api);
        if (action === "mission") cycleOpenWorldMission(api, state);
        if (action === "vehicle") toggleOpenWorldVehicle(api, state);
        if (action === "finish") finishOpenWorld(api, "手動結算");
      };
      api.onControl = (target, pressed) => {
        const state = api._openWorldState;
        if (!state) return;
        if (target.dataset.hold) setOpenWorldInput(state, target.dataset.hold, pressed);
        if (!pressed) return;
        if (target.dataset.openWorldControl === "vehicle") toggleOpenWorldVehicle(api, state);
        if (target.dataset.openWorldControl === "gadget") fireOpenWorldGadget(api, state);
        if (target.dataset.openWorldControl === "mission") cycleOpenWorldMission(api, state);
      };
      api.onKey = (event, pressed) => handleOpenWorldKey(api, event, pressed);
      const ready = () => {
        if (!disposed) showOpenWorldReady(api);
      };
      if (!window.THREE && typeof ensureThreeJsLoaded === "function") {
        api.root.innerHTML = `<div class="game-page-empty"><strong>3D 引擎載入中</strong><span>正在準備都市開放世界。</span></div>`;
        api.status("3D 引擎載入中。");
        ensureThreeJsLoaded().then(ready).catch(ready);
      } else {
        ready();
      }
      return () => {
        disposed = true;
        cleanupJoystick();
        disposeOpenWorldState(api._openWorldState);
        api._openWorldState = null;
      };
    },
  });
}());
