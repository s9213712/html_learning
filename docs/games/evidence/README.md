# Games Evidence Data

Raw test, replay, Stockfish audit, gauntlet, and training evidence files were
moved out of the repo during the 2026-05-18 cleanup.

Archive location on this machine:

```text
/tmp/hackme_web_repo_data_archive_20260518/docs_games/evidence
```

Keep narrative development history under `docs/games/chess_debug/` and
`docs/games/archive/chess_debug/`. Do not re-add raw replay, FEN, move, JSONL,
or training evidence files to this directory. `_runtime/` subtrees are forbidden;
new run output belongs under `/tmp/hackme_web_test_artifacts` (or an explicitly
configured external test-artifact root).
