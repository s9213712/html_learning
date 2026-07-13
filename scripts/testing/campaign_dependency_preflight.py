#!/usr/bin/env python3
"""Fail-closed Level 1 dependency preflight for operational campaigns.

The preflight deliberately treats process availability and an HTTP success as
insufficient.  External probes must publish a versioned terminal-state
contract with positive side-effect evidence.  Missing configuration is
``BLOCKED``; it is never converted to a skip or a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tarfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


PREFLIGHT_SCHEMA_VERSION = "hackme.campaign.dependency-preflight/v1"
EXTERNAL_PROBE_SCHEMA_VERSION = "hackme.campaign.external-dependency-probe/v1"
BACKUP_RESTORE_MANIFEST_SCHEMA_VERSION = "hackme.backup-restore-manifest/v2"
BACKUP_SQLITE_CHECK_SCHEMA_VERSION = "hackme.backup-sqlite-check/v2"
BACKUP_SNAPSHOT_MARKER_TABLE = "campaign_snapshot_markers"
REVIEWED_BACKUP_SNAPSHOT_METHODS = frozenset({
    "sqlite_backup_api",
    "vacuum_into",
})
REQUIRED_BROWSER_ENGINES = ("chromium", "firefox", "webkit")
REQUIRED_EXTERNAL_PROBES = (
    "bt_seed_download",
    "comfyui_terminal",
    "ai_provider_terminal",
    "backup_restore",
    "production_security_sentinel",
)
MAX_EXTERNAL_REPORT_BYTES = 16 * 1024 * 1024
MAX_PNG_DIMENSION = 16_384
MAX_PNG_PIXELS = 64_000_000
BROWSER_OBSERVATION_SCHEMA_VERSION = "hackme.browser-launch-observation/v1"
FFPROBE_OBSERVATION_SCHEMA_VERSION = "hackme.ffprobe-media-observation/v1"


class PreflightStatus(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    FAIL_EXTERNAL = "FAIL_EXTERNAL"
    FAIL_PRODUCT = "FAIL_PRODUCT"
    FAIL_HARNESS = "FAIL_HARNESS"
    FAIL_INFRA = "FAIL_INFRA"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ExternalProbeSpec:
    dependency: str
    command: tuple[str, ...]
    timeout_seconds: float = 600.0
    environment: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExternalProbeSpec":
        return cls(
            dependency=str(payload.get("dependency") or ""),
            command=tuple(str(item) for item in (payload.get("command") or ())),
            timeout_seconds=float(payload.get("timeout_seconds") or 600.0),
            environment={str(k): str(v) for k, v in (payload.get("environment") or {}).items()},
        )


def _result(name: str, status: PreflightStatus, started: float, **details: Any) -> dict[str, Any]:
    return {
        "name": name,
        "status": status.value,
        "ok": status is PreflightStatus.PASS,
        "elapsed_seconds": round(max(0.0, time.monotonic() - started), 3),
        "details": details,
    }


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _all_true(evidence: Mapping[str, Any], names: Sequence[str]) -> bool:
    return all(evidence.get(name) is True for name in names)


def _browser_failure_status(error: str) -> PreflightStatus:
    lowered = error.lower()
    unavailable_markers = (
        "executable doesn't exist",
        "enoent",
        "no such file or directory",
        "host system is missing dependencies",
        "missing libraries:",
    )
    return PreflightStatus.BLOCKED if any(marker in lowered for marker in unavailable_markers) else PreflightStatus.FAIL_INFRA


def validate_external_probe(dependency: str, payload: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Validate positive evidence without trusting an aggregate ``ok`` flag."""

    errors: list[str] = []
    if payload.get("schema_version") != EXTERNAL_PROBE_SCHEMA_VERSION:
        errors.append("schema_version")
    if payload.get("dependency") != dependency:
        errors.append("dependency")
    if payload.get("available") is not True:
        errors.append("available")
    if payload.get("synthetic") is not False:
        errors.append("synthetic")
    if str(payload.get("terminal_state") or "").lower() not in {"completed", "success", "passed"}:
        errors.append("terminal_state")
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        return False, errors + ["evidence"]

    if dependency == "bt_seed_download":
        if not _all_true(evidence, (
            "seed_started", "torrent_created", "peer_observed", "download_terminal",
            "payload_sha256_match", "downloaded_via_bt",
        )):
            errors.append("bt_side_effects")
        if not _nonempty(evidence.get("info_hash")):
            errors.append("info_hash")
        if not _nonempty(evidence.get("download_path")) or not _nonempty(evidence.get("payload_sha256")):
            errors.append("bt_artifact_identity")
    elif dependency == "comfyui_terminal":
        if not _all_true(evidence, (
            "job_submitted", "terminal_polled", "history_terminal",
            "output_exists", "output_decodable",
        )):
            errors.append("comfyui_side_effects")
        if not _nonempty(evidence.get("job_id")) or not _nonempty(evidence.get("prompt_id")):
            errors.append("comfyui_ids")
        if not _nonempty(evidence.get("output_path")) or not _nonempty(evidence.get("output_sha256")):
            errors.append("comfyui_artifact_identity")
    elif dependency == "ai_provider_terminal":
        if not _all_true(evidence, (
            "provider_called", "terminal_polled", "response_nonempty", "usage_reported",
        )):
            errors.append("ai_provider_side_effects")
        if not all(_nonempty(evidence.get(key)) for key in ("provider", "model", "request_id")):
            errors.append("ai_provider_identity")
    elif dependency == "backup_restore":
        if not _all_true(evidence, (
            "archive_created", "archive_readable", "restore_completed",
            "source_restore_digest_match", "sqlite_quick_check", "manifest_validated",
            "consistent_snapshot_created", "wal_checkpoint_completed",
            "snapshot_marker_verified", "backup_api_completed",
        )):
            errors.append("backup_restore_side_effects")
        if not _nonempty(evidence.get("snapshot_id")) or not _nonempty(evidence.get("archive_sha256")):
            errors.append("backup_identity")
        if not _nonempty(evidence.get("archive_path")):
            errors.append("backup_archive_path")
        if evidence.get("snapshot_method") not in REVIEWED_BACKUP_SNAPSHOT_METHODS:
            errors.append("backup_snapshot_method")
        if not _nonempty(evidence.get("snapshot_marker_id")):
            errors.append("backup_snapshot_marker")
    elif dependency == "production_security_sentinel":
        if not _all_true(evidence, (
            "production_mode", "csrf_enforced", "rbac_enforced",
            "confirmation_enforced", "audit_chain_verified", "cross_worker_session_verified",
        )):
            errors.append("security_side_effects")
    else:
        errors.append("unknown_dependency")
    return not errors, errors


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _process_start_ticks(pid: int) -> int:
    fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()
    if len(fields) < 22:
        raise RuntimeError(f"browser process {pid} has an incomplete /proc stat")
    value = int(fields[21])
    if value <= 0:
        raise RuntimeError(f"browser process {pid} has an invalid start tick")
    return value


