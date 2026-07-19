from __future__ import annotations

import json
import hashlib
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.testing.campaign_watchdog_sigstop_e2e import (
    AuthenticatedControlRuntime,
    EXACT_STALE_SECONDS,
    EXPECTED_LIMITS,
    INCIDENT_EXIT_CODE,
    REQUIRED_ASSERTIONS,
    SCHEMA_VERSION,
    SigstopE2EError,
    _artifact_record,
    _build_authenticated_watchdog_config,
    _orchestrator_fixture,
    _timed_path_lock,
    _verify_signed_runner_artifacts,
    _verify_watchdog_liveness,
    _wait_for_json,
    _wait_for_watchdog_incident,
    _write_fail_closed_control,
    _write_signed_runner_artifacts,
    assess_e2e_evidence,
    build_parser,
    main,
    validate_campaign_root,
)
from scripts.testing.campaign_control_channel import (
    ControlChannelError,
    sign_authenticated_payload,
)
from scripts.testing.campaign_watchdog import (
    WatchdogPaths,
    build_watchdog_command,
    capture_process_identity,
)


COMMIT = "a" * 40
SCOPE = "/user.slice/user-1000.slice/hackme-web-campaign-test.scope"
SUPERVISOR_CGROUP = "/user.slice/user-1000.slice/session-test.scope"
BOOT_ID = "11111111-2222-3333-4444-555555555555"


