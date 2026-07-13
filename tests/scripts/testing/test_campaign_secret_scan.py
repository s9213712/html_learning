from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.testing import campaign_secret_scan as secret_scan_module
from scripts.testing import operational_campaign_24h as campaign_module
from scripts.testing.campaign_secret_scan import (
    ControlSnapshotConfig,
    SecretScanConfig,
    scan_campaign_secret_files,
    scan_campaign_secrets,
    snapshot_control_evidence,
)
from scripts.testing.campaign_state import CampaignStateMachine, process_start_ticks


def scan(
    root: Path,
    *,
    needles: dict[str, str | bytes] | None = None,
    controlled_runtime_roots: tuple[Path, ...] = (),
    progress_callback=None,
    **limits,
) -> dict:
    return scan_campaign_secrets(
        SecretScanConfig(
            artifact_root=root,
            needles=needles or {"credential": b"never-present"},
            controlled_runtime_roots=controlled_runtime_roots,
            **limits,
        ),
        progress_callback=progress_callback,
    )


def error_codes(result: dict) -> set[str]:
    return {str(row.get("code")) for row in result["errors"]}


def path_sha256(path: Path | str) -> str:
    return hashlib.sha256(os.fsencode(str(path))).hexdigest()


def test_secret_match_crosses_chunk_boundary_and_progress_is_byte_driven(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "trace.har"
    artifact.write_bytes(b"123456SE" + b"CRETtail")
    progress: list[str] = []

    result = scan(
        tmp_path,
        needles={"root": b"SECRET"},
        chunk_bytes=8,
        progress_bytes=8,
        progress_entries=100,
        progress_callback=progress.append,
    )

    assert result["ok"] is False
    assert result["hit_count"] == 1
    assert result["hits"][0]["byte_offset"] == 6
    assert result["files"] == result["files_scanned"] == 1
    assert result["bytes_scanned"] == artifact.stat().st_size
    assert result["error_count"] == 0
    assert result["progress_events"] >= 1
    assert progress and "bytes=" in progress[0]


def test_file_larger_than_historical_100_mib_limit_is_fully_streamed(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "large-video.bin"
    size = 100 * 1024 * 1024 + 4097
    with artifact.open("wb") as stream:
        stream.seek(size - 1)
        stream.write(b"\0")

    result = scan(tmp_path, chunk_bytes=4 * 1024 * 1024)

    assert result["ok"] is True
    assert result["files"] == result["files_attempted"] == result["files_scanned"] == 1
    assert result["bytes_scanned"] == size
    assert result["error_count"] == 0


def test_symlink_is_inventory_evidence_and_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"safe")
    link = tmp_path / "linked.bin"
    link.symlink_to(target)

    result = scan(tmp_path)

    assert result["ok"] is False
    assert result["symlink_count"] == 1
    assert result["symlinks"] == [{
        "path_sha256": path_sha256(link.name),
        "target_sha256": path_sha256(target),
    }]
    assert "path" not in result["symlinks"][0]
    assert "target" not in result["symlinks"][0]
    assert "artifact_symlink_rejected" in error_codes(result)


def test_file_open_error_is_not_silently_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = tmp_path / "blocked.bin"
    blocked.write_bytes(b"data")
    original_open = secret_scan_module.os.open

    def denied_open(path, flags, *args, **kwargs):
        if path == blocked.name:
            raise PermissionError("injected read denial")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(secret_scan_module.os, "open", denied_open)

    result = scan(tmp_path)

    assert result["ok"] is False
    assert result["files"] == 1
    assert result["files_attempted"] == 1
    assert result["files_scanned"] == 0
    assert "artifact_snapshot_failed" in error_codes(result)


def test_directory_enumeration_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "artifact.bin").write_bytes(b"data")

    def denied_scandir(_descriptor):
        raise PermissionError("injected enumeration denial")

    monkeypatch.setattr(secret_scan_module.os, "scandir", denied_scandir)

    result = scan(tmp_path)

    assert result["ok"] is False
    assert result["enumeration_complete"] is False
    assert "artifact_directory_enumeration_failed" in error_codes(result)


