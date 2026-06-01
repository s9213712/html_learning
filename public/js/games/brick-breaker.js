'use strict';

(function () {
  const { makeCtx, registerScore, clamp } = window.HACKME_LOCAL_GAME_HELPERS;
  const BRICK_BREAKER_ASSET_SOURCES = Object.freeze({
    puzzlePack: {
      name: "Kenney Puzzle Pack 2",
      url: "https://kenney.nl/assets/puzzle-pack-2",
      license: "Creative Commons CC0",
      usage: "bundled PNG brick tiles, paddle panel, balls, coins and hit particles with canvas fallback",
    },
  });
  const BRICK_BREAKER_ASSET_BASE = "/assets/games/vendor/kenney/puzzle-pack-2/";
  const BRICK_BREAKER_IMAGE_ASSETS = Object.freeze({
    brickBlue: `${BRICK_BREAKER_ASSET_BASE}tiles/blue_01.png`,
    brickGreen: `${BRICK_BREAKER_ASSET_BASE}tiles/green_01.png`,
    brickRed: `${BRICK_BREAKER_ASSET_BASE}tiles/red_01.png`,
    brickYellow: `${BRICK_BREAKER_ASSET_BASE}tiles/yellow_01.png`,
    brickGrey: `${BRICK_BREAKER_ASSET_BASE}tiles/grey_01.png`,
    paddle: `${BRICK_BREAKER_ASSET_BASE}paddles/paddle_01.png`,
    ballBlue: `${BRICK_BREAKER_ASSET_BASE}balls/blue_ball.png`,
    ballYellow: `${BRICK_BREAKER_ASSET_BASE}balls/yellow_ball.png`,
    coin: `${BRICK_BREAKER_ASSET_BASE}coins/coin_01.png`,
    particleBlue: `${BRICK_BREAKER_ASSET_BASE}particles/blue_1.png`,
    particleYellow: `${BRICK_BREAKER_ASSET_BASE}particles/yellow_1.png`,
  });
  const BRICK_BREAKER_IMAGES = {};

  function brickBreakerImageFor(key) {
    const src = BRICK_BREAKER_IMAGE_ASSETS[key];
    if (!src || typeof Image === "undefined") return null;
    if (!BRICK_BREAKER_IMAGES[key]) {
      const image = new Image();
      image.decoding = "async";
      image.src = src;
      BRICK_BREAKER_IMAGES[key] = image;
    }
    return BRICK_BREAKER_IMAGES[key];
  }

  function brickBreakerImageReady(image) {
    return Boolean(image?.complete && image.naturalWidth > 0);
  }

  function drawBrickBreakerImage(ctx, key, x, y, w, h, options = {}) {
    const image = brickBreakerImageFor(key);
    if (!brickBreakerImageReady(image)) return false;
    ctx.save();
    ctx.globalAlpha = options.alpha ?? 1;
    ctx.translate(x + w / 2, y + h / 2);
    if (options.rotation) ctx.rotate(options.rotation);
    ctx.drawImage(image, -w / 2, -h / 2, w, h);
    ctx.restore();
    return true;
  }

  function drawBreakerRoundedRect(ctx, x, y, w, h, radius = 4) {
    const r = Math.min(radius, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  function drawBreakerBackdrop(ctx, tick = 0) {
    const gradient = ctx.createLinearGradient(0, 0, 0, 480);
    gradient.addColorStop(0, "#07111f");
    gradient.addColorStop(0.56, "#10183a");
    gradient.addColorStop(1, "#07111f");
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 360, 480);
    ctx.strokeStyle = "rgba(56,189,248,.08)";
    for (let y = 36; y < 440; y += 24) {
      ctx.beginPath();
      ctx.moveTo(20, y + Math.sin((tick + y) / 45) * 2);
      ctx.lineTo(340, y + Math.cos((tick + y) / 52) * 2);
      ctx.stroke();
    }
    ctx.fillStyle = "rgba(255,255,255,.08)";
    for (let i = 0; i < 42; i += 1) {
      ctx.fillRect((i * 43 + tick * 0.7) % 360, (i * 59 + tick * 0.45) % 480, 2, 2);
    }
  }

  function drawBreakerBrick(ctx, brick) {
    if (!brick.on) return;
    const color = brick.boss ? "#f97316" : brick.shield ? "#a78bfa" : "#38bdf8";
    const trim = brick.boss ? "#fed7aa" : brick.shield ? "#ddd6fe" : "#bae6fd";
    const asset = brick.boss ? "brickRed" : brick.shield ? "brickGrey" : (brick.x / 41) % 2 > 1 ? "brickGreen" : "brickBlue";
    if (drawBrickBreakerImage(ctx, asset, brick.x, brick.y - 3, brick.w, brick.h + 6)) {
      if (brick.hp > 1) {
        ctx.fillStyle = "rgba(15,23,42,.72)";
        ctx.fillRect(brick.x + 5, brick.y + 5, brick.w - 10, 3);
        ctx.fillStyle = trim;
        ctx.fillRect(brick.x + 5, brick.y + 5, (brick.w - 10) * Math.min(1, brick.hp / (brick.boss ? 5 : 2)), 3);
      }
      return;
    }
    drawBreakerRoundedRect(ctx, brick.x, brick.y, brick.w, brick.h, 4);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,.2)";
    ctx.fillRect(brick.x + 3, brick.y + 2, brick.w - 6, 3);
    ctx.fillStyle = "rgba(15,23,42,.2)";
    ctx.fillRect(brick.x + 3, brick.y + brick.h - 4, brick.w - 6, 2);
    if (brick.boss) {
      ctx.strokeStyle = trim;
      ctx.strokeRect(brick.x + 5, brick.y + 3, brick.w - 10, brick.h - 6);
    }
    if (brick.hp > 1) {
      ctx.fillStyle = "#0f172a";
      ctx.fillRect(brick.x + 5, brick.y + 6, brick.w - 10, 3);
      ctx.fillStyle = trim;
      ctx.fillRect(brick.x + 5, brick.y + 6, (brick.w - 10) * Math.min(1, brick.hp / (brick.boss ? 5 : 2)), 3);
    }
  }

  function drawBreakerPaddle(ctx, x) {
    if (drawBrickBreakerImage(ctx, "paddle", x - 47, 436, 94, 22)) return;
    drawBreakerRoundedRect(ctx, x - 44, 441, 88, 13, 6);
    ctx.fillStyle = "#e5e7eb";
    ctx.fill();
    ctx.fillStyle = "#38bdf8";
    ctx.fillRect(x - 34, 443, 18, 8);
    ctx.fillRect(x + 16, 443, 18, 8);
    ctx.fillStyle = "rgba(15,23,42,.38)";
    ctx.fillRect(x - 6, 442, 12, 10);
  }

  function drawBreakerBall(ctx, ball) {
    const glow = ctx.createRadialGradient(ball[0], ball[1], 0, ball[0], ball[1], 18);
    glow.addColorStop(0, "rgba(250,204,21,.34)");
    glow.addColorStop(1, "rgba(250,204,21,0)");
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(ball[0], ball[1], 18, 0, Math.PI * 2);
    ctx.fill();
    if (drawBrickBreakerImage(ctx, "ballYellow", ball[0] - 8, ball[1] - 8, 16, 16, { rotation: ball[0] / 24 + ball[1] / 31 })) return;
    ctx.beginPath();
    ctx.arc(ball[0], ball[1], 7, 0, Math.PI * 2);
    ctx.fillStyle = "#facc15";
    ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,.72)";
    ctx.beginPath();
    ctx.arc(ball[0] - 2.3, ball[1] - 2.6, 2, 0, Math.PI * 2);
    ctx.fill();
  }

  function addBreakerParticles(state, x, y, color, count = 10) {
    for (let i = 0; i < count; i += 1) {
      const angle = Math.random() * Math.PI * 2;
      const speed = 1 + Math.random() * 3.2;
      state.particles.push({ x, y, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed, life: 18 + Math.random() * 16, color });
    }
  }

  function updateBreakerParticles(state) {
    state.particles.forEach((particle) => {
      particle.x += particle.vx;
      particle.y += particle.vy;
      particle.vy += 0.05;
      particle.life -= 1;
    });
    state.particles = state.particles.filter((particle) => particle.life > 0);
  }

  function drawBreakerParticles(ctx, state) {
    state.particles.forEach((particle) => {
      const alpha = Math.max(0, Math.min(1, particle.life / 24));
      ctx.globalAlpha = alpha;
      if (!drawBrickBreakerImage(ctx, particle.color === "#fed7aa" ? "particleYellow" : "particleBlue", particle.x - 3, particle.y - 3, 6, 6, { alpha })) {
        ctx.fillStyle = particle.color;
        ctx.fillRect(particle.x - 2, particle.y - 2, 4, 4);
      }
      ctx.globalAlpha = 1;
    });
  }

  window.registerHackmeLocalGameModule("brick_breaker", {
    mount(api) {
      makeCtx(api, "打磚塊");
      api.setSwipeMode?.("hold");
      const state = { startedAt: 0, score: 0, lives: 3, x: 180, balls: [[180, 280, 0, 0]], bricks: [], particles: [], left: false, right: false, timer: null, over: false, multiball: 0, boss: 0, tick: 0, dailyChallenge: null };
      api.root.innerHTML = `<canvas class="arcade-canvas tall" width="360" height="480" aria-label="打磚塊"></canvas>`;
      api.setControls(`<button class="btn game-mini-btn" data-hold="left">左</button><button class="btn game-mini-btn btn-primary" data-action="new">重開</button><button class="btn game-mini-btn" data-hold="right">右</button>`);
      const canvas = api.root.querySelector("canvas"), ctx = canvas.getContext("2d");
      canvas.style.touchAction = "none";
      const paddleXFromEvent = (event) => {
        const rect = canvas.getBoundingClientRect();
        if (!rect.width) return state.x;
        return clamp(((event.clientX - rect.left) / rect.width) * canvas.width, 44, 316);
      };
      const movePaddleToEvent = (event) => {
        state.x = paddleXFromEvent(event);
        state.left = false;
        state.right = false;
        draw();
      };
      const resetBricks = () => {
        state.bricks = [];
        for (let y = 0; y < 5; y += 1) for (let x = 0; x < 8; x += 1) {
          const boss = y === 0 && x === 3;
          const shield = y === 1 && x % 3 === 0;
          state.bricks.push({ x: 16 + x * 41, y: 42 + y * 22, w: 34, h: 14, on: true, hp: boss ? 5 : shield ? 2 : 1, boss, shield });
        }
      };
      const draw = () => {
        drawBreakerBackdrop(ctx, state.tick);
        state.bricks.forEach((b) => drawBreakerBrick(ctx, b));
        drawBreakerPaddle(ctx, state.x);
        state.balls.forEach((ball) => drawBreakerBall(ctx, ball));
        drawBreakerParticles(ctx, state);
      };
      const drawGameOver = () => {
        ctx.fillStyle = "rgba(7,17,31,.78)";
        ctx.fillRect(42, 184, 276, 112);
        ctx.textAlign = "center";
        ctx.fillStyle = "#ff4f6d";
        ctx.font = "700 28px system-ui, sans-serif";
        ctx.fillText("GAME OVER", 180, 226);
        ctx.fillStyle = "rgba(226,232,240,.9)";
        ctx.font = "13px system-ui, sans-serif";
        ctx.fillText(`分數 ${Number(state.score || 0).toLocaleString()} · Boss 磚 ${state.boss || 0}`, 180, 254);
        ctx.textAlign = "start";
      };
      const finish = () => {
        if (state.over) return;
        state.over = true;
        clearInterval(state.timer);
        state.timer = null;
        state.balls = [];
        api.sound?.("uiSuccess", { volume: 0.14, throttleMs: 250 });
        draw();
        drawGameOver();
        registerScore(api, state.score, state, state.dailyChallenge?.difficulty || "standard");
        api.status(`遊戲結束 · 分數 ${state.score} · Boss 磚 ${state.boss || 0}`);
      };
      const triggerMultiball = () => {
        if (state.balls.length >= 3) return;
        const base = state.balls[0] || [state.x, 320, 3, -4];
        state.balls.push([base[0], base[1], -Math.abs(base[2]) - 1, -4.5], [base[0], base[1], Math.abs(base[2]) + 1, -4.2]);
        state.multiball += 1;
        api.achievement?.("multiball", "多球開局", "啟動多球。");
        api.mission?.("multiball", state.multiball, 1, "啟動多球");
      };
      const tick = () => {
        state.tick += 1;
        if (state.left) state.x -= 6; if (state.right) state.x += 6; state.x = clamp(state.x, 44, 316);
        state.balls.forEach((ball) => {
          ball[0] += ball[2]; ball[1] += ball[3];
          if (ball[0] < 8 || ball[0] > 352) ball[2] *= -1;
          if (ball[1] < 8) ball[3] *= -1;
          if (ball[1] > 438 && Math.abs(ball[0] - state.x) < 50) { ball[3] = -Math.abs(ball[3]); ball[2] += (ball[0] - state.x) * 0.04; }
          for (const b of state.bricks) if (b.on && ball[0] > b.x && ball[0] < b.x + b.w && ball[1] > b.y && ball[1] < b.y + b.h) {
            b.hp -= 1; ball[3] *= -1; state.score += b.boss ? 40 : 25;
            api.sound?.(b.boss ? "metalHit" : "woodHit", { volume: 0.12, throttleMs: 70 });
            addBreakerParticles(state, ball[0], ball[1], b.boss ? "#fed7aa" : b.shield ? "#ddd6fe" : "#bae6fd", b.boss ? 16 : 8);
            if (b.hp <= 0) {
              b.on = false;
              if (b.boss) { state.boss += 1; triggerMultiball(); api.achievement?.("boss-brick", "Boss 磚擊破", "擊破 Boss 磚。"); api.mission?.("boss", state.boss, 1, "擊破 Boss 磚"); }
            }
            break;
          }
        });
        state.balls = state.balls.filter((ball) => ball[1] <= 490);
        if (!state.balls.length) {
          state.lives -= 1;
          api.sound?.("uiDrop", { volume: 0.13, throttleMs: 180 });
          if (state.lives <= 0) {
            finish();
            return;
          }
          state.balls = [[state.x, 320, 3, -4]];
        }
        if (state.bricks.every((b) => !b.on)) { state.score += 500; resetBricks(); }
        updateBreakerParticles(state);
        api.status(`分數 ${state.score} · 生命 ${state.lives} · 球 ${state.balls.length}`);
        draw();
      };
      const start = () => { clearInterval(state.timer); Object.assign(state, { startedAt: Date.now(), score: 0, lives: 3, x: 180, balls: [[180, 280, 3, -4]], particles: [], left: false, right: false, over: false, multiball: 0, boss: 0, tick: 0, dailyChallenge: api.dailyChallenge?.() || null }); resetBricks(); state.timer = setInterval(tick, 16); };
      canvas.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        try {
          canvas.setPointerCapture?.(event.pointerId);
        } catch (_) {
          /* ignore unsupported capture */
        }
        movePaddleToEvent(event);
      }, { passive: false });
      canvas.addEventListener("pointermove", (event) => {
        if (state.over) return;
        event.preventDefault();
        movePaddleToEvent(event);
      }, { passive: false });
      api.onAction = (action) => { if (action === "new") start(); };
      api.onControl = (target, pressed) => { if (target.dataset.hold === "left") state.left = pressed; if (target.dataset.hold === "right") state.right = pressed; };
      api.onKey = (event, pressed) => { if (event.key === "ArrowLeft") state.left = pressed; if (event.key === "ArrowRight") state.right = pressed; };
      resetBricks();
      draw();
      api.status("待機 · 按開始後才會發球、計時與送成績。");
      return () => clearInterval(state.timer);
    },
  });
}());
