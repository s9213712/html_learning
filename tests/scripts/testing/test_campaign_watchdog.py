from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.testing.campaign_watchdog import (
    CONTROL_SCHEMA_VERSION,
    INCIDENT_EXIT_CODE,
    CgroupIdentity,
    DuplicateWatchdogError,
    ExternalCampaignWatchdog,
    WatchdogConfig,
    WatchdogError,
    WatchdogPaths,
    build_watchdog_command,
    capture_cgroup_identity,
    capture_process_identity,
    load_json,
    locked_path,
)


CAMPAIGN_UUID = "campaign-watchdog-test-001"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def proc_stat(pid: int, *, start_ticks: int, state: str = "S", name: str = "campaign worker") -> str:
    # fields[0] is Linux stat field 3 (state); fields[19] is field 22
    # (starttime).  Parentheses in comm are intentional parser coverage.
    fields = [state] + ["0"] * 49
    fields[19] = str(start_ticks)
    return f"{pid} ({name}) " + " ".join(fields) + "\n"


def add_fake_process(
    proc_root: Path,
    *,
    pid: int,
    start_ticks: int,
    cgroup: str,
    state: str = "S",
) -> None:
    process = proc_root / str(pid)
    process.mkdir(parents=True, exist_ok=True)
    (process / "stat").write_text(proc_stat(pid, start_ticks=start_ticks, state=state), encoding="utf-8")
    (process / "cgroup").write_text(f"0::{cgroup}\n", encoding="utf-8")
    (process / "status").write_text(
        f"Name:\ttest\nState:\t{state}\nPid:\t{pid}\nPPid:\t1\nThreads:\t1\nVmRSS:\t16 kB\n",
        encoding="utf-8",
    )


def fake_cgroup(cgroup_root: Path, path: str = "/test.slice/campaign.scope") -> CgroupIdentity:
    target = cgroup_root / path.lstrip("/")
    target.mkdir(parents=True, exist_ok=True)
    files = {
        "cgroup.kill": "0\n",
        "cgroup.freeze": "0\n",
        "cgroup.procs": "",
        "cgroup.events": "populated 0\nfrozen 0\n",
        "memory.current": "1024\n",
        "memory.events": "oom 0\noom_kill 0\n",
        "memory.max": str(8 * 1024**3) + "\n",
        "memory.high": str(7 * 1024**3) + "\n",
        "memory.swap.max": str(1024**3) + "\n",
        "cpu.max": "600000 100000\n",
        "pids.current": "3\n",
        "pids.max": "768\n",
    }
    for name, value in files.items():
        (target / name).write_text(value, encoding="ascii")
    return capture_cgroup_identity(path, cgroup_root=cgroup_root)