def test_controlled_private_secret_store_is_scanned_but_hits_are_exempted(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "primary"
    runtime.mkdir()
    protected = runtime / "restart_develop_server.env"
    protected.write_bytes(b"ROOT_PASSWORD=SECRET")
    protected.chmod(0o600)

    result = scan(
        tmp_path,
        needles={"root": b"SECRET"},
        controlled_runtime_roots=(runtime,),
        chunk_bytes=4,
    )

    assert result["ok"] is True
    assert result["hit_count"] == 0
    assert result["files"] == result["files_scanned"] == 1
    assert result["bytes_scanned"] == protected.stat().st_size
    assert result["protected_files"] == 1
    assert result["protected_bytes"] == protected.stat().st_size
    assert result["protected_secret_stores"] == [
        {
            "path_sha256": path_sha256(protected),
            "controlled_runtime_path": True,
            "owner_uid": os.getuid(),
            "expected_owner_uid": os.getuid(),
            "mode": "0o600",
            "size_bytes": protected.stat().st_size,
            "link_count": 1,
            "credential_hit_exempted": True,
            "stable_snapshot_verified": True,
            "ok": True,
            "scanned_bytes": protected.stat().st_size,
        }
    ]


@pytest.mark.parametrize("case", ["unsafe_mode", "outside_controlled_root"])
def test_protected_store_policy_mismatch_is_not_exempted(
    tmp_path: Path,
    case: str,
) -> None:
    runtime = tmp_path / "primary"
    runtime.mkdir()
    parent = runtime if case == "unsafe_mode" else tmp_path
    protected = parent / "restart_develop_server.env"
    protected.write_bytes(b"ROOT_PASSWORD=SECRET")
    protected.chmod(0o644 if case == "unsafe_mode" else 0o600)

    result = scan(
        tmp_path,
        needles={"root": b"SECRET"},
        controlled_runtime_roots=(runtime,),
    )

    assert result["ok"] is False
    assert result["hit_count"] == 1
    assert result["protected_files"] == 0
    assert result["protected_secret_stores"][0]["stable_snapshot_verified"] is True
    assert result["protected_secret_stores"][0]["credential_hit_exempted"] is False
    assert "protected_secret_store_policy_failed" in error_codes(result)


def test_entry_hard_cap_stops_bounded_enumeration_and_reports_progress(
    tmp_path: Path,
) -> None:
    for index in range(6):
        (tmp_path / f"artifact-{index}.bin").write_bytes(b"safe")
    progress: list[str] = []

    result = scan(
        tmp_path,
        max_entries=3,
        progress_entries=2,
        progress_bytes=1024,
        progress_callback=progress.append,
    )

    assert result["ok"] is False
    assert result["enumeration_complete"] is False
    assert result["entries"] == 4
    assert result["files_scanned"] <= 3
    assert "entry_count_hard_cap_exceeded" in error_codes(result)
    assert progress


def test_file_size_hard_cap_is_an_explicit_failure(tmp_path: Path) -> None:
    (tmp_path / "oversize.bin").write_bytes(b"x" * 11)

    result = scan(tmp_path, max_file_bytes=10)

    assert result["ok"] is False
    assert result["files"] == 1
    assert result["files_attempted"] == result["files_scanned"] == 0
    assert "file_size_hard_cap_exceeded" in error_codes(result)


@pytest.mark.parametrize("mutation", ["rewrite", "truncate", "replace"])
def test_concurrent_artifact_mutation_fails_stable_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    artifact = root / "mutable.bin"
    artifact.write_bytes(b"a" * 128)
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"b" * 128)
    original_read = secret_scan_module.os.read
    injected = False

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal injected
        data = original_read(descriptor, count)
        if not injected:
            injected = True
            if mutation == "rewrite":
                artifact.write_bytes(b"c" * 128)
            elif mutation == "truncate":
                artifact.write_bytes(b"d" * 4)
            else:
                os.replace(replacement, artifact)
        return data

    monkeypatch.setattr(secret_scan_module.os, "read", racing_read)

    result = scan(root, chunk_bytes=8)

    assert injected is True
    assert result["ok"] is False
    assert result["files_scanned"] == 0
    assert "artifact_snapshot_failed" in error_codes(result)


