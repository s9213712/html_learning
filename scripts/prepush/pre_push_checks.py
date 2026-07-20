#!/usr/bin/env python3
"""Thin entry point for the modular pre-push v2 gate."""
from __future__ import annotations

import pathlib
import sys

sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.prepush.runner import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