def _passing_evidence() -> dict:
    placement_inside = {
        "ok": True,
        "inside_campaign_scope": True,
        "campaign_cgroup": SCOPE,
        "procfs_cgroupfs_agree": True,
    }
    placement_outside = {
        "ok": True,
        "inside_campaign_scope": False,
        "campaign_cgroup": SCOPE,
        "procfs_cgroupfs_agree": True,
    }
    artifact = {
        "artifact_id": "watchdog_incident",
        "path": "/tmp/campaign/artifacts/watchdog_incident.json",
        "relative_path": "artifacts/watchdog_incident.json",
        "media_type": "application/json",
        "schema_version": "hackme.campaign-watchdog.v1",
        "sha256": "b" * 64,
        "size": 100,
        "validated": True,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "campaign_uuid": "watchdog-sigstop-test",
        "commit": COMMIT,
        "actual_external_execution": True,
        "stale_timeout_seconds": EXACT_STALE_SECONDS,
        "source": {
            "actual_commit": COMMIT,
            "expected_commit": COMMIT,
            "commit_matches": True,
            "worktree_clean": True,
        },
        "cgroup": {
            "path": SCOPE,
            "limits": {
                "ok": True,
                "hard_limit_state": "verified",
                "checks": {
                    "memory.high": {"actual": EXPECTED_LIMITS["memory.high"], "ok": True},
                    "memory.max": {"actual": EXPECTED_LIMITS["memory.max"], "ok": True},
                    "memory.swap.max": {"actual": EXPECTED_LIMITS["memory.swap.max"], "ok": True},
                    "pids.max": {"actual": EXPECTED_LIMITS["pids.max"], "ok": True},
                    "cpu.max": {"actual_percent": EXPECTED_LIMITS["cpu.quota_percent"], "ok": True},
                    "io.weight": {"actual": EXPECTED_LIMITS["io.weight"], "ok": True},
                },
            },
            "after_pids": [],
            "cleanup": {"ok": True, "cgroup_empty": True},
        },
        "processes": {
            "supervisor": {
                "pid": 42000,
                "start_ticks": 1000,
                "boot_id": BOOT_ID,
                "cgroup": SUPERVISOR_CGROUP,
                "placement": placement_outside,
            },
            "orchestrator": {
                "pid": 42001,
                "start_ticks": 1001,
                "boot_id": BOOT_ID,
                "cgroup": SCOPE,
                "placement": placement_inside,
                "state_after_sigstop": "T",
                "terminated": True,
            },
            "load": {
                "pid": 42002,
                "start_ticks": 1002,
                "boot_id": BOOT_ID,
                "cgroup": SCOPE,
                "placement": placement_inside,
                "terminated": True,
            },
            "watchdog": {
                "pid": 42003,
                "start_ticks": 1003,
                "boot_id": BOOT_ID,
                "cgroup": SUPERVISOR_CGROUP,
                "placement": placement_outside,
                "terminated_after_result": True,
                "returncode": INCIDENT_EXIT_CODE,
            },
        },
        "authentication": {
            "socket": {
                "ok": True,
                "transport": "unix_sock_seqpacket",
                "mode": "0o600",
                "directory_mode": "0o700",
                "directory_path_pinned": True,
                "socket_path_pinned": True,
            },
            "supervisor_identity": {
                "pid": 42000,
                "start_ticks": 1000,
                "boot_id": BOOT_ID,
                "cgroup_path": SUPERVISOR_CGROUP,
            },
            "runner_key_sha256": "c" * 64,
            "watchdog_key_sha256": "d" * 64,
            "role_separated_keys": True,
            "session_keys_persisted": False,
            "runner": {
                "ok": True,
                "anti_replay_verified": True,
                "handshake": {"role": "runner"},
                "placement": placement_inside,
                "session_secret_sha256": "c" * 64,
                "session_secret_persisted": False,
            },
            "runner_client": {
                "server_identity_verified": True,
                "session_secret_received": True,
                "role": "runner",
                "session_secret_sha256": "c" * 64,
                "server_process": {
                    "pid": 42000,
                    "start_ticks": 1000,
                    "boot_id": BOOT_ID,
                    "cgroup_path": SUPERVISOR_CGROUP,
                },
            },
            "watchdog": {
                "ok": True,
                "anti_replay_verified": True,
                "handshake": {"role": "watchdog"},
                "placement": placement_outside,
                "session_secret_sha256": "d" * 64,
                "session_secret_persisted": False,
            },
            "watchdog_client": {
                "server_identity_verified": True,
                "session_secret_received": True,
                "role": "watchdog",
                "session_secret_sha256": "d" * 64,
                "server_process": {
                    "pid": 42000,
                    "start_ticks": 1000,
                    "boot_id": BOOT_ID,
                    "cgroup_path": SUPERVISOR_CGROUP,
                },
            },
            "signed_runner_streams_at_sigstop": {
                "ok": True,
                "role_key": "runner_derived_key",
                "orchestrator_pid": 42001,
                "orchestrator_start_ticks": 1001,
                "checkpoint_revision": 8,
                "heartbeat": {
                    "mac_verified": True,
                    "replay_checked": True,
                    "stream": "runner_heartbeat",
                },
                "checkpoint": {
                    "mac_verified": True,
                    "replay_checked": True,
                    "stream": "runner_checkpoint",
                },
            },
            "cleanup": {
                "ok": True,
                "socket_removed": True,
                "directory_removed": True,
            },
        },
        "watchdog": {
            "initial": {
                "verified": True,
                "external_process": True,
                "watchdog_outside_campaign_cgroup": True,
                "initial_health": {"ok": True},
            },
            "final": {
                "reason": "HEARTBEAT_STALE",
                "evidence_path": "/tmp/campaign/artifacts/watchdog/watchdog_incident.json",
                "finished_at": "2026-07-13T00:02:01Z",
                "cgroup_stop": {
                    "freeze_written": True,
                    "kill_written": True,
                    "population_cleared": True,
                },
            },
            "liveness_initial": {
                "ok": True,
                "mac_verified": True,
                "replay_checked": True,
                "stream": "watchdog_liveness",
                "sequence": 1,
                "role_key": "watchdog_master_key",
                "process_identity_reverified": True,
                "age_seconds": 0.1,
                "watchdog": {
                    "pid": 42003,
                    "start_ticks": 1003,
                    "boot_id": BOOT_ID,
                    "cgroup": SUPERVISOR_CGROUP,
                },
            },
            "liveness_final": {
                "ok": True,
                "mac_verified": True,
                "replay_checked": True,
                "stream": "watchdog_liveness",
                "sequence": 120,
                "role_key": "watchdog_master_key",
                "process_identity_reverified": False,
                "age_seconds": 0.5,
            },
            "liveness_monitor": {
                "ok": True,
                "fail_closed_on_invalid": True,
                "samples_verified": 480,
                "first_sequence": 1,
                "last_sequence": 120,
                "maximum_age_seconds": 1.2,
                "deadline_seconds": 10.0,
            },
        },
        "timings": {
            "heartbeat_last_monotonic_ns": 1_000_000_000,
            "sigstop_monotonic_ns": 1_100_000_000,
            "state_lock_guarded_sigstop": True,
            "admission_closed_monotonic_ns": 121_100_000_000,
            "timer_stopped_monotonic_ns": 121_100_000_000,
            "scope_empty_monotonic_ns": 121_200_000_000,
            "stale_observed_seconds": 120.1,
            "heartbeat_to_detection_seconds": 120.1,
            "sigstop_to_detection_seconds": 120.0,
        },
        "state": {
            "at_sigstop": {"clock": {"continuous_active_seconds": 2.5}},
            "final": {
                "state": "INTERRUPTED",
                "control": {"admit_new_jobs": False, "load_generator_should_run": False},
                "clock": {
                    "continuous_active_seconds": 2.5,
                    "formal_segment_valid": False,
                    "clock_pause_reason": "HEARTBEAT_STALE",
                    "active_finished_at": "2026-07-13T00:02:01Z",
                },
            },
            "final_control": {"admit_new_jobs": False, "load_generator_should_run": False},
        },
        "incident": {
            "schema_version": "hackme.campaign-watchdog.v1",
            "incident_id": "watchdog-test",
            "reason": "HEARTBEAT_STALE",
            "credential_material_collected": False,
        },
        "artifacts": [artifact],
    }