def test_campaign_secret_scan_progress_uses_external_real_state_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = campaign_module.build_parser().parse_args([
        "--campaign-root", str(tmp_path / "campaign"),
        "--duration-seconds", "60",
        "--allow-short-duration",
        "--primary-port", "55111",
        "--recovery-port", "55112",
        "--security-port", "55113",
        "--minimum-free-gb", "0",
    ])
    campaign = campaign_module.Campaign(args)
    campaign.root.mkdir(mode=0o700)
    (campaign.root / "large-trace.har").write_bytes(b"safe-artifact-" * 32)
    control_root = tmp_path / ".campaign-control"
    checkpoint = control_root / "checkpoint"
    checkpoint.mkdir(parents=True, mode=0o700)
    control_root.chmod(0o700)
    campaign.supervised = True
    campaign.control_root = control_root
    campaign.state_path = checkpoint / "campaign.state.json"
    campaign.control_path = checkpoint / "campaign.control.json"
    campaign.heartbeat_path = checkpoint / "campaign.heartbeat.json"
    campaign.checkpoint_path = checkpoint / "campaign.checkpoint.json"
    campaign.watchdog_ready_path = checkpoint / "watchdog.status.json"
    campaign.activation_gate_path = checkpoint / "campaign.activation.json"
    campaign.supervisor_contract_path = checkpoint / "supervisor.contract.json"
    campaign.source_freeze_path = control_root / "artifacts" / "source" / "H0" / "source_freeze.json"
    campaign.supervisor_contract = {
        "runner_stdout": str(control_root / "logs" / "runner.stdout"),
        "watchdog_stdout": str(control_root / "logs" / "watchdog.stdout"),
        "supervisor_source_root": str(control_root / "artifacts" / "source"),
    }
    campaign.state_machine = CampaignStateMachine(campaign.state_path)
    campaign.state_machine.initialize(
        campaign_uuid="integration-secret-scan",
        required_active_seconds=60,
        orchestrator_pid=os.getpid(),
        orchestrator_start_ticks=process_start_ticks(os.getpid()),
    )
    campaign.campaign_uuid = "integration-secret-scan"
    campaign_module.atomic_write_json(campaign.checkpoint_path, {
        "schema_version": "hackme.campaign-checkpoint.v1",
        "campaign_uuid": campaign.campaign_uuid,
        "revision": 1,
    })
    campaign._server_progress("secret_scan_integration_setup")
    initial_revision = campaign.main_progress_revision
    monkeypatch.setattr(campaign_module, "CAMPAIGN_SECRET_SCAN_PROGRESS_BYTES", 32)

    result = campaign.secret_scan()

    assert result["ok"] is True
    assert result["writer_seal"]["ok"] is True
    assert result["control_snapshot"]["ok"] is True
    assert result["artifact_cutoff_at"]
    assert result["post_scan_artifacts"]
    assert campaign.main_progress_revision > initial_revision
    assert campaign.root not in campaign.heartbeat_path.parents
    assert campaign.root not in campaign.state_path.parents
    expected_state_snapshot = path_sha256(
        "artifacts/runner_control_snapshot/checkpoint/campaign.state.json"
    )
    assert expected_state_snapshot in {
        row["path_sha256"] for row in result["file_inventory"]
    }
    assert all("path" not in row for row in result["file_inventory"])


