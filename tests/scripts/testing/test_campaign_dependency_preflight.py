from __future__ import annotations

import json
import hashlib
import subprocess
import tarfile
from pathlib import Path

from PIL import Image

from scripts.testing.campaign_dependency_preflight import (
    BROWSER_OBSERVATION_SCHEMA_VERSION,
    EXTERNAL_PROBE_SCHEMA_VERSION,
    DependencyPreflight,
    ExternalProbeSpec,
    REQUIRED_EXTERNAL_PROBES,
    _browser_failure_status,
    production_security_probe_contract,
    validate_external_probe,
)


def browser_launcher(engine: str) -> dict:
    marker = f"level1-{engine}"
    return {
        "engine": engine,
        "version": "1",
        "dom_marker": marker,
        "raw_observation": {
            "schema_version": BROWSER_OBSERVATION_SCHEMA_VERSION,
            "engine": engine,
            "browser_version": "1",
            "executable_path": f"/opt/{engine}",
            "browser_pid": 1000 + len(engine),
            "process_start_ticks": 123456 + len(engine),
            "dom_marker_expected": marker,
            "dom_marker_observed": marker,
            "page_url": "about:blank",
            "console_errors": [],
            "page_errors": [],
            "closed_cleanly": True,
            "started_at": "2026-07-13T08:00:00Z",
            "finished_at": "2026-07-13T08:00:01Z",
        },
    }


def canonical(dependency: str) -> dict:
    evidence = {
        "bt_seed_download": {
            "seed_started": True, "torrent_created": True, "peer_observed": True,
            "download_terminal": True, "payload_sha256_match": True,
            "downloaded_via_bt": True, "info_hash": "a" * 40,
            "download_path": "/tmp/bt-download.bin", "payload_sha256": "b" * 64,
        },
        "comfyui_terminal": {
            "job_submitted": True, "terminal_polled": True, "history_terminal": True,
            "output_exists": True, "output_decodable": True,
            "job_id": "job-1", "prompt_id": "prompt-1",
            "output_path": "/tmp/comfy-output.png", "output_sha256": "b" * 64,
        },
        "ai_provider_terminal": {
            "provider_called": True, "terminal_polled": True, "response_nonempty": True,
            "usage_reported": True, "provider": "openai-compatible", "model": "real-model",
            "request_id": "request-1",
        },
        "backup_restore": {
            "archive_created": True, "archive_readable": True, "restore_completed": True,
            "source_restore_digest_match": True, "sqlite_quick_check": True,
            "manifest_validated": True, "consistent_snapshot_created": True,
            "wal_checkpoint_completed": True, "snapshot_marker_verified": True,
            "backup_api_completed": True, "snapshot_method": "sqlite_backup_api",
            "snapshot_marker_id": "snapshot-marker-1",
            "snapshot_id": "snapshot-1", "archive_sha256": "b" * 64,
            "archive_path": "/tmp/runtime-backup.tar",
        },
        "production_security_sentinel": {
            "production_mode": True, "csrf_enforced": True, "rbac_enforced": True,
            "confirmation_enforced": True, "audit_chain_verified": True,
            "cross_worker_session_verified": True,
        },
    }[dependency]
    return {
        "schema_version": EXTERNAL_PROBE_SCHEMA_VERSION,
        "dependency": dependency,
        "available": True,
        "synthetic": False,
        "terminal_state": "completed",
        "evidence": evidence,
    }


def test_external_contract_rejects_ok_only_and_synthetic_fake_green() -> None:
    valid, errors = validate_external_probe("ai_provider_terminal", {"ok": True})
    assert valid is False
    assert {"schema_version", "available", "synthetic", "terminal_state", "evidence"} <= set(errors)

    payload = canonical("ai_provider_terminal")
    payload["synthetic"] = True
    valid, errors = validate_external_probe("ai_provider_terminal", payload)
    assert valid is False
    assert "synthetic" in errors


def test_each_external_contract_requires_terminal_side_effects() -> None:
    for dependency in REQUIRED_EXTERNAL_PROBES:
        payload = canonical(dependency)
        valid, errors = validate_external_probe(dependency, payload)
        assert valid is True, (dependency, errors)
        first = next(key for key, value in payload["evidence"].items() if value is True)
        payload["evidence"][first] = False
        valid, errors = validate_external_probe(dependency, payload)
        assert valid is False, dependency


def test_browser_unavailable_and_runtime_failures_are_distinct() -> None:
    assert _browser_failure_status("Executable doesn't exist").value == "BLOCKED"
    assert _browser_failure_status("Missing libraries: libicu.so").value == "BLOCKED"
    assert _browser_failure_status("sandbox shutdown: Operation not permitted").value == "FAIL_INFRA"


def test_native_production_security_report_requires_every_boundary() -> None:
    names = (
        "production_launcher_contract", "production_mode_active",
        "login_missing_csrf_denied", "authenticated_missing_csrf_denied",
        "anonymous_root_denied", "manager_root_boundary_denied", "user_root_boundary_denied",
        "dangerous_confirmation_required", "production_security_controls", "audit_log_chain",
        "cross_worker_session_consistency",
    )
    native = {"ok": True, "checks": [{"name": name, "ok": True} for name in names], "failed_checks": []}
    contract = production_security_probe_contract(native)
    assert validate_external_probe("production_security_sentinel", contract) == (True, [])

    native["checks"][-1]["ok"] = False
    contract = production_security_probe_contract(native)
    valid, errors = validate_external_probe("production_security_sentinel", contract)
    assert valid is False
    assert "terminal_state" in errors


