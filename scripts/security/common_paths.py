from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime

from scripts.test_artifacts import test_artifact_path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_ROOT = test_artifact_path("runtime")


def _outside_checkout(path: Path, *, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise ValueError(f"{label} must stay outside the source checkout")
    return resolved


def runtime_root() -> Path:
    raw = str(os.environ.get("HACKME_RUNTIME_DIR") or "").strip()
    if raw:
        return _outside_checkout(Path(raw), label="HACKME_RUNTIME_DIR")
    return _outside_checkout(DEFAULT_RUNTIME_ROOT, label="default runtime root")


def reports_parent_root() -> Path:
    raw = str(os.environ.get("HTML_LEARNING_REPORTS_DIR") or "").strip()
    if raw:
        return _outside_checkout(Path(raw), label="HTML_LEARNING_REPORTS_DIR")
    if str(os.environ.get("HACKME_RUNTIME_DIR") or "").strip():
        return runtime_root() / "reports"
    return test_artifact_path("reports")


def security_reports_root() -> Path:
    return reports_parent_root() / "security"


def timestamped_security_report_paths(prefix: str, *, stamp: str | None = None) -> tuple[Path, Path]:
    ts = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    root = security_reports_root()
    return root / f"{prefix}_{ts}.json", root / f"{prefix}_{ts}.md"