def make_harness(
    tmp_path: Path,
    *,
    now_ns: int = 10_000_000_000,
    heartbeat_ns: int = 9_000_000_000,
    state: str = "ACTIVE",
    expected_start_ticks: int = 456,
    actual_start_ticks: int = 456,
    self_cgroup: str = "/watchdog.slice/watchdog.scope",
) -> tuple[ExternalCampaignWatchdog, WatchdogPaths, Path, Path]:
    root = tmp_path / "campaign"
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    boot_id_path = proc_root / "sys" / "kernel" / "random" / "boot_id"
    boot_id_path.parent.mkdir(parents=True, exist_ok=True)
    boot_id_path.write_text("boot-test-001\n", encoding="ascii")
    add_fake_process(
        proc_root,
        pid=4242,
        start_ticks=actual_start_ticks,
        cgroup="/orchestrator.slice/orchestrator.scope",
    )
    add_fake_process(
        proc_root,
        pid=os.getpid(),
        start_ticks=999,
        cgroup=self_cgroup,
    )
    cgroup_identity = fake_cgroup(cgroup_root)
    paths = WatchdogPaths(
        campaign_root=root,
        state=root / "checkpoint" / "state.json",
        control=root / "checkpoint" / "control.json",
        heartbeat=root / "checkpoint" / "heartbeat.json",
        checkpoint=root / "checkpoint" / "checkpoint.json",
        ready=root / "checkpoint" / "watchdog.json",
        evidence=root / "artifacts" / "watchdog",
        process_lock=root / "checkpoint" / "watchdog.lock",
    )
    write_json(paths.state, {
        "schema_version": "hackme.campaign-state.v1",
        "campaign_uuid": CAMPAIGN_UUID,
        "revision": 4,
        "state": state,
        "control": {
            "admit_new_jobs": state == "ACTIVE",
            "load_generator_should_run": state == "ACTIVE",
            "preserve_evidence_requested": False,
        },
        "clock": {
            "active_started_at": "2026-07-12T00:00:00Z",
            "continuous_active_seconds": 12.5,
            "wall_clock_seconds": 12.5,
            "invalid_seconds": 0.0,
            "formal_segment_valid": True,
            "last_tick_monotonic_ns": heartbeat_ns,
        },
        "heartbeat": {
            "orchestrator_pid": 4242,
            "orchestrator_start_ticks": expected_start_ticks,
            "orchestrator_monotonic_ns": heartbeat_ns,
            "checkpoint_revision": 3,
        },
    })
    write_json(paths.heartbeat, {
        "campaign_uuid": CAMPAIGN_UUID,
        "heartbeat": {
            "orchestrator_pid": 4242,
            "orchestrator_start_ticks": expected_start_ticks,
            "orchestrator_monotonic_ns": heartbeat_ns,
            "checkpoint_revision": 3,
        },
    })
    write_json(paths.checkpoint, {"campaign_uuid": CAMPAIGN_UUID, "revision": 3, "state": state})
    write_json(paths.control, {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "campaign_uuid": CAMPAIGN_UUID,
        "state": state,
        "admit_new_jobs": state == "ACTIVE",
    })
    config = WatchdogConfig(
        campaign_uuid=CAMPAIGN_UUID,
        paths=paths,
        orchestrator_pid=4242,
        orchestrator_start_ticks=expected_start_ticks,
        orchestrator_boot_id="boot-test-001",
        orchestrator_cgroup="/orchestrator.slice/orchestrator.scope",
        campaign_cgroup=cgroup_identity,
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        stale_after_seconds=120,
        poll_seconds=0.01,
        kill_verify_seconds=0,
        production=False,
    )
    return ExternalCampaignWatchdog(config, monotonic_ns=lambda: now_ns), paths, proc_root, cgroup_root


def test_healthy_watchdog_sample_records_external_heartbeat_without_stopping_load(tmp_path: Path) -> None:
    watchdog, paths, _proc_root, cgroup_root = make_harness(tmp_path)

    assert watchdog.run(once=True) == 0

    state = load_json(paths.state)
    assert state["state"] == "ACTIVE"
    assert state["control"]["admit_new_jobs"] is True
    assert state["heartbeat"]["watchdog_pid"] == os.getpid()
    assert state["heartbeat"]["watchdog_outside_campaign_cgroup"] is True
    assert load_json(paths.ready)["verified"] is True
    assert (cgroup_root / "test.slice" / "campaign.scope" / "cgroup.kill").read_text().startswith("0")