def test_complete_real_evidence_can_become_candidate() -> None:
    result = assess_e2e_evidence(_passing_evidence(), real_external_execution=True)

    assert result["status"] == "PASS_CANDIDATE"
    assert result["verification_scope"] == "end_to_end"
    assert result["machine_verified"] is True
    assert result["formal_gate_candidate"] is True
    assert result["failed_assertions"] == []
    assert set(result["assertions"]) == set(REQUIRED_ASSERTIONS)
    assert all(row["status"] == "PASS" and row["evidence"] for row in result["assertions"].values())


def test_component_assessment_never_claims_a_formal_gate() -> None:
    result = assess_e2e_evidence(_passing_evidence())

    assert result["status"] == "PARTIAL_PASS"
    assert result["verification_scope"] == "component_only"
    assert result["machine_verified"] is False
    assert result["formal_gate_candidate"] is False


@pytest.mark.parametrize(
    ("mutate", "failed_assertion"),
    [
        (lambda value: value["source"].update(worktree_clean=False), "source_at_commit_clean"),
        (lambda value: value["cgroup"]["limits"]["checks"]["memory.max"].update(actual=9), "exact_cgroup_limits"),
        (lambda value: value["cgroup"]["limits"]["checks"]["io.weight"].update(actual=100), "exact_cgroup_limits"),
        (lambda value: value["cgroup"]["limits"]["checks"].pop("io.weight"), "exact_cgroup_limits"),
        (lambda value: value["processes"]["supervisor"]["placement"].update(inside_campaign_scope=True), "supervisor_outside_scope"),
        (lambda value: value["processes"]["watchdog"]["placement"].update(inside_campaign_scope=True), "watchdog_outside_scope"),
        (lambda value: value["authentication"]["runner"].update(anti_replay_verified=False), "authenticated_control_channel"),
        (lambda value: value["authentication"].update(watchdog_key_sha256="c" * 64), "role_separated_auth_keys"),
        (lambda value: value["authentication"]["signed_runner_streams_at_sigstop"]["heartbeat"].update(mac_verified=False), "signed_runner_streams"),
        (lambda value: value["watchdog"]["liveness_initial"].update(mac_verified=False), "reciprocal_watchdog_liveness"),
        (lambda value: value["timings"].update(state_lock_guarded_sigstop=False), "sigstop_delivered"),
        (lambda value: value["timings"].update(stale_observed_seconds=119.999), "stale_timeout_120_observed"),
        (lambda value: value["state"]["final"]["control"].update(admit_new_jobs=True), "admission_closed"),
        (lambda value: value["state"]["final"]["clock"].update(continuous_active_seconds=3.0), "continuous_time_stopped"),
        (lambda value: value["cgroup"].update(after_pids=[42002]), "cgroup_empty_after"),
        (lambda value: value["artifacts"][0].update(sha256="not-a-hash"), "artifact_hashes_valid"),
    ],
)
def test_any_missing_proof_fails_closed(mutate, failed_assertion: str) -> None:
    evidence = _passing_evidence()
    mutate(evidence)

    result = assess_e2e_evidence(evidence, real_external_execution=True)

    assert result["status"] == "FAIL_HARNESS"
    assert result["formal_gate_candidate"] is False
    assert failed_assertion in result["failed_assertions"]


