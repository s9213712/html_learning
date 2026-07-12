#!/usr/bin/env python3
"""Download and configure KataGo for the Go neural-network difficulty.

The script installs into the configured external runtime's ``katago/`` by default. The game backend also
auto-detects that directory, so exporting the generated env file is optional
unless a custom install path is used.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.server.runtime import default_runtime_root_path  # noqa: E402


DEFAULT_INSTALL_DIR = Path(os.environ.get("HACKME_RUNTIME_DIR") or default_runtime_root_path()) / "katago"
DEFAULT_KATAGO_VERSION = "1.16.4"
DEFAULT_MODEL_NAME = "kata1-zhizi-b40c768nbt-fdx6d"
DEFAULT_MODEL_URL = (
    "https://media.katagotraining.org/uploaded/networks/models/kata1/"
    f"{DEFAULT_MODEL_NAME}.bin.gz"
)
KATAGO_RELEASE_BASE = "https://github.com/lightvector/KataGo/releases/download"
BACKEND_ASSETS = {
    "opencl": "katago-v{version}-opencl-linux-x64.zip",
    "eigen": "katago-v{version}-eigen-linux-x64.zip",
    "eigenavx2": "katago-v{version}-eigenavx2-linux-x64.zip",
    "cuda12.1": "katago-v{version}-cuda12.1-cudnn8.9.7-linux-x64.zip",
    "cuda12.5": "katago-v{version}-cuda12.5-cudnn8.9.7-linux-x64.zip",
    "cuda12.8": "katago-v{version}-cuda12.8-cudnn9.8.0-linux-x64.zip",
}


def _say(message: str) -> None:
    print(f"[setup-katago] {message}", file=sys.stderr, flush=True)


def _asset_name(version: str, backend: str) -> str:
    try:
        template = BACKEND_ASSETS[backend]
    except KeyError as exc:
        raise ValueError(f"unsupported backend: {backend}") from exc
    return template.format(version=version)


def _default_binary_url(version: str, backend: str) -> str:
    asset = _asset_name(version, backend)
    return f"{KATAGO_RELEASE_BASE}/v{version}/{asset}"


def _ensure_supported_default_platform(binary_url: str | None) -> None:
    if binary_url:
        return
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system != "linux" or machine not in {"x86_64", "amd64"}:
        raise SystemExit("default KataGo assets currently target Linux x64; pass --binary-url for this platform")


def _download(url: str, path: Path, *, force: bool) -> str:
    if path.exists() and not force:
        _say(f"reuse {path}")
        return "exists"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    _say(f"download {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "hackme-web-katago-setup/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, tmp_path.open("wb") as output:
        shutil.copyfileobj(response, output)
    os.replace(tmp_path, path)
    return "downloaded"


def _safe_extract_zip(archive_path: Path, output_dir: Path) -> None:
    output_root = output_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (output_dir / member.filename).resolve()
            if output_root not in {target, *target.parents}:
                raise ValueError(f"unsafe zip member path: {member.filename}")
        archive.extractall(output_dir)


def _find_katago_binary(install_dir: Path) -> Path:
    candidates = [install_dir / "katago"]
    if install_dir.exists():
        candidates.extend(sorted(install_dir.rglob("katago")))
    for candidate in candidates:
        if candidate.is_file():
            mode = candidate.stat().st_mode
            candidate.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            return candidate
    raise FileNotFoundError("katago binary was not found after extraction")


def _generate_config(binary_path: Path, model_path: Path, config_path: Path, *, force: bool) -> str:
    if config_path.exists() and not force:
        _say(f"reuse {config_path}")
        return "exists"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(binary_path),
        "genconfig",
        "-model",
        str(model_path),
        "-output",
        str(config_path),
    ]
    _say("generate analysis.cfg")
    completed = subprocess.run(command, text=True, capture_output=True, timeout=180, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"KataGo genconfig failed: {detail}")
    return "generated"


def _env_lines(binary_path: Path, config_path: Path, model_path: Path, max_visits: int, timeout_seconds: int) -> list[str]:
    values = {
        "HACKME_KATAGO_BIN": str(binary_path),
        "HACKME_KATAGO_CONFIG": str(config_path),
        "HACKME_KATAGO_MODEL": str(model_path),
        "HACKME_KATAGO_MAX_VISITS": str(max_visits),
        "HACKME_KATAGO_TIMEOUT_SECONDS": str(timeout_seconds),
    }
    return [f"export {key}={shlex.quote(value)}" for key, value in values.items()]


def _write_env_file(install_dir: Path, lines: list[str]) -> Path:
    env_path = install_dir / "hackme_katago.env"
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download and configure KataGo for hackme_web Go AI.")
    parser.add_argument("--install-dir", default=str(DEFAULT_INSTALL_DIR), help="Install directory. Default: runtime/katago")
    parser.add_argument("--version", default=DEFAULT_KATAGO_VERSION, help="KataGo release version")
    parser.add_argument("--backend", default="opencl", choices=sorted(BACKEND_ASSETS), help="KataGo binary backend")
    parser.add_argument("--binary-url", default="", help="Override KataGo release asset URL")
    parser.add_argument("--model-url", default=DEFAULT_MODEL_URL, help="KataGo network .bin.gz URL")
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME, help="Local model filename without .bin.gz")
    parser.add_argument("--force", action="store_true", help="Re-download and regenerate existing files")
    parser.add_argument("--skip-config", action="store_true", help="Download files but do not run katago genconfig")
    parser.add_argument("--dry-run", action="store_true", help="Print planned URLs and paths without downloading")
    parser.add_argument("--emit-env", action="store_true", help="Also print shell export lines to stderr")
    parser.add_argument("--max-visits", type=int, default=64, help="Suggested runtime HACKME_KATAGO_MAX_VISITS")
    parser.add_argument("--timeout-seconds", type=int, default=8, help="Suggested runtime HACKME_KATAGO_TIMEOUT_SECONDS")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _ensure_supported_default_platform(args.binary_url or None)

    install_dir = Path(args.install_dir).expanduser().resolve()
    archive_path = install_dir / _asset_name(args.version, args.backend)
    binary_url = args.binary_url or _default_binary_url(args.version, args.backend)
    model_filename = args.model_name if args.model_name.endswith(".bin.gz") else f"{args.model_name}.bin.gz"
    model_path = install_dir / model_filename
    config_path = install_dir / "analysis.cfg"
    expected_binary_path = install_dir / "katago"
    env_lines = _env_lines(expected_binary_path, config_path, model_path, args.max_visits, args.timeout_seconds)

    plan = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "install_dir": str(install_dir),
        "backend": args.backend,
        "version": args.version,
        "binary_url": binary_url,
        "model_url": args.model_url,
        "binary_path": str(expected_binary_path),
        "config_path": str(config_path),
        "model_path": str(model_path),
        "env_file": str(install_dir / "hackme_katago.env"),
        "env": env_lines,
    }
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    install_dir.mkdir(parents=True, exist_ok=True)
    binary_status = _download(binary_url, archive_path, force=args.force)
    if binary_status == "downloaded" or not expected_binary_path.exists():
        _safe_extract_zip(archive_path, install_dir)
    binary_path = _find_katago_binary(install_dir)
    model_status = _download(args.model_url, model_path, force=args.force)
    config_status = "skipped"
    if not args.skip_config:
        config_status = _generate_config(binary_path, model_path, config_path, force=args.force)

    env_lines = _env_lines(binary_path, config_path, model_path, args.max_visits, args.timeout_seconds)
    env_path = _write_env_file(install_dir, env_lines)
    result = {
        **plan,
        "binary_path": str(binary_path),
        "binary_status": binary_status,
        "model_status": model_status,
        "config_status": config_status,
        "env_file": str(env_path),
        "auto_detected_by_runtime": install_dir == DEFAULT_INSTALL_DIR.resolve(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.emit_env:
        print("\n".join(env_lines), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
