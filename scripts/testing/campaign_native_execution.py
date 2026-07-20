#!/usr/bin/env python3
"""Execute and capture one formal qualification gate without self-attestation.

The native runner in this module is the only writer of
``hackme.formal-native-execution.v1`` receipts.  It starts the supplied command
itself, measures both wall and monotonic boundaries, requires every declared
native artifact to be newly created during that execution, re-verifies the
exact clean source before and after the child, pins each artifact by
device/inode/hash, and remains alive while the qualification writer
independently derives the gate result.

There are deliberately no CLI flags for ``actual_execution``, ``simulated``,
``component_only``, PASS, skip, fallback, or expected-gap state.  A zero child
exit status only permits semantic capture to begin; it never promotes the
gate by itself.  The 60-minute rehearsal accepts only the exact supervised
rehearsal entrypoint/argument contract and a kernel-sealed projection context;
all other commands and gates remain fail-closed.  Arbitrary commands and
``python -c`` are therefore never a formal producer.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.campaign_gate_bundle import (  # noqa: E402
    GATE_RAW_SPECS,
    NATIVE_EXECUTION_RECEIPT_SCHEMA_VERSION,
    NATIVE_PRODUCER_KIND,
    protected_source_identity_digest,
)
from scripts.testing.campaign_qualification_capture import (  # noqa: E402
    FileIdentity,
    MAX_JSON_BYTES,
    MAX_NDJSON_BYTES,
    MAX_NDJSON_LINE_BYTES,
    QualificationContext,
    REHEARSAL_PROJECTION_CONTEXT_ENV,
    REHEARSAL_PROJECTION_CONTEXT_SHA256_ENV,
    _absolute_path,
    _inspect_native,
    _read_bounded_json,
    build_rehearsal_projection_context,
    capture_gate_evidence,
    encoded_rehearsal_projection_context,
    planned_capture_paths,
    project_bound_json_identity,
)
from scripts.testing.campaign_scenario_binding import (  # noqa: E402
    FORMAL_SCENARIO_BINDINGS,
    native_artifact_bundle_validation_errors,
    validate_scenario_runtime_receipt,
)
from scripts.testing.campaign_source_freeze import (  # noqa: E402
    GitSourceFreezer,
    SourceFreezeError,
)
from scripts.testing.campaign_secret_scan import (  # noqa: E402
    SecretScanConfig,
    build_sensitive_needle_inventory,
    scan_campaign_secret_files,
)
from scripts.testing.campaign_watchdog import (  # noqa: E402
    ProcessIdentityError,
    capture_process_identity,
)


NATIVE_EXECUTION_RESULT_SCHEMA_VERSION = "hackme.formal-native-execution-result.v1"
_SHA256_CHUNK_BYTES = 1024 * 1024
_ARTIFACT_CLOCK_TOLERANCE_NS = 5_000_000_000
_PROCESS_GROUP_DRAIN_SECONDS = 2.0
_ADOPTED_CHILD_DRAIN_SECONDS = 5.0
_MARKER_SCAN_MAX_NODES = 1_000_000
_MARKER_SCAN_MAX_DEPTH = 64
_DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60
_GATE_TIMEOUT_SECONDS: Mapping[str, float] = {
    "180_second_smoke_passed": 60 * 60,
    "60_minute_rehearsal_passed": 12 * 60 * 60,
}

# Only the 60-minute supervised rehearsal has an activated command contract.
# Every other gate remains fail closed.  The contract below is code, not a
# configurable registry entry: it pins the tracked supervisor entrypoint,
# exact argv grammar, sealed activation/projection channel, and the real
# supervisor/runner terminal artifacts that are independently re-opened.
_REVIEWED_NATIVE_GATE_COMMANDS: Mapping[str, Mapping[str, Any]] = {
    "60_minute_rehearsal_passed": {
        "entrypoint": str(
            (ROOT / "scripts" / "testing" / "operational_campaign_supervisor.py")
            .resolve(strict=True)
        ),
        "level": "rehearsal",
        "duration_seconds": 3600,
    },
}


class NativeExecutionError(RuntimeError):
    """The command or its native authority could not be proven."""


class _LiveSourceGuard:
    """Continuously observe the exact H0 tree, including transient writes."""

    def __init__(
        self,
        context: QualificationContext,
        source_before: Mapping[str, Any],
        *,
        scratch_root: Path,
    ) -> None:
        repo_root = Path(str(source_before.get("repo_root") or "")).resolve(strict=True)
        self.freezer = GitSourceFreezer(repo_root, scratch_root)
        try:
            baseline = self.freezer.load_baseline(context.source_authority.path)
            _require(
                baseline.get("commit") == context.commit
                and baseline.get("tracked_content_digest") == context.source_digest,
                "continuous source guard loaded another H0 authority",
            )
            protected = protected_source_identity_digest(
                str(baseline.get("protected_ignored_manifest_digest") or ""),
                str(baseline.get("protected_ignored_content_digest") or ""),
            )
            _require(
                protected == context.protected_source_digest,
                "continuous source guard loaded another protected authority",
            )
        except Exception:
            self.freezer.close()
            raise

    def verify(self) -> dict[str, Any]:
        try:
            evidence = self.freezer.lightweight_drift_check()
        except SourceFreezeError as exc:
            raise NativeExecutionError(
                f"continuous source guard failed: {exc}"
            ) from exc
        _require(
            evidence.get("verified") is True
            and evidence.get("incident") is False
            and evidence.get("monitor_failure") is False,
            "source changed during native execution",
        )
        return evidence

    def close(self) -> None:
        self.freezer.close()


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise NativeExecutionError(message)


def _validate_reviewed_command(
    *,
    gate_name: str,
    command: Sequence[str],
    cwd: Path,
) -> dict[str, Any]:
    contract = _REVIEWED_NATIVE_GATE_COMMANDS.get(gate_name)
    _require(
        isinstance(contract, Mapping),
        f"{gate_name} has no activated reviewed native command contract",
    )
    values = tuple(str(value) for value in command)
    _require(len(values) >= 3, "reviewed native command is incomplete")
    try:
        executable = Path(values[0]).expanduser().resolve(strict=True)
        expected_executable = Path(sys.executable).resolve(strict=True)
        entrypoint = Path(values[1]).expanduser().resolve(strict=True)
    except OSError as exc:
        raise NativeExecutionError(
            f"reviewed native command path is unavailable: {exc.__class__.__name__}: {exc}"
        ) from exc
    _require(
        executable == expected_executable,
        "reviewed native command must use the current exact Python executable",
    )
    _require(
        entrypoint == Path(str(contract["entrypoint"])),
        "reviewed native command entrypoint mismatch",
    )
    _require(cwd == ROOT.resolve(strict=True), "reviewed native command cwd mismatch")

    required = {
        "--campaign-root",
        "--level",
        "--duration-seconds",
        "--comfyui-python-executable",
        "--comfyui-main",
        "--comfyui-working-root",
        "--comfyui-models-root",
        "--comfyui-api-url",
        "--comfyui-port",
    }
    optional = {
        "--source-poll-seconds",
        "--comfyui-readiness-timeout-seconds",
    }
    parsed: dict[str, str] = {}
    arguments = values[2:]
    _require("--" not in arguments, "reviewed rehearsal command cannot pass runner overrides")
    index = 0
    while index < len(arguments):
        option = arguments[index]
        _require(
            option in required | optional and option not in parsed,
            f"reviewed rehearsal command option is unsupported or duplicated: {option}",
        )
        _require(index + 1 < len(arguments), f"reviewed option has no value: {option}")
        value = arguments[index + 1]
        _require(
            value and not value.startswith("--") and "\x00" not in value,
            f"reviewed option value is invalid: {option}",
        )
        parsed[option] = value
        index += 2
    _require(set(parsed) >= required, "reviewed rehearsal command misses required options")
    _require(parsed["--level"] == contract["level"], "reviewed rehearsal level mismatch")
    try:
        duration = int(parsed["--duration-seconds"])
        port = int(parsed["--comfyui-port"])
        source_poll = float(parsed.get("--source-poll-seconds", "5"))
        readiness = float(
            parsed.get("--comfyui-readiness-timeout-seconds", "300")
        )
    except ValueError as exc:
        raise NativeExecutionError("reviewed rehearsal numeric option is invalid") from exc
    _require(
        duration == int(contract["duration_seconds"]),
        "reviewed rehearsal duration must be exactly 3600 seconds",
    )
    _require(1 <= port <= 65535, "reviewed ComfyUI port is invalid")
    _require(0.25 <= source_poll <= 60.0, "reviewed source poll interval is invalid")
    _require(10.0 <= readiness <= 1800.0, "reviewed ComfyUI readiness timeout is invalid")

    root_text = parsed["--campaign-root"]
    campaign_root = Path(root_text)
    _require(
        campaign_root.is_absolute()
        and str(campaign_root) == root_text
        and campaign_root.resolve(strict=False) == campaign_root
        and Path("/tmp") in campaign_root.parents
        and not os.path.lexists(campaign_root),
        "reviewed campaign root must be a new canonical child of /tmp",
    )
    for option, expected_kind in (
        ("--comfyui-python-executable", "file"),
        ("--comfyui-main", "file"),
        ("--comfyui-working-root", "directory"),
        ("--comfyui-models-root", "directory"),
    ):
        raw = parsed[option]
        path = Path(raw)
        _require(
            path.is_absolute()
            and str(path) == raw
            and path.resolve(strict=True) == path,
            f"reviewed path is not exact/canonical: {option}",
        )
        _require(
            path.is_file() if expected_kind == "file" else path.is_dir(),
            f"reviewed path kind mismatch: {option}",
        )
    api = urlsplit(parsed["--comfyui-api-url"])
    _require(
        api.scheme == "http"
        and api.hostname in {"127.0.0.1", "localhost", "::1"}
        and api.port == port
        and api.path in {"", "/"}
        and not api.query
        and not api.fragment
        and not api.username
        and not api.password,
        "reviewed ComfyUI API URL must be the exact local managed endpoint",
    )
    return {
        "contract": "supervised_rehearsal_v1",
        "campaign_root": campaign_root,
        "entrypoint": entrypoint,
        "duration_seconds": duration,
    }


def reviewed_rehearsal_native_artifact_paths(
    campaign_root: Path,
) -> dict[str, Path]:
    """Return the one accepted 41-role native projection inventory."""

    root = Path(campaign_root).expanduser().resolve(strict=False)
    artifact_root = root / "artifacts" / "formal_native_rehearsal"
    gate = "60_minute_rehearsal_passed"
    suffixes = {
        "application/json": ".json",
        "application/x-tar": ".tar",
    }
    return {
        role: artifact_root / f"{role}{suffixes[spec.media_type]}"
        for role, spec in GATE_RAW_SPECS[gate].items()
    }


def _precise_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_precise_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _same_identity(left: FileIdentity, right: FileIdentity) -> bool:
    return left == right


def _stable_sha256(path: Path, identity: FileIdentity, *, label: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except Exception as exc:
        raise NativeExecutionError(
            f"cannot open {label}: {exc.__class__.__name__}: {exc}"
        ) from exc
    digest = hashlib.sha256()
    try:
        opened = FileIdentity.from_stat(os.fstat(descriptor))
        _require(_same_identity(opened, identity), f"{label} changed before hashing")
        while True:
            chunk = os.read(descriptor, _SHA256_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        final_opened = FileIdentity.from_stat(os.fstat(descriptor))
        _require(_same_identity(final_opened, identity), f"{label} changed while hashing")
    finally:
        os.close(descriptor)
    final_path = _inspect_native(path, label=f"{label} final readback")
    _require(_same_identity(final_path, identity), f"{label} path changed while hashing")
    return digest.hexdigest()


def _run_git(repo_root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[:1000]
        raise NativeExecutionError(
            f"git {' '.join(arguments)} failed with {completed.returncode}: {detail}"
        )
    return bytes(completed.stdout)


def _verify_live_source(
    context: QualificationContext,
    *,
    scratch_root: Path,
) -> dict[str, Any]:
    """Recompute clean Git and content authority from the live checkout."""

    authority_identity = _inspect_native(
        context.source_authority.path,
        label="native execution source authority",
    )
    authority, authority_sha, _authority_size = _read_bounded_json(
        context.source_authority.path,
        authority_identity,
        label="native execution source authority",
        maximum_bytes=MAX_JSON_BYTES,
    )
    _require(
        authority_identity == context.source_authority.file
        and authority_sha == context.source_authority.sha256,
        "source authority changed before native execution",
    )
    _require(isinstance(authority, dict), "source authority must be a JSON object")
    repo_value = str(authority.get("repo_root") or "")
    _require(repo_value, "source authority has no repo_root")
    repo_root = Path(repo_value).expanduser().resolve(strict=True)
    _require((repo_root / ".git").exists(), "source authority repo_root is not a Git checkout")

    actual_commit = _run_git(repo_root, "rev-parse", "HEAD").decode("ascii").strip().lower()
    status = _run_git(
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    diff = _run_git(repo_root, "diff", "--binary", "HEAD", "--")
    submodules = _run_git(repo_root, "submodule", "status", "--recursive")
    _require(actual_commit == context.commit, "live Git commit differs from source authority")
    _require(status == b"", "live Git worktree is not clean")
    _require(diff == b"", "live Git diff is not empty")
    dirty_submodules = [
        row for row in submodules.decode("utf-8", errors="replace").splitlines()
        if row[:1] in {"-", "+", "U"}
    ]
    _require(not dirty_submodules, "live Git submodule authority is not clean")

    try:
        freezer = GitSourceFreezer(repo_root, scratch_root)
        tracked = freezer.tracked_entries()
        tracked_digest = freezer.content_digest(tracked)
        protected = freezer.protected_ignored_entries()
        protected_digest = protected_source_identity_digest(
            freezer.untracked_manifest_digest(protected),
            freezer.untracked_content_digest(protected),
        )
    except SourceFreezeError as exc:
        raise NativeExecutionError(f"live source digest failed: {exc}") from exc
    _require(
        tracked_digest == context.source_digest,
        "live tracked source digest differs from source authority",
    )
    _require(
        protected_digest == context.protected_source_digest,
        "live protected source digest differs from source authority",
    )
    return {
        "repo_root": str(repo_root),
        "commit": actual_commit,
        "source_digest": tracked_digest,
        "protected_source_digest": protected_digest,
        "git_status_empty": True,
        "git_diff_binary_empty": True,
        "submodules_clean": True,
        "source_authority_sha256": authority_sha,
    }


def _canonical_new_path(value: Path, *, label: str) -> Path:
    candidate = Path(value)
    _require(candidate.is_absolute(), f"{label} must be absolute")
    _require(str(candidate) == os.fspath(value), f"{label} path string is not canonical")
    resolved = candidate.resolve(strict=False)
    _require(resolved == candidate, f"{label} must be an exact canonical path")
    _require(not os.path.lexists(candidate), f"{label} already exists")
    return candidate


def _parse_artifacts(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        role, separator, raw_path = str(value).partition("=")
        if not separator or not role or not raw_path or role in result:
            raise NativeExecutionError(f"invalid --artifact value: {value!r}")
        result[role] = _canonical_new_path(
            Path(raw_path),
            label=f"native artifact {role}",
        )
    return result


def _artifact_records(
    paths: Mapping[str, Path],
    *,
    started_epoch_ns: int,
    finished_epoch_ns: int,
    started_monotonic_ns: int,
    finished_monotonic_ns: int,
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    monotonic_elapsed_ns = max(0, finished_monotonic_ns - started_monotonic_ns)
    earliest = min(
        started_epoch_ns,
        finished_epoch_ns - monotonic_elapsed_ns,
    ) - _ARTIFACT_CLOCK_TOLERANCE_NS
    latest = max(
        finished_epoch_ns,
        started_epoch_ns + monotonic_elapsed_ns,
    ) + _ARTIFACT_CLOCK_TOLERANCE_NS
    for role, path in paths.items():
        identity = _inspect_native(path, label=f"native artifact {role}")
        _require(identity.uid == os.geteuid(), f"native artifact {role} owner mismatch")
        _require(
            earliest <= identity.ctime_ns <= latest,
            f"native artifact {role} was not created during the measured execution",
        )
        _require(
            identity.mtime_ns <= latest,
            f"native artifact {role} has a future modification time",
        )
        records[role] = {
            "path": str(path),
            "file_identity": identity.to_dict(),
            "sha256": _stable_sha256(
                path,
                identity,
                label=f"native artifact {role}",
            ),
        }
    return records


def _marker_active(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "none", "no"}
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _scan_forbidden_markers(value: Any, *, label: str) -> list[str]:
    active_gap_keys = {
        "skip", "skipped", "skips", "fallback", "fallbacks",
        "fallback_error", "fallback_used", "used_fallback",
        "skip_reason", "skipped_reason", "fallback_reason",
        "expected_gap", "expected_gaps", "expected_gap_reason",
        "not_run", "not_executed", "bypassed", "placeholder", "stubbed",
        "omitted",
    }
    prohibited_truthy = {"simulated", "component_only", "synthetic", "fake", "mock"}
    prohibited_modes = {
        "component_only", "expected_gap", "fake", "fallback", "mock",
        "not_run", "simulated", "simulation", "stub", "synthetic",
    }
    mode_keys = {"mode", "execution_mode", "evidence_mode", "run_mode"}
    failures: list[str] = []
    stack: list[tuple[str, Any, int]] = [(label, value, 0)]
    nodes = 0
    while stack:
        path, current, depth = stack.pop()
        nodes += 1
        _require(nodes <= _MARKER_SCAN_MAX_NODES, f"{label} marker scan exceeds node budget")
        _require(depth <= _MARKER_SCAN_MAX_DEPTH, f"{label} marker scan exceeds depth budget")
        if isinstance(current, Mapping):
            for raw_key, child in current.items():
                key = str(raw_key).strip().lower().replace("-", "_")
                child_path = f"{path}.{raw_key}"
                if key in active_gap_keys and _marker_active(child):
                    failures.append(child_path)
                if key in prohibited_truthy and _marker_active(child):
                    failures.append(child_path)
                if (
                    key in mode_keys
                    and isinstance(child, str)
                    and child.strip().lower().replace("-", "_") in prohibited_modes
                ):
                    failures.append(child_path)
                # A native report may omit this field and let its reviewed
                # semantic validator prove execution.  Once it declares the
                # field, however, only the exact JSON boolean ``true`` is
                # acceptable; strings and numeric lookalikes fail closed.
                if key == "actual_execution" and child is not True:
                    failures.append(child_path)
                stack.append((child_path, child, depth + 1))
        elif isinstance(current, list):
            for index, child in enumerate(current):
                stack.append((f"{path}[{index}]", child, depth + 1))
    return sorted(set(failures))


def _reject_forbidden_artifact_markers(
    gate_name: str,
    paths: Mapping[str, Path],
) -> None:
    failures: list[str] = []
    for role, path in paths.items():
        spec = GATE_RAW_SPECS[gate_name][role]
        identity = _inspect_native(path, label=f"native artifact {role} marker scan")
        if spec.media_type == "application/json":
            payload, _sha, _size = _read_bounded_json(
                path,
                identity,
                label=f"native artifact {role} marker scan",
                maximum_bytes=MAX_JSON_BYTES,
            )
            failures.extend(_scan_forbidden_markers(payload, label=role))
        elif spec.media_type == "application/x-ndjson":
            _require(
                identity.size <= MAX_NDJSON_BYTES,
                f"native artifact {role} marker scan exceeds NDJSON limit",
            )
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    _require(
                        len(line.encode("utf-8")) <= MAX_NDJSON_LINE_BYTES,
                        f"native artifact {role} NDJSON row exceeds marker scan limit",
                    )
                    try:
                        row = json.loads(line)
                    except Exception as exc:
                        raise NativeExecutionError(
                            f"native artifact {role} NDJSON row {line_number} is invalid: "
                            f"{exc.__class__.__name__}: {exc}"
                        ) from exc
                    failures.extend(
                        _scan_forbidden_markers(row, label=f"{role}[{line_number}]")
                    )
    _require(
        not failures,
        "native artifacts contain forbidden skip/fallback/fake markers: "
        + ", ".join(failures[:50]),
    )


def _reject_artifact_secrets(paths: Mapping[str, Path]) -> None:
    """Scan the exact native files before they can be sealed as gate evidence."""

    parents = [str(path.parent) for path in paths.values()]
    _require(bool(parents), "native artifact secret scan has no input files")
    scan_root = Path(os.path.commonpath(parents)).resolve(strict=True)
    needles = build_sensitive_needle_inventory({}, environment=os.environ)
    scan = scan_campaign_secret_files(
        SecretScanConfig(
            artifact_root=scan_root,
            needles=needles,
        ),
        tuple(paths.values()),
    )
    _require(
        scan.get("ok") is True
        and scan.get("enumeration_complete") is True
        and int(scan.get("error_count") or 0) == 0
        and int(scan.get("hit_count") or 0) == 0
        and int(scan.get("expected_file_count") or -1) == len(paths)
        and int(scan.get("files_scanned") or -1) == len(paths),
        "native artifact credential scan failed closed",
    )


def _write_private_json_once(path: Path, payload: Mapping[str, Any]) -> None:
    parent = path.parent
    info = parent.lstat()
    _require(stat.S_ISDIR(info.st_mode), "native receipt parent is not a directory")
    _require(not stat.S_ISLNK(info.st_mode), "native receipt parent is a symlink")
    _require(info.st_uid == os.geteuid(), "native receipt parent owner mismatch")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    encoded = (
        json.dumps(dict(payload), ensure_ascii=True, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise NativeExecutionError("native receipt write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _load_native_json(path: Path, *, label: str) -> dict[str, Any]:
    identity = _inspect_native(path, label=label)
    payload, _digest, _size = _read_bounded_json(
        path,
        identity,
        label=label,
        maximum_bytes=MAX_JSON_BYTES,
    )
    _require(isinstance(payload, dict), f"{label} is not a JSON object")
    return dict(payload)


def _copy_private_file_once(source: Path, destination: Path, *, label: str) -> tuple[str, int]:
    source_identity = _inspect_native(source, label=f"{label} source")
    source_sha = _stable_sha256(source, source_identity, label=f"{label} source")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    input_fd = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    output_fd = os.open(destination, flags, 0o600)
    copied = 0
    try:
        _require(
            FileIdentity.from_stat(os.fstat(input_fd)) == source_identity,
            f"{label} source changed before copy",
        )
        while True:
            chunk = os.read(input_fd, _SHA256_CHUNK_BYTES)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(output_fd, view)
                _require(written > 0, f"{label} copy made no progress")
                copied += written
                view = view[written:]
        os.fsync(output_fd)
    finally:
        os.close(input_fd)
        os.close(output_fd)
    os.chmod(destination, 0o600)
    copied_identity = _inspect_native(destination, label=f"{label} copied")
    copied_sha = _stable_sha256(
        destination,
        copied_identity,
        label=f"{label} copied",
    )
    _require(
        copied == source_identity.size
        and copied_identity.size == source_identity.size
        and copied_sha == source_sha,
        f"{label} copy identity mismatch",
    )
    return copied_sha, copied


def _verify_rehearsal_archive(
    archive_path: Path,
    bundle: Mapping[str, Any],
    *,
    scenario_id: str,
) -> None:
    inventory_value = bundle.get("member_inventory")
    _require(isinstance(inventory_value, list), f"{scenario_id} member inventory missing")
    expected: dict[str, tuple[str, int]] = {}
    for item in inventory_value:
        _require(isinstance(item, Mapping), f"{scenario_id} member inventory row invalid")
        name = str(item.get("member_path") or "")
        sha = str(item.get("sha256") or "")
        size = item.get("size_bytes")
        pure = Path(name)
        _require(
            name
            and not pure.is_absolute()
            and all(part not in {"", ".", ".."} for part in pure.parts)
            and name not in expected
            and len(sha) == 64
            and type(size) is int
            and int(size) >= 0,
            f"{scenario_id} member inventory authority invalid",
        )
        expected[name] = (sha, int(size))
    actual: dict[str, tuple[str, int]] = {}
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            for member in archive.getmembers():
                _require(
                    member.isfile()
                    and member.name in expected
                    and member.name not in actual,
                    f"{scenario_id} archive contains an unsafe/unexpected member",
                )
                handle = archive.extractfile(member)
                _require(handle is not None, f"{scenario_id} archive member cannot be reopened")
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = handle.read(_SHA256_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                actual[member.name] = (digest.hexdigest(), size)
    except (tarfile.TarError, OSError) as exc:
        raise NativeExecutionError(
            f"{scenario_id} archive reopen failed: {exc.__class__.__name__}: {exc}"
        ) from exc
    _require(actual == expected, f"{scenario_id} archive content/inventory mismatch")


def _projected_json_link(
    *,
    context: QualificationContext,
    role: str,
    native_path: Path,
    planned_path: Path,
) -> dict[str, Any]:
    projection = project_bound_json_identity(
        context=context,
        gate_name="60_minute_rehearsal_passed",
        role=role,
        native_path=native_path,
    )
    return {
        "path": str(planned_path),
        "sha256": projection["sha256"],
        "size_bytes": projection["size_bytes"],
    }


def _materialize_rehearsal_projection(
    *,
    context: QualificationContext,
    projection_context: Mapping[str, Any],
    reviewed_command: Mapping[str, Any],
    native_artifact_paths: Mapping[str, Path],
    started_at: datetime,
    started_monotonic_ns: int,
    child_finished_at: datetime,
    child_finished_monotonic_ns: int,
) -> None:
    """Project real PASS artifacts without rerunning or promoting any handler."""

    gate = "60_minute_rehearsal_passed"
    campaign_root = Path(str(reviewed_command["campaign_root"]))
    expected_paths = reviewed_rehearsal_native_artifact_paths(campaign_root)
    _require(
        dict(native_artifact_paths) == expected_paths,
        "rehearsal native artifact paths differ from the reviewed 41-role inventory",
    )
    projection_native = {
        role: Path(str(path))
        for role, path in projection_context["native_artifact_paths"].items()
    }
    _require(
        projection_native == expected_paths,
        "sealed projection native paths differ from reviewed inventory",
    )
    planned = {
        role: Path(str(path))
        for role, path in projection_context["planned_capture_paths"].items()
    }
    stage_root = campaign_root / "artifacts" / "formal_native_rehearsal"
    _require(not os.path.lexists(stage_root), "rehearsal projection root already exists")

    runner_source = campaign_root / "reports" / "operational_campaign_24h.json"
    supervisor_source = campaign_root / "artifacts" / "campaign_supervisor.json"
    runner = _load_native_json(runner_source, label="real rehearsal runner result")
    supervisor = _load_native_json(
        supervisor_source,
        label="real rehearsal supervisor result",
    )
    _require(
        runner.get("schema_version") == "hackme.campaign-operational-result/v1"
        and runner.get("ok") is True
        and runner.get("verdict") == "PASS"
        and runner.get("classification") == "PASS"
        and int(runner.get("required_active_test_seconds") or 0) == 3600
        and float(runner.get("active_test_seconds") or 0.0) >= 3600.0
        and type(runner.get("invalid_seconds")) in {int, float}
        and not isinstance(runner.get("invalid_seconds"), bool)
        and float(runner.get("invalid_seconds")) == 0.0
        and runner.get("scenario_scope") == "mandatory_full_feature_matrix",
        "real rehearsal runner did not independently PASS",
    )
    _require(
        supervisor.get("schema_version") == "hackme.campaign-supervisor.v1"
        and supervisor.get("level") == "rehearsal"
        and supervisor.get("ok") is True
        and supervisor.get("classification") == "PASS"
        and supervisor.get("runner_returncode") == 0
        and supervisor.get("runner_verdict") == "PASS"
        and supervisor.get("runner_report") == str(runner_source)
        and isinstance(supervisor.get("source_final"), Mapping)
        and supervisor["source_final"].get("verified") is True,
        "real rehearsal supervisor did not independently PASS",
    )
    cleanup = supervisor.get("cleanup")
    _require(isinstance(cleanup, Mapping), "real rehearsal cleanup authority is missing")
    for cleanup_role in ("source_monitor", "watchdog", "scope"):
        _require(
            isinstance(cleanup.get(cleanup_role), Mapping)
            and cleanup[cleanup_role].get("ok") is True,
            f"real rehearsal cleanup did not PASS: {cleanup_role}",
        )
    campaign_uuid = str(supervisor.get("campaign_uuid") or "")
    _require(
        campaign_uuid and runner.get("campaign_uuid") == campaign_uuid,
        "real rehearsal supervisor/runner campaign mismatch",
    )
    capture = projection_context["capture_context"]
    common_identity = {
        "qualification_campaign_uuid": capture["qualification_campaign_uuid"],
        "campaign_uuid": campaign_uuid,
        "campaign_attempt_uuid": projection_context["campaign_attempt_uuid"],
        "native_invocation_id": projection_context["outer_native_invocation_id"],
        "commit": capture["commit"],
        "source_digest": capture["source_digest"],
        "protected_source_digest": capture["protected_source_digest"],
    }
    _require(
        all(runner.get(key) == value for key, value in common_identity.items()),
        "real rehearsal runner differs from sealed outer authority",
    )
    scenario_ids = tuple(projection_context["scenario_authorities"])
    _require(
        set(scenario_ids) == set(FORMAL_SCENARIO_BINDINGS)
        and len(scenario_ids) == 13,
        "sealed rehearsal scenario inventory is not exactly 13",
    )
    source_index = runner.get("scenario_receipts")
    scenarios = runner.get("scenarios")
    _require(
        isinstance(source_index, Mapping)
        and set(source_index) == set(scenario_ids)
        and isinstance(scenarios, Mapping)
        and set(scenarios) == set(scenario_ids),
        "real rehearsal scenario inventory mismatch",
    )

    stage_root.mkdir(mode=0o700)
    os.chmod(stage_root, 0o700)
    archive_links: dict[str, dict[str, Any]] = {}
    bundle_sources: dict[str, dict[str, Any]] = {}
    receipt_sources: dict[str, dict[str, Any]] = {}
    for scenario_id in scenario_ids:
        index = source_index[scenario_id]
        result = scenarios[scenario_id]
        _require(
            isinstance(index, Mapping)
            and isinstance(result, Mapping)
            and result.get("ok") is True
            and result.get("classification") == "PASS",
            f"real rehearsal scenario did not PASS: {scenario_id}",
        )
        source_receipt_link = index.get("receipt")
        source_bundle_link = index.get("artifact_bundle")
        source_archive_link = index.get("artifact_archive")
        _require(
            all(
                isinstance(link, Mapping)
                and set(link) == {"path", "sha256", "size_bytes"}
                for link in (
                    source_receipt_link,
                    source_bundle_link,
                    source_archive_link,
                )
            ),
            f"real rehearsal source index shape mismatch: {scenario_id}",
        )
        receipt_path = Path(str(source_receipt_link["path"])).resolve(strict=True)
        bundle_path = Path(str(source_bundle_link["path"])).resolve(strict=True)
        archive_path = Path(str(source_archive_link["path"])).resolve(strict=True)
        expected_root = (
            campaign_root / "reports" / "scenarios" / scenario_id
        ).resolve(strict=True)
        bundle_path.relative_to(expected_root)
        archive_path.relative_to(expected_root)
        _require(
            receipt_path
            == (campaign_root / "reports" / "scenario_receipts" / f"{scenario_id}.json")
            .resolve(strict=True),
            f"real rehearsal receipt path mismatch: {scenario_id}",
        )
        for link, path, label in (
            (source_receipt_link, receipt_path, "receipt"),
            (source_bundle_link, bundle_path, "bundle"),
            (source_archive_link, archive_path, "archive"),
        ):
            identity = _inspect_native(path, label=f"{scenario_id} source {label}")
            _require(
                link.get("sha256")
                == _stable_sha256(path, identity, label=f"{scenario_id} source {label}")
                and link.get("size_bytes") == identity.size,
                f"real rehearsal source {label} identity mismatch: {scenario_id}",
            )
        receipt = _load_native_json(receipt_path, label=f"{scenario_id} source receipt")
        bundle = _load_native_json(bundle_path, label=f"{scenario_id} source bundle")
        expected_scenario_authority = {
            **common_identity,
            **projection_context["scenario_authorities"][scenario_id],
        }
        receipt_validation = validate_scenario_runtime_receipt(
            receipt,
            FORMAL_SCENARIO_BINDINGS[scenario_id],
        )
        _require(
            receipt_validation.valid
            and receipt_validation.contract_pass
            and receipt_validation.status.value == "PASS"
            and isinstance(receipt.get("authority"), Mapping)
            and isinstance(bundle.get("authority"), Mapping)
            and bundle.get("authority") == receipt.get("authority")
            and all(
                receipt["authority"].get(key) == value
                for key, value in expected_scenario_authority.items()
            ),
            f"real rehearsal scenario authority/receipt invalid: {scenario_id}",
        )
        _require(
            not native_artifact_bundle_validation_errors(
                bundle,
                FORMAL_SCENARIO_BINDINGS[scenario_id],
                expected_authority=expected_scenario_authority,
            ),
            f"real rehearsal artifact bundle invalid: {scenario_id}",
        )
        _require(
            result.get("runtime_receipt") == receipt
            and index.get("scenario_attempt_uuid")
            == expected_scenario_authority["scenario_attempt_uuid"]
            and index.get("native_invocation_id")
            == expected_scenario_authority["native_invocation_id"],
            f"real rehearsal scenario result/index authority mismatch: {scenario_id}",
        )
        _verify_rehearsal_archive(archive_path, bundle, scenario_id=scenario_id)
        archive_role = f"scenario_archive_{scenario_id}"
        archive_sha, archive_size = _copy_private_file_once(
            archive_path,
            expected_paths[archive_role],
            label=f"{scenario_id} archive projection",
        )
        archive_links[scenario_id] = {
            "path": str(planned[archive_role]),
            "sha256": archive_sha,
            "size_bytes": archive_size,
        }
        bundle_sources[scenario_id] = bundle
        receipt_sources[scenario_id] = receipt

    bundle_links: dict[str, dict[str, Any]] = {}
    for scenario_id in scenario_ids:
        role = f"scenario_bundle_{scenario_id}"
        bundle = dict(bundle_sources[scenario_id])
        archive_reference = dict(bundle.get("artifact_archive") or {})
        archive_reference.update(archive_links[scenario_id])
        bundle["artifact_archive"] = archive_reference
        _write_private_json_once(expected_paths[role], bundle)
        bundle_links[scenario_id] = _projected_json_link(
            context=context,
            role=role,
            native_path=expected_paths[role],
            planned_path=planned[role],
        )

    receipt_links: dict[str, dict[str, Any]] = {}
    for scenario_id in scenario_ids:
        role = f"scenario_{scenario_id}"
        receipt = dict(receipt_sources[scenario_id])
        bundle_reference = dict(receipt.get("artifact_bundle") or {})
        bundle_reference.update(bundle_links[scenario_id])
        bundle_reference["artifact_archive_sha256"] = archive_links[scenario_id][
            "sha256"
        ]
        bundle_reference["artifact_archive_size_bytes"] = archive_links[
            scenario_id
        ]["size_bytes"]
        receipt["artifact_bundle"] = bundle_reference
        _write_private_json_once(expected_paths[role], receipt)
        receipt_links[scenario_id] = _projected_json_link(
            context=context,
            role=role,
            native_path=expected_paths[role],
            planned_path=planned[role],
        )

    runner_projection = dict(runner)
    runner_projection["scenario_receipts"] = {
        scenario_id: {
            "scenario_attempt_uuid": projection_context["scenario_authorities"][
                scenario_id
            ]["scenario_attempt_uuid"],
            "native_invocation_id": projection_context["scenario_authorities"][
                scenario_id
            ]["native_invocation_id"],
            "receipt": receipt_links[scenario_id],
            "artifact_bundle": bundle_links[scenario_id],
            "artifact_archive": archive_links[scenario_id],
        }
        for scenario_id in scenario_ids
    }
    _write_private_json_once(expected_paths["runner_result"], runner_projection)
    runner_link = _projected_json_link(
        context=context,
        role="runner_result",
        native_path=expected_paths["runner_result"],
        planned_path=planned["runner_result"],
    )
    supervisor_projection = dict(supervisor)
    supervisor_projection.update(common_identity)
    supervisor_projection.update({
        "started_at": _format_precise_utc(started_at),
        "finished_at": _format_precise_utc(child_finished_at),
        "started_monotonic_ns": started_monotonic_ns,
        "finished_monotonic_ns": child_finished_monotonic_ns,
        "runner_report": runner_link,
    })
    _write_private_json_once(
        expected_paths["supervisor_result"],
        supervisor_projection,
    )
    _require(
        set(path.name for path in stage_root.iterdir())
        == set(path.name for path in expected_paths.values())
        and all(path.is_file() for path in expected_paths.values()),
        "rehearsal projection did not materialize exactly 41 roles",
    )


def _create_sealed_projection_memfd(payload: Mapping[str, Any]) -> tuple[int, str, str]:
    content = encoded_rehearsal_projection_context(payload)
    flags = getattr(os, "MFD_CLOEXEC", 0x0001) | getattr(
        os, "MFD_ALLOW_SEALING", 0x0002
    )
    try:
        create_memfd = getattr(os, "memfd_create", None)
        if callable(create_memfd):
            descriptor = create_memfd("hackme-rehearsal-projection", flags=flags)
        else:
            libc = ctypes.CDLL(None, use_errno=True)
            libc_memfd_create = libc.memfd_create
            libc_memfd_create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
            libc_memfd_create.restype = ctypes.c_int
            descriptor = int(libc_memfd_create(b"hackme-rehearsal-projection", flags))
            if descriptor < 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number))
    except (AttributeError, OSError) as exc:
        raise NativeExecutionError(
            f"sealed rehearsal projection memfd unavailable: {exc.__class__.__name__}: {exc}"
        ) from exc
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            _require(written > 0, "rehearsal projection memfd write made no progress")
            view = view[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = (
            getattr(fcntl, "F_SEAL_SEAL", 0x0001)
            | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
            | getattr(fcntl, "F_SEAL_GROW", 0x0004)
            | getattr(fcntl, "F_SEAL_WRITE", 0x0008)
        )
        add_seals_command = getattr(fcntl, "F_ADD_SEALS", 1033)
        get_seals_command = getattr(fcntl, "F_GET_SEALS", 1034)
        fcntl.fcntl(descriptor, add_seals_command, seals)
        _require(
            int(fcntl.fcntl(descriptor, get_seals_command)) & seals == seals,
            "rehearsal projection memfd seal verification failed",
        )
    except Exception:
        os.close(descriptor)
        raise
    return (
        descriptor,
        f"/proc/{os.getpid()}/fd/{descriptor}",
        hashlib.sha256(content).hexdigest(),
    )


def _cleanup_failed_rehearsal_projection(campaign_root: Path) -> None:
    """Remove only the exact parent-owned projection files after a failed attempt."""

    root = (
        Path(campaign_root).resolve(strict=False)
        / "artifacts"
        / "formal_native_rehearsal"
    )
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return
    _require(
        stat.S_ISDIR(root_info.st_mode)
        and not stat.S_ISLNK(root_info.st_mode)
        and root_info.st_uid == os.geteuid(),
        "failed rehearsal projection root is not a parent-owned directory",
    )
    expected = set(reviewed_rehearsal_native_artifact_paths(campaign_root).values())
    for entry in tuple(root.iterdir()):
        _require(
            entry in expected
            and not entry.is_symlink()
            and entry.is_file(),
            "failed rehearsal projection contains an unexpected entry",
        )
        entry.unlink()
    root.rmdir()


def _process_group_alive(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37


def _child_subreaper_state() -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    current = ctypes.c_int(0)
    if libc.prctl(
        _PR_GET_CHILD_SUBREAPER,
        ctypes.byref(current),
        0,
        0,
        0,
    ) != 0:
        error = ctypes.get_errno()
        raise NativeExecutionError(
            f"cannot read child-subreaper state: errno={error}"
        )
    return bool(current.value)


def _set_child_subreaper(enabled: bool) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(
        _PR_SET_CHILD_SUBREAPER,
        int(bool(enabled)),
        0,
        0,
        0,
    ) != 0:
        error = ctypes.get_errno()
        raise NativeExecutionError(
            f"cannot set child-subreaper state: errno={error}"
        )


def _direct_child_identities() -> dict[int, int]:
    """Return PID -> start ticks for every child owned by any local thread."""

    _reap_owned_children()
    result: dict[int, int] = {}
    task_root = Path(f"/proc/{os.getpid()}/task")
    try:
        task_paths = tuple(task_root.iterdir())
    except OSError as exc:
        raise NativeExecutionError(
            f"cannot enumerate native runner tasks: {exc.__class__.__name__}"
        ) from exc
    for task in task_paths:
        try:
            text_value = (task / "children").read_text(encoding="ascii").strip()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NativeExecutionError(
                f"cannot enumerate native runner children: {exc.__class__.__name__}"
            ) from exc
        for token in text_value.split():
            try:
                identity = capture_process_identity(int(token))
            except (FileNotFoundError, ProcessLookupError, ProcessIdentityError):
                continue
            result[identity.pid] = identity.start_ticks
    return result


def _new_adopted_children(baseline: Mapping[int, int]) -> dict[int, int]:
    return {
        pid: start_ticks
        for pid, start_ticks in _direct_child_identities().items()
        if baseline.get(pid) != start_ticks
    }


def _reap_owned_children() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid <= 0:
            return


def _terminate_adopted_children(
    baseline: Mapping[int, int],
    *,
    grace: float = _ADOPTED_CHILD_DRAIN_SECONDS,
) -> list[dict[str, int]]:
    """Kill descendants that escaped the command's original process group."""

    observed: dict[tuple[int, int], dict[str, int]] = {}
    deadline = time.monotonic() + max(0.1, float(grace))
    signalled_term: set[tuple[int, int]] = set()
    while time.monotonic() < deadline:
        children = _new_adopted_children(baseline)
        if not children:
            _reap_owned_children()
            if not _new_adopted_children(baseline):
                break
        for pid, start_ticks in children.items():
            key = (pid, start_ticks)
            observed[key] = {"pid": pid, "start_ticks": start_ticks}
            if key in signalled_term:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            signalled_term.add(key)
        _reap_owned_children()
        time.sleep(0.05)

    for pid, start_ticks in _new_adopted_children(baseline).items():
        key = (pid, start_ticks)
        observed[key] = {"pid": pid, "start_ticks": start_ticks}
        try:
            live = capture_process_identity(pid)
            if live.start_ticks == start_ticks:
                os.kill(pid, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError):
            pass
    final_deadline = time.monotonic() + 1.0
    while time.monotonic() < final_deadline:
        _reap_owned_children()
        if not _new_adopted_children(baseline):
            break
        time.sleep(0.02)
    return [observed[key] for key in sorted(observed)]