def test_campaign_root_must_be_strictly_below_tmp(tmp_path: Path) -> None:
    assert validate_campaign_root(tmp_path / "campaign").is_relative_to(Path("/tmp"))
    with pytest.raises(SigstopE2EError, match="strictly below /tmp"):
        validate_campaign_root(Path("/tmp"))
    with pytest.raises(SigstopE2EError, match="strictly below /tmp"):
        validate_campaign_root(Path.home() / "not-a-campaign")


@pytest.mark.parametrize("forbidden", ["--stale-after-seconds", "--development-mode"])
def test_cli_exposes_no_shortened_or_development_mode(tmp_path: Path, forbidden: str) -> None:
    extra = [forbidden, "1"] if forbidden == "--stale-after-seconds" else [forbidden]
    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "--campaign-root", str(tmp_path / "campaign"),
            "--expected-commit", COMMIT,
            *extra,
        ])


def test_wait_accepts_durable_terminal_artifact_after_process_exit(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"incident_id": "done", "reason": "HEARTBEAT_STALE"}), encoding="utf-8")

    class ExitedProcess:
        returncode = INCIDENT_EXIT_CODE

        @staticmethod
        def poll() -> int:
            return INCIDENT_EXIT_CODE

    payload = _wait_for_json(
        path,
        lambda value: value.get("incident_id") == "done",
        timeout=0.1,
        label="terminal watchdog evidence",
        process=ExitedProcess(),  # type: ignore[arg-type]
    )

    assert payload["reason"] == "HEARTBEAT_STALE"


def test_incident_wait_continuously_verifies_reciprocal_liveness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path = tmp_path / "watchdog.status.json"
    liveness_path = tmp_path / "watchdog.liveness.json"
    status_reads = iter([
        {"verified": True},
        {"verified": True},
        {"incident_id": "expected", "reason": "HEARTBEAT_STALE"},
    ])
    proofs = iter([
        {"sequence": 2, "payload_sha256": "b" * 64, "age_seconds": 0.2, "ok": True},
        {"sequence": 3, "payload_sha256": "c" * 64, "age_seconds": 0.3, "ok": True},
    ])
    monkeypatch.setattr(
        "scripts.testing.campaign_watchdog_sigstop_e2e.load_json",
        lambda path: next(status_reads) if path == status_path else {},
    )
    monkeypatch.setattr(
        "scripts.testing.campaign_watchdog_sigstop_e2e._verify_watchdog_liveness",
        lambda **_kwargs: next(proofs),
    )
    monkeypatch.setattr("scripts.testing.campaign_watchdog_sigstop_e2e.time.sleep", lambda _seconds: None)

    class RunningProcess:
        returncode = None

        @staticmethod
        def poll():
            return None

    status, final_liveness, monitor = _wait_for_watchdog_incident(
        status_path=status_path,
        liveness_path=liveness_path,
        process=RunningProcess(),  # type: ignore[arg-type]
        campaign_uuid="incident-monitor-test",
        expected_identity=SimpleNamespace(pid=42003),
        watchdog_auth_key=b"w" * 32,
        initial_liveness={"sequence": 1, "payload_sha256": "a" * 64, "age_seconds": 0.1},
        timeout=1.0,
    )

    assert status["incident_id"] == "expected"
    assert final_liveness["sequence"] == 3
    assert monitor["samples_verified"] == 2
    assert monitor["first_sequence"] == 1
    assert monitor["last_sequence"] == 3
    assert monitor["fail_closed_on_invalid"] is True