def test_stale_heartbeat_atomically_stops_admission_preserves_time_and_kills_scope(tmp_path: Path) -> None:
    watchdog, paths, _proc_root, cgroup_root = make_harness(
        tmp_path,
        now_ns=130_000_000_000,
        heartbeat_ns=9_000_000_000,
    )

    assert watchdog.run(once=True) == INCIDENT_EXIT_CODE

    state = load_json(paths.state)
    assert state["state"] == "INTERRUPTED"
    assert state["classification"] == "FAIL_HARNESS"
    assert state["reason"] == "HEARTBEAT_STALE"
    assert state["control"] == {
        "admit_new_jobs": False,
        "load_generator_should_run": False,
        "preserve_evidence_requested": True,
    }
    assert state["clock"]["continuous_active_seconds"] == 12.5
    assert state["clock"]["invalid_seconds"] == 121.0
    assert state["clock"]["formal_segment_valid"] is False
    assert state["clock"]["active_finished_at"]
    assert state["hard_stop"]["reason_code"] == "HEARTBEAT_STALE"
    assert [event["state"] for event in state["events"]] == [
        "STOPPING_LOAD",
        "PRESERVING_EVIDENCE",
        "INTERRUPTED",
    ]
    control = load_json(paths.control)
    assert control["admit_new_jobs"] is False
    assert control["load_generator_should_run"] is False
    assert (cgroup_root / "test.slice" / "campaign.scope" / "cgroup.freeze").read_text().startswith("1")
    assert (cgroup_root / "test.slice" / "campaign.scope" / "cgroup.kill").read_text().startswith("1")
    result = load_json(paths.ready)
    evidence = Path(result["evidence_path"])
    assert evidence.is_file()
    evidence_payload = load_json(evidence)
    assert evidence_payload["credential_material_collected"] is False
    assert evidence_payload["files"]["checkpoint"]["sha256"]


def test_pid_reuse_is_an_incident_and_never_signals_the_reused_pid(tmp_path: Path) -> None:
    watchdog, paths, _proc_root, cgroup_root = make_harness(
        tmp_path,
        expected_start_ticks=456,
        actual_start_ticks=9999,
    )

    assert watchdog.run(once=True) == INCIDENT_EXIT_CODE
    state = load_json(paths.state)
    assert state["hard_stop"]["reason_code"] == "ORCHESTRATOR_IDENTITY_LOST"
    assert "start_ticks" in state["hard_stop"]["evidence"]["identity_error"]
    # The watchdog only writes cgroup.freeze/cgroup.kill.  It never calls
    # os.kill() on the untrusted/reused PID.
    assert (cgroup_root / "test.slice" / "campaign.scope" / "cgroup.kill").read_text().startswith("1")


def test_orchestrator_disappearance_triggers_the_same_fail_closed_path(tmp_path: Path) -> None:
    watchdog, paths, proc_root, cgroup_root = make_harness(tmp_path)
    (proc_root / "4242" / "stat").unlink()

    assert watchdog.run(once=True) == INCIDENT_EXIT_CODE

    state = load_json(paths.state)
    assert state["state"] == "INTERRUPTED"
    assert state["hard_stop"]["reason_code"] == "ORCHESTRATOR_IDENTITY_LOST"
    assert state["control"]["admit_new_jobs"] is False
    assert (cgroup_root / "test.slice" / "campaign.scope" / "cgroup.kill").read_text().startswith("1")


def test_unverified_cgroup_stop_is_a_terminal_harness_failure(tmp_path: Path) -> None:
    watchdog, paths, _proc_root, cgroup_root = make_harness(
        tmp_path,
        now_ns=130_000_000_000,
        heartbeat_ns=9_000_000_000,
    )
    target = cgroup_root / "test.slice" / "campaign.scope"
    (target / "cgroup.events").write_text("populated 1\nfrozen 0\n", encoding="ascii")
    (target / "cgroup.procs").write_text("99999\n", encoding="ascii")

    assert watchdog.run(once=True) == INCIDENT_EXIT_CODE

    state = load_json(paths.state)
    result = load_json(paths.ready)
    assert state["state"] == "FAILED"
    assert state["control"]["admit_new_jobs"] is False
    assert result["ok"] is False
    assert any("remained populated" in error for error in result["collector_errors"])


def test_checkpoint_must_be_at_least_as_durable_as_heartbeat(tmp_path: Path) -> None:
    watchdog, paths, _proc_root, _cgroup_root = make_harness(tmp_path)
    write_json(paths.checkpoint, {"campaign_uuid": CAMPAIGN_UUID, "revision": 2})

    assert watchdog.run(once=True) == INCIDENT_EXIT_CODE
    assert load_json(paths.state)["hard_stop"]["reason_code"] == "CHECKPOINT_NOT_DURABLE"