def _terminate_process_group(process: subprocess.Popen[Any], *, grace: float = 5.0) -> None:
    if process.poll() is None or _process_group_alive(process.pid):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + max(0.0, grace)
        while time.monotonic() < deadline and _process_group_alive(process.pid):
            time.sleep(0.05)
        if _process_group_alive(process.pid):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if process.poll() is None:
        process.wait(timeout=max(1.0, grace))


def execute_and_capture_gate(
    *,
    attempt_root: Path,
    gate_name: str,
    source_authority_path: Path,
    qualification_campaign_uuid: str,
    native_artifact_paths: Mapping[str, Path],
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run one command and return only a sealed, independently derived gate."""

    _require(gate_name in GATE_RAW_SPECS, f"unknown formal gate: {gate_name}")
    _require(
        set(native_artifact_paths) == set(GATE_RAW_SPECS[gate_name]),
        f"{gate_name} native artifact role set mismatch",
    )
    values = tuple(str(value) for value in command)
    _require(values, "native execution command is empty")
    _require(
        0.0 < float(timeout_seconds) <= 48 * 60 * 60,
        "native execution timeout is outside the reviewed range",
    )
    attempt = _canonical_new_path(Path(attempt_root), label="attempt root")
    working_directory = Path(cwd).expanduser().resolve(strict=True)
    _require(working_directory.is_dir(), "native execution cwd is not a directory")
    reviewed_command = _validate_reviewed_command(
        gate_name=gate_name,
        command=values,
        cwd=working_directory,
    )
    canonical_artifacts = {
        role: _canonical_new_path(Path(path), label=f"native artifact {role}")
        for role, path in native_artifact_paths.items()
    }
    _require(
        len(set(canonical_artifacts.values())) == len(canonical_artifacts),
        "native artifact paths are reused across roles",
    )
    if gate_name == "60_minute_rehearsal_passed" and reviewed_command.get(
        "contract"
    ) == "supervised_rehearsal_v1":
        _require(
            canonical_artifacts
            == reviewed_rehearsal_native_artifact_paths(
                Path(str(reviewed_command["campaign_root"]))
            ),
            "rehearsal native artifacts must use the reviewed 41-role paths",
        )
    for role, path in canonical_artifacts.items():
        _require(
            path != attempt and attempt not in path.parents,
            f"native artifact {role} is inside the sealed attempt root",
        )

    invocation_id = f"native:{gate_name}:{uuid.uuid4().hex}"
    activation_nonce = f"activation:{gate_name}:{uuid.uuid4().hex}"
    context = QualificationContext.create(
        qualification_campaign_uuid=qualification_campaign_uuid,
        source_authority_path=_absolute_path(
            source_authority_path,
            label="source authority",
        ),
        invocation_id=f"capture:{gate_name}:{uuid.uuid4().hex}",
    )
    projection_context: dict[str, Any] = {}
    projection_fd: int | None = None
    projection_locator = ""
    projection_sha256 = ""
    if gate_name == "60_minute_rehearsal_passed":
        scenario_authorities = {
            scenario_id: {
                "scenario_attempt_uuid": (
                    f"scenario-attempt:{uuid.uuid4().hex}"
                ),
                "native_invocation_id": (
                    f"scenario-invocation:{uuid.uuid4().hex}"
                ),
            }
            for scenario_id in FORMAL_SCENARIO_BINDINGS
        }
        projection_context = build_rehearsal_projection_context(
            context=context,
            attempt_root=attempt,
            native_artifact_paths=canonical_artifacts,
            outer_native_invocation_id=invocation_id,
            activation_nonce=activation_nonce,
            campaign_attempt_uuid=f"campaign-attempt:{uuid.uuid4().hex}",
            scenario_authorities=scenario_authorities,
        )
    receipt_path = _canonical_new_path(
        attempt.parent / f".{attempt.name}.{gate_name}.{uuid.uuid4().hex}.native.json",
        label="native execution receipt",
    )
    source_before = _verify_live_source(
        context,
        scratch_root=receipt_path.parent / f".{receipt_path.name}.source-before",
    )
    source_guard = _LiveSourceGuard(
        context,
        source_before,
        scratch_root=receipt_path.parent / f".{receipt_path.name}.source-guard",
    )

    started_at = _precise_utc_now()
    started_epoch_ns = time.time_ns()
    started_monotonic_ns = time.monotonic_ns()
    process: subprocess.Popen[Any] | None = None
    original_subreaper: bool | None = None
    subreaper_changed = False
    child_baseline: dict[int, int] = {}
    child_tracking_started = False
    execution_succeeded = False
    try:
        if projection_context:
            (
                projection_fd,
                projection_locator,
                projection_sha256,
            ) = _create_sealed_projection_memfd(projection_context)
        original_subreaper = _child_subreaper_state()
        if not original_subreaper:
            _set_child_subreaper(True)
            subreaper_changed = True
        child_baseline = _direct_child_identities()
        child_tracking_started = True
        child_environment = os.environ.copy()
        child_environment.pop(REHEARSAL_PROJECTION_CONTEXT_ENV, None)
        child_environment.pop(REHEARSAL_PROJECTION_CONTEXT_SHA256_ENV, None)
        if projection_fd is not None:
            child_environment[REHEARSAL_PROJECTION_CONTEXT_ENV] = projection_locator
            child_environment[
                REHEARSAL_PROJECTION_CONTEXT_SHA256_ENV
            ] = projection_sha256
        process = subprocess.Popen(
            list(values),
            cwd=str(working_directory),
            env=child_environment,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        try:
            returncode = process.wait(timeout=float(timeout_seconds))
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise NativeExecutionError(
                f"native execution timed out after {float(timeout_seconds):g} seconds"
            ) from exc
        child_finished_monotonic_ns = time.monotonic_ns()
        child_finished_at = _precise_utc_now()
        _require(returncode == 0, f"native execution exited with {returncode}")
        deadline = time.monotonic() + _PROCESS_GROUP_DRAIN_SECONDS
        while time.monotonic() < deadline and _process_group_alive(process.pid):
            time.sleep(0.05)
        if _process_group_alive(process.pid):
            _terminate_process_group(process)
            raise NativeExecutionError(
                "native execution left live descendants in its process group"
            )
        escaped_children = _terminate_adopted_children(child_baseline)
        _require(
            not escaped_children,
            "native execution left descendants outside its process group",
        )

        if gate_name == "60_minute_rehearsal_passed":
            _require(
                reviewed_command.get("contract") == "supervised_rehearsal_v1"
                and bool(projection_context),
                "rehearsal projection lacks its reviewed command/context",
            )
            _materialize_rehearsal_projection(
                context=context,
                projection_context=projection_context,
                reviewed_command=reviewed_command,
                native_artifact_paths=canonical_artifacts,
                started_at=started_at,
                started_monotonic_ns=started_monotonic_ns,
                child_finished_at=child_finished_at,
                child_finished_monotonic_ns=child_finished_monotonic_ns,
            )
        finished_monotonic_ns = time.monotonic_ns()
        finished_epoch_ns = time.time_ns()
        finished_at = _precise_utc_now()

        source_guard.verify()
        source_after = _verify_live_source(
            context,
            scratch_root=receipt_path.parent / f".{receipt_path.name}.source-after",
        )
        _require(source_before == source_after, "live source authority changed during native execution")
        artifacts = _artifact_records(
            canonical_artifacts,
            started_epoch_ns=started_epoch_ns,
            finished_epoch_ns=finished_epoch_ns,
            started_monotonic_ns=started_monotonic_ns,
            finished_monotonic_ns=finished_monotonic_ns,
        )
        _reject_forbidden_artifact_markers(gate_name, canonical_artifacts)
        _reject_artifact_secrets(canonical_artifacts)
        producer_identity = capture_process_identity(os.getpid())
        producer = {
            "kind": NATIVE_PRODUCER_KIND,
            "pid": producer_identity.pid,
            "start_ticks": producer_identity.start_ticks,
            "boot_id": producer_identity.boot_id,
            "cgroup_path": producer_identity.cgroup_path,
            "invocation_id": invocation_id,
        }
        receipt = {
            "schema_version": NATIVE_EXECUTION_RECEIPT_SCHEMA_VERSION,
            "gate_name": gate_name,
            "qualification_campaign_uuid": context.qualification_campaign_uuid,
            "commit": context.commit,
            "source_digest": context.source_digest,
            "protected_source_digest": context.protected_source_digest,
            "invocation_id": invocation_id,
            "activation_nonce": activation_nonce,
            "actual_execution": True,
            "simulated": False,
            "component_only": False,
            "started_at": _format_precise_utc(started_at),
            "finished_at": _format_precise_utc(finished_at),
            "started_monotonic_ns": started_monotonic_ns,
            "finished_monotonic_ns": finished_monotonic_ns,
            "producer": producer,
            "source_authority_sha256": context.source_authority.sha256,
            "artifacts": artifacts,
        }
        _write_private_json_once(receipt_path, receipt)
        captured = capture_gate_evidence(
            attempt_root=attempt,
            context=context,
            gate_name=gate_name,
            native_artifact_paths=canonical_artifacts,
            native_execution_receipt_path=receipt_path,
        )
        source_guard.verify()
        os.chmod(receipt_path, 0o400)
        execution_succeeded = True
        return {
            "schema_version": NATIVE_EXECUTION_RESULT_SCHEMA_VERSION,
            "status": "PASS",
            "machine_verified": True,
            "gate_name": gate_name,
            "qualification_campaign_uuid": context.qualification_campaign_uuid,
            "commit": context.commit,
            "source_digest": context.source_digest,
            "protected_source_digest": context.protected_source_digest,
            "invocation_id": invocation_id,
            "activation_nonce": activation_nonce,
            "command_sha256": hashlib.sha256(
                b"\0".join(value.encode("utf-8", errors="surrogatepass") for value in values)
            ).hexdigest(),
            "native_execution_receipt": str(receipt_path),
            "attempt_root": captured["_attempt_root"],
            "attempt_manifest": captured["_attempt_manifest"],
            "evidence_path": captured["_evidence_path"],
            "derived_sha256": captured["_derived_sha256"],
        }
    finally:
        if process is not None and (process.poll() is None or _process_group_alive(process.pid)):
            _terminate_process_group(process)
        if child_tracking_started:
            _terminate_adopted_children(child_baseline)
        source_guard.close()
        if subreaper_changed:
            _set_child_subreaper(False)
        if projection_fd is not None:
            os.close(projection_fd)
        if (
            not execution_succeeded
            and gate_name == "60_minute_rehearsal_passed"
            and reviewed_command.get("contract") == "supervised_rehearsal_v1"
        ):
            try:
                _cleanup_failed_rehearsal_projection(
                    Path(str(reviewed_command["campaign_root"]))
                )
            except Exception:
                # Preserve the original failure.  The exact cleanup helper is
                # directly tested and never follows an unexpected entry.
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--attempt-root", type=Path, required=True)
    parser.add_argument("--gate-name", choices=tuple(GATE_RAW_SPECS), required=True)
    parser.add_argument("--source-authority", type=Path, required=True)
    parser.add_argument("--qualification-campaign-uuid", required=True)
    parser.add_argument("--artifact", action="append", default=[], metavar="ROLE=ABSOLUTE_PATH")
    parser.add_argument("--cwd", type=Path, default=ROOT)
    parser.add_argument("--timeout-seconds", type=float, default=0.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = tuple(args.command[1:] if args.command[:1] == ["--"] else args.command)
    timeout_seconds = float(
        args.timeout_seconds
        or _GATE_TIMEOUT_SECONDS.get(args.gate_name, _DEFAULT_TIMEOUT_SECONDS)
    )
    try:
        artifact_paths = _parse_artifacts(args.artifact)
        result = execute_and_capture_gate(
            attempt_root=args.attempt_root,
            gate_name=args.gate_name,
            source_authority_path=args.source_authority,
            qualification_campaign_uuid=args.qualification_campaign_uuid,
            native_artifact_paths=artifact_paths,
            command=command,
            cwd=args.cwd,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        print(json.dumps({
            "schema_version": NATIVE_EXECUTION_RESULT_SCHEMA_VERSION,
            "status": "FAIL_HARNESS",
            "machine_verified": False,
            "error": f"{exc.__class__.__name__}: {exc}",
            "attempt_root": str(args.attempt_root),
        }, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