def test_supervised_writer_seal_rejects_historical_in_root_control_layout(
    tmp_path: Path,
) -> None:
    args = campaign_module.build_parser().parse_args([
        "--campaign-root", str(tmp_path / "campaign"),
        "--duration-seconds", "60",
        "--allow-short-duration",
        "--primary-port", "55121",
        "--recovery-port", "55122",
        "--security-port", "55123",
        "--minimum-free-gb", "0",
    ])
    campaign = campaign_module.Campaign(args)
    campaign.root.mkdir(mode=0o700)
    campaign.supervised = True
    campaign.supervisor_contract = {
        "runner_stdout": str(campaign.root / "logs" / "runner.stdout"),
        "watchdog_stdout": str(campaign.root / "logs" / "watchdog.stdout"),
        "supervisor_source_root": str(campaign.root / "artifacts" / "source"),
    }

    result = campaign.seal_artifact_writers_for_secret_scan()

    assert result["ok"] is False
    assert "external_control_root_invalid" in {
        row["code"] for row in result["errors"]
    }
    assert "live_control_writer_inside_artifact_root" in {
        row["code"] for row in result["errors"]
    }


def test_control_snapshot_copies_every_regular_file_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    source = tmp_path / ".control"
    source.mkdir(mode=0o700)
    (source / "checkpoint").mkdir()
    (source / "checkpoint" / "state.json").write_bytes(b"stable-state")
    snapshot = tmp_path / "campaign" / "artifacts" / "control-snapshot"

    result = snapshot_control_evidence(
        ControlSnapshotConfig(source_root=source, snapshot_root=snapshot)
    )

    assert result["ok"] is True
    assert result["files"] == 1
    assert (snapshot / "checkpoint" / "state.json").read_bytes() == b"stable-state"
    manifest_path = snapshot / "control_snapshot_manifest.json"
    assert manifest_path.is_file()
    assert result["manifest_path_sha256"] == path_sha256(manifest_path)
    assert result["manifest_readback_verified"] is True
    assert result["manifest_sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    assert manifest_path.stat().st_mode & 0o777 == 0o600

    unsafe_source = tmp_path / ".unsafe-control"
    unsafe_source.mkdir(mode=0o700)
    (unsafe_source / "target").write_bytes(b"data")
    (unsafe_source / "link").symlink_to(unsafe_source / "target")
    unsafe = snapshot_control_evidence(ControlSnapshotConfig(
        source_root=unsafe_source,
        snapshot_root=tmp_path / "campaign" / "artifacts" / "unsafe-snapshot",
        max_rounds=2,
    ))

    assert unsafe["ok"] is False
    assert "control_snapshot_symlink_rejected" in {
        row["code"] for row in unsafe["errors"]
    }


def test_control_snapshot_continuous_rewrite_exhausts_bounded_rounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".control"
    source.mkdir(mode=0o700)
    mutable = source / "state.json"
    mutable.write_bytes(b"a" * 128)
    original_read = secret_scan_module.os.read
    rewrites = 0

    def always_rewrite(descriptor: int, count: int) -> bytes:
        nonlocal rewrites
        data = original_read(descriptor, count)
        rewrites += 1
        mutable.write_bytes((b"a" if rewrites % 2 else b"b") * 128)
        return data

    monkeypatch.setattr(secret_scan_module.os, "read", always_rewrite)

    result = snapshot_control_evidence(ControlSnapshotConfig(
        source_root=source,
        snapshot_root=tmp_path / "campaign" / "artifacts" / "snapshot",
        chunk_bytes=8,
        max_rounds=2,
    ))

    assert rewrites > 0
    assert result["ok"] is False
    assert result["rounds"] == 2
    assert "control_snapshot_file_copy_failed" in {
        row["code"] for row in result["errors"]
    }


def test_control_snapshot_pins_source_root_against_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".control"
    source.mkdir(mode=0o700)
    (source / "state.json").write_bytes(b"trusted-state")
    moved = tmp_path / ".control-pinned"
    snapshot = tmp_path / "campaign" / "artifacts" / "snapshot"
    original_open = secret_scan_module.os.open
    injected = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal injected
        if path == "state.json" and dir_fd is not None and not injected:
            injected = True
            source.rename(moved)
            source.mkdir(mode=0o700)
            (source / "state.json").write_bytes(b"replacement-state")
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(secret_scan_module.os, "open", racing_open)

    result = snapshot_control_evidence(ControlSnapshotConfig(
        source_root=source,
        snapshot_root=snapshot,
        max_rounds=1,
    ))

    assert injected is True
    assert result["ok"] is False
    assert "control_snapshot_source_identity_changed" in error_codes(result)
    assert (snapshot / "state.json").read_bytes() == b"trusted-state"


def test_control_snapshot_deadline_is_checked_inside_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".control"
    source.mkdir(mode=0o700)
    (source / "state.json").write_bytes(b"state")
    ticks = 0.0

    def advancing_monotonic() -> float:
        nonlocal ticks
        ticks += 0.06
        return ticks

    monkeypatch.setattr(secret_scan_module.time, "monotonic", advancing_monotonic)

    result = snapshot_control_evidence(ControlSnapshotConfig(
        source_root=source,
        snapshot_root=tmp_path / "campaign" / "artifacts" / "snapshot",
        max_seconds=0.1,
    ))

    assert result["ok"] is False
    assert "control_snapshot_deadline_exceeded" in error_codes(result)


def test_control_snapshot_disk_reserve_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".control"
    source.mkdir(mode=0o700)
    (source / "state.json").write_bytes(b"state")
    monkeypatch.setattr(
        secret_scan_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=15),
    )

    result = snapshot_control_evidence(ControlSnapshotConfig(
        source_root=source,
        snapshot_root=tmp_path / "campaign" / "artifacts" / "snapshot",
        minimum_free_reserve_bytes=16,
    ))

    assert result["ok"] is False
    assert "control_snapshot_disk_reserve_breached" in error_codes(result)