def test_watchdog_refuses_to_run_inside_campaign_cgroup(tmp_path: Path) -> None:
    watchdog, paths, _proc_root, _cgroup_root = make_harness(
        tmp_path,
        self_cgroup="/test.slice/campaign.scope/watchdog",
    )

    with pytest.raises(WatchdogError, match="inside the managed campaign cgroup"):
        watchdog.run(once=True)
    # Direct API raises for the supervisor to classify; the CLI wrapper is
    # separately tested to close this gate on every startup failure.
    assert load_json(paths.control)["admit_new_jobs"] is True


def test_runtime_watchdog_cgroup_migration_is_detected(tmp_path: Path) -> None:
    watchdog, paths, proc_root, _cgroup_root = make_harness(tmp_path)
    watchdog.validate_startup()
    (proc_root / str(os.getpid()) / "cgroup").write_text(
        "0::/test.slice/campaign.scope/watchdog\n",
        encoding="utf-8",
    )

    assert watchdog.run_once() == INCIDENT_EXIT_CODE

    state = load_json(paths.state)
    assert state["state"] == "INTERRUPTED"
    assert state["hard_stop"]["reason_code"] == "WATCHDOG_CONTAINMENT_VIOLATION"


def test_terminal_campaign_state_stops_watchdog_without_reclassifying_result(tmp_path: Path) -> None:
    watchdog, paths, proc_root, cgroup_root = make_harness(tmp_path, state="PASS")
    # The orchestrator/runner may exit immediately after durably committing a
    # terminal result.  Terminal state must be checked before PID liveness.
    (proc_root / "4242" / "stat").unlink()

    assert watchdog.run(once=True) == 0

    state = load_json(paths.state)
    assert state["state"] == "PASS"
    assert state.get("hard_stop") is None
    assert load_json(paths.ready)["status"] == "campaign_terminal"
    assert (cgroup_root / "test.slice" / "campaign.scope" / "cgroup.kill").read_text().startswith("0")


def test_frozen_closed_gate_is_not_mistaken_for_an_external_hard_stop(tmp_path: Path) -> None:
    watchdog, paths, _proc_root, cgroup_root = make_harness(tmp_path, state="FROZEN")

    assert watchdog.run(once=True) == 0

    state = load_json(paths.state)
    assert state["state"] == "FROZEN"
    assert state["control"]["admit_new_jobs"] is False
    assert state.get("hard_stop") is None
    assert (cgroup_root / "test.slice" / "campaign.scope" / "cgroup.kill").read_text().startswith("0")


def test_external_hard_stop_control_is_serviced_before_liveness_and_preserves_origin(tmp_path: Path) -> None:
    watchdog, paths, proc_root, cgroup_root = make_harness(tmp_path, state="STOPPING_LOAD")
    state = load_json(paths.state)
    state.update({
        "classification": "FAIL_PRODUCT",
        "reason": "DB_INVARIANT_FAILURE",
        "hard_stop": {
            "reason_code": "DB_INVARIANT_FAILURE",
            "classification": "FAIL_PRODUCT",
            "evidence": {"database": "finance.db", "invariant": "balanced_ledger"},
        },
    })
    write_json(paths.state, state)
    # Prove the control path wins before PID evaluation.
    (proc_root / "4242" / "stat").unlink()

    assert watchdog.run(once=True) == INCIDENT_EXIT_CODE

    final_state = load_json(paths.state)
    status = load_json(paths.ready)
    assert final_state["state"] == "INTERRUPTED"
    assert final_state["classification"] == "FAIL_PRODUCT"
    assert final_state["reason"] == "DB_INVARIANT_FAILURE"
    assert final_state["hard_stop"]["reason_code"] == "DB_INVARIANT_FAILURE"
    assert final_state["hard_stop"]["evidence"]["invariant"] == "balanced_ledger"
    assert final_state["watchdog_takeover"]["reason_code"] == "EXTERNAL_HARD_STOP_REQUESTED"
    assert status["reason"] == "EXTERNAL_HARD_STOP_REQUESTED"
    assert status["cgroup_stop"]["population_cleared"] is True
    assert (cgroup_root / "test.slice" / "campaign.scope" / "cgroup.kill").read_text().startswith("1")


