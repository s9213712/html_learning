"""GPU probing helpers shared by admin and ComfyUI resource dashboards."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


NVIDIA_SMI_CANDIDATES = (
    "/usr/bin/nvidia-smi",
    "/usr/local/bin/nvidia-smi",
    "/usr/lib/wsl/lib/nvidia-smi",
    "/usr/lib/wsl/lib/nvidia-smi.exe",
    "/mnt/c/Windows/System32/nvidia-smi.exe",
)


def find_nvidia_smi(env=None, *, which=shutil.which, candidates=None):
    env = env or os.environ
    raw_override = str(env.get("NVIDIA_SMI_PATH") or env.get("HACKME_NVIDIA_SMI_PATH") or "").strip()
    search = []
    if raw_override:
        search.append(raw_override)
    found = which("nvidia-smi")
    if found:
        search.append(found)
    search.extend(NVIDIA_SMI_CANDIDATES if candidates is None else candidates)

    seen = set()
    for item in search:
        path_text = str(item or "").strip()
        if not path_text or path_text in seen:
            continue
        seen.add(path_text)
        try:
            path = Path(path_text).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        except OSError:
            continue
    return ""