def test_control_snapshot_manifest_readback_corruption_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".control"
    source.mkdir(mode=0o700)
    (source / "state.json").write_bytes(b"state")
    snapshot = tmp_path / "campaign" / "artifacts" / "snapshot"
    original_read = secret_scan_module.os.read

    def corrupt_manifest_readback(descriptor: int, count: int) -> bytes:
        data = original_read(descriptor, count)
        try:
            opened_path = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            opened_path = ""
        if opened_path.endswith("/control_snapshot_manifest.json") and data:
            return b"X" + data[1:]
        return data

    monkeypatch.setattr(secret_scan_module.os, "read", corrupt_manifest_readback)

    result = snapshot_control_evidence(ControlSnapshotConfig(
        source_root=source,
        snapshot_root=snapshot,
    ))

    assert result["ok"] is False
    assert "control_snapshot_manifest_verification_failed" in error_codes(result)
    assert result.get("manifest_readback_verified") is not True
    persisted = (snapshot / "control_snapshot_manifest.json").read_bytes()
    assert os.fsencode(str(source)) not in persisted
    assert os.fsencode(str(snapshot)) not in persisted


def test_exact_post_cutoff_file_scan_has_explicit_scope_and_inventory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "campaign"
    root.mkdir()
    final_report = root / "campaign_supervisor.json"
    final_report.write_bytes(b'{"status":"SECRET"}')

    result = scan_campaign_secret_files(
        SecretScanConfig(artifact_root=root, needles={"root": b"SECRET"}),
        (final_report,),
    )

    assert result["scope"] == "exact_files"
    assert result["expected_path_sha256"] == [path_sha256(final_report)]
    assert "expected_paths" not in result
    assert result["files"] == result["files_scanned"] == 1
    assert result["file_inventory"][0]["sha256"]
    assert result["hit_count"] == 1
    assert result["ok"] is False