def test_cgroup_device_inode_mismatch_fails_before_any_kill_write(tmp_path: Path) -> None:
    watchdog, _paths, _proc_root, cgroup_root = make_harness(tmp_path)
    original = watchdog.config.campaign_cgroup
    object.__setattr__(
        watchdog.config,
        "campaign_cgroup",
        CgroupIdentity(original.path, original.device, original.inode + 1),
    )

    with pytest.raises(WatchdogError, match="identity changed"):
        watchdog.run(once=True)
    assert (cgroup_root / "test.slice" / "campaign.scope" / "cgroup.kill").read_text().startswith("0")


def test_production_configuration_rejects_test_filesystems_and_nonstandard_timeout(tmp_path: Path) -> None:
    watchdog, _paths, _proc_root, _cgroup_root = make_harness(tmp_path)
    object.__setattr__(watchdog.config, "production", True)
    object.__setattr__(watchdog.config, "stale_after_seconds", 30)

    with pytest.raises(WatchdogError, match="real /proc|exactly 120"):
        watchdog.run(once=False)


def test_production_watchdog_forbids_one_shot_mode(tmp_path: Path) -> None:
    watchdog, _paths, _proc_root, _cgroup_root = make_harness(tmp_path)
    object.__setattr__(watchdog.config, "production", True)

    with pytest.raises(WatchdogError, match="--once is forbidden"):
        watchdog.run(once=True)


def test_duplicate_watchdog_cannot_take_ownership_or_close_a_healthy_gate(tmp_path: Path) -> None:
    watchdog, paths, _proc_root, _cgroup_root = make_harness(tmp_path)

    with locked_path(paths.process_lock, nonblocking=True):
        with pytest.raises(DuplicateWatchdogError, match="already holds"):
            watchdog.run(once=True)

    assert load_json(paths.state)["state"] == "ACTIVE"
    assert load_json(paths.control)["admit_new_jobs"] is True


def test_watchdog_signal_during_active_campaign_is_fail_closed_not_a_clean_exit(tmp_path: Path) -> None:
    watchdog, paths, _proc_root, cgroup_root = make_harness(tmp_path)
    watchdog.stop_requested = True
    watchdog.signal_received = signal.SIGTERM

    assert watchdog.run(once=False) == INCIDENT_EXIT_CODE

    state = load_json(paths.state)
    assert state["state"] == "INTERRUPTED"
    assert state["hard_stop"]["reason_code"] == "WATCHDOG_SIGNALLED"
    assert state["hard_stop"]["evidence"]["signal"] == signal.SIGTERM
    assert state["control"]["admit_new_jobs"] is False
    assert (cgroup_root / "test.slice" / "campaign.scope" / "cgroup.kill").read_text().startswith("1")


def test_integration_command_is_explicit_and_secret_free(tmp_path: Path) -> None:
    watchdog, paths, _proc_root, _cgroup_root = make_harness(tmp_path)

    command = build_watchdog_command(watchdog.config, once=True)

    for path in (
        paths.state,
        paths.control,
        paths.heartbeat,
        paths.checkpoint,
        paths.ready,
        paths.evidence,
        paths.process_lock,
    ):
        assert str(path) in command
    assert "--once" in command
    assert "--development-mode" in command
    assert all("password" not in value.lower() and "token" not in value.lower() for value in command)


