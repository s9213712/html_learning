#!/usr/bin/env python3
"""Run the safe Exp5 restart smoke sequence from the accepted V28e baseline.

The script is intentionally conservative:
- it prints every phase before running it;
- it writes match artifacts only to an operator-selected output directory;
- it does not run full held-out validation;
- it does not expose FEN, moves, teacher PV, or per-question answers.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


BASELINE_PROFILE = (
    "fixed_depth_fianchetto_tail_castle_guard_v28e_depth3_no_null_mate_net30_"
    "fast_king_mobility4"
)
DEFAULT_STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _phase(name: str) -> None:
    print(f"\n== {name} ==", flush=True)


def _run(cmd: list[str], *, repo_root: Path, env: dict[str, str], dry_run: bool) -> None:
    print("+ " + " ".join(cmd), flush=True)
    if dry_run:
        return
    subprocess.run(cmd, cwd=repo_root, env=env, check=True)


def _existing_stockfish(path: str) -> bool:
    return bool(path) and Path(path).is_file() and os.access(path, os.X_OK)


def _match_command(
    *,
    repo_root: Path,
    profile: str,
    stockfish_path: str,
    out_dir: Path,
    label: str,
    depth_schedule: str,
    games: int,
    targeted: bool,
) -> list[str]:
    replay = out_dir / f"{label}_replay.jsonl"
    summary = out_dir / f"{label}_summary.json"
    cmd = [
        sys.executable,
        "scripts/games/chess_exp5_blockfish_match.py",
        "--profile",
        profile,
        "--stockfish-path",
        stockfish_path,
        "--stockfish-depth-schedule",
        depth_schedule,
        "--games",
        str(games),
        "--max-plies",
        "600",
        "--private-jsonl",
        str(replay),
        "--summary-json",
        str(summary),
    ]
    if targeted:
        cmd.extend(
            [
                "--opening-ids",
                "open_game,english",
                "--exp5-colors",
                "black,white",
            ]
        )
    return cmd


def _compile_targets() -> list[str]:
    return [
        "services/games/chess_nnue.py",
        "scripts/games/chess_exp5_blockfish_match.py",
        "scripts/games/chess_exp5_expanded_validation.py",
        "scripts/games/chess_exp5_failure_taxonomy.py",
        "scripts/games/chess_exp5_restart_smoke.py",
    ]


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the safe Exp5 restart smoke sequence from V28e.",
    )
    parser.add_argument("--profile", default=BASELINE_PROFILE)
    parser.add_argument(
        "--stockfish-path",
        default=os.environ.get("STOCKFISH_PATH", DEFAULT_STOCKFISH_PATH),
    )
    parser.add_argument(
        "--out-dir",
        default=f"/tmp/exp5_restart_smoke_{_timestamp()}",
        help="Directory for private smoke artifacts. Defaults to /tmp.",
    )
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="Skip focused chess architecture pytest.",
    )
    parser.add_argument(
        "--skip-targeted",
        action="store_true",
        help="Skip the targeted two-game Blockfish screen.",
    )
    parser.add_argument(
        "--run-staged",
        action="store_true",
        help="Also run the longer staged five-game Blockfish screen.",
    )
    parser.add_argument(
        "--require-stockfish",
        action="store_true",
        help="Fail instead of skipping match screens when Stockfish is missing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without running them.",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = _repo_root()
    out_dir = Path(args.out_dir)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    env["PYTHONPYCACHEPREFIX"] = str(out_dir / "pycache")

    _phase("Exp5 Restart Smoke Configuration")
    print(f"repo_root: {repo_root}", flush=True)
    print(f"profile: {args.profile}", flush=True)
    print(f"stockfish_path: {args.stockfish_path}", flush=True)
    print(f"out_dir: {out_dir}", flush=True)
    print(f"dry_run: {args.dry_run}", flush=True)

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    _phase("Static Compile")
    _run(
        [sys.executable, "-m", "py_compile", *_compile_targets()],
        repo_root=repo_root,
        env=env,
        dry_run=args.dry_run,
    )

    if not args.skip_pytest:
        _phase("Focused Chess Architecture Tests")
        _run(
            [str(repo_root / "scripts/testing/pytest_in_tmp.sh"), "-q", "tests/games/test_chess_exp5_architecture.py"],
            repo_root=repo_root,
            env=env,
            dry_run=args.dry_run,
        )

    stockfish_ok = _existing_stockfish(args.stockfish_path)
    if not stockfish_ok and not args.dry_run:
        message = (
            "Stockfish binary not found or not executable; skipping Blockfish "
            "screens. Set STOCKFISH_PATH or pass --stockfish-path."
        )
        if args.require_stockfish:
            print(message, file=sys.stderr, flush=True)
            return 2
        print(message, flush=True)
        return 0

    if not args.skip_targeted:
        _phase("Targeted Two-Game Blockfish Screen")
        _run(
            _match_command(
                repo_root=repo_root,
                profile=args.profile,
                stockfish_path=args.stockfish_path,
                out_dir=out_dir,
                label="targeted_2_game",
                depth_schedule="3,6",
                games=2,
                targeted=True,
            ),
            repo_root=repo_root,
            env=env,
            dry_run=args.dry_run,
        )

    if args.run_staged:
        _phase("Staged Five-Game Blockfish Screen")
        _run(
            _match_command(
                repo_root=repo_root,
                profile=args.profile,
                stockfish_path=args.stockfish_path,
                out_dir=out_dir,
                label="staged_5_game",
                depth_schedule="2,3,4,5,6",
                games=5,
                targeted=False,
            ),
            repo_root=repo_root,
            env=env,
            dry_run=args.dry_run,
        )

    _phase("Done")
    print(f"Artifacts: {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
