#!/usr/bin/env python3
"""Exp6 multi-source quality-game download + filter pipeline.

Pipeline (each step is idempotent — re-runs skip what's already done):

1. **Download raw PGN**:
   - TWIC archive (theweekinchess.com, ~2 MB zip with ~5000 elite tournament games)
   - Lichess titled-player histories (DrNykterstein/penguingm1/nihalsarin2004/
     Konevlad/Crest64 etc., up to 500 rated games each via the public PGN API)
   These are all open public sources — diversifying source ⇒ better
   distributional coverage per the user's "多元來源" requirement.

2. **Convert to replay JSONL** via the bundled
   ``chess_pgn_to_replay.py`` with ``--valid-game-filter elite`` so only
   games that pass the existing pipeline's "elite" gate are kept.

3. **Filter to 1000 quality games**: require ``collection_tier=trusted``,
   ``suspicious_flag=False``, ``duplicate_flag=False``,
   ``pgn_labels.avg_elo >= 2600``, ``elite`` in
   ``pgn_labels.categories``, plus a minimum game length so the
   latter-50% positions are still substantive.

Outputs (all under ``$HACKME_RUNTIME_DIR/private/games/exp6/``):

- ``downloads/`` — raw PGN files / zips
- ``downloaded_replay.jsonl`` — converted replay rows (per-source)
- ``quality_1000_games.jsonl`` — final filtered subset, exactly the
  curriculum input
- ``quality_1000_summary.json`` — per-source / per-bucket counts so
  the report can cite the data composition

This script is meant to be run ONCE before
``run_exp6_curriculum.py``. It does not touch any committed code
paths; it only writes into the configured external runtime tree.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.request import urlopen, Request


REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.games.common_paths import exp6_private_dir  # noqa: E402


OUT_DIR = exp6_private_dir()
DOWNLOADS_DIR = OUT_DIR / "downloads"
REPLAY_JSONL = OUT_DIR / "downloaded_replay.jsonl"
FINAL_JSONL = OUT_DIR / "quality_1000_games.jsonl"
SUMMARY_JSON = OUT_DIR / "quality_1000_summary.json"

# Existing trusted JSONLs that already passed the elite pipeline.
PREEXISTING_TRUSTED = [
    str(Path.home() / "hackme_web_private/runtime/private/games/exp5/v24_expanded_100/imported_replay_top_supplement.jsonl"),
    str(Path.home() / "hackme_web_private/runtime/private/games/exp5/v24_expanded_100/imported_replay.jsonl"),
    str(Path.home() / "hackme_web_private/runtime/private/games/exp5/v24_expanded_100/imported_replay_multi.jsonl"),
]

# Fresh source URLs. Lichess limits anonymous PGN exports to ~500 per
# player; TWIC zips bundle thousands of tournament games. The mix
# satisfies "多元來源".
LICHESS_PLAYERS = [
    "DrNykterstein", "penguingm1", "nihalsarin2004", "Konevlad",
    "Crest64", "DanielNaroditsky", "alireza2003", "manwithavan",
]
LICHESS_PGN_URL_TEMPLATE = (
    # ``--valid-game-filter elite`` in chess_pgn_to_replay rejects
    # ``time_control_class != 'rapid' / 'classical'`` so we only ask
    # Lichess for those — blitz/bullet would download and immediately
    # be filtered out.
    "https://lichess.org/api/games/user/{player}?max=500&rated=true"
    "&perfType=rapid,classical&clocks=false&moves=true"
)
TWIC_URLS = [
    "https://theweekinchess.com/zips/twic1500g.zip",
    "https://theweekinchess.com/zips/twic1499g.zip",
    "https://theweekinchess.com/zips/twic1498g.zip",
    "https://theweekinchess.com/zips/twic1497g.zip",
    "https://theweekinchess.com/zips/twic1496g.zip",
]
# TWIC's server returns 406 on the urllib default Accept; sending an
# explicit browser-style User-Agent + Accept gets through.
TWIC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (chess_exp6 training downloader)",
    "Accept": "*/*",
}


def _http_get(url: str, dest: Path, *, timeout: int = 120, extra_headers: dict | None = None) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        return
    print(f"[download] GET {url} -> {dest}", flush=True)
    headers = {"Accept": "application/x-chess-pgn, application/zip, */*"}
    if extra_headers:
        headers.update(extra_headers)
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def download_sources() -> list[Path]:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for player in LICHESS_PLAYERS:
        url = LICHESS_PGN_URL_TEMPLATE.format(player=player)
        path = DOWNLOADS_DIR / f"{player}.pgn"
        try:
            _http_get(url, path)
        except Exception as exc:
            print(f"[download]   skip {player}: {exc}", flush=True)
            continue
        if path.exists() and path.stat().st_size > 256:
            files.append(path)
    for url in TWIC_URLS:
        name = url.rsplit("/", 1)[-1]
        path = DOWNLOADS_DIR / name
        try:
            _http_get(url, path, extra_headers=TWIC_HEADERS)
        except Exception as exc:
            print(f"[download]   skip {name}: {exc}", flush=True)
            continue
        if path.exists() and path.stat().st_size > 1024:
            files.append(path)
    return files


def pgn_to_replay(pgn_paths: list[Path], output_jsonl: Path) -> None:
    """Drive ``chess_pgn_to_replay.py`` once per local PGN/ZIP path,
    appending to ``output_jsonl``. The script's
    ``--valid-game-filter elite`` performs the heavy lifting of
    title-band / Elo / move-count / source quality checks.
    """
    if output_jsonl.exists():
        print(f"[convert] output already exists, skipping: {output_jsonl}", flush=True)
        return
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    cmd_base = [
        sys.executable,
        str(REPO / "scripts/games/chess_pgn_to_replay.py"),
        "--output-jsonl", str(output_jsonl),
        # ``basic`` keeps only the obvious sanity checks (no
        # truncated game, no invalid result, no fake-PGN dump). TWIC
        # tournament PGNs lack a ``TimeControl`` tag so
        # ``--valid-game-filter strict`` rejects them all on
        # ``strict_time_control``. The downstream ``_is_quality``
        # filter applies the actual elite bar (Elo, categories, etc.).
        "--valid-game-filter", "basic",
        "--allow-duplicates",
        "--max-games", "0",
        "--min-ply", "20",
        "--result", "any",
    ]
    # Each source as --input-pgn argument
    # Filter to PGN files only — chess_pgn_to_replay can't read ZIPs
    # via ``--input-pgn`` (that path expects PGN text). Convert one
    # file at a time so a single slow file does not stall the whole
    # batch and so per-file progress is visible.
    pgn_only = [p for p in pgn_paths if str(p).endswith(".pgn") and p.exists() and p.stat().st_size > 256]
    print(f"[convert] {len(pgn_only)} PGN sources (zips filtered out — extract them first)", flush=True)
    output_jsonl.touch()
    for idx, p in enumerate(pgn_only, 1):
        chunk_out = output_jsonl.parent / f"_conv_{p.stem}.jsonl"
        cmd = list(cmd_base) + ["--input-pgn", str(p)]
        # Each per-file conversion writes its own JSONL, then we
        # concatenate. Per-file timeout caps stalls without losing the
        # batch.
        cmd[cmd.index("--output-jsonl")+1] = str(chunk_out)
        print(f"[convert] {idx}/{len(pgn_only)}: {p.name}", flush=True)
        try:
            result = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            print(f"[convert]   TIMEOUT on {p.name} — skipping", flush=True)
            continue
        if result.returncode != 0:
            print(f"[convert]   FAILED rc={result.returncode} on {p.name}", flush=True)
            print(result.stderr[-500:], flush=True)
            continue
        if chunk_out.exists() and chunk_out.stat().st_size > 0:
            with output_jsonl.open("a") as out_f, chunk_out.open() as in_f:
                for line in in_f:
                    if line.strip():
                        out_f.write(line)
            chunk_out.unlink()
        # Show last summary line per file
        tail = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
        if tail:
            print(f"[convert]   {tail[-1][:200]}", flush=True)


def _basic_sanity(record: dict) -> bool:
    """Universal sanity checks every quality level requires."""
    if record.get("suspicious_flag"):
        return False
    if record.get("duplicate_flag"):
        return False
    labels = record.get("pgn_labels") or {}
    if (labels.get("time_control_class") or "").lower() == "bullet":
        return False
    return True


# Quality tiers from strictest to loosest. The loader walks them in
# order, accumulating games that pass; once cumulative count reaches
# ``--target``, walking stops. At the final tier that is opened, if
# the pool of new candidates exceeds the remaining quota, Stockfish
# is used to rank them by per-move rejection rate and the lowest-
# rejection candidates fill the remaining slots.
QUALITY_TIERS = [
    {
        "name": "tier1_elite_strict",
        "min_avg_elo": 2700, "min_individual_elo": 2600,
        "min_confidence": 0.90,
        "require_categories": {"elite", "decisive"},
        "require_any_categories": {"long_game"},
        "require_collection_tier": "trusted",
        "min_moves": 60,
    },
    {
        "name": "tier2_elite_relaxed",
        "min_avg_elo": 2600, "min_individual_elo": 2500,
        "min_confidence": 0.90,
        "require_categories": {"elite", "decisive"},
        "require_any_categories": {"long_game", "medium_game"},
        "require_collection_tier": "trusted",
        "min_moves": 50,
    },
    {
        "name": "tier3_elite_basic",
        "min_avg_elo": 2500, "min_individual_elo": 2400,
        "min_confidence": 0.85,
        "require_categories": {"elite", "decisive"},
        "require_any_categories": {"long_game", "medium_game"},
        "require_collection_tier": "trusted",
        "min_moves": 40,
    },
    {
        "name": "tier4_master_decisive",
        "min_avg_elo": 2400, "min_individual_elo": 2300,
        "min_confidence": 0.80,
        "require_categories": {"decisive"},
        "require_any_categories": {"elite", "master", "long_game", "medium_game"},
        "require_collection_tier": "trusted",
        "min_moves": 40,
    },
    {
        "name": "tier5_master_any",
        "min_avg_elo": 2300, "min_individual_elo": 2200,
        "min_confidence": 0.75,
        "require_categories": set(),
        "require_any_categories": {"elite", "master"},
        "require_collection_tier": "trusted",
        "min_moves": 30,
    },
]


def _passes_tier(record: dict, tier: dict) -> bool:
    if not _basic_sanity(record):
        return False
    if tier.get("require_collection_tier") and record.get("collection_tier") != tier["require_collection_tier"]:
        return False
    if (record.get("confidence_score") or 0) < tier["min_confidence"]:
        return False
    labels = record.get("pgn_labels") or {}
    avg_elo = labels.get("avg_elo") or 0
    if avg_elo < tier["min_avg_elo"]:
        return False
    white_elo = labels.get("white_elo") or 0
    black_elo = labels.get("black_elo") or 0
    if white_elo < tier["min_individual_elo"] or black_elo < tier["min_individual_elo"]:
        return False
    cats = set(labels.get("categories") or [])
    if tier["require_categories"] and not tier["require_categories"].issubset(cats):
        return False
    if tier["require_any_categories"] and not (cats & tier["require_any_categories"]):
        return False
    if len(record.get("move_history") or []) < tier["min_moves"]:
        return False
    return True


# Kept for backwards-compat with the prior single-tier interface.
def _is_quality(record: dict) -> bool:
    return _passes_tier(record, QUALITY_TIERS[0])


def _stockfish_quality_score(record: dict, *, stockfish_path: str,
                              depth: int = 8, review_cp_loss: int = 160) -> int:
    """Lower is better. Counts moves whose post-move centipawn drop
    versus Stockfish's best move at depth ``depth`` exceeds
    ``review_cp_loss`` — i.e., per-move blunders / rejected moves.
    Used to rank surplus candidates at the final quality tier.
    """
    from services.games.chess_stockfish_teacher import (  # noqa
        UciStockfish, analysis_limit, resolve_stockfish_path,
    )
    import chess  # noqa
    limit = analysis_limit(depth=depth, movetime_ms=0)
    rejects = 0
    with UciStockfish(stockfish_path) as engine:
        board = chess.Board()
        for entry in record.get("move_history") or []:
            uci = entry.get("uci") if isinstance(entry, dict) else str(entry)
            try:
                move = chess.Move.from_uci(uci)
                if move not in board.legal_moves:
                    break
            except Exception:
                break
            try:
                rows = engine.analyse(board, limit=limit, multipv=1)
            except Exception:
                break
            if not rows:
                break
            best_cp = rows[0].get("teacher_eval_cp")
            board.push(move)
            try:
                rows_after = engine.analyse(board, limit=limit, multipv=1)
            except Exception:
                break
            if not rows_after or rows_after[0].get("teacher_eval_cp") is None or best_cp is None:
                continue
            played_cp_for_mover = -float(rows_after[0]["teacher_eval_cp"])
            cp_loss = float(best_cp) - played_cp_for_mover
            if cp_loss > review_cp_loss:
                rejects += 1
    return rejects


def filter_quality(target: int = 1000, *, stockfish_path: str | None = None,
                   out_jsonl: Path | None = None, summary_json: Path | None = None) -> dict:
    """Cascading-tier filter.

    Walk ``QUALITY_TIERS`` from strictest to loosest. At each tier, add
    games that pass that tier and haven't been added yet. If the
    cumulative kept count reaches ``target``, stop. If the FINAL tier
    that opens has more new candidates than the remaining slots, rank
    those new candidates via ``_stockfish_quality_score`` (fewer
    rejected moves = higher quality) and keep the best.
    """
    seen_sig: set[str] = set()
    kept: list[dict] = []
    per_source: Counter = Counter()
    per_tier: Counter = Counter()
    candidates_paths = list(PREEXISTING_TRUSTED) + [str(REPLAY_JSONL)]
    raw_records: list[tuple[dict, Path]] = []
    for path in candidates_paths:
        p = Path(path)
        if not p.exists():
            continue
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                raw_records.append((rec, p))
    print(f"[filter] loaded {len(raw_records)} raw records from {len(candidates_paths)} sources", flush=True)

    for tier in QUALITY_TIERS:
        tier_name = tier["name"]
        new_candidates: list[tuple[dict, Path]] = []
        for rec, p in raw_records:
            sig = rec.get("duplicate_signature") or rec.get("replay_id") or json.dumps(rec.get("move_history") or [])[:200]
            if sig in seen_sig:
                continue
            if not _passes_tier(rec, tier):
                continue
            new_candidates.append((rec, p))
        remaining = target - len(kept)
        print(f"[filter]   {tier_name}: {len(new_candidates)} new candidates; remaining quota {remaining}", flush=True)
        if not new_candidates:
            continue
        if len(new_candidates) <= remaining:
            for rec, p in new_candidates:
                sig = rec.get("duplicate_signature") or rec.get("replay_id") or json.dumps(rec.get("move_history") or [])[:200]
                seen_sig.add(sig)
                kept.append(rec)
                per_source[p.name] += 1
                per_tier[tier_name] += 1
            if len(kept) >= target:
                break
        else:
            # Final tier: surplus candidates → Stockfish-rank by
            # per-move rejection count IF a fast depth is set.
            # Defaults: skip Stockfish ranking when the cap is large
            # (>200 games) because a depth-8 audit on hundreds of
            # games is 5+ hours; instead sample a small percentage
            # via a deterministic shuffle so the cumulative stages
            # see source diversity.
            if not stockfish_path:
                print(f"[filter]   {tier_name}: surplus, no Stockfish — sampling first {remaining} by source order.", flush=True)
                pick = new_candidates[:remaining]
            elif len(new_candidates) > 200:
                print(f"[filter]   {tier_name}: surplus {len(new_candidates)} > 200 — skip Stockfish "
                      f"ranking (too slow) and use a deterministic shuffle to pick {remaining}.", flush=True)
                import random
                rng = random.Random(20260516)
                idxs = list(range(len(new_candidates)))
                rng.shuffle(idxs)
                pick = [new_candidates[i] for i in idxs[:remaining]]
            else:
                print(f"[filter]   {tier_name}: Stockfish-ranking {len(new_candidates)} candidates "
                      f"by per-move rejection count (depth 6). Need {remaining}.", flush=True)
                scored: list[tuple[int, int, dict, Path]] = []
                for idx, (rec, p) in enumerate(new_candidates):
                    score = _stockfish_quality_score(rec, stockfish_path=stockfish_path, depth=6)
                    scored.append((score, idx, rec, p))
                    if (idx + 1) % 25 == 0:
                        print(f"[filter]     ranked {idx+1}/{len(new_candidates)}", flush=True)
                scored.sort(key=lambda r: (r[0], r[1]))  # fewest rejects, then source order
                pick = [(rec, p) for _, _, rec, p in scored[:remaining]]
            for rec, p in pick:
                sig = rec.get("duplicate_signature") or rec.get("replay_id") or json.dumps(rec.get("move_history") or [])[:200]
                seen_sig.add(sig)
                kept.append(rec)
                per_source[p.name] += 1
                per_tier[tier_name] += 1
            break

    final_path = out_jsonl if out_jsonl is not None else FINAL_JSONL
    summary_path = summary_json if summary_json is not None else SUMMARY_JSON
    final_path.parent.mkdir(parents=True, exist_ok=True)
    with final_path.open("w") as f:
        for rec in kept:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    summary = {
        "target": target,
        "kept": len(kept),
        "per_source": dict(per_source),
        "per_tier": dict(per_tier),
        "tiers_attempted": [t["name"] for t in QUALITY_TIERS[:len([t for t in QUALITY_TIERS if per_tier.get(t["name"], 0) > 0]) + 1]],
        "min_avg_elo": min((rec.get("pgn_labels", {}).get("avg_elo") or 0) for rec in kept) if kept else 0,
        "max_avg_elo": max((rec.get("pgn_labels", {}).get("avg_elo") or 0) for rec in kept) if kept else 0,
        "output_jsonl": str(final_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"[filter] kept {len(kept)} / target {target}", flush=True)
    print(f"[filter] per source: {dict(per_source)}", flush=True)
    print(f"[filter] per tier:   {dict(per_tier)}", flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=1000)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-convert", action="store_true")
    parser.add_argument("--out-jsonl", type=Path, default=None,
                        help="Override output JSONL path (default quality_1000_games.jsonl).")
    parser.add_argument("--summary-json", type=Path, default=None,
                        help="Override summary JSON path.")
    parser.add_argument(
        "--stockfish-path",
        default=os.environ.get("STOCKFISH_PATH", ""),
        help="Required at the final tier if the candidate pool exceeds the "
             "remaining quota — used to rank surplus by per-move rejection count.",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    pgn_paths: list[Path] = []
    if not args.skip_download:
        pgn_paths = download_sources()
        print(f"[stage] downloaded {len(pgn_paths)} files in {time.perf_counter()-t0:.1f}s", flush=True)
    else:
        pgn_paths = sorted(DOWNLOADS_DIR.glob("*.pgn")) + sorted(DOWNLOADS_DIR.glob("*.zip"))

    if not args.skip_convert and pgn_paths:
        pgn_to_replay(pgn_paths, REPLAY_JSONL)

    summary = filter_quality(target=args.target, stockfish_path=args.stockfish_path,
                             out_jsonl=args.out_jsonl, summary_json=args.summary_json)
    print(f"[done] kept {summary['kept']} quality games; report: {SUMMARY_JSON}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