def _descendant_pids(root_pid: int) -> set[int]:
    parents: dict[int, int] = {}
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat_path.read_text(encoding="utf-8").split()
            parents[int(stat_path.parent.name)] = int(fields[3])
        except (OSError, ValueError, IndexError):
            continue
    result: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {pid for pid, parent in parents.items() if parent in frontier and pid not in result}
        result.update(children)
        frontier = children
    return result


def _browser_process(engine_name: str, executable_path: str, before: set[int]) -> tuple[int, int]:
    candidates = _descendant_pids(os.getpid()) - before
    executable_name = Path(executable_path).name
    matches: list[tuple[int, int]] = []
    for pid in candidates:
        try:
            command = (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace",
            )
            if executable_path not in command and executable_name not in command:
                continue
            matches.append((pid, _process_start_ticks(pid)))
        except (OSError, RuntimeError):
            continue
    if not matches:
        raise RuntimeError(f"could not identify the live {engine_name} browser process")
    return max(matches, key=lambda item: item[1])


def _validate_png(path: Path) -> None:
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
        if (
            image.format != "PNG"
            or width <= 0
            or height <= 0
            or width > MAX_PNG_DIMENSION
            or height > MAX_PNG_DIMENSION
            or width * height > MAX_PNG_PIXELS
        ):
            raise ValueError("PNG dimensions/pixel count exceed the reviewed cap")
        image.verify()
    with Image.open(path) as image:
        if image.format != "PNG" or image.size != (width, height):
            raise ValueError("PNG identity changed between verify and decode")
        image.load()


