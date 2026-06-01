'use strict';

(function () {
  const { makeCtx, registerScore } = window.HACKME_LOCAL_GAME_HELPERS;
  const FACE_NAMES = ["U", "R", "F", "D", "L", "B"];
  const KOCIEMBA_FACE_ORDER = ["U", "R", "F", "D", "L", "B"];
  const SOLVED_FACELETS = "UUUUUUUUURRRRRRRRRFFFFFFFFFDDDDDDDDDLLLLLLLLLBBBBBBBBB";
  const FACE_LABELS = { U: "上", D: "下", F: "前", B: "後", R: "右", L: "左" };
  const FACE_COLORS = {
    U: "#f8fafc",
    D: "#facc15",
    F: "#22c55e",
    B: "#3b82f6",
    R: "#ef4444",
    L: "#f97316",
  };
  const FACE_NORMALS = {
    U: [0, 1, 0],
    D: [0, -1, 0],
    F: [0, 0, 1],
    B: [0, 0, -1],
    R: [1, 0, 0],
    L: [-1, 0, 0],
  };
  const FACE_AXES = {
    U: ["y", 1, -1],
    D: ["y", -1, 1],
    F: ["z", 1, -1],
    B: ["z", -1, 1],
    R: ["x", 1, -1],
    L: ["x", -1, 1],
  };
  const AXIS_INDEX = { x: 0, y: 1, z: 2 };
  const TURN_ANIMATION_MS = 280;
  const MAX_SOLVER_HINTS_PER_SCRAMBLE = 3;
  const inverseMove = (move) => {
    const layer = parseLayerMove(move);
    if (layer) return `${layer.axis}:${layer.layer}:${layer.sign > 0 ? "-" : "+"}`;
    return move.endsWith("'") ? move.slice(0, -1) : `${move}'`;
  };

  function layerMove(axis, layer, sign) {
    return `${axis}:${Number(layer)}:${sign > 0 ? "+" : "-"}`;
  }

  function parseLayerMove(move) {
    const match = String(move || "").match(/^([xyz]):(-?1|0):([+-])$/);
    if (!match) return null;
    return {
      axis: match[1],
      layer: Number(match[2]),
      sign: match[3] === "+" ? 1 : -1,
    };
  }

  function moveSpec(move) {
    const layer = parseLayerMove(move);
    if (layer) return layer;
    const face = String(move || "")[0];
    const axisSpec = FACE_AXES[face];
    if (!axisSpec) return null;
    const prime = String(move || "").endsWith("'");
    const [axis, layerIndex, baseSign] = axisSpec;
    return { axis, layer: layerIndex, sign: prime ? -baseSign : baseSign, face };
  }

  function moveLabel(move) {
    const layer = parseLayerMove(move);
    if (!layer) {
      const face = String(move || "")[0];
      const faceText = FACE_LABELS[face] || "這一面";
      return `${faceText}${String(move || "").endsWith("'") ? "反方向" : "順方向"}轉一下`;
    }
    const axisText = layer.axis === "y" ? "橫排" : "直欄";
    const layerText = layer.layer > 0 ? "上方" : layer.layer < 0 ? "下方" : "中間";
    const directionText = layer.axis === "x"
      ? (layer.sign > 0 ? "往下" : "往上")
      : (layer.sign > 0 ? "往右" : "往左");
    return `${layerText}${axisText}${directionText}`;
  }

  function sameVec(a, b) {
    return a[0] === b[0] && a[1] === b[1] && a[2] === b[2];
  }

  function faceFromDir(dir) {
    return FACE_NAMES.find((face) => sameVec(FACE_NORMALS[face], dir)) || "";
  }

  function faceCellFromPos(face, pos) {
    const [x, y, z] = pos;
    let row = 0;
    let col = 0;
    if (face === "F") { row = 1 - y; col = x + 1; }
    if (face === "B") { row = 1 - y; col = 1 - x; }
    if (face === "R") { row = 1 - y; col = 1 - z; }
    if (face === "L") { row = 1 - y; col = z + 1; }
    if (face === "U") { row = z + 1; col = x + 1; }
    if (face === "D") { row = 1 - z; col = x + 1; }
    return { row, col };
  }

  function stickerTransformForFace(face) {
    if (face === "F") return "translateZ(var(--rubiks-cubie-half))";
    if (face === "B") return "rotateY(180deg) translateZ(var(--rubiks-cubie-half))";
    if (face === "R") return "rotateY(90deg) translateZ(var(--rubiks-cubie-half))";
    if (face === "L") return "rotateY(-90deg) translateZ(var(--rubiks-cubie-half))";
    if (face === "U") return "rotateX(90deg) translateZ(var(--rubiks-cubie-half))";
    if (face === "D") return "rotateX(-90deg) translateZ(var(--rubiks-cubie-half))";
    return "";
  }

  function rotateVec(vec, axis, sign) {
    const [x, y, z] = vec;
    if (axis === "x") return sign > 0 ? [x, -z, y] : [x, z, -y];
    if (axis === "y") return sign > 0 ? [z, y, -x] : [-z, y, x];
    return sign > 0 ? [-y, x, z] : [y, -x, z];
  }

  function createSolvedCube() {
    const cube = [];
    for (let x = -1; x <= 1; x += 1) {
      for (let y = -1; y <= 1; y += 1) {
        for (let z = -1; z <= 1; z += 1) {
          if (x === 0 && y === 0 && z === 0) continue;
          const stickers = [];
          if (y === 1) stickers.push({ dir: [0, 1, 0], face: "U" });
          if (y === -1) stickers.push({ dir: [0, -1, 0], face: "D" });
          if (z === 1) stickers.push({ dir: [0, 0, 1], face: "F" });
          if (z === -1) stickers.push({ dir: [0, 0, -1], face: "B" });
          if (x === 1) stickers.push({ dir: [1, 0, 0], face: "R" });
          if (x === -1) stickers.push({ dir: [-1, 0, 0], face: "L" });
          cube.push({ pos: [x, y, z], stickers });
        }
      }
    }
    return cube;
  }

  function moveCube(cube, move) {
    const spec = moveSpec(move);
    if (!spec) return;
    const axisIndex = AXIS_INDEX[spec.axis];
    cube.forEach((cubie) => {
      if (cubie.pos[axisIndex] !== spec.layer) return;
      cubie.pos = rotateVec(cubie.pos, spec.axis, spec.sign);
      cubie.stickers.forEach((sticker) => {
        sticker.dir = rotateVec(sticker.dir, spec.axis, spec.sign);
      });
    });
  }

  function faceGrid(cube, face) {
    const normal = FACE_NORMALS[face];
    const grid = Array.from({ length: 9 }, () => "");
    cube.forEach((cubie) => {
      const sticker = cubie.stickers.find((item) => sameVec(item.dir, normal));
      if (!sticker) return;
      const [x, y, z] = cubie.pos;
      let row = 0;
      let col = 0;
      if (face === "F") { row = 1 - y; col = x + 1; }
      if (face === "B") { row = 1 - y; col = 1 - x; }
      if (face === "R") { row = 1 - y; col = 1 - z; }
      if (face === "L") { row = 1 - y; col = z + 1; }
      if (face === "U") { row = z + 1; col = x + 1; }
      if (face === "D") { row = 1 - z; col = x + 1; }
      grid[row * 3 + col] = sticker.face;
    });
    return grid;
  }

  function isSolved(cube) {
    return FACE_NAMES.every((face) => {
      const grid = faceGrid(cube, face);
      return grid.every((value) => value === face);
    });
  }

  function cancelSolutionStack(stack, move) {
    const inverse = inverseMove(move);
    if (stack[stack.length - 1] === inverse) stack.pop();
    else stack.push(inverse);
  }

  function scoreFor(state) {
    const elapsedSeconds = Math.floor((Date.now() - state.startedAt) / 1000);
    return Math.max(100, Math.round(6000 - state.moves * 85 - elapsedSeconds * 4 + Math.max(0, state.scrambleLength - 20) * 25));
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function pointerHitFromEvent(event) {
    const sticker = event.target?.closest?.(".rubiks-cubie-sticker");
    if (sticker) {
      return {
        face: sticker.dataset.surface || "",
        row: Number(sticker.dataset.row || -1),
        col: Number(sticker.dataset.col || -1),
      };
    }
    return { face: "", row: -1, col: -1 };
  }

  function stickerCenter(face, row, col) {
    const sticker = document.querySelector(`.rubiks-cubie-sticker[data-surface="${face}"][data-row="${row}"][data-col="${col}"]`);
    if (!sticker) return null;
    const rect = sticker.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }

  function vectorBetween(a, b) {
    if (!a || !b) return null;
    return { x: b.x - a.x, y: b.y - a.y };
  }

  function screenBasisForFace(face, row, col) {
    const center = stickerCenter(face, row, col);
    const colForward = vectorBetween(center, stickerCenter(face, row, Math.min(2, col + 1)));
    const colBackward = vectorBetween(stickerCenter(face, row, Math.max(0, col - 1)), center);
    const rowForward = vectorBetween(center, stickerCenter(face, Math.min(2, row + 1), col));
    const rowBackward = vectorBetween(stickerCenter(face, Math.max(0, row - 1), col), center);
    const colVec = colForward && (colForward.x || colForward.y) ? colForward : colBackward;
    const rowVec = rowForward && (rowForward.x || rowForward.y) ? rowForward : rowBackward;
    return {
      col: colVec || { x: 1, y: 0 },
      row: rowVec || { x: 0, y: 1 },
    };
  }

  function unit(vec) {
    const length = Math.hypot(Number(vec?.x || 0), Number(vec?.y || 0)) || 1;
    return { x: Number(vec?.x || 0) / length, y: Number(vec?.y || 0) / length };
  }

  function gestureFromScreenDrag(pointer, dx, dy) {
    const colUnit = unit(pointer?.basis?.col || { x: 1, y: 0 });
    const rowUnit = unit(pointer?.basis?.row || { x: 0, y: 1 });
    const colDot = dx * colUnit.x + dy * colUnit.y;
    const rowDot = dx * rowUnit.x + dy * rowUnit.y;
    if (Math.abs(colDot) >= Math.abs(rowDot)) {
      return { orientation: "row", direction: colDot >= 0 ? 1 : -1, vector: colUnit };
    }
    return { orientation: "col", direction: rowDot >= 0 ? 1 : -1, vector: rowUnit };
  }

  function layerMoveFromFaceGesture(face, row, col, orientation, direction) {
    if (!FACE_AXES[face]) return "";
    if (orientation === "row") {
      if (face === "U") return layerMove("z", row - 1, direction < 0 ? 1 : -1);
      if (face === "D") return layerMove("z", 1 - row, direction > 0 ? 1 : -1);
      return layerMove("y", 1 - row, direction > 0 ? 1 : -1);
    }
    if (face === "F") return layerMove("x", col - 1, direction > 0 ? 1 : -1);
    if (face === "B") return layerMove("x", 1 - col, direction < 0 ? 1 : -1);
    if (face === "R") return layerMove("z", 1 - col, direction < 0 ? 1 : -1);
    if (face === "L") return layerMove("z", col - 1, direction > 0 ? 1 : -1);
    return layerMove("x", col - 1, direction > 0 ? 1 : -1);
  }

  function dragAnimationFromGesture(face, row, col, gesture) {
    const distance = 44;
    const vector = unit(gesture?.vector || (gesture?.orientation === "row" ? { x: 1, y: 0 } : { x: 0, y: 1 }));
    const direction = Number(gesture?.direction || 1);
    const offsetX = vector.x * direction * distance;
    const offsetY = vector.y * direction * distance;
    return {
      face,
      row,
      col,
      orientation: gesture?.orientation || "row",
      direction,
      invertCubeAngle: gesture?.orientation === "col",
      offsetX: `${offsetX}px`,
      offsetY: `${offsetY}px`,
      rotateX: gesture?.orientation === "row" ? "0deg" : `${direction > 0 ? -54 : 54}deg`,
      rotateY: gesture?.orientation === "row" ? `${direction > 0 ? 54 : -54}deg` : "0deg",
    };
  }

  function layerCubeAnimation(animation, move) {
    const spec = moveSpec(move);
    if (!spec) return animation;
    const visualSign = animation?.invertCubeAngle ? -spec.sign : spec.sign;
    return {
      ...animation,
      axis: spec.axis,
      layer: spec.layer,
      sign: spec.sign,
      cubeAxisX: spec.axis === "x" ? 1 : 0,
      cubeAxisY: spec.axis === "y" ? 1 : 0,
      cubeAxisZ: spec.axis === "z" ? 1 : 0,
      cubeAngle: `${visualSign > 0 ? 90 : -90}deg`,
    };
  }

  function hintAnimationForMove(move) {
    const spec = moveSpec(move);
    if (!spec) return null;
    const face = spec.face || (spec.axis === "x" ? (spec.layer > 0 ? "R" : "L") : spec.axis === "y" ? (spec.layer > 0 ? "U" : "D") : (spec.layer > 0 ? "F" : "B"));
    let animation;
    if (face === "U" || face === "D") {
      animation = {
        face,
        row: spec.layer > 0 ? 2 : 0,
        col: 1,
        orientation: "row",
        offsetX: spec.sign > 0 ? "-44px" : "44px",
        offsetY: "0px",
        rotateX: "0deg",
        rotateY: spec.sign > 0 ? "54deg" : "-54deg",
      };
    } else if (spec.axis === "x") {
      animation = {
        face,
        row: 1,
        col: spec.layer > 0 ? 2 : 0,
        orientation: "col",
        offsetX: "0px",
        offsetY: spec.sign > 0 ? "-44px" : "44px",
        rotateX: spec.sign > 0 ? "54deg" : "-54deg",
        rotateY: "0deg",
      };
    } else {
      animation = {
        face,
        row: spec.layer > 0 ? 2 : 0,
        col: 1,
        orientation: "row",
        offsetX: spec.sign > 0 ? "-44px" : "44px",
        offsetY: "0px",
        rotateX: "0deg",
        rotateY: spec.sign > 0 ? "54deg" : "-54deg",
      };
    }
    return layerCubeAnimation(animation, move);
  }

  function dragActionLabel(animation) {
    if (!animation) return "";
    if (animation.orientation === "row") {
      const rowText = animation.row === 0 ? "上面那一排" : animation.row === 1 ? "中間那一排" : "下面那一排";
      const directionText = Number(animation.direction || 1) > 0 ? "往右" : "往左";
      return `${rowText}${directionText}`;
    }
    const colText = animation.col === 0 ? "左邊那一欄" : animation.col === 1 ? "中間那一欄" : "右邊那一欄";
    const directionText = Number(animation.direction || 1) > 0 ? "往下" : "往上";
    return `${colText}${directionText}`;
  }

  function ensureRubiksInteractionStyles() {
    if (document.getElementById("rubiks-interaction-styles")) return;
    const style = document.createElement("style");
    style.id = "rubiks-interaction-styles";
    style.textContent = `
      .rubiks-stage {
        touch-action: none;
        overscroll-behavior: contain;
        user-select: none;
        -webkit-user-select: none;
        cursor: grab;
        display: flex;
        align-items: center;
        justify-content: center;
        min-height: clamp(20rem, 54vh, 34rem);
        overflow: hidden;
      }
      .rubiks-cube-3d {
        --rubiks-user-scale: 1;
        --rubiks-cubie-size-base: 3.42rem;
        --rubiks-cubie-size: calc(var(--rubiks-cubie-size-base) * var(--rubiks-user-scale));
        --rubiks-cubie-half: calc(var(--rubiks-cubie-size) / 2);
        --rubiks-cubie-step: calc(var(--rubiks-cubie-size) * 1.13);
        transform-style: preserve-3d;
        flex: 0 0 auto;
      }
      .rubiks-game-shell {
        display: grid;
        grid-template-columns: minmax(18rem, 1fr) minmax(14rem, .82fr);
        gap: .95rem;
        align-items: stretch;
        min-width: 0;
      }
      .rubiks-side-panel {
        display: grid;
        gap: .55rem;
        align-content: start;
        min-width: 0;
      }
      .rubiks-chip,
      .rubiks-next-hint {
        min-width: 0;
        max-width: 100%;
        overflow-wrap: anywhere;
        word-break: break-word;
        line-height: 1.35;
      }
      .rubiks-chip {
        display: flex;
        flex-direction: column;
        gap: .18rem;
      }
      .rubiks-chip strong,
      .rubiks-chip span,
      #rubiks-min-moves {
        display: block;
        min-width: 0;
        max-width: 100%;
        white-space: normal;
      }
      .rubiks-control-grid,
      .rubiks-view-controls {
        display: grid;
        gap: .4rem;
        min-width: 0;
      }
      .rubiks-control-grid {
        grid-template-columns: repeat(6, minmax(2.15rem, 1fr));
      }
      .rubiks-view-controls {
        grid-template-columns: repeat(4, minmax(3.1rem, 1fr));
        margin-top: .45rem;
      }
      .rubiks-control-grid .game-mini-btn,
      .rubiks-view-controls .game-mini-btn {
        min-width: 0;
        touch-action: manipulation;
        white-space: nowrap;
      }
      .rubiks-stage.is-face-drag {
        cursor: grab;
      }
      .rubiks-cubie {
        position: absolute;
        left: 50%;
        top: 50%;
        width: var(--rubiks-cubie-size);
        height: var(--rubiks-cubie-size);
        margin-left: calc(var(--rubiks-cubie-size) / -2);
        margin-top: calc(var(--rubiks-cubie-size) / -2);
        transform: var(--rubiks-cubie-transform);
        transform-style: preserve-3d;
        will-change: transform;
      }
      .rubiks-cubie.is-layer-turning {
        animation: rubiks-cubie-true-layer-turn ${TURN_ANIMATION_MS}ms cubic-bezier(.16,.88,.2,1);
      }
      .rubiks-cubie::before {
        content: "";
        position: absolute;
        inset: .12rem;
        border-radius: .62rem;
        background: linear-gradient(135deg, #111827, #020617);
        box-shadow: 0 .22rem .5rem rgba(2,6,23,.24);
        transform: translateZ(0);
      }
      .rubiks-cubie-sticker {
        position: absolute;
        inset: 0;
        border: .24rem solid #0f172a;
        border-radius: .72rem;
        background:
          radial-gradient(circle at 28% 20%, rgba(255,255,255,.62), transparent 28%),
          linear-gradient(135deg, rgba(255,255,255,.24), rgba(15,23,42,.16)),
          var(--rubiks-color);
        box-shadow:
          inset 0 -.18rem 0 rgba(15,23,42,.18),
          0 .18rem .5rem rgba(15,23,42,.24);
        box-sizing: border-box;
        cursor: grab;
        transform: var(--rubiks-sticker-transform);
        backface-visibility: hidden;
        touch-action: none;
        user-select: none;
        -webkit-user-select: none;
      }
      .rubiks-cubie-sticker:active {
        cursor: grabbing;
      }
      .rubiks-center-label {
        position: absolute;
        left: 50%;
        top: 50%;
        transform: translate(-50%, -50%);
        color: rgba(15,23,42,.28);
        font-size: clamp(.5rem, 1.8vw, 1.15rem);
        font-weight: 900;
        line-height: 1;
        white-space: nowrap;
        pointer-events: none;
      }
      @media (max-width: 760px) {
        .rubiks-game-shell {
          display: flex;
          flex-direction: column;
          gap: .65rem;
        }
        .rubiks-stage {
          min-height: clamp(17.5rem, 72vw, 24rem);
          padding: .35rem;
          border-radius: 1rem;
        }
        .rubiks-cube-3d {
          --rubiks-cubie-size-base: clamp(1.92rem, 7.8vw, 2.72rem);
          --rubiks-cubie-half: calc(var(--rubiks-cubie-size) / 2);
          --rubiks-cubie-step: calc(var(--rubiks-cubie-size) * 1.13);
        }
        .rubiks-side-panel {
          gap: .45rem;
        }
        .rubiks-chip {
          gap: .1rem;
        }
        .rubiks-chip strong {
          font-size: .76rem;
          letter-spacing: .02em;
        }
        .rubiks-chip span,
        #rubiks-min-moves,
        .rubiks-next-hint {
          font-size: .82rem;
        }
        .rubiks-control-grid {
          grid-template-columns: repeat(6, minmax(0, 1fr));
          gap: .32rem;
        }
        .rubiks-view-controls {
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: .32rem;
          margin-top: .35rem;
        }
        .rubiks-control-grid .game-mini-btn,
        .rubiks-view-controls .game-mini-btn {
          min-height: 2.1rem;
          padding: .38rem .18rem;
          font-size: .76rem;
          border-radius: .65rem;
        }
      }
      @media (max-width: 420px) {
        .rubiks-stage {
          min-height: clamp(16rem, 76vw, 20rem);
        }
        .rubiks-cube-3d {
          --rubiks-cubie-size-base: clamp(1.64rem, 7.35vw, 2.18rem);
          --rubiks-cubie-half: calc(var(--rubiks-cubie-size) / 2);
          --rubiks-cubie-step: calc(var(--rubiks-cubie-size) * 1.13);
        }
        .rubiks-cubie-sticker {
          border-width: .18rem;
          border-radius: .48rem;
        }
        .rubiks-center-label {
          font-size: .52rem;
        }
        .rubiks-chip span,
        #rubiks-min-moves,
        .rubiks-next-hint {
          font-size: .78rem;
        }
      }
      .rubiks-face-inner {
        grid-column: 1 / -1;
        grid-row: 1 / -1;
        align-self: stretch;
        justify-self: stretch;
        width: 100%;
        height: 100%;
        box-sizing: border-box;
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        grid-template-rows: repeat(3, 1fr);
        gap: .32rem;
        padding: 0;
        position: relative;
        transform-origin: center;
        will-change: transform, filter;
      }
      .rubiks-face-inner.is-turning {
        animation: rubiks-face-quarter-turn ${TURN_ANIMATION_MS}ms cubic-bezier(.2,.9,.2,1);
        filter: brightness(1.18) saturate(1.2);
      }
      .rubiks-face-inner.is-turning .rubiks-sticker {
        box-shadow:
          inset 0 0 0 1px rgba(255,255,255,.45),
          0 .22rem .7rem rgba(15,23,42,.22);
      }
      .rubiks-sticker.is-layer-turning {
        animation: rubiks-sticker-layer-slide ${TURN_ANIMATION_MS}ms cubic-bezier(.16,.88,.2,1);
        filter: brightness(1.18) saturate(1.16);
        z-index: 2;
        transform-origin: center;
        will-change: transform, opacity, filter;
      }
      .rubiks-cube-3d.is-layer-turning {
        animation: rubiks-cube-layer-emphasis ${TURN_ANIMATION_MS}ms cubic-bezier(.16,.88,.2,1);
        transform-origin: center center;
        will-change: transform;
      }
      @keyframes rubiks-face-quarter-turn {
        0% { transform: rotate(var(--rubiks-turn-angle, 90deg)) scale(.92); }
        62% { transform: rotate(-6deg) scale(1.025); }
        100% { transform: rotate(0deg) scale(1); }
      }
      @keyframes rubiks-sticker-layer-slide {
        0% {
          opacity: 1;
          transform: translate(0, 0) rotateX(0deg) rotateY(0deg) scale(1);
        }
        68% {
          opacity: .98;
          transform:
            translate(calc(var(--rubiks-layer-offset-x, 0) * .78), calc(var(--rubiks-layer-offset-y, 0) * .78))
            rotateX(calc(var(--rubiks-layer-rotate-x, 0deg) * .78))
            rotateY(calc(var(--rubiks-layer-rotate-y, 0deg) * .78))
            scale(1.035);
        }
        100% {
          opacity: .82;
          transform:
            translate(var(--rubiks-layer-offset-x, 0), var(--rubiks-layer-offset-y, 0))
            rotateX(var(--rubiks-layer-rotate-x, 0deg))
            rotateY(var(--rubiks-layer-rotate-y, 0deg))
          scale(.94);
        }
      }
      @keyframes rubiks-cube-layer-emphasis {
        0% {
          transform: var(--rubiks-view-transform) scale(1);
        }
        58% {
          transform: var(--rubiks-view-transform) translateZ(.18rem) scale(1.01);
        }
        100% {
          transform: var(--rubiks-view-transform) scale(1);
        }
      }
      @keyframes rubiks-cubie-true-layer-turn {
        0% {
          transform:
            rotate3d(var(--rubiks-layer-axis-x, 0), var(--rubiks-layer-axis-y, 1), var(--rubiks-layer-axis-z, 0), 0deg)
            var(--rubiks-cubie-transform);
        }
        100% {
          transform:
            rotate3d(var(--rubiks-layer-axis-x, 0), var(--rubiks-layer-axis-y, 1), var(--rubiks-layer-axis-z, 0), var(--rubiks-layer-cube-angle, 90deg))
            var(--rubiks-cubie-transform);
        }
      }
    `;
    document.head.appendChild(style);
  }

  window.registerHackmeLocalGameModule("rubiks_cube", {
    mount(api) {
      ensureRubiksInteractionStyles();
      makeCtx(api, "3D 魔術方塊");
      const state = {
        cube: createSolvedCube(),
        active: false,
        solved: true,
        startedAt: 0,
        moves: 0,
        score: 0,
        scrambleLength: 24,
        solutionStack: [],
        solverPending: false,
        solverError: "",
        solverSolution: null,
        solverRawSolution: [],
        solverHalfTurnLength: null,
        solverQuarterTurnLength: null,
        solverSeq: 0,
        solverHintsUsed: 0,
        solverHintLimit: MAX_SOLVER_HINTS_PER_SCRAMBLE,
        viewX: -27,
        viewY: -34,
        viewScale: 1,
        pointer: null,
        activePointers: new Map(),
        pinch: null,
        turnAnimation: null,
        turnTimer: null,
        dailyChallenge: null,
      };

      api.root.innerHTML = `
        <div class="rubiks-game-shell">
          <div class="rubiks-stage" tabindex="0" aria-label="3D 魔術方塊，可拖曳旋轉視角">
            <div class="rubiks-cube-3d" aria-hidden="true"></div>
          </div>
          <div class="rubiks-side-panel">
            <div class="rubiks-chip"><strong>目標</strong><span>用六面轉動把每面恢復同色。</span></div>
            <div class="rubiks-chip"><strong>操作</strong><span>手機用手指按住色塊往想轉的方向滑；空白處滑動可轉視角。</span></div>
            <div class="rubiks-chip"><strong>Solver</strong><span id="rubiks-min-moves">Solver：尚未計算。</span></div>
            <div class="rubiks-next-hint" id="rubiks-next-hint">按「打亂」開始。</div>
          </div>
        </div>
      `;
      const stage = api.root.querySelector(".rubiks-stage");
      const cubeEl = api.root.querySelector(".rubiks-cube-3d");
      const hintEl = api.root.querySelector("#rubiks-next-hint");
      const minMovesEl = api.root.querySelector("#rubiks-min-moves");

      const renderActions = () => api.setActions(`
        <button class="btn game-mini-btn btn-primary" type="button" data-action="new">打亂</button>
        <button class="btn game-mini-btn" type="button" data-action="hint">提示</button>
        <button class="btn game-mini-btn" type="button" data-action="solve">自動還原</button>
        <button class="btn game-mini-btn" type="button" data-action="reset">重置</button>
      `);
      const renderControls = () => {
        const moves = ["U", "U'", "D", "D'", "F", "F'", "B", "B'", "R", "R'", "L", "L'"];
        api.setControls(`
          <div class="rubiks-control-grid">
            ${moves.map((move) => `<button class="btn game-mini-btn" type="button" data-rubiks-move="${move}">${move}</button>`).join("")}
          </div>
          <div class="rubiks-view-controls">
            <button class="btn game-mini-btn" type="button" data-rubiks-view="left">視角左</button>
            <button class="btn game-mini-btn" type="button" data-rubiks-view="right">視角右</button>
            <button class="btn game-mini-btn" type="button" data-rubiks-view="up">視角上</button>
            <button class="btn game-mini-btn" type="button" data-rubiks-view="down">視角下</button>
          </div>
        `);
      };
      const statusText = () => {
        if (state.solved && state.score > 0) return `完成 · ${state.moves} 步 · 分數 ${state.score}`;
        if (state.active) {
          let solverText = "Solver 尚未計算";
          const hintsLeft = Math.max(0, state.solverHintLimit - state.solverHintsUsed);
          if (state.solverPending) solverText = "Solver 計算中";
          else if (state.solverError) solverText = `Solver：${state.solverError}`;
          else if (Array.isArray(state.solverSolution)) solverText = `Solver 剩 ${state.solverSolution.length} 步 · 提示 ${hintsLeft}/${state.solverHintLimit}`;
          else if (state.solutionStack.length) solverText = `備援提示 ${state.solutionStack.length}`;
          return `解題中 · ${state.moves} 步 · ${solverText}`;
        }
        return state.solved ? "已還原 · 按打亂開始新題。" : `暫停 · ${state.moves} 步`;
      };
      const cubeToFacelets = (cube) => {
        const cells = Object.fromEntries(KOCIEMBA_FACE_ORDER.map((face) => [face, Array(9).fill("?")]));
        cube.forEach((cubie) => {
          cubie.stickers.forEach((sticker) => {
            const surface = faceFromDir(sticker.dir);
            const cell = faceCellFromPos(surface, cubie.pos);
            if (cells[surface] && cell.row >= 0 && cell.row < 3 && cell.col >= 0 && cell.col < 3) {
              cells[surface][cell.row * 3 + cell.col] = sticker.face;
            }
          });
        });
        return KOCIEMBA_FACE_ORDER.map((face) => cells[face].join("")).join("");
      };
      const referenceMovesText = () => {
        if (state.solverPending) return "Solver：計算中，請稍候。";
        if (state.solverError) return `Solver：${state.solverError}`;
        if (state.solved) return "Solver：已完成，0 步。";
        if (Array.isArray(state.solverSolution)) {
          const htm = Number.isFinite(state.solverHalfTurnLength) ? state.solverHalfTurnLength : state.solverSolution.length;
          const qtm = Number.isFinite(state.solverQuarterTurnLength) ? state.solverQuarterTurnLength : state.solverSolution.length;
          const hintsLeft = Math.max(0, state.solverHintLimit - state.solverHintsUsed);
          return `Kociemba solver：${htm} 步；實際轉動 ${qtm} 次；本局提示剩 ${hintsLeft}/${state.solverHintLimit}。`;
        }
        return "Solver：尚未計算。";
      };
      const cubieTransform = (cubie) => {
        const [x, y, z] = cubie.pos;
        return `translate3d(calc(${x} * var(--rubiks-cubie-step)), calc(${-y} * var(--rubiks-cubie-step)), calc(${z} * var(--rubiks-cubie-step)))`;
      };
      const cubieIsTurning = (cubie) => {
        const turn = state.turnAnimation;
        if (!turn?.axis) return false;
        return cubie.pos[AXIS_INDEX[turn.axis]] === turn.layer;
      };
      const cubieStickerMarkup = (cubie, sticker, stickerIndex) => {
        const surface = faceFromDir(sticker.dir);
        const cell = faceCellFromPos(surface, cubie.pos);
        const isCenter = Math.abs(cubie.pos[0]) + Math.abs(cubie.pos[1]) + Math.abs(cubie.pos[2]) === 1;
        return `
          <span
            class="rubiks-cubie-sticker"
            style="--rubiks-color:${FACE_COLORS[sticker.face] || "#111827"};--rubiks-sticker-transform:${stickerTransformForFace(surface)}"
            data-surface="${surface}"
            data-row="${cell.row}"
            data-col="${cell.col}"
            data-face="${sticker.face || ""}"
            data-sticker-index="${stickerIndex}"
          >${isCenter ? `<span class="rubiks-center-label">${FACE_LABELS[sticker.face] || ""}</span>` : ""}</span>
        `;
      };
      const cubieMarkup = (cubie, index) => {
        const turn = state.turnAnimation || {};
        return `
          <div
            class="rubiks-cubie ${cubieIsTurning(cubie) ? "is-layer-turning" : ""}"
            style="--rubiks-cubie-transform:${cubieTransform(cubie)};--rubiks-layer-axis-x:${turn.cubeAxisX ?? 0};--rubiks-layer-axis-y:${turn.cubeAxisY ?? 1};--rubiks-layer-axis-z:${turn.cubeAxisZ ?? 0};--rubiks-layer-cube-angle:${turn.cubeAngle || "0deg"}"
            data-cubie-index="${index}"
          >
            ${cubie.stickers.map((sticker, stickerIndex) => cubieStickerMarkup(cubie, sticker, stickerIndex)).join("")}
          </div>
        `;
      };
      const updateView = () => {
        const viewTransform = `rotateX(${state.viewX}deg) rotateY(${state.viewY}deg)`;
        const turn = state.turnAnimation;
        cubeEl.style.setProperty("--rubiks-user-scale", String(state.viewScale || 1));
        cubeEl.style.setProperty("--rubiks-view-transform", viewTransform);
        cubeEl.style.setProperty("--rubiks-layer-axis-x", String(turn?.cubeAxisX ?? 0));
        cubeEl.style.setProperty("--rubiks-layer-axis-y", String(turn?.cubeAxisY ?? 1));
        cubeEl.style.setProperty("--rubiks-layer-axis-z", String(turn?.cubeAxisZ ?? 0));
        cubeEl.style.setProperty("--rubiks-layer-cube-angle", turn?.cubeAngle || "0deg");
        cubeEl.classList.toggle("is-layer-turning", Boolean(turn && turn.orientation !== "face"));
        cubeEl.style.transform = viewTransform;
      };
      const render = () => {
        updateView();
        cubeEl.innerHTML = state.cube.map((cubie, index) => cubieMarkup(cubie, index)).join("");
        if (minMovesEl) minMovesEl.textContent = referenceMovesText();
        api.status(statusText());
      };
      const refreshSolver = async () => {
        const facelets = cubeToFacelets(state.cube);
        const seq = state.solverSeq + 1;
        state.solverSeq = seq;
        state.solverError = "";
        if (facelets === SOLVED_FACELETS) {
          state.solverPending = false;
          state.solverSolution = [];
          state.solverRawSolution = [];
          state.solverHalfTurnLength = 0;
          state.solverQuarterTurnLength = 0;
          render();
          return state.solverSolution;
        }
        const request = window.hackmeGameRequest || window.gameRequest;
        if (typeof request !== "function") {
          state.solverPending = false;
          state.solverError = "前端請求工具尚未載入";
          state.solverSolution = null;
          render();
          return null;
        }
        state.solverPending = true;
        render();
        try {
          const json = await request("/games/rubiks_cube/solve", {
            method: "POST",
            body: { facelets, max_depth: 24 },
          });
          if (seq !== state.solverSeq) return null;
          state.solverRawSolution = Array.isArray(json.solution) ? json.solution : [];
          state.solverSolution = Array.isArray(json.expanded_solution) ? json.expanded_solution : [];
          state.solverHalfTurnLength = Number(json.length || 0);
          state.solverQuarterTurnLength = Number(json.quarter_turn_length || state.solverSolution.length);
          state.solverError = "";
          return state.solverSolution;
        } catch (err) {
          if (seq !== state.solverSeq) return null;
          state.solverError = err?.message || "Solver 計算失敗";
          state.solverSolution = null;
          return null;
        } finally {
          if (seq === state.solverSeq) {
            state.solverPending = false;
            render();
          }
        }
      };
      const finishIfSolved = () => {
        if (!state.active || !isSolved(state.cube)) return;
        state.active = false;
        state.solved = true;
        state.score = scoreFor(state);
        hintEl.textContent = `完成：${state.moves} 步，分數 ${state.score}。`;
        if (minMovesEl) minMovesEl.textContent = referenceMovesText();
        api.status(statusText());
        api.achievement?.("solve", "魔術方塊復原", "解開一顆 3D 魔術方塊。");
        api.mission?.("solve", 1, 1, "解開一顆 3D 魔術方塊");
        api.mission?.("under-40", state.moves <= 40 ? 40 : state.moves, 40, "40 步內復原");
        api.mission?.("under-3m", Date.now() - state.startedAt, 180000, "3 分鐘內復原");
        registerScore(api, state.score, state, state.dailyChallenge?.difficulty || "standard");
      };
      const applyMove = (move, options = {}) => {
        if (!move) return;
        if (state.turnAnimation && !options.silent) return;
        if (options.silent) {
          moveCube(state.cube, move);
          render();
          finishIfSolved();
          return;
        }
        const spec = moveSpec(move);
        if (!options.silent) {
          state.active = true;
          state.solved = false;
          state.moves += 1;
          cancelSolutionStack(state.solutionStack, move);
          window.clearTimeout(state.turnTimer);
          state.turnAnimation = options.animation || layerCubeAnimation({
            face: spec?.face || "",
            angle: spec?.sign > 0 ? "90deg" : "-90deg",
            orientation: "layer",
            row: 1,
            col: 1,
          }, move);
          state.turnTimer = window.setTimeout(() => {
            moveCube(state.cube, move);
            state.turnAnimation = null;
            render();
            finishIfSolved();
            void refreshSolver();
          }, TURN_ANIMATION_MS);
          hintEl.textContent = `已幫你把${dragActionLabel(state.turnAnimation) || moveLabel(move)}。繼續拖曳其他排或欄即可轉動。`;
          api.sound?.("uiClick", { volume: 0.08 });
        }
        render();
      };
      const scramble = () => {
        const moves = ["U", "D", "F", "B", "R", "L"];
        state.cube = createSolvedCube();
        state.solutionStack = [];
        state.moves = 0;
        state.score = 0;
        state.solverSolution = null;
        state.solverRawSolution = [];
        state.solverHalfTurnLength = null;
        state.solverQuarterTurnLength = null;
        state.solverError = "";
        state.solverHintsUsed = 0;
        state.active = true;
        state.solved = false;
        state.startedAt = Date.now();
        state.dailyChallenge = api.dailyChallenge?.() || null;
        let previous = "";
        for (let i = 0; i < state.scrambleLength; i += 1) {
          let face = moves[Math.floor(Math.random() * moves.length)];
          while (face === previous) face = moves[Math.floor(Math.random() * moves.length)];
          previous = face;
          const move = Math.random() > 0.5 ? face : `${face}'`;
          moveCube(state.cube, move);
          state.solutionStack.push(inverseMove(move));
        }
        hintEl.textContent = "已打亂。可自行解題，或按提示讓 solver 直接幫你走下一步。";
        render();
        void refreshSolver();
      };
      const resetSolved = () => {
        state.cube = createSolvedCube();
        state.solutionStack = [];
        state.moves = 0;
        state.score = 0;
        state.solverSolution = [];
        state.solverRawSolution = [];
        state.solverHalfTurnLength = 0;
        state.solverQuarterTurnLength = 0;
        state.solverError = "";
        state.solverHintsUsed = 0;
        state.active = false;
        state.solved = true;
        state.startedAt = 0;
        hintEl.textContent = "已重置為完成狀態。";
        render();
      };
      const autoSolve = async () => {
        let stack = Array.isArray(state.solverSolution) ? state.solverSolution.slice() : null;
        if (!stack?.length) stack = await refreshSolver();
        if (!stack?.length) {
          hintEl.textContent = state.solverError || "沒有可用 solver 解題步驟。";
          return;
        }
        stack.forEach((move) => moveCube(state.cube, move));
        state.moves += stack.length;
        state.active = false;
        state.solved = true;
        state.score = Math.max(50, scoreFor(state) - 500);
        state.solverSolution = [];
        state.solverRawSolution = [];
        state.solverHalfTurnLength = 0;
        state.solverQuarterTurnLength = 0;
        hintEl.textContent = `Solver 自動還原完成，實際轉動 ${stack.length} 次。`;
        render();
        void refreshSolver();
      };
      const showHint = async () => {
        if (state.turnAnimation) return;
        if (state.solved) {
          hintEl.textContent = "已經完成，不需要提示。";
          return;
        }
        if (state.solverHintsUsed >= state.solverHintLimit) {
          hintEl.textContent = `本局 ${state.solverHintLimit} 次提示已用完，請自己完成或使用「自動還原」。`;
          render();
          return;
        }
        let solverMoves = Array.isArray(state.solverSolution) ? state.solverSolution : null;
        if (!solverMoves?.length) solverMoves = await refreshSolver();
        const move = solverMoves?.[0] || state.solutionStack[state.solutionStack.length - 1] || "";
        if (!move) {
          hintEl.textContent = state.solverError || "目前沒有 solver 提示，可能已經接近完成。";
          return;
        }
        if (Array.isArray(state.solverSolution) && state.solverSolution[0] === move) {
          state.solverSolution = state.solverSolution.slice(1);
          if (Number.isFinite(state.solverQuarterTurnLength)) {
            state.solverQuarterTurnLength = Math.max(0, state.solverQuarterTurnLength - 1);
          }
        }
        state.solverHintsUsed += 1;
        hintEl.textContent = `Solver 直接幫你把${moveLabel(move)}。本局提示剩 ${Math.max(0, state.solverHintLimit - state.solverHintsUsed)} 次。`;
        applyMove(move, { animation: hintAnimationForMove(move) });
      };
      const rotateView = (dir) => {
        if (dir === "left") state.viewY -= 18;
        if (dir === "right") state.viewY += 18;
        if (dir === "up") state.viewX -= 14;
        if (dir === "down") state.viewX += 14;
        state.viewX = Math.max(-75, Math.min(55, state.viewX));
        updateView();
      };
      const rememberPointer = (event) => {
        state.activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      };
      const forgetPointer = (event) => {
        state.activePointers.delete(event.pointerId);
      };
      const pointerPair = () => Array.from(state.activePointers.values()).slice(0, 2);
      const pointerDistance = (points) => {
        if (points.length < 2) return 0;
        return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
      };
      const startPinchZoom = () => {
        const distance = pointerDistance(pointerPair());
        if (distance > 0) state.pinch = { distance, scale: state.viewScale || 1 };
      };
      const updatePinchZoom = (event) => {
        rememberPointer(event);
        if (!state.pinch || state.activePointers.size < 2) return false;
        const distance = pointerDistance(pointerPair());
        if (!distance) return false;
        state.viewScale = Math.max(0.72, Math.min(1.55, state.pinch.scale * distance / state.pinch.distance));
        updateView();
        return true;
      };

      stage.addEventListener("wheel", (event) => {
        event.preventDefault();
        const direction = event.deltaY > 0 ? -1 : 1;
        state.viewScale = Math.max(0.72, Math.min(1.55, (state.viewScale || 1) + direction * 0.08));
        updateView();
      }, { passive: false });
      stage.addEventListener("pointerdown", (event) => {
        rememberPointer(event);
        if (state.activePointers.size >= 2) {
          state.pointer = null;
          startPinchZoom();
          stage.classList.remove("is-face-drag");
          stage.setPointerCapture?.(event.pointerId);
          event.preventDefault?.();
          return;
        }
        const hit = pointerHitFromEvent(event);
        state.pointer = {
          x: event.clientX,
          y: event.clientY,
          viewX: state.viewX,
          viewY: state.viewY,
        face: hit.face,
        row: hit.row,
        col: hit.col,
        basis: hit.face ? screenBasisForFace(hit.face, hit.row, hit.col) : null,
        turnX: event.clientX,
        turnY: event.clientY,
        lastGesture: null,
        nextTurnAt: 0,
        mode: "pending",
      };
        stage.classList.toggle("is-face-drag", Boolean(state.pointer.face));
        stage.setPointerCapture?.(event.pointerId);
        event.preventDefault?.();
      });
      stage.addEventListener("pointermove", (event) => {
        if (state.activePointers.has(event.pointerId)) rememberPointer(event);
        if (state.pinch && updatePinchZoom(event)) {
          event.preventDefault?.();
          return;
        }
        if (!state.pointer) return;
        const dx = event.clientX - state.pointer.x;
        const dy = event.clientY - state.pointer.y;
        const distance = Math.hypot(dx, dy);
      if (state.pointer.face) {
        const turnDx = event.clientX - (state.pointer.turnX ?? state.pointer.x);
        const turnDy = event.clientY - (state.pointer.turnY ?? state.pointer.y);
        const turnDistance = Math.hypot(turnDx, turnDy);
        const readyAt = state.pointer.nextTurnAt || 0;
        if (turnDistance > 18 && Date.now() >= readyAt) {
          const gesture = gestureFromScreenDrag(state.pointer, turnDx, turnDy);
          const lastGesture = state.pointer.lastGesture;
          const isReturnTurn = !lastGesture
            || (gesture.orientation === lastGesture.orientation && gesture.direction === -lastGesture.direction);
          if (isReturnTurn) {
            state.pointer.mode = "turn";
            state.pointer.lastGesture = gesture;
            state.pointer.turnX = event.clientX;
            state.pointer.turnY = event.clientY;
            state.pointer.nextTurnAt = Date.now() + TURN_ANIMATION_MS + 40;
            const move = layerMoveFromFaceGesture(
              state.pointer.face,
              state.pointer.row,
              state.pointer.col,
              gesture.orientation,
              gesture.direction,
            );
            const animation = layerCubeAnimation(
              dragAnimationFromGesture(state.pointer.face, state.pointer.row, state.pointer.col, gesture),
              move,
            );
            applyMove(move, { animation });
          }
        }
        event.preventDefault?.();
        return;
      }
        if (!state.pointer.face && distance > 4) state.pointer.mode = "view";
        if (state.pointer.mode === "view") {
          state.viewY = state.pointer.viewY + dx * 0.35;
          state.viewX = Math.max(-75, Math.min(55, state.pointer.viewX - dy * 0.28));
          updateView();
        }
        event.preventDefault?.();
      });
      stage.addEventListener("pointerup", (event) => {
        forgetPointer(event);
        if (state.activePointers.size < 2) state.pinch = null;
        if (state.pointer?.face && state.pointer.mode === "pending") {
          hintEl.textContent = "請按住某一排或某一欄並拖曳，才會轉動對應 layer。";
        }
        state.pointer = null;
        stage.classList.remove("is-face-drag");
      });
      stage.addEventListener("pointercancel", () => {
        state.activePointers.clear();
        state.pinch = null;
        state.pointer = null;
        stage.classList.remove("is-face-drag");
      });

      api.onAction = (action) => {
        if (action === "new") scramble();
        if (action === "reset") resetSolved();
        if (action === "solve") autoSolve();
        if (action === "hint") showHint();
      };
      api.onControl = (target) => {
        const move = target?.dataset?.rubiksMove || "";
        const view = target?.dataset?.rubiksView || "";
        if (move) applyMove(move);
        if (view) rotateView(view);
      };
      api.onKey = (event, pressed) => {
        if (!pressed) return;
        const key = String(event.key || "").toUpperCase();
        if (FACE_AXES[key]) {
          event.preventDefault?.();
          applyMove(event.shiftKey ? `${key}'` : key);
          return;
        }
        const viewKey = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" }[event.key];
        if (viewKey) {
          event.preventDefault?.();
          rotateView(viewKey);
        }
      };
      renderActions();
      renderControls();
      render();
      return () => {
        window.clearTimeout(state.turnTimer);
      };
    },
  });
}());