def test_incident_wait_fails_closed_on_invalid_watchdog_liveness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.testing.campaign_watchdog_sigstop_e2e.load_json",
        lambda _path: {"verified": True},
    )

    def invalid_liveness(**_kwargs):
        raise ControlChannelError("authenticated payload MAC mismatch")

    monkeypatch.setattr(
        "scripts.testing.campaign_watchdog_sigstop_e2e._verify_watchdog_liveness",
        invalid_liveness,
    )

    class RunningProcess:
        returncode = None

        @staticmethod
        def poll():
            return None

    with pytest.raises(SigstopE2EError, match="reciprocal liveness failed closed"):
        _wait_for_watchdog_incident(
            status_path=tmp_path / "watchdog.status.json",
            liveness_path=tmp_path / "watchdog.liveness.json",
            process=RunningProcess(),  # type: ignore[arg-type]
            campaign_uuid="incident-monitor-test",
            expected_identity=SimpleNamespace(pid=42003),
            watchdog_auth_key=b"w" * 32,
            initial_liveness={"sequence": 1, "payload_sha256": "a" * 64, "age_seconds": 0.1},
            timeout=1.0,
        )


def test_incident_wait_accepts_terminal_status_committed_during_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = iter([
        {"verified": True},
        {"incident_id": "durable", "reason": "HEARTBEAT_STALE"},
    ])
    monkeypatch.setattr(
        "scripts.testing.campaign_watchdog_sigstop_e2e.load_json",
        lambda _path: next(rows),
    )

    class ExitedProcess:
        returncode = INCIDENT_EXIT_CODE

        @staticmethod
        def poll():
            return INCIDENT_EXIT_CODE

    status, liveness, monitor = _wait_for_watchdog_incident(
        status_path=tmp_path / "watchdog.status.json",
        liveness_path=tmp_path / "watchdog.liveness.json",
        process=ExitedProcess(),  # type: ignore[arg-type]
        campaign_uuid="exit-race-test",
        expected_identity=SimpleNamespace(pid=42003),
        watchdog_auth_key=b"w" * 32,
        initial_liveness={"sequence": 3, "payload_sha256": "a" * 64, "age_seconds": 0.1},
        timeout=1.0,
    )

    assert status["incident_id"] == "durable"
    assert liveness["sequence"] == 3
    assert monitor["fail_closed_on_invalid"] is True