def verify_external_artifacts(dependency: str, payload: Mapping[str, Any]) -> list[str]:
    """Re-open important outputs rather than trusting probe booleans."""

    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        return ["evidence"]
    if dependency == "bt_seed_download":
        path_key, hash_key = "download_path", "payload_sha256"
    elif dependency == "comfyui_terminal":
        path_key, hash_key = "output_path", "output_sha256"
    elif dependency == "backup_restore":
        path_key, hash_key = "archive_path", "archive_sha256"
    else:
        return []
    path = Path(str(evidence.get(path_key) or ""))
    expected = str(evidence.get(hash_key) or "").lower()
    if not path.is_file() or path.stat().st_size <= 0:
        return [f"{path_key}_missing"]
    if len(expected) != 64 or _sha256(path) != expected:
        return [f"{hash_key}_mismatch"]
    if dependency == "comfyui_terminal":
        try:
            _validate_png(path)
        except Exception:
            return ["output_decode_failed"]
    if dependency == "backup_restore":
        try:
            # Formal backup authorities use one deterministic, uncompressed
            # tar stream.  Auto-detected gzip/bzip/xz and ZIP are forbidden.
            with tarfile.open(path, mode="r:") as archive:
                found = False
                for index, _member in enumerate(archive, start=1):
                    if index > 100_000:
                        return ["archive_member_limit"]
                    found = True
                if not found:
                    return ["archive_empty"]
            with path.open("rb") as handle:
                if handle.read(2) == b"\x1f\x8b":
                    return ["archive_unreadable"]
        except Exception:
            return ["archive_unreadable"]
    return []