def test_cli_is_a_real_external_process_and_emits_machine_readable_ready_proof(tmp_path: Path) -> None:
    root = tmp_path / "external-campaign"
    cgroup_root = tmp_path / "external-cgroup"
    cgroup_identity = fake_cgroup(cgroup_root)
    identity = capture_process_identity(os.getpid())
    now_ns = time.monotonic_ns()
    paths = WatchdogPaths(
        campaign_root=root,
        state=root / "state.json",
        control=root / "control.json",
        heartbeat=root / "heartbeat.json",
        checkpoint=root / "checkpoint.json",
        ready=root / "watchdog.json",
        evidence=root / "evidence",
        process_lock=root / "watchdog.lock",
    )
    write_json(paths.state, {
        "schema_version": "hackme.campaign-state.v1",
        "campaign_uuid": CAMPAIGN_UUID,
        "revision": 1,
        "state": "PREFLIGHT",
        "control": {"admit_new_jobs": False},
        "clock": {"continuous_active_seconds": 0},
        "heartbeat": {
            "orchestrator_pid": identity.pid,
            "orchestrator_start_ticks": identity.start_ticks,
            "orchestrator_monotonic_ns": now_ns,
            "checkpoint_revision": 1,
        },
    })
    write_json(paths.control, {"admit_new_jobs": False})
    write_json(paths.heartbeat, {
        "campaign_uuid": CAMPAIGN_UUID,
        "heartbeat": {
            "orchestrator_pid": identity.pid,
            "orchestrator_start_ticks": identity.start_ticks,
            "orchestrator_monotonic_ns": now_ns,
            "checkpoint_revision": 1,
        },
    })
    write_json(paths.checkpoint, {"revision": 1})
    script = Path(__file__).resolve().parents[3] / "scripts" / "testing" / "campaign_watchdog.py"
    command = [
        sys.executable,
        str(script),
        "--campaign-root", str(root),
        "--campaign-uuid", CAMPAIGN_UUID,
        "--state-path", str(paths.state),
        "--control-path", str(paths.control),
        "--heartbeat-path", str(paths.heartbeat),
        "--checkpoint-path", str(paths.checkpoint),
        "--ready-path", str(paths.ready),
        "--evidence-path", str(paths.evidence),
        "--process-lock-path", str(paths.process_lock),
        "--orchestrator-pid", str(identity.pid),
        "--orchestrator-start-ticks", str(identity.start_ticks),
        "--orchestrator-boot-id", identity.boot_id,
        "--orchestrator-cgroup", identity.cgroup_path,
        "--campaign-cgroup", cgroup_identity.path,
        "--campaign-cgroup-device", str(cgroup_identity.device),
        "--campaign-cgroup-inode", str(cgroup_identity.inode),
        "--cgroup-root", str(cgroup_root),
        "--development-mode",
        "--once",
    ]

    completed = subprocess.run(command, text=True, capture_output=True, timeout=15, check=False)

    assert completed.returncode == 0, completed.stderr
    state = load_json(paths.state)
    assert state["heartbeat"]["watchdog_pid"] not in {0, os.getpid()}
    status = load_json(paths.ready)
    assert status["verified"] is True
    assert status["production"] is False
    assert status["external_process"] is True
    assert status["watchdog_outside_campaign_cgroup"] is True
    assert status["initial_health"]["ok"] is True
    assert status["initial_health"]["reason"] == "HEALTHY"