def test_fail_closed_mirror_stops_load_without_state_lock(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    (root / "checkpoint").mkdir(parents=True)

    _write_fail_closed_control(
        root,
        "fail-closed-test",
        "WATCHDOG_LIVENESS_INVALID",
        revision=9,
    )

    control = json.loads((root / "checkpoint" / "campaign.control.json").read_text(encoding="utf-8"))
    assert control["campaign_uuid"] == "fail-closed-test"
    assert control["revision"] == 9
    assert control["state"] == "FAILED"
    assert control["admit_new_jobs"] is False
    assert control["load_generator_should_run"] is False
    assert control["preserve_evidence_requested"] is True
    assert control["reason"] == "WATCHDOG_LIVENESS_INVALID"


def test_sigstop_state_lock_acquisition_has_a_finite_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def always_busy(_path, *, nonblocking=False):
        assert nonblocking is True
        raise BlockingIOError("busy")
        yield

    monkeypatch.setattr(
        "scripts.testing.campaign_watchdog_sigstop_e2e.locked_path",
        always_busy,
    )

    with pytest.raises(SigstopE2EError, match="timed out acquiring the state lock"):
        with _timed_path_lock(tmp_path / "state.lock", timeout=0.03):
            pytest.fail("lock should not have been acquired")


def test_cli_rejects_non_tmp_root_before_running(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([
        "--campaign-root", str(Path.home() / "watchdog-test"),
        "--expected-commit", COMMIT,
    ]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "FAIL_HARNESS"
    assert payload["formal_gate_candidate"] is False


def test_artifact_record_reparses_json_and_records_hash(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    path = root / "artifacts" / "evidence.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": "example.v1", "ok": True}), encoding="utf-8")

    record = _artifact_record(path, campaign_root=root, artifact_id="evidence")

    assert record["validated"] is True
    assert record["schema_version"] == "example.v1"
    assert record["size"] > 0
    assert len(record["sha256"]) == 64


def test_artifact_record_rejects_empty_and_non_object_json(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    path = root / "artifacts" / "bad.json"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    with pytest.raises(SigstopE2EError, match="empty"):
        _artifact_record(path, campaign_root=root, artifact_id="bad")

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SigstopE2EError, match="object"):
        _artifact_record(path, campaign_root=root, artifact_id="bad")


def test_runner_fixture_publishes_role_bound_signed_streams(tmp_path: Path) -> None:
    key = secrets.token_bytes(32)
    identity = SimpleNamespace(pid=42001, start_ticks=1001)
    heartbeat = tmp_path / "runner.heartbeat.json"
    checkpoint = tmp_path / "campaign.checkpoint.json"

    written = _write_signed_runner_artifacts(
        heartbeat_path=heartbeat,
        checkpoint_path=checkpoint,
        campaign_uuid="signed-runner-test",
        identity=identity,
        revision=7,
        runner_auth_key=key,
    )
    proof = _verify_signed_runner_artifacts(
        heartbeat_path=heartbeat,
        checkpoint_path=checkpoint,
        campaign_uuid="signed-runner-test",
        expected_identity=identity,
        runner_auth_key=key,
    )

    assert written["ok"] is True
    assert proof["ok"] is True
    assert proof["heartbeat"]["stream"] == "runner_heartbeat"
    assert proof["checkpoint"]["stream"] == "runner_checkpoint"
    assert proof["checkpoint_revision"] == 7

    tampered = json.loads(heartbeat.read_text(encoding="utf-8"))
    tampered["checkpoint_revision"] = 8
    heartbeat.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ControlChannelError, match="digest mismatch"):
        _verify_signed_runner_artifacts(
            heartbeat_path=heartbeat,
            checkpoint_path=checkpoint,
            campaign_uuid="signed-runner-test",
            expected_identity=identity,
            runner_auth_key=key,
        )


def test_watchdog_liveness_requires_watchdog_role_key(tmp_path: Path) -> None:
    key = secrets.token_bytes(32)
    wrong_key = secrets.token_bytes(32)
    identity = SimpleNamespace(
        pid=42003,
        start_ticks=1003,
        boot_id=BOOT_ID,
        cgroup_path=SUPERVISOR_CGROUP,
    )
    now_ns = time.monotonic_ns()
    payload = sign_authenticated_payload(
        {
            "schema_version": "hackme.campaign-watchdog-liveness.v1",
            "campaign_uuid": "signed-watchdog-test",
            "watchdog": {
                "pid": identity.pid,
                "start_ticks": identity.start_ticks,
                "boot_id": identity.boot_id,
                "cgroup": identity.cgroup_path,
                "monotonic_ns": now_ns,
            },
        },
        session_secret=key,
        campaign_uuid="signed-watchdog-test",
        stream="watchdog_liveness",
        sequence=1,
        monotonic_ns=now_ns,
    )
    path = tmp_path / "watchdog.liveness.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    proof = _verify_watchdog_liveness(
        path=path,
        campaign_uuid="signed-watchdog-test",
        expected_identity=identity,
        watchdog_auth_key=key,
        require_live_process=False,
    )
    assert proof["mac_verified"] is True
    assert proof["role_key"] == "watchdog_master_key"

    with pytest.raises(ControlChannelError, match="MAC mismatch"):
        _verify_watchdog_liveness(
            path=path,
            campaign_uuid="signed-watchdog-test",
            expected_identity=identity,
            watchdog_auth_key=wrong_key,
            require_live_process=False,
        )