def production_security_probe_contract(report: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the project's native sentinel report without weakening it."""

    checks = {
        str(item.get("name") or ""): bool(item.get("ok"))
        for item in (report.get("checks") or [])
        if isinstance(item, Mapping)
    }

    def passed(*names: str) -> bool:
        return all(checks.get(name) is True for name in names)

    evidence = {
        "production_mode": passed("production_launcher_contract", "production_mode_active"),
        "csrf_enforced": passed("login_missing_csrf_denied", "authenticated_missing_csrf_denied"),
        "rbac_enforced": passed("anonymous_root_denied", "manager_root_boundary_denied", "user_root_boundary_denied"),
        "confirmation_enforced": passed("dangerous_confirmation_required"),
        "audit_chain_verified": passed("production_security_controls", "audit_log_chain"),
        "cross_worker_session_verified": passed("cross_worker_session_consistency"),
    }
    all_passed = report.get("ok") is True and all(evidence.values())
    return {
        "schema_version": EXTERNAL_PROBE_SCHEMA_VERSION,
        "dependency": "production_security_sentinel",
        "available": True,
        "synthetic": False,
        "terminal_state": "completed" if all_passed else "failed",
        "evidence": evidence,
        "native_schema_version": report.get("schema_version"),
        "native_failed_checks": list(report.get("failed_checks") or []),
    }


def _default_browser_launcher(engine_name: str) -> Mapping[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser_type = getattr(playwright, engine_name)
        executable_path = str(Path(browser_type.executable_path).resolve())
        before = _descendant_pids(os.getpid())
        started_at = utc_now()
        browser = browser_type.launch(headless=True)
        browser_pid = 0
        process_start_ticks = 0
        closed_cleanly = False
        try:
            browser_pid, process_start_ticks = _browser_process(
                engine_name,
                executable_path,
                before,
            )
            page = browser.new_page()
            marker = f"level1-{engine_name}"
            console_errors: list[str] = []
            page_errors: list[str] = []
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.set_content(f"<main id='marker'>{marker}</main>")
            observed = page.locator("#marker").inner_text()
            if observed != marker:
                raise RuntimeError(f"DOM marker mismatch: {observed!r}")
            version = browser.version
            page_url = page.url
        finally:
            browser.close()
            closed_cleanly = True
        observation = {
            "schema_version": BROWSER_OBSERVATION_SCHEMA_VERSION,
            "engine": engine_name,
            "browser_version": version,
            "executable_path": executable_path,
            "browser_pid": browser_pid,
            "process_start_ticks": process_start_ticks,
            "dom_marker_expected": marker,
            "dom_marker_observed": observed,
            "page_url": page_url,
            "console_errors": console_errors,
            "page_errors": page_errors,
            "closed_cleanly": closed_cleanly,
            "started_at": started_at,
            "finished_at": utc_now(),
        }
        return {
            "engine": engine_name,
            "version": version,
            "dom_marker": observed,
            "raw_observation": observation,
        }


class DependencyPreflight:
    def __init__(
        self,
        artifact_root: Path,
        external_probes: Mapping[str, ExternalProbeSpec],
        *,
        browser_launcher: Callable[[str], Mapping[str, Any]] = _default_browser_launcher,
        process_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
    ) -> None:
        self.artifact_root = Path(artifact_root)
        self.external_probes = dict(external_probes)
        self.browser_launcher = browser_launcher
        self.process_runner = process_runner
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    def _browser(self, engine: str) -> dict[str, Any]:
        started = time.monotonic()
        try:
            evidence = dict(self.browser_launcher(engine))
            observation = evidence.pop("raw_observation", None)
            if (
                evidence.get("engine") != engine
                or not _nonempty(evidence.get("version"))
                or not _nonempty(evidence.get("dom_marker"))
                or not isinstance(observation, Mapping)
            ):
                return _result(f"browser_{engine}", PreflightStatus.FAIL_HARNESS, started, error="incomplete launch evidence")
            expected_fields = {
                "schema_version", "engine", "browser_version", "executable_path",
                "browser_pid", "process_start_ticks", "dom_marker_expected",
                "dom_marker_observed", "page_url", "console_errors", "page_errors",
                "closed_cleanly", "started_at", "finished_at",
            }
            if (
                set(observation) != expected_fields
                or observation.get("schema_version") != BROWSER_OBSERVATION_SCHEMA_VERSION
                or observation.get("engine") != engine
                or observation.get("browser_version") != evidence["version"]
                or observation.get("dom_marker_observed") != evidence["dom_marker"]
                or observation.get("dom_marker_expected") != evidence["dom_marker"]
                or observation.get("closed_cleanly") is not True
                or observation.get("console_errors") != []
                or observation.get("page_errors") != []
                or type(observation.get("browser_pid")) is not int
                or observation["browser_pid"] <= 0
                or type(observation.get("process_start_ticks")) is not int
                or observation["process_start_ticks"] <= 0
            ):
                return _result(f"browser_{engine}", PreflightStatus.FAIL_HARNESS, started, error="invalid raw launch observation")
            raw_path = (self.artifact_root / f"browser_{engine}_launch.json").resolve()
            atomic_write_json(raw_path, observation)
            evidence["raw_authority_path"] = str(raw_path)
            evidence["raw_authority_sha256"] = _sha256(raw_path)
            return _result(f"browser_{engine}", PreflightStatus.PASS, started, evidence=evidence)
        except Exception as exc:
            text = f"{exc.__class__.__name__}: {exc}"
            status = _browser_failure_status(text)
            return _result(f"browser_{engine}", status, started, error=text)

    def _hls(self) -> dict[str, Any]:
        started = time.monotonic()
        ffmpeg_path = shutil.which(self.ffmpeg)
        ffprobe_path = shutil.which(self.ffprobe)
        if not ffmpeg_path or not ffprobe_path:
            return _result("ffmpeg_hls", PreflightStatus.BLOCKED, started, ffmpeg=ffmpeg_path, ffprobe=ffprobe_path)
        work = self.artifact_root / "ffmpeg_hls"
        work.mkdir(parents=True, exist_ok=True)
        playlist = work / "playlist.m3u8"
        segment_pattern = work / "segment_%03d.ts"
        command = (
            ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-filter_threads", "1",
            "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
            "-t", "2", "-threads", "1", "-c:v", "mpeg2video", "-f", "hls",
            "-g", "20", "-hls_time", "10", "-hls_list_size", "0",
            "-hls_segment_filename", str(segment_pattern), str(playlist),
        )
        try:
            generated = self.process_runner(command, capture_output=True, text=True, timeout=45, check=False)
            if generated.returncode != 0:
                return _result("ffmpeg_hls", PreflightStatus.FAIL_INFRA, started, error=generated.stderr[-2000:])
            segments = sorted(work.glob("segment_*.ts"))
            if not playlist.is_file() or len(segments) != 1 or segments[0].stat().st_size <= 0:
                return _result("ffmpeg_hls", PreflightStatus.FAIL_HARNESS, started, error="playlist or segment missing")
            text = playlist.read_text(encoding="utf-8")
            if "#EXTM3U" not in text or "#EXT-X-ENDLIST" not in text:
                return _result("ffmpeg_hls", PreflightStatus.FAIL_HARNESS, started, error="invalid HLS playlist")
            segment_uris = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
            if segment_uris != [segments[0].name]:
                return _result("ffmpeg_hls", PreflightStatus.FAIL_HARNESS, started, error="playlist must reference exactly one reviewed segment")
            inspected = self.process_runner(
                (ffprobe_path, "-v", "error", "-show_entries", "stream=codec_type", "-show_entries", "format=duration", "-of", "json", str(playlist)),
                capture_output=True, text=True, timeout=30, check=False,
            )
            if inspected.returncode != 0:
                return _result("ffmpeg_hls", PreflightStatus.FAIL_INFRA, started, error=inspected.stderr[-2000:])
            metadata = json.loads(inspected.stdout)
            has_video = any(item.get("codec_type") == "video" for item in metadata.get("streams") or [])
            duration = float((metadata.get("format") or {}).get("duration") or 0.0)
            if not has_video or duration <= 0:
                return _result("ffmpeg_hls", PreflightStatus.FAIL_HARNESS, started, error="ffprobe did not validate video/duration")
            ffprobe_observation = {
                "schema_version": FFPROBE_OBSERVATION_SCHEMA_VERSION,
                "input_path": str(playlist.resolve()),
                "returncode": 0,
                "segment_count": 1,
                "segment_sha256": _sha256(segments[0]),
                "streams": metadata.get("streams") or [],
                "format": metadata.get("format") or {},
            }
            ffprobe_authority = (work / "ffprobe.json").resolve()
            atomic_write_json(ffprobe_authority, ffprobe_observation)
            return _result("ffmpeg_hls", PreflightStatus.PASS, started, evidence={
                "playlist": str(playlist.resolve()),
                "segment_path": str(segments[0].resolve()),
                "ffprobe_path": str(ffprobe_authority),
                "segments": 1,
                "duration_seconds": duration,
                "playlist_parsed": True,
                "segments_nonempty": True,
            })
        except Exception as exc:
            return _result("ffmpeg_hls", PreflightStatus.FAIL_HARNESS, started, error=f"{exc.__class__.__name__}: {exc}")

    def _external(self, dependency: str) -> dict[str, Any]:
        started = time.monotonic()
        spec = self.external_probes.get(dependency)
        if spec is None or not spec.command:
            return _result(dependency, PreflightStatus.BLOCKED, started, error="mandatory probe command is not configured")
        report_path = self.artifact_root / "external" / f"{dependency}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.unlink(missing_ok=True)
        command = tuple(item.replace("{result_path}", str(report_path)) for item in spec.command)
        environment = {**os.environ, **dict(spec.environment), "HACKME_LEVEL1_RESULT_PATH": str(report_path)}
        try:
            completed = self.process_runner(
                command, capture_output=True, text=True, timeout=spec.timeout_seconds,
                check=False, env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            return _result(dependency, PreflightStatus.FAIL_EXTERNAL, started, error=f"timeout after {exc.timeout}s")
        except Exception as exc:
            return _result(dependency, PreflightStatus.FAIL_HARNESS, started, error=f"{exc.__class__.__name__}: {exc}")
        if not report_path.is_file():
            return _result(dependency, PreflightStatus.FAIL_HARNESS, started, returncode=completed.returncode, error="probe did not create result_path")
        try:
            if report_path.stat().st_size > MAX_EXTERNAL_REPORT_BYTES:
                raise ValueError("report exceeds size limit")
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return _result(dependency, PreflightStatus.FAIL_HARNESS, started, error=f"invalid JSON report: {exc}")
        if not isinstance(payload, Mapping):
            return _result(dependency, PreflightStatus.FAIL_HARNESS, started, error="report root is not an object")
        if payload.get("available") is not True:
            return _result(dependency, PreflightStatus.BLOCKED, started, returncode=completed.returncode, evidence=dict(payload), error="dependency unavailable before campaign")
        valid, errors = validate_external_probe(dependency, payload)
        artifact_errors: list[str] = []
        if valid:
            artifact_errors = verify_external_artifacts(dependency, payload)
            errors.extend(artifact_errors)
            valid = not errors
        if valid and completed.returncode == 0:
            return _result(dependency, PreflightStatus.PASS, started, evidence=dict(payload))
        if artifact_errors:
            status = PreflightStatus.FAIL_HARNESS
        elif errors:
            status = PreflightStatus.FAIL_HARNESS if any(name in errors for name in ("schema_version", "dependency", "evidence", "unknown_dependency")) else PreflightStatus.FAIL_PRODUCT
        else:
            status = PreflightStatus.FAIL_EXTERNAL if payload.get("failure_kind") == "external" else PreflightStatus.FAIL_PRODUCT
        return _result(dependency, status, started, returncode=completed.returncode, contract_errors=errors, evidence=dict(payload))

    def run(self) -> dict[str, Any]:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        checks = [self._browser(engine) for engine in REQUIRED_BROWSER_ENGINES]
        checks.append(self._hls())
        checks.extend(self._external(name) for name in REQUIRED_EXTERNAL_PROBES)
        statuses = [PreflightStatus(item["status"]) for item in checks]
        priority = (
            PreflightStatus.FAIL_HARNESS, PreflightStatus.FAIL_PRODUCT,
            PreflightStatus.FAIL_INFRA, PreflightStatus.FAIL_EXTERNAL,
            PreflightStatus.BLOCKED,
        )
        overall = PreflightStatus.PASS
        for candidate in priority:
            if candidate in statuses:
                overall = candidate
                break
        payload = {
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
            "checked_at": utc_now(),
            "status": overall.value,
            "ok": overall is PreflightStatus.PASS,
            "mandatory_checks": [item["name"] for item in checks],
            "failed_checks": [item["name"] for item in checks if not item["ok"]],
            "checks": checks,
        }
        atomic_write_json(self.artifact_root / "dependency_preflight.json", payload)
        return payload


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run fail-closed Level 1 dependency preflight")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    specs = {
        str(item.get("dependency") or ""): ExternalProbeSpec.from_dict(item)
        for item in config.get("external_probes") or []
    }
    result = DependencyPreflight(args.artifact_root, specs).run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