def test_external_watchdog_detects_sigstop_fault_without_shared_orchestrator_memory(tmp_path: Path) -> None:
    orchestrator = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        identity = capture_process_identity(orchestrator.pid)
        root = tmp_path / "sigstop-campaign"
        cgroup_root = tmp_path / "sigstop-cgroup"
        cgroup_identity = fake_cgroup(cgroup_root)
        paths = WatchdogPaths(
            campaign_root=root,
            state=root / "state.json",
            control=root / "control.json",
            heartbeat=root / "heartbeat.json",
            checkpoint=root / "checkpoint.json",
            ready=root / "watchdog.json",
            evidence=root / "evidence",
            process_lock=root / "watchdog.lock",
        )
        heartbeat_ns = time.monotonic_ns()
        common_heartbeat = {
            "orchestrator_pid": identity.pid,
            "orchestrator_start_ticks": identity.start_ticks,
            "orchestrator_monotonic_ns": heartbeat_ns,
            "checkpoint_revision": 1,
        }
        write_json(paths.state, {
            "schema_version": "hackme.campaign-state.v1",
            "campaign_uuid": CAMPAIGN_UUID,
            "revision": 1,
            "state": "ACTIVE",
            "control": {"admit_new_jobs": True, "load_generator_should_run": True},
            "clock": {
                "active_started_at": "2026-07-12T00:00:00Z",
                "continuous_active_seconds": 3,
                "wall_clock_seconds": 3,
                "invalid_seconds": 0,
                "formal_segment_valid": True,
                "last_tick_monotonic_ns": heartbeat_ns,
            },
            "heartbeat": common_heartbeat,
        })
        write_json(paths.control, {"campaign_uuid": CAMPAIGN_UUID, "admit_new_jobs": True})
        write_json(paths.heartbeat, {"campaign_uuid": CAMPAIGN_UUID, "heartbeat": common_heartbeat})
        write_json(paths.checkpoint, {"campaign_uuid": CAMPAIGN_UUID, "revision": 1})
        config = WatchdogConfig(
            campaign_uuid=CAMPAIGN_UUID,
            paths=paths,
            orchestrator_pid=identity.pid,
            orchestrator_start_ticks=identity.start_ticks,
            orchestrator_boot_id=identity.boot_id,
            orchestrator_cgroup=identity.cgroup_path,
            campaign_cgroup=cgroup_identity,
            cgroup_root=cgroup_root,
            stale_after_seconds=0.05,
            poll_seconds=0.01,
            kill_verify_seconds=0,
            production=False,
        )
        os.kill(orchestrator.pid, signal.SIGSTOP)
        time.sleep(0.08)

        completed = subprocess.run(
            build_watchdog_command(config, once=True),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        assert completed.returncode == INCIDENT_EXIT_CODE, completed.stderr
        state = load_json(paths.state)
        assert state["state"] == "INTERRUPTED"
        assert state["hard_stop"]["reason_code"] == "HEARTBEAT_STALE"
        assert state["control"]["admit_new_jobs"] is False
        assert state["clock"]["continuous_active_seconds"] == 3
        assert (cgroup_root / "test.slice" / "campaign.scope" / "cgroup.kill").read_text().startswith("1")
    finally:
        try:
            os.kill(orchestrator.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        orchestrator.terminate()
        try:
            orchestrator.wait(timeout=5)
        except subprocess.TimeoutExpired:
            orchestrator.kill()
            orchestrator.wait(timeout=5)


def test_cli_startup_failure_closes_separate_control_gate(tmp_path: Path) -> None:
    watchdog, paths, _proc_root, _cgroup_root = make_harness(
        tmp_path,
        self_cgroup="/test.slice/campaign.scope/watchdog",
    )
    args = [
        "--campaign-root", str(paths.campaign_root),
        "--campaign-uuid", CAMPAIGN_UUID,
        "--state-path", str(paths.state),
        "--control-path", str(paths.control),
        "--heartbeat-path", str(paths.heartbeat),
        "--checkpoint-path", str(paths.checkpoint),
        "--ready-path", str(paths.ready),
        "--evidence-path", str(paths.evidence),
        "--process-lock-path", str(paths.process_lock),
        "--orchestrator-pid", "4242",
        "--orchestrator-start-ticks", "456",
        "--orchestrator-boot-id", "boot-test-001",
        "--orchestrator-cgroup", "/orchestrator.slice/orchestrator.scope",
        "--campaign-cgroup", watchdog.config.campaign_cgroup.path,
        "--campaign-cgroup-device", str(watchdog.config.campaign_cgroup.device),
        "--campaign-cgroup-inode", str(watchdog.config.campaign_cgroup.inode),
        "--proc-root", str(watchdog.config.proc_root),
        "--cgroup-root", str(watchdog.config.cgroup_root),
        "--development-mode",
        "--once",
    ]
    from scripts.testing.campaign_watchdog import main

    assert main(args) == 2
    control = load_json(paths.control)
    assert control["state"] == "FAILED"
    assert control["admit_new_jobs"] is False
    assert control["reason"] == "WATCHDOG_FAIL_CLOSED"