def test_authenticated_control_runtime_is_private_pinned_and_removed() -> None:
    runtime = AuthenticatedControlRuntime(f"unit-{uuid.uuid4()}")
    try:
        socket_evidence = runtime.open()
        assert socket_evidence["transport"] == "unix_sock_seqpacket"
        assert socket_evidence["mode"] == "0o600"
        assert socket_evidence["directory_mode"] == "0o700"
        assert socket_evidence["socket_path_pinned"] is True
        assert socket_evidence["directory_path_pinned"] is True
        assert runtime.runner_auth_key != runtime.watchdog_auth_key
    finally:
        cleanup = runtime.close()

    assert cleanup == {
        "ok": True,
        "socket_removed": True,
        "directory_removed": True,
        "errors": [],
    }


def test_authenticated_runtime_refuses_inode_reuse_cleanup() -> None:
    runtime = AuthenticatedControlRuntime(f"inode-{uuid.uuid4()}")
    replacement: socket.socket | None = None
    runtime.open()
    try:
        runtime.path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        replacement.bind(str(runtime.path))

        cleanup = runtime.close()

        assert cleanup["ok"] is False
        assert "socket_identity_changed" in cleanup["errors"]
        assert runtime.path.exists()
        assert cleanup["socket_removed"] is False
        assert cleanup["directory_removed"] is False
    finally:
        if replacement is not None:
            replacement.close()
        if runtime.path.exists() or runtime.path.is_symlink():
            runtime.path.unlink()
        if runtime.directory.exists():
            runtime.directory.rmdir()


def test_authenticated_runtime_delivers_only_the_role_specific_key() -> None:
    runtime = AuthenticatedControlRuntime(f"roles-{uuid.uuid4()}")
    supervisor = capture_process_identity(os.getpid())
    child_source = """
import hashlib, json, os, sys, time
from pathlib import Path
from scripts.testing.campaign_control_channel import PeerIdentity, send_hello
result = send_hello(
    Path(sys.argv[1]),
    campaign_uuid=sys.argv[2],
    role=sys.argv[3],
    require_session_secret=True,
    expected_server_peer=PeerIdentity(int(sys.argv[4]), os.getuid(), os.getgid()),
    expected_server_process={
        'pid': int(sys.argv[4]),
        'start_ticks': int(sys.argv[5]),
        'boot_id': sys.argv[6],
        'cgroup_path': sys.argv[7],
    },
)
evidence, key = result
print(json.dumps({'key_sha256': hashlib.sha256(key).hexdigest(), 'evidence': evidence}), flush=True)
time.sleep(1)
"""
    results: dict[str, dict] = {}
    children: list[subprocess.Popen[str]] = []
    try:
        runtime.open()
        for role, key, expected_inside in (
            ("runner", runtime.runner_auth_key, True),
            ("watchdog", runtime.watchdog_auth_key, False),
        ):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_source,
                    str(runtime.path),
                    runtime.campaign_uuid,
                    role,
                    str(supervisor.pid),
                    str(supervisor.start_ticks),
                    supervisor.boot_id,
                    supervisor.cgroup_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                close_fds=True,
            )
            children.append(process)
            identity = capture_process_identity(process.pid)

            def placement(_pid, actual, *, inside=expected_inside):
                return {
                    "ok": True,
                    "pid": actual.pid,
                    "start_ticks": actual.start_ticks,
                    "boot_id": actual.boot_id,
                    "actual_cgroup": actual.cgroup_path,
                    "inside_campaign_scope": inside,
                }

            server_proof = runtime.authenticate(
                process=process,
                expected_identity=identity,
                role=role,
                session_secret=key,
                placement_check=placement,
                expected_inside=expected_inside,
            )
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == 0, stderr
            results[role] = json.loads(stdout)
            assert server_proof["anti_replay_verified"] is True
            assert server_proof["session_secret_sha256"] == hashlib.sha256(key).hexdigest()
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
        cleanup = runtime.close()

    assert cleanup["ok"] is True
    assert results["runner"]["key_sha256"] == hashlib.sha256(runtime.runner_auth_key).hexdigest()
    assert results["watchdog"]["key_sha256"] == hashlib.sha256(runtime.watchdog_auth_key).hexdigest()
    assert results["runner"]["key_sha256"] != results["watchdog"]["key_sha256"]
    assert results["runner"]["evidence"]["server_identity_verified"] is True
    assert results["watchdog"]["evidence"]["server_identity_verified"] is True