def test_missing_external_probes_are_blocked_not_skipped(tmp_path: Path, monkeypatch) -> None:
    preflight = DependencyPreflight(
        tmp_path,
        {},
        browser_launcher=browser_launcher,
    )
    monkeypatch.setattr(preflight, "_hls", lambda: {
        "name": "ffmpeg_hls", "status": "PASS", "ok": True,
        "elapsed_seconds": 0.0, "details": {},
    })
    result = preflight.run()
    assert result["ok"] is False
    assert result["status"] == "BLOCKED"
    assert set(REQUIRED_EXTERNAL_PROBES) <= set(result["failed_checks"])
    assert json.loads((tmp_path / "dependency_preflight.json").read_text())["status"] == "BLOCKED"


def test_external_probe_report_is_fresh_and_machine_validated(tmp_path: Path, monkeypatch) -> None:
    specs = {
        name: ExternalProbeSpec(name, ("probe", name, "{result_path}"), timeout_seconds=3)
        for name in REQUIRED_EXTERNAL_PROBES
    }

    def runner(command, **_kwargs):
        dependency = command[1]
        payload = canonical(dependency)
        evidence = payload["evidence"]
        if dependency == "bt_seed_download":
            artifact = tmp_path / "bt.bin"
            artifact.write_bytes(b"bt payload")
            evidence.update(download_path=str(artifact), payload_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
        elif dependency == "comfyui_terminal":
            artifact = tmp_path / "comfy.png"
            Image.new("RGB", (2, 2), (1, 2, 3)).save(artifact)
            evidence.update(output_path=str(artifact), output_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
        elif dependency == "backup_restore":
            member = tmp_path / "backup-member.txt"
            member.write_text("backup", encoding="utf-8")
            artifact = tmp_path / "backup.tar"
            with tarfile.open(artifact, "w:") as archive:
                archive.add(member, arcname="backup-member.txt")
            evidence.update(archive_path=str(artifact), archive_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
        Path(command[2]).write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    preflight = DependencyPreflight(
        tmp_path,
        specs,
        browser_launcher=browser_launcher,
        process_runner=runner,
    )
    monkeypatch.setattr(preflight, "_hls", lambda: {
        "name": "ffmpeg_hls", "status": "PASS", "ok": True,
        "elapsed_seconds": 0.0, "details": {},
    })
    result = preflight.run()
    assert result["ok"] is True
    assert result["failed_checks"] == []


def test_real_ffmpeg_generates_parseable_hls_when_installed(tmp_path: Path) -> None:
    preflight = DependencyPreflight(tmp_path, {})
    result = preflight._hls()
    if result["status"] == "BLOCKED":
        return
    assert result["status"] == "PASS", result
    assert result["details"]["evidence"]["segments"] == 1
    assert result["details"]["evidence"]["duration_seconds"] > 0
    assert Path(result["details"]["evidence"]["segment_path"]).is_file()
    assert Path(result["details"]["evidence"]["ffprobe_path"]).is_file()


def test_hls_preflight_disables_stdin_and_bounds_ffmpeg_threads(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command, **_kwargs):
        argv = tuple(str(value) for value in command)
        commands.append(argv)
        if Path(argv[0]).name == "ffmpeg":
            work = tmp_path / "ffmpeg_hls"
            (work / "segment_000.ts").write_bytes(b"segment")
            (work / "playlist.m3u8").write_text(
                "#EXTM3U\n#EXTINF:2.0,\nsegment_000.ts\n#EXT-X-ENDLIST\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({
                "streams": [{"codec_type": "video"}],
                "format": {"duration": "2.0"},
            }),
            "",
        )

    result = DependencyPreflight(tmp_path, {}, process_runner=runner)._hls()

    assert result["status"] == "PASS"
    ffmpeg_command = commands[0]
    assert "-nostdin" in ffmpeg_command
    assert ffmpeg_command[ffmpeg_command.index("-filter_threads") + 1] == "1"
    assert ffmpeg_command[ffmpeg_command.index("-threads") + 1] == "1"
    assert result["details"]["evidence"]["segment_path"].endswith("segment_000.ts")
    ffprobe = json.loads(Path(result["details"]["evidence"]["ffprobe_path"]).read_text())
    assert ffprobe["segment_count"] == 1


def test_browser_requires_and_hashes_raw_launch_authority(tmp_path: Path) -> None:
    result = DependencyPreflight(tmp_path, {}, browser_launcher=browser_launcher)._browser("chromium")
    assert result["status"] == "PASS"
    authority = Path(result["details"]["evidence"]["raw_authority_path"])
    assert authority.is_file()
    assert hashlib.sha256(authority.read_bytes()).hexdigest() == result["details"]["evidence"]["raw_authority_sha256"]

    incomplete = DependencyPreflight(
        tmp_path / "incomplete",
        {},
        browser_launcher=lambda engine: {
            "engine": engine,
            "version": "1",
            "dom_marker": f"level1-{engine}",
        },
    )._browser("chromium")
    assert incomplete["status"] == "FAIL_HARNESS"


def test_backup_artifact_rejects_compressed_tar(tmp_path: Path) -> None:
    payload = canonical("backup_restore")
    member = tmp_path / "member.txt"
    member.write_text("backup", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(member, arcname="member.txt")
    payload["evidence"].update(
        archive_path=str(archive),
        archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    from scripts.testing.campaign_dependency_preflight import verify_external_artifacts

    assert verify_external_artifacts("backup_restore", payload) == ["archive_unreadable"]
