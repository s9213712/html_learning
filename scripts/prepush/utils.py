from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
from pathlib import Path
from typing import Iterable


TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".css",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}


LOCAL_PATH_PATTERNS = {
    "/mnt/d": "WSL_DRIVE_PATH",
    "/mnt/c/Users": "WINDOWS_WSL_USER_PATH",
    "C:\\Users\\": "WINDOWS_USER_PATH",
    "D:\\": "WINDOWS_D_DRIVE",
    "G:\\": "WINDOWS_G_DRIVE",
    "\\\\wsl.localhost\\": "WSL_HOST_PATH",
    "html_learning_storage": "LEGACY_STORAGE_DIR",
    "hackme_web_economy_fix": "OLD_WORKTREE_NAME",
}
_HOME_MARKER = str(Path.home()).replace("\\", "/").rstrip("/")
if _HOME_MARKER and _HOME_MARKER not in {"/", "."}:
    LOCAL_PATH_PATTERNS[_HOME_MARKER] = "LOCAL_HOME_PATH"

SECRET_REDACTION = re.compile(r"([A-Za-z0-9_./+=:@-]{4})[A-Za-z0-9_./+=:@-]{8,}([A-Za-z0-9_./+=:@-]{4})")


def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 30,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def git_lines(repo_root: Path, *args: str) -> list[str]:
    result = run_command(["git", "-C", str(repo_root), *args], cwd=repo_root, timeout=30)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def is_text_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    try:
        with path.open("rb") as handle:
            chunk = handle.read(2048)
    except OSError:
        return False
    return b"\0" not in chunk


def iter_repo_text_files(repo_root: Path, paths: Iterable[str]) -> Iterable[Path]:
    seen: set[Path] = set()
    for rel in paths:
        path = (repo_root / rel).resolve()
        try:
            path.relative_to(repo_root)
        except ValueError:
            continue
        if path in seen or not is_text_file(path):
            continue
        seen.add(path)
        yield path


def relpath(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return sanitize_path(str(path))


def sanitize_path(value: str) -> str:
    safe = value
    for marker, name in LOCAL_PATH_PATTERNS.items():
        safe = safe.replace(marker, f"<{name}>")
    safe = re.sub(r"/tmp/html_learning_[A-Za-z0-9_.-]+", "/tmp/<prepush-runtime>", safe)
    safe = re.sub(r"/tmp/hackme_[A-Za-z0-9_.-]+", "/tmp/<hackme-runtime>", safe)
    return safe


def redact_secret(value: str) -> str:
    sanitized = sanitize_path(value)
    return SECRET_REDACTION.sub(lambda match: f"{match.group(1)}...REDACTED...{match.group(2)}", sanitized)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def tool_exists(name: str) -> bool:
    return shutil.which(name) is not None


def env_without_local_runtime() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("HTML_LEARNING_") and key.endswith(("_DIR", "_PATH")):
            env.pop(key, None)
    return env