def test_production_watchdog_command_carries_auth_and_supervisor_identity(tmp_path: Path) -> None:
    orchestrator = SimpleNamespace(
        pid=42001,
        start_ticks=1001,
        boot_id=BOOT_ID,
        cgroup_path=SCOPE,
    )
    supervisor = SimpleNamespace(
        pid=42000,
        start_ticks=1000,
        boot_id=BOOT_ID,
        cgroup_path=SUPERVISOR_CGROUP,
    )
    paths = WatchdogPaths(
        campaign_root=tmp_path,
        state=tmp_path / "campaign.state.json",
        control=tmp_path / "campaign.control.json",
        heartbeat=tmp_path / "runner.heartbeat.json",
        checkpoint=tmp_path / "campaign.checkpoint.json",
        ready=tmp_path / "watchdog.status.json",
        evidence=tmp_path / "evidence",
        process_lock=tmp_path / "watchdog.lock",
        liveness=tmp_path / "watchdog.liveness.json",
    )
    auth_socket = Path("/tmp/.hws-fi-test/control.sock")
    config = _build_authenticated_watchdog_config(
        campaign_uuid="watchdog-contract-test",
        paths=paths,
        orchestrator_identity=orchestrator,
        scope_identity={"path": SCOPE, "device": 8, "inode": 9},
        supervisor_identity=supervisor,
        auth_socket=auth_socket,
    )
    command = build_watchdog_command(config)

    assert config.production is True
    assert config.auth_socket == auth_socket
    assert config.supervisor_pid == supervisor.pid
    assert config.supervisor_start_ticks == supervisor.start_ticks
    assert config.supervisor_boot_id == supervisor.boot_id
    assert config.supervisor_cgroup == supervisor.cgroup_path
    assert config.paths.heartbeat != config.paths.state
    assert config.paths.liveness not in {config.paths.state, config.paths.heartbeat, config.paths.checkpoint}
    assert "--auth-socket" in command
    assert str(auth_socket) in command
    assert "--supervisor-pid" in command
    assert str(supervisor.pid) in command
    assert "--development-mode" not in command
    assert not any("secret" in token.lower() or "auth-key" in token.lower() for token in command)


def test_internal_orchestrator_fails_closed_without_authenticated_supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = "/test.scope"
    monkeypatch.setenv("HACKME_CAMPAIGN_CGROUP_PATH", scope)
    monkeypatch.setattr(
        "scripts.testing.campaign_watchdog_sigstop_e2e.capture_process_identity",
        lambda _pid: SimpleNamespace(cgroup_path=scope, pid=42001, start_ticks=1001),
    )
    args = SimpleNamespace(
        campaign_root=str(tmp_path),
        campaign_uuid="runner-auth-required",
        expected_commit=COMMIT,
        auth_socket=None,
        supervisor_pid=0,
        supervisor_start_ticks=0,
        supervisor_boot_id="",
        supervisor_cgroup="",
    )

    with pytest.raises(SigstopE2EError, match="authenticated supervisor contract is incomplete"):
        _orchestrator_fixture(args)
