from __future__ import annotations

import json
import inspect
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

import pytest

from scripts.testing import operational_campaign_24h as campaign_module
from scripts.testing.operational_campaign_24h import (
    MIN_FORMAL_SECONDS,
    EFFECTIVE_LOAD_EVIDENCE_SCHEMA_VERSION,
    SUPERVISED_RUNNER_PROFILES,
    SUPERVISED_LOAD_POLICIES,
    Campaign,
    ServerController,
    WebClient,
    build_parser,
    sanitized_command,
    source_manifest,
    validate_supervised_runtime_contract,
    validate_effective_load_evidence,
    validate_tmp_path,
)
from scripts.testing.operation_coverage import CAMPAIGN_SCENARIO_CONTRACTS
from scripts.testing.campaign_load import EffectiveLoadWindow


def launch_core_activation_child(
    campaign: Campaign,
    tmp_path: Path,
    *,
    wrong_uuid: bool = False,
) -> subprocess.Popen[str]:
    campaign.supervised = True
    campaign.campaign_level = "rehearsal"
    campaign.campaign_uuid = str(uuid.uuid4())
    campaign.supervisor_contract = {"commit": "a" * 40}
    campaign.core_profile_digest = campaign_module.canonical_digest(
        SUPERVISED_RUNNER_PROFILES["rehearsal"]
    )
    campaign.core_root.mkdir(parents=True, exist_ok=True)
    os.chmod(campaign.core_root, 0o700)
    campaign_module.prepare_private_directory(
        campaign.core_activation_dir,
        authority_root=campaign.core_root,
    )
    campaign_module.assert_fresh_artifact_paths([
        campaign.core_ready_file,
        campaign.core_activation_file,
        campaign.core_activation_ack_file,
    ])
    result_path = tmp_path / "child_activation_result.json"
    contract = {
        "required": True,
        "campaign_uuid": campaign.campaign_uuid,
        "campaign_commit": "a" * 40,
        "runner_profile_digest": campaign.core_profile_digest,
        "campaign_runner_pid": os.getpid(),
        "campaign_runner_start_ticks": campaign_module.process_start_ticks(os.getpid()),
        "nonce": campaign.core_activation_nonce,
        "ready_file": str(campaign.core_ready_file),
        "activation_file": str(campaign.core_activation_file),
        "ack_file": str(campaign.core_activation_ack_file),
        "authority_root": str(campaign.core_root),
        "timeout_seconds": 5.0,
        "duration_seconds": int(campaign.args.duration_seconds),
    }
    if wrong_uuid:
        code = """
import json, os, time
from pathlib import Path
from scripts.testing.campaign_activation import CORE_READY_SCHEMA_VERSION, secure_write_once_json
from scripts.testing.campaign_state import process_start_ticks
c = json.loads(os.environ['CORE_CONTRACT'])
payload = {
    'schema_version': CORE_READY_SCHEMA_VERSION,
    'campaign_uuid': str(__import__('uuid').uuid4()),
    'campaign_commit': c['campaign_commit'],
    'runner_profile_digest': c['runner_profile_digest'],
    'activation_nonce': c['nonce'],
    'campaign_runner_pid': int(c['campaign_runner_pid']),
    'campaign_runner_start_ticks': int(c['campaign_runner_start_ticks']),
    'child_pid': os.getpid(),
    'child_start_ticks': process_start_ticks(os.getpid()),
    'ready_sequence': 1,
    'ready_monotonic_ns': time.monotonic_ns(),
    'ready_at': 'test',
}
secure_write_once_json(Path(c['ready_file']), payload, authority_root=Path(c['authority_root']))
stop = Path(os.environ['CORE_STOP'])
while not stop.exists():
    time.sleep(0.01)
"""
    else:
        code = """
import json, os, time
from pathlib import Path
from scripts.testing.operational_soak_probe import publish_ready_and_wait_for_activation
c = json.loads(os.environ['CORE_CONTRACT'])
for name in ('ready_file', 'activation_file', 'ack_file', 'authority_root'):
    c[name] = Path(c[name])
time.sleep(0.2)
result = publish_ready_and_wait_for_activation(
    c,
    duration_seconds=int(c['duration_seconds']),
    stop_file=Path(os.environ['CORE_STOP']),
)
Path(os.environ['CORE_RESULT']).write_text(json.dumps(result), encoding='utf-8')
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
        "CORE_CONTRACT": json.dumps(contract),
        "CORE_STOP": str(campaign.core_stop_file),
        "CORE_RESULT": str(result_path),
    }
    campaign.core_process_started_monotonic_ns = time.monotonic_ns()
    process = subprocess.Popen(
        [campaign_module.sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[3]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    campaign.core_process = process
    return process


def campaign_args(tmp_path: Path, *extra: str):
    return build_parser().parse_args([
        "--campaign-root",
        str(tmp_path / "campaign"),
        "--duration-seconds",
        "60",
        "--allow-short-duration",
        "--primary-port",
        "55101",
        "--recovery-port",
        "55102",
        "--security-port",
        "55103",
        "--minimum-free-gb",
        "0",
        *extra,
    ])


def test_formal_duration_is_24_hours() -> None:
    assert MIN_FORMAL_SECONDS == 86_400
    assert SUPERVISED_LOAD_POLICIES["formal"]["ramp_levels"] == [4, 8, 16, 32]
    assert SUPERVISED_LOAD_POLICIES["formal"]["minimum_ramp_stage_seconds"] == {
        "4": 600.0,
        "8": 1_200.0,
        "16": 1_800.0,
        "32": 0.0,
    }
    assert sum(SUPERVISED_LOAD_POLICIES["formal"]["minimum_ramp_stage_seconds"].values()) == 3_600.0
    assert SUPERVISED_LOAD_POLICIES["rehearsal"]["minimum_ramp_stage_seconds"] == {
        "4": 60.0,
        "8": 120.0,
        "16": 180.0,
        "32": 0.0,
    }
    assert SUPERVISED_LOAD_POLICIES["formal"]["ramp_completion_deadline_seconds"] == 3_600.0
    assert SUPERVISED_LOAD_POLICIES["formal"]["minimum_post_ramp_seconds"] == 82_800.0
    assert SUPERVISED_LOAD_POLICIES["rehearsal"]["ramp_completion_deadline_seconds"] == 360.0
    assert SUPERVISED_LOAD_POLICIES["rehearsal"]["minimum_post_ramp_seconds"] == 3_240.0


def test_supervised_runtime_contract_rejects_duration_and_source_divergence(
    tmp_path: Path,
) -> None:
    args = campaign_args(
        tmp_path,
        "--supervised",
        "--campaign-uuid",
        "campaign-1",
        "--cgroup-path",
        "/test.scope",
        "--checkpoint-mirror-path",
        str(Path.home() / "logs" / "hackme_web_campaign_24h" / "campaign-1" / "campaign.checkpoint.json"),
    )
    args.minimum_free_gb = 20.0
    campaign_root = Path(args.campaign_root).resolve()
    control_root = campaign_root.parent / ".campaign-control"
    args.control_root = str(control_root)
    args.state_path = str(control_root / "checkpoint" / "campaign.state.json")
    args.control_path = str(control_root / "checkpoint" / "campaign.control.json")
    args.heartbeat_path = str(control_root / "checkpoint" / "campaign.heartbeat.json")
    args.checkpoint_path = str(control_root / "checkpoint" / "campaign.checkpoint.json")
    args.source_freeze_path = str(control_root / "artifacts" / "source" / "H0" / "source_freeze.json")
    args.activation_gate = str(control_root / "checkpoint" / "campaign.activation.json")
    args.supervisor_contract = str(control_root / "checkpoint" / "supervisor.contract.json")
    contract = {
        "level": "smoke",
        "duration_seconds": 180,
        "campaign_root": str(campaign_root),
        "control_root": str(control_root),
        "checkpoint_mirror_path": str(Path(args.checkpoint_mirror_path).resolve()),
        "cgroup_path": "/test.scope",
        "cgroup_event_baseline": {
            "memory.events": {"max": 0, "oom": 0, "oom_kill": 0},
            "pids.events": {"max": 0},
        },
        "commit": "a" * 40,
        "source_digest": "b" * 64,
        "runner_profile": dict(SUPERVISED_RUNNER_PROFILES["smoke"]),
        "load_policy": dict(SUPERVISED_LOAD_POLICIES["smoke"]),
        "gates": {
            name: {"status": "PASS", "machine_verified": True}
            for name in (
                "cgroup_limits_verified",
                "external_watchdog_verified",
                "runner_and_watchdog_placement_verified",
                "cgroup_event_baseline_verified",
                "source_baseline_frozen",
            )
        },
    }
    source = {
        "schema_version": campaign_module.SOURCE_FREEZE_SCHEMA_VERSION,
        "verified": True,
        "label": "H0",
        "repo_root": str(campaign_module.ROOT),
        "commit": contract["commit"],
        "tracked_content_digest": contract["source_digest"],
        "require_clean": False,
    }
    args.duration_seconds = 180

    validate_supervised_runtime_contract(args, contract, source)

    args.duration_seconds = 179
    with pytest.raises(RuntimeError, match="runner_duration_seconds"):
        validate_supervised_runtime_contract(args, contract, source)
    args.duration_seconds = 180
    source["tracked_content_digest"] = "c" * 64
    with pytest.raises(RuntimeError, match="source_digest"):
        validate_supervised_runtime_contract(args, contract, source)


def test_supervised_runtime_contract_rejects_weakened_load_sla_and_resource_values(
    tmp_path: Path,
) -> None:
    args = campaign_args(
        tmp_path,
        "--supervised",
        "--campaign-uuid",
        "campaign-1",
        "--cgroup-path",
        "/test.scope",
        "--checkpoint-mirror-path",
        str(Path.home() / "logs" / "hackme_web_campaign_24h" / "campaign-1" / "campaign.checkpoint.json"),
    )
    args.duration_seconds = 180
    args.minimum_free_gb = 20.0
    contract = {
        "level": "smoke",
        "duration_seconds": 180,
        "campaign_root": str(Path(args.campaign_root).resolve()),
        "checkpoint_mirror_path": str(Path(args.checkpoint_mirror_path).resolve()),
        "cgroup_path": "/test.scope",
        "cgroup_event_baseline": {
            "memory.events": {"max": 0, "oom": 0, "oom_kill": 0},
            "pids.events": {"max": 0},
        },
        "commit": "a" * 40,
        "source_digest": "b" * 64,
        "runner_profile": dict(SUPERVISED_RUNNER_PROFILES["smoke"]),
        "load_policy": dict(SUPERVISED_LOAD_POLICIES["smoke"]),
        "gates": {
            name: {"status": "PASS", "machine_verified": True}
            for name in (
                "cgroup_limits_verified",
                "external_watchdog_verified",
                "runner_and_watchdog_placement_verified",
                "cgroup_event_baseline_verified",
                "source_baseline_frozen",
            )
        },
    }
    source = {
        "schema_version": campaign_module.SOURCE_FREEZE_SCHEMA_VERSION,
        "verified": True,
        "label": "H0",
        "repo_root": str(campaign_module.ROOT),
        "commit": contract["commit"],
        "tracked_content_digest": contract["source_digest"],
        "require_clean": False,
    }

    for name, weak_value in (
        ("concurrency", 1),
        ("round_ops", 1),
        ("max_ordinary_p95_ms", 999_999.0),
        ("max_server_busy_rate", 1.0),
        ("minimum_free_gb", 0.0),
        ("resource_interval", 999.0),
    ):
        original = getattr(args, name)
        setattr(args, name, weak_value)
        with pytest.raises(RuntimeError, match=f"runner_profile:{name}"):
            validate_supervised_runtime_contract(args, contract, source)
        setattr(args, name, original)

    contract["runner_profile"]["concurrency"] = 1
    with pytest.raises(RuntimeError, match="contract_runner_profile:concurrency"):
        validate_supervised_runtime_contract(args, contract, source)
    contract["runner_profile"]["concurrency"] = 32
    contract["load_policy"]["minimum_target_load_coverage"] = 0.0
    with pytest.raises(RuntimeError, match="load_policy"):
        validate_supervised_runtime_contract(args, contract, source)


def test_effective_load_validation_fails_closed_for_idle_workers() -> None:
    evidence = {
        "schema_version": EFFECTIVE_LOAD_EVIDENCE_SCHEMA_VERSION,
        "required": True,
        "campaign_level": "rehearsal",
        "ramp": {
            "required_levels": [4, 8, 16, 32],
            "completed_levels": [4, 8, 16],
            "ok": False,
        },
        "baseline_32_operations_per_minute": 1_000.0,
        "target_load_summary": {
            "ok": False,
            "target_load_coverage": 0.0,
            "invalid_samples": [],
        },
        "ok": False,
    }

    result = validate_effective_load_evidence(
        {"effective_load": evidence},
        campaign_level="rehearsal",
    )

    assert result["ok"] is False
    assert "ramp_completed_levels" in result["errors"]
    assert "target_load_coverage" in result["errors"]


def test_effective_load_validation_rederives_native_worker_and_ramp_evidence() -> None:
    def native(level: int, workers: int, operations: int = 1_000) -> dict[str, object]:
        return {
            "schema_version": "hackme.system-stress-worker-telemetry.v1",
            "method": "native_inflight_operation_counter_time_samples",
            "configured_workers": level,
            "sample_count": 10,
            "active_worker_histogram": {str(workers): 10},
            "sustained_active_workers": workers,
            "operations_started": operations,
            "operations_completed": operations,
            "active_workers_at_stop": 0,
            "complete": True,
        }

    policy = SUPERVISED_LOAD_POLICIES["rehearsal"]
    stages: dict[str, dict[str, object]] = {}
    scheduled_start = 0.0
    schedule = []
    for level in (4, 8, 16):
        seconds = policy["minimum_ramp_stage_seconds"][str(level)]
        scheduled_end = scheduled_start + seconds
        schedule.append({
            "level": level,
            "start_seconds": scheduled_start,
            "end_seconds": scheduled_end,
        })
        workers = level
        stages[str(level)] = {
            "minimum_stage_seconds": seconds,
            "scheduled_start_seconds": scheduled_start,
            "scheduled_end_seconds": scheduled_end,
            "completed_elapsed_seconds": scheduled_end,
            "observed_seconds": seconds,
            "valid_terminal_rounds": 1,
            "measured_active_workers_peak": workers,
            "normalized_32_throughput_samples": [1_000.0],
            "completed": True,
            "round_evidence": [{
                "worker_telemetry": native(level, workers),
                "measured_active_workers": workers,
                "terminal_valid": True,
                "round_ok": True,
                "partial": False,
                "returncode": 0,
                "window_seconds": seconds,
                "window_started_elapsed_seconds": scheduled_start,
                "window_finished_elapsed_seconds": scheduled_end,
                "operations_completed": 1_000,
                "expected_operations": 1_000,
            }],
        }
        scheduled_start = scheduled_end
    stages["32"] = {"completed": True}
    sample = EffectiveLoadWindow(
        window_started_at="2026-07-13T00:00:00Z",
        window_seconds=3_240.0,
        scheduled_load_level=32,
        active_workers=32,
        inflight_requests=32,
        operations_completed=54_000,
        expected_operations=54_000.0,
        blocked_workers=0,
        idle_workers=0,
        queue_depth=0,
        retries=0,
        attempts=54_000,
        baseline_32_operations_per_minute=1_000.0,
    ).evidence()
    sample.update({
        "round_ok": True,
        "window_started_elapsed_seconds": 360.0,
        "window_finished_elapsed_seconds": 3_600.0,
        "worker_measurement": {
            "method": "native_inflight_operation_counter_time_samples",
            "native": native(32, 32, 54_000),
            "measured_active_workers": 32,
            "configured_concurrency_not_used_as_measurement": True,
        },
    })
    evidence = {
        "schema_version": EFFECTIVE_LOAD_EVIDENCE_SCHEMA_VERSION,
        "required": True,
        "campaign_level": "rehearsal",
        "ramp": {
            "required_levels": [4, 8, 16, 32],
            "completed_levels": [4, 8, 16, 32],
            "minimum_stage_seconds": policy["minimum_ramp_stage_seconds"],
            "schedule": schedule,
            "completion_elapsed_seconds": 360.0,
            "completion_deadline_seconds": 360.0,
            "maximum_stage_boundary_lag_seconds": 15.0,
            "schedule_failure": "",
            "stages": stages,
            "ok": True,
        },
        "minimum_post_ramp_seconds": 3_240.0,
        "baseline_32_operations_per_minute": 1_000.0,
        "target_load_samples": [sample],
        "target_load_summary": {
            "ok": True,
            "target_load_seconds": 3_240.0,
            "post_ramp_wall_seconds": 3_240.0,
            "maintenance_seconds_excluded": 0.0,
            "eligible_post_ramp_wall_seconds": 3_240.0,
            "target_load_coverage": 1.0,
            "invalid_samples": [],
        },
        "ok": True,
    }

    result = validate_effective_load_evidence(
        {"effective_load": evidence},
        campaign_level="rehearsal",
    )

    assert result == {"required": True, "ok": True, "errors": []}

    sample["worker_measurement"]["native"]["active_worker_histogram"] = {"4": 10}
    tampered = validate_effective_load_evidence(
        {"effective_load": evidence},
        campaign_level="rehearsal",
    )
    assert tampered["ok"] is False
    assert "target_sample:0:worker_measurement" in tampered["errors"]

    sample["worker_measurement"]["native"]["active_worker_histogram"] = {"32": 10}
    sample["window_seconds"] = 6_480.0
    inflated = validate_effective_load_evidence(
        {"effective_load": evidence},
        campaign_level="rehearsal",
    )
    assert inflated["ok"] is False
    assert "target_sample:0:fixed_target_window" in inflated["errors"]

    sample["window_seconds"] = 3_240.0
    overlapping = dict(sample)
    overlapping["window_started_elapsed_seconds"] = 360.0
    overlapping["window_finished_elapsed_seconds"] = 3_600.0
    evidence["target_load_samples"] = [sample, overlapping]
    overlap_result = validate_effective_load_evidence(
        {"effective_load": evidence},
        campaign_level="rehearsal",
    )
    assert overlap_result["ok"] is False
    assert "target_sample:1:overlapping_window" in overlap_result["errors"]


def test_effective_load_validation_rejects_almost_full_campaign_ramp() -> None:
    policy = SUPERVISED_LOAD_POLICIES["formal"]
    evidence = {
        "schema_version": EFFECTIVE_LOAD_EVIDENCE_SCHEMA_VERSION,
        "required": True,
        "campaign_level": "formal",
        "ramp": {
            "required_levels": [4, 8, 16, 32],
            "completed_levels": [4, 8, 16, 32],
            "minimum_stage_seconds": policy["minimum_ramp_stage_seconds"],
            "schedule": [
                {"level": 4, "start_seconds": 0.0, "end_seconds": 600.0},
                {"level": 8, "start_seconds": 600.0, "end_seconds": 1_800.0},
                {"level": 16, "start_seconds": 1_800.0, "end_seconds": 3_600.0},
            ],
            "completion_elapsed_seconds": 86_000.0,
            "completion_deadline_seconds": 3_600.0,
            "maximum_stage_boundary_lag_seconds": 15.0,
            "schedule_failure": "",
            "stages": {},
            "ok": True,
        },
        "minimum_post_ramp_seconds": 82_800.0,
        "baseline_32_operations_per_minute": 1_000.0,
        "target_load_samples": [],
        "target_load_summary": {
            "ok": True,
            "target_load_seconds": 400.0,
            "eligible_post_ramp_wall_seconds": 400.0,
            "target_load_coverage": 1.0,
            "invalid_samples": [],
        },
        "ok": True,
    }

    result = validate_effective_load_evidence(
        {"effective_load": evidence},
        campaign_level="formal",
    )

    assert result["ok"] is False
    assert "ramp_completion_deadline" in result["errors"]
    assert "minimum_post_ramp_seconds" in result["errors"]


def test_checkpoint_commit_writes_private_reboot_safe_mirror(tmp_path: Path) -> None:
    campaign = Campaign.__new__(Campaign)
    campaign.checkpoint_path = tmp_path / "volatile" / "campaign.checkpoint.json"
    campaign.checkpoint_mirror_path = tmp_path / "persistent" / "campaign.checkpoint.json"
    payload = {
        "schema_version": "hackme.campaign-checkpoint.v1",
        "campaign_uuid": "campaign-1",
        "revision": 7,
    }

    campaign._commit_checkpoint(payload)

    assert campaign_module.load_json(campaign.checkpoint_path) == payload
    assert campaign_module.load_json(campaign.checkpoint_mirror_path) == payload
    assert campaign.checkpoint_mirror_path.stat().st_mode & 0o077 == 0
    assert campaign.checkpoint_mirror_path.parent.stat().st_mode & 0o077 == 0


def test_campaign_manifest_covers_product_code_harness_and_tests() -> None:
    manifest = source_manifest()

    for path in (
        "server.py",
        "test_for_develop.sh",
        "public/index.html",
        "routes/ai_agent.py",
        "services/snapshots/schema.py",
        "scripts/testing/operational_campaign_24h.py",
        "tests/scripts/testing/test_operational_campaign_24h.py",
    ):
        assert path in manifest


def test_campaign_matrix_contains_every_mandatory_operational_category(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    specs = campaign.scenario_specs()
    assert [spec.scenario_id for spec in specs] == list(CAMPAIGN_SCENARIO_CONTRACTS)
    assert [spec.category for spec in specs] == [
        contract.category for contract in CAMPAIGN_SCENARIO_CONTRACTS.values()
    ]
    assert [spec.fraction for spec in specs] == [
        contract.scheduled_fraction for contract in CAMPAIGN_SCENARIO_CONTRACTS.values()
    ]
    assert all(spec.mandatory for spec in specs)
    assert max(spec.fraction for spec in specs) < 1


def test_supervised_180_second_smoke_cannot_claim_full_feature_coverage(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.supervised = True
    campaign.campaign_level = "smoke"

    assert campaign.scenario_specs() == []


def test_level_0_smoke_marks_formal_scenario_binding_not_applicable(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.campaign_level = "smoke"

    result, required = campaign.formal_scenario_binding_preflight()

    assert required is False
    assert result["status"] == "NOT_APPLICABLE"
    assert result["gate_pass"] is False
    assert result["formal_campaign_pass"] is False


def test_rehearsal_fails_closed_while_native_scenario_bindings_are_unwired(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.campaign_level = "rehearsal"

    result, required = campaign.formal_scenario_binding_preflight()

    assert required is True
    assert result["status"] == "FAIL_HARNESS"
    assert result["gate_pass"] is False
    assert result["reviewed_scenario_count"] == 13
    assert result["required_evidence_count"] == 91
    assert result["registered_runner_count"] == 4
    assert result["registered_evidence_adapter_count"] == 0
    assert result["registered_validator_count"] == 0
    assert result["fully_bound_scenario_count"] == 0
    assert result["registration_coverage"]["media_long_hls_share"]["runner_registered"] is True
    assert result["registration_coverage"]["bt_download_stream_restart"]["runner_registered"] is False


def test_incomplete_native_scenario_cannot_execute_as_formal_pass(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))

    result = campaign.run_formal_native_scenario("media_long_hls_share")

    assert result["ok"] is False
    assert result["classification"] == "FAIL_HARNESS"
    assert result["error"] == "formal_native_binding_incomplete"
    assert result["registration_coverage"]["runner_registered"] is True
    assert result["binding_blockers"]


def test_supervised_smoke_uses_bounded_lifecycle_load_without_secret_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.supervised = True
    campaign.campaign_level = "smoke"

    class Process:
        pid = os.getpid()

        def poll(self):
            return None

    monkeypatch.setattr(campaign_module.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(campaign_module.time, "sleep", lambda _seconds: None)

    result = campaign.start_core_soak()
    campaign.core_stdout_handle.close()

    assert result["ok"] is True
    assert result["scope"] == "harness_lifecycle_only"
    assert campaign.core_command[1].endswith("campaign_smoke_load.py")
    assert campaign.credentials.test not in campaign.core_command
    assert "--stop-file" in campaign.core_command


def test_managed_server_launcher_keeps_credentials_out_of_argv(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    controller: ServerController = campaign.primary
    command = controller.launcher_command()
    assert "--root-password" not in command
    assert "--manager-password" not in command
    assert "--test-password" not in command
    assert campaign.credentials.root not in command
    assert campaign.credentials.manager not in command
    assert campaign.credentials.test not in command


def test_managed_server_environment_disables_mutable_capacity_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HACKME_DEV_CAPACITY_DEFAULTS_FILE", "/tmp/host-controlled.env")
    monkeypatch.setenv("HACKME_DEV_CAPACITY_REPORT_FILE", "/tmp/host-controlled.json")
    campaign = Campaign(campaign_args(tmp_path))

    env = campaign.primary._env()

    assert env["HACKME_DEV_USE_CAPACITY_DEFAULTS"] == "0"
    assert "HACKME_DEV_CAPACITY_DEFAULTS_FILE" not in env
    assert "HACKME_DEV_CAPACITY_REPORT_FILE" not in env
    command = campaign.primary.launcher_command()
    assert command[command.index("--gunicorn-workers") + 1] == "4"
    assert command[command.index("--gunicorn-threads") + 1] == "8"


def test_slow_launcher_reports_only_changed_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    controller = campaign.primary

    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    class Process:
        pid = os.getpid()

        def __init__(self, clock: Clock, *, completes_at: float | None) -> None:
            self.clock = clock
            self.completes_at = completes_at

        def poll(self) -> int | None:
            if self.completes_at is not None and self.clock.now >= self.completes_at:
                return 0
            return None

        def wait(self, timeout: float | None = None) -> int:
            return -15

    clock = Clock()
    progress: list[tuple[float, str]] = []
    controller.progress_callback = lambda detail: progress.append((clock.now, detail))
    monkeypatch.setattr(campaign_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(campaign_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(
        controller,
        "_launcher_observation",
        lambda _process, _log: (int(clock.now * 2),),
    )

    healthy = controller._wait_launcher(
        Process(clock, completes_at=2.0),  # type: ignore[arg-type]
        tmp_path / "launcher.log",
        timeout=5.0,
    )

    assert healthy["returncode"] == 0
    assert healthy["timed_out"] is False
    assert healthy["observations"] >= 4
    assert any("launcher_completed:0" in detail for _at, detail in progress)

    clock.now = 0.0
    progress.clear()
    monkeypatch.setattr(controller, "_launcher_observation", lambda _process, _log: ("unchanged",))
    monkeypatch.setattr(campaign_module, "terminate_process_group", lambda *_args, **_kwargs: None)
    stalled = controller._wait_launcher(
        Process(clock, completes_at=None),  # type: ignore[arg-type]
        tmp_path / "launcher.log",
        timeout=2.0,
    )

    assert stalled["timed_out"] is True
    assert stalled["observations"] == 1
    assert len(progress) == 1


def test_slow_layered_readiness_advances_after_each_completed_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    controller = campaign.primary
    controller.strict_readiness = True

    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = Clock()
    progress: list[tuple[float, str]] = []
    controller.progress_callback = lambda detail: progress.append((clock.now, detail))

    class SlowProbe:
        attempts = 0

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def probe_once(self) -> dict[str, object]:
            type(self).attempts += 1
            clock.sleep(40.0)
            return {
                "overall": type(self).attempts >= 4,
                "elapsed_seconds": 40.0,
            }

    monkeypatch.setattr(campaign_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(campaign_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(campaign_module, "LayeredReadinessProbe", SlowProbe)

    result = controller.wait_ready(timeout=300.0)

    completed = [row for row in progress if "readiness_probe_completed" in row[1]]
    assert result["ok"] is True
    assert len(completed) == 5
    assert max(b[0] - a[0] for a, b in zip(completed, completed[1:])) < 120.0


def test_sanitized_command_redacts_all_supported_secret_flags() -> None:
    command = sanitized_command([
        "probe",
        "--root-password",
        "root-secret",
        "--member-password=member-secret",
        "--accounts",
        "a:secret,b:secret",
    ])
    assert command == [
        "probe",
        "--root-password",
        "[redacted]",
        "--member-password=[redacted]",
        "--accounts",
        "[redacted]",
    ]


def test_run_step_rejects_returncode_zero_without_machine_success_evidence(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))

    result = campaign.run_step(
        "evidence_contract",
        "returncode_only",
        [campaign_module.sys.executable, "-c", "raise SystemExit(0)"],
        timeout=10,
    )

    assert result["returncode"] == 0
    assert result["ok"] is False
    assert result["evidence_errors"] == ["machine_success_evidence_required"]


def test_run_step_requires_explicit_ok_true_in_declared_json_artifact(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    artifact = tmp_path / "result.json"

    missing_proof = campaign.run_step(
        "evidence_contract",
        "empty_json",
        [
            campaign_module.sys.executable,
            "-c",
            "from pathlib import Path; Path(r'" + str(artifact) + "').write_text('{}')",
        ],
        timeout=10,
        artifact=artifact,
    )
    explicit_pass = campaign.run_step(
        "evidence_contract",
        "explicit_pass",
        [
            campaign_module.sys.executable,
            "-c",
            "from pathlib import Path; Path(r'" + str(artifact) + "').write_text('{\"ok\": true}')",
        ],
        timeout=10,
        artifact=artifact,
    )

    assert missing_proof["ok"] is False
    assert missing_proof["evidence_errors"] == [
        "declared_artifact_missing_explicit_ok_true"
    ]
    assert explicit_pass["ok"] is True
    assert explicit_pass["evidence_errors"] == []


def test_campaign_paths_must_stay_under_tmp(tmp_path: Path) -> None:
    assert validate_tmp_path(tmp_path / "ok", label="test") == (tmp_path / "ok").resolve()
    with pytest.raises(ValueError, match="must remain under /tmp"):
        validate_tmp_path(Path("/var/lib/hackme-campaign"), label="test")


def test_cli_restore_contract_preserves_storage_and_append_only_finance() -> None:
    script = (Path(__file__).resolve().parents[3] / "test_for_develop.sh").read_text(encoding="utf-8")
    assert "append_only_financial_restore_disabled" in script
    assert 'mv "$backup_existing/storage" "$RUNTIME_ROOT/storage"' in script
    for name in ("finance.db", "points_chain.db", "trading.db"):
        assert name in script


def test_campaign_detects_early_core_exit_without_waiting_for_delayed_scenarios(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.active_started = time.monotonic() - 10
    assert campaign.required_duration_completed() is False

    campaign.active_started = time.monotonic() - 60
    assert campaign.required_duration_completed() is True


def test_supervised_runner_persists_main_progress_without_helper_revision_inflation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.supervised = True
    campaign.root.mkdir(parents=True)

    class FakeStateMachine:
        def snapshot(self) -> dict[str, object]:
            return {"clock": {"continuous_active_seconds": 0.0}}

        def heartbeat(self, **_kwargs: object) -> None:
            return None

    campaign.state_machine = FakeStateMachine()  # type: ignore[assignment]
    monkeypatch.setattr(campaign_module, "HEARTBEAT_PUMP_INTERVAL_SECONDS", 0.01)

    initial_revision = campaign.checkpoint_revision
    campaign.start_heartbeat_pump()
    expected_revision = initial_revision + 1
    deadline = time.monotonic() + 2
    while not campaign.heartbeat_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    before = campaign_module.load_json(campaign.heartbeat_path)["heartbeat"]
    time.sleep(0.05)
    after = campaign_module.load_json(campaign.heartbeat_path)["heartbeat"]
    campaign.stop_heartbeat_pump()

    assert campaign.checkpoint_revision == expected_revision
    assert campaign.checkpoint_path.exists()
    assert campaign.heartbeat_path.exists()
    checkpoint = campaign_module.load_json(campaign.checkpoint_path)
    heartbeat = campaign_module.load_json(campaign.heartbeat_path)
    assert checkpoint["revision"] == campaign.checkpoint_revision
    assert heartbeat["heartbeat"]["checkpoint_revision"] == campaign.checkpoint_revision
    assert before["main_progress_revision"] == after["main_progress_revision"] == 1
    assert before["orchestrator_monotonic_ns"] == after["orchestrator_monotonic_ns"]
    assert campaign.heartbeat_pump_error == ""


def test_live_helper_and_scenario_thread_cannot_mask_main_loop_stall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.supervised = True
    campaign.root.mkdir(parents=True)

    state_heartbeats: list[dict[str, object]] = []

    class FakeStateMachine:
        def snapshot(self) -> dict[str, object]:
            return {"clock": {"continuous_active_seconds": 0.0}}

        def heartbeat(self, **kwargs: object) -> None:
            state_heartbeats.append(kwargs)

    campaign.state_machine = FakeStateMachine()  # type: ignore[assignment]
    monkeypatch.setattr(campaign_module, "HEARTBEAT_PUMP_INTERVAL_SECONDS", 0.01)
    campaign.start_heartbeat_pump()
    initial = campaign_module.load_json(campaign.heartbeat_path)["heartbeat"]

    helper_error: list[str] = []

    def helper_activity() -> None:
        campaign.write_checkpoint("scenario_worker_completed")
        try:
            campaign.mark_main_loop_progress("forged_helper_progress")
        except RuntimeError as exc:
            helper_error.append(str(exc))

    thread = campaign_module.threading.Thread(target=helper_activity)
    thread.start()
    thread.join(timeout=2)
    deadline = time.monotonic() + 2
    stalled = campaign_module.load_json(campaign.heartbeat_path)["heartbeat"]
    while stalled["checkpoint_revision"] < campaign.checkpoint_revision and time.monotonic() < deadline:
        time.sleep(0.01)
        stalled = campaign_module.load_json(campaign.heartbeat_path)["heartbeat"]
    campaign.stop_heartbeat_pump()

    assert helper_error == ["main-loop progress cannot be advanced by a helper thread"]
    assert stalled["main_progress_revision"] == initial["main_progress_revision"]
    assert stalled["orchestrator_monotonic_ns"] == initial["orchestrator_monotonic_ns"]
    assert stalled["checkpoint_revision"] == campaign.checkpoint_revision
    assert len(state_heartbeats) == 1


def test_active_condition_matches_watchdog_pid_start_ticks_not_pid_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.root.mkdir(parents=True)
    campaign.watchdog_ready_path.parent.mkdir(parents=True, exist_ok=True)
    campaign_module.atomic_write_json(campaign.watchdog_ready_path, {
        "verified": True,
        "watchdog_pid": os.getpid(),
        "watchdog_start_ticks": 222,
    })
    for controller in (campaign.primary, campaign.recovery, campaign.security_sentinel):
        monkeypatch.setattr(controller, "pid", lambda: os.getpid())
    monkeypatch.setattr(campaign.resource_monitor, "is_alive", lambda: True)
    campaign.core_process = type("Core", (), {"poll": lambda self: None})()  # type: ignore[assignment]

    monkeypatch.setattr(campaign_module, "process_start_ticks", lambda _pid: 111)
    assert campaign._active_conditions()["watchdog_alive"] is False

    monkeypatch.setattr(campaign_module, "process_start_ticks", lambda _pid: 222)
    assert campaign._active_conditions()["watchdog_alive"] is True


def test_web_client_refreshes_user_csrf_after_login(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __init__(self, status: int, body: dict[str, object]):
            self.status_code = status
            self._body = body
            self.content = b"{}"
            self.text = ""

        def json(self) -> dict[str, object]:
            return self._body

    class Session:
        def __init__(self):
            self.verify = False
            self.cookies: dict[str, str] = {}
            self.logged_in = False

        def get(self, _url: str, **_kwargs: object) -> Response:
            token = "root-csrf" if self.logged_in else "public-csrf"
            return Response(200, {"ok": True, "csrf_token": token})

        def request(self, method: str, url: str, *, headers=None, **_kwargs: object) -> Response:
            headers = headers or {}
            if url.endswith("/api/login"):
                assert headers.get("X-CSRF-Token") == "public-csrf"
                self.logged_in = True
                return Response(200, {"ok": True})
            assert method == "POST"
            assert headers.get("X-CSRF-Token") == "root-csrf"
            return Response(201, {"ok": True})

    monkeypatch.setattr(campaign_module.requests, "Session", Session)
    progress: list[str] = []
    client = WebClient(
        "https://campaign.invalid",
        "root",
        "secret",
        progress_callback=progress.append,
    )

    login = client.login()
    write = client.request("POST", "/api/admin/users", json_body={"username": "member"})

    assert login["authenticated_csrf_rotated"] is True
    assert write["ok"] is True
    assert write["status"] == 201
    assert progress == [
        "csrf_request_completed:200",
        "request_completed:POST:200",
        "csrf_request_completed:200",
        "request_completed:POST:201",
    ]


def test_web_client_publishes_completed_request_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        def __init__(self):
            self.verify = False
            self.cookies: dict[str, str] = {}

        def request(self, _method: str, _url: str, **_kwargs: object) -> object:
            raise TimeoutError("completed timeout")

    monkeypatch.setattr(campaign_module.requests, "Session", Session)
    progress: list[str] = []
    client = WebClient(
        "https://campaign.invalid",
        "root",
        "secret",
        progress_callback=progress.append,
    )
    client.csrf = "csrf"

    result = client.request("POST", "/api/admin/users")

    assert result["ok"] is False
    assert progress == ["request_error:POST:TimeoutError"]


def test_campaign_account_cleanup_deletes_and_verifies_exact_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.account_inventory = [{"username": "campaign-user", "user_id": 42, "source": "campaign_runner"}]
    deleted: set[int] = set()

    callbacks: list[Callable[[str], None]] = []

    class FakeClient:
        def __init__(self, *_args: object, **kwargs: object):
            callback = kwargs.get("progress_callback")
            assert callable(callback)
            callbacks.append(callback)

        def login(self) -> dict[str, object]:
            callbacks[-1]("csrf_request_completed:200")
            callbacks[-1]("request_completed:POST:200")
            callbacks[-1]("csrf_request_completed:200")
            return {"ok": True, "status": 200}

        def request(self, method: str, path: str, **_kwargs: object) -> dict[str, object]:
            if method == "DELETE":
                deleted.add(int(path.rsplit("/", 1)[1]))
                return {"ok": True, "status": 200, "body": {"ok": True, "cleanup": {"warnings": []}}}
            users = [] if 42 in deleted else [{"id": 42, "username": "campaign-user"}]
            return {"ok": True, "status": 200, "body": {"users": users}}

    monkeypatch.setattr(campaign_module, "WebClient", FakeClient)

    result = campaign.cleanup_campaign_accounts()

    assert result["ok"] is True
    assert result["records"][0]["residual_exact_count"] == 0
    assert (campaign.reports / "account_cleanup.json").exists()
    assert campaign.main_progress_revision >= 3


def test_campaign_account_cleanup_fails_on_cleanup_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.account_inventory = [{"username": "campaign-user", "user_id": 42, "source": "campaign_runner"}]

    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object):
            return None

        def login(self) -> dict[str, object]:
            return {"ok": True, "status": 200}

        def request(self, method: str, _path: str, **_kwargs: object) -> dict[str, object]:
            if method == "DELETE":
                return {
                    "ok": True,
                    "status": 200,
                    "body": {"ok": True, "cleanup": {"warnings": [{"scope": "storage"}]}},
                }
            return {"ok": True, "status": 200, "body": {"users": []}}

    monkeypatch.setattr(campaign_module, "WebClient", FakeClient)

    result = campaign.cleanup_campaign_accounts()

    assert result["ok"] is False
    assert result["records"][0]["cleanup_warnings"] == [{"scope": "storage"}]


def test_final_control_checks_publish_main_thread_audit_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    progress: list[str] = []
    monkeypatch.setattr(campaign, "_server_progress", progress.append)
    monkeypatch.setattr(
        campaign.primary,
        "wait_ready",
        lambda **_kwargs: {"ok": True, "layered": {"overall": True, "target": "primary"}},
    )
    monkeypatch.setattr(
        campaign.recovery,
        "wait_ready",
        lambda **_kwargs: {"ok": True, "layered": {"overall": True, "target": "recovery"}},
    )

    result = campaign.final_control_checks()

    assert result["primary"]["ok"] is True
    assert result["recovery"]["ok"] is True
    assert progress == [
        "audit_readiness_started:primary",
        "audit_readiness_completed:primary",
        "audit_readiness_started:recovery",
        "audit_readiness_completed:recovery",
    ]


def test_server_log_scan_streams_with_boundary_matches_and_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    log_dir = campaign.primary.runtime_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Force the token to cross a read boundary; a whole-file scanner and an
    # overlap implementation that double-counts both fail this assertion.
    (log_dir / "server.log").write_text(
        "x" * 55 + "Traceback (most recent call last):\n"
        + "y" * 90
        + "database is locked\n",
        encoding="utf-8",
    )
    progress: list[str] = []
    monkeypatch.setattr(campaign, "_server_progress", progress.append)
    monkeypatch.setattr(campaign_module, "LOG_SCAN_CHUNK_CHARACTERS", 64)
    monkeypatch.setattr(campaign_module, "LOG_SCAN_OVERLAP_CHARACTERS", 48)
    monkeypatch.setattr(campaign_module, "LOG_SCAN_PROGRESS_CHARACTERS", 64)

    result = campaign.scan_server_logs([campaign.primary])["primary"]

    assert result["counts"]["traceback"] == 1
    assert result["counts"]["database_locked"] == 1
    assert result["errors"] == []
    assert result["scanned_characters"] > 64
    assert any(item.startswith("audit_log_scan_progress:") for item in progress)
    assert progress[-1].startswith("audit_log_scanned:")


def test_security_sentinel_completed_requests_publish_main_thread_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    progress: list[str] = []
    monkeypatch.setattr(campaign, "_server_progress", progress.append)

    class Response:
        status_code = 200

    class Session:
        verify = True

        def request(self, _method: str, _url: str, **_kwargs: object) -> Response:
            return Response()

    class Sentinel:
        def __init__(self, _config: object, *, session_factory: object):
            self.session_factory = session_factory

        def run_once(self) -> dict[str, object]:
            session = self.session_factory()
            session.request("GET", "https://campaign.invalid/api/version")
            return {
                "schema_version": "hackme.production-security-sentinel.v1",
                "checks": [{"name": "transport", "ok": True}],
                "failed_checks": [],
                "ok": True,
            }

    monkeypatch.setattr(campaign_module.requests, "Session", Session)
    monkeypatch.setattr(campaign_module, "ProductionSecuritySentinel", Sentinel)

    result = campaign.production_security_sentinel_check(phase="final")

    assert result["ok"] is True
    assert progress == ["security_final:request_completed:GET:200"]


def test_long_video_scenario_uploads_waits_and_measures_hls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    captured: dict[str, list[str]] = {}

    def fake_run_step(_scenario_id: str, _step_id: str, command: list[str], **_kwargs: object) -> dict[str, bool]:
        captured["command"] = command
        return {"ok": True}

    monkeypatch.setattr(campaign, "run_step", fake_run_step)

    result = campaign.scenario_media_long()

    assert result["ok"] is True
    for flag in ("--upload", "--wait", "--measure", "--verify-share", "--browser-seek", "--browser-mobile"):
        assert flag in captured["command"]


def test_core_soak_ready_activation_ack_share_one_future_active_edge(
    tmp_path: Path,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    process = launch_core_activation_child(campaign, tmp_path)

    # This is a protocol correctness test, not a subprocess startup latency
    # benchmark.  Allow for a loaded CI host before evaluating the binding.
    ready = campaign.wait_for_core_ready(timeout_seconds=30.0)
    activation = campaign.activate_core_soak(
        ready,
        ack_timeout_seconds=30.0,
        lead_seconds=1.0,
    )
    process.wait(timeout=30)
    output = process.stdout.read() if process.stdout else ""
    assert process.returncode == 0, output
    child = json.loads((tmp_path / "child_activation_result.json").read_text(encoding="utf-8"))

    assert activation["ok"] is True
    assert activation["activation_monotonic_ns"] > ready["payload"]["ready_monotonic_ns"]
    assert activation["activation_monotonic_ns"] == child["activation_monotonic_ns"]
    assert activation["activation_epoch_ns"] == child["activation_epoch_ns"]
    assert activation["ready_sha256"] == child["ready_sha256"]
    assert activation["activation_sha256"] == child["activation_sha256"]
    assert activation["ack_sha256"] == child["ack_sha256"]
    assert campaign.core_activation_artifacts_intact() is True


def test_core_soak_ready_rejects_wrong_campaign_uuid_and_stops_child(
    tmp_path: Path,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    process = launch_core_activation_child(campaign, tmp_path, wrong_uuid=True)

    with pytest.raises(campaign_module.ActivationArtifactError, match="binding mismatch"):
        campaign.wait_for_core_ready(timeout_seconds=30.0)

    process.wait(timeout=30)
    assert process.returncode == 0
    assert campaign.core_stop_file.exists()


def test_core_soak_activation_detects_ready_tamper_before_ack(
    tmp_path: Path,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    process = launch_core_activation_child(campaign, tmp_path)
    ready = campaign.wait_for_core_ready(timeout_seconds=30.0)
    tampered = dict(ready["payload"])
    tampered["campaign_commit"] = "b" * 40
    campaign.core_ready_file.write_text(
        json.dumps(tampered, sort_keys=True),
        encoding="utf-8",
    )
    os.chmod(campaign.core_ready_file, 0o600)

    with pytest.raises(campaign_module.ActivationArtifactError):
        campaign.activate_core_soak(
            ready,
            ack_timeout_seconds=30.0,
            lead_seconds=1.0,
        )

    process.wait(timeout=30)
    assert process.returncode != 0
    assert campaign.core_activation_artifacts_intact() is False


def test_core_ready_wait_does_not_self_refresh_when_child_makes_no_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.supervised = True
    campaign.campaign_level = "rehearsal"

    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    class StalledProcess:
        pid = os.getpid()
        returncode = None

        def poll(self) -> None:
            return None

    clock = Clock()
    stopped: list[str] = []
    campaign.core_process = StalledProcess()  # type: ignore[assignment]
    monkeypatch.setattr(campaign_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(campaign_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(campaign, "_core_observation", lambda: ("unchanged",))
    monkeypatch.setattr(campaign, "_stop_core_before_active", stopped.append)
    initial_revision = campaign.main_progress_revision

    with pytest.raises(campaign_module.ActivationArtifactError, match="before timeout"):
        campaign.wait_for_core_ready(timeout_seconds=1.0)

    assert campaign.main_progress_revision == initial_revision + 1
    assert stopped == ["core_ready_timeout"]


def test_launcher_log_snapshot_streams_full_file_keeps_bounded_redacted_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "launcher.log"
    secret = "split-secret-value"
    path.write_bytes(b"x" * 13 + secret.encode() + b"y" * 80 + secret.encode())
    progress: list[str] = []
    monkeypatch.setattr(campaign_module, "LAUNCHER_LOG_CHUNK_BYTES", 16)
    monkeypatch.setattr(campaign_module, "LAUNCHER_LOG_DIAGNOSTIC_BYTES", 32)
    monkeypatch.setattr(campaign_module, "LAUNCHER_LOG_PROGRESS_BYTES", 16)

    result = campaign_module.bounded_launcher_log_snapshot(
        path,
        {"root": secret},
        progress_callback=progress.append,
    )

    assert result["ok"] is True
    assert result["secret_leak_labels"] == ["root"]
    assert result["diagnostic_bytes"] <= 32
    assert result["diagnostic_truncated"] is True
    assert secret not in result["diagnostic_tail"]
    assert "[redacted:root]" in result["diagnostic_tail"]
    assert progress


def test_launcher_log_snapshot_rejects_path_replacement_during_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "launcher.log"
    rotated = tmp_path / "launcher.log.1"
    path.write_bytes(b"x" * 256)
    replaced = False

    def replace_on_progress(_detail: str) -> None:
        nonlocal replaced
        if replaced:
            return
        replaced = True
        path.replace(rotated)
        path.write_bytes(b"replacement")

    monkeypatch.setattr(campaign_module, "LAUNCHER_LOG_CHUNK_BYTES", 32)
    monkeypatch.setattr(campaign_module, "LAUNCHER_LOG_PROGRESS_BYTES", 32)
    result = campaign_module.bounded_launcher_log_snapshot(
        path,
        {},
        progress_callback=replace_on_progress,
    )

    assert result["ok"] is False
    assert replaced is True
    assert "launcher_log_replaced_or_rotated_during_snapshot" in {
        row["code"] for row in result["errors"]
    }


def test_repo_runtime_scan_prunes_ignored_runtime_and_reports_unignored_pollution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    (root / "runtime" / "very" / "deep").mkdir(parents=True)
    for index in range(100):
        (root / "runtime" / "very" / "deep" / f"{index}.tmp").write_text("x")
    (root / "src" / "__pycache__" / "deep").mkdir(parents=True)
    (root / "src" / "pkg" / "runtime" / "nested").mkdir(parents=True)
    (root / ".git" / "objects" / "runtime").mkdir(parents=True)
    progress: list[str] = []
    monkeypatch.setattr(campaign_module, "PREFLIGHT_SCAN_PROGRESS_ENTRIES", 1)

    result = campaign_module.bounded_repo_runtime_scan(
        root,
        progress_callback=progress.append,
        ignored_classifier=lambda _root, _paths: {"runtime", "src/__pycache__"},
        max_entries=50,
    )

    assert result["ok"] is True
    assert result["ignored_runtime_paths"] == ["runtime", "src/__pycache__"]
    assert result["repo_runtime_pollution"] == ["src/pkg/runtime"]
    assert result["entries_scanned"] < 25
    assert any("scan_progress" in item for item in progress)


def test_repo_runtime_scan_fails_closed_on_directory_traversal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    blocked = root / "blocked"
    blocked.mkdir(parents=True)
    real_scandir = campaign_module.os.scandir

    def failing_scandir(path: object):
        if Path(path) == blocked:
            raise PermissionError("adversarial traversal denial")
        return real_scandir(path)

    monkeypatch.setattr(campaign_module.os, "scandir", failing_scandir)
    result = campaign_module.bounded_repo_runtime_scan(
        root,
        ignored_classifier=lambda _root, _paths: set(),
    )

    assert result["ok"] is False
    assert result["complete"] is False
    assert "preflight_scan_directory_failed" in {
        row["code"] for row in result["errors"]
    }


def test_server_log_snapshot_fails_closed_on_truncate_during_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    log_dir = campaign.primary.runtime_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "server.log"
    path.write_bytes(b"x" * 512 + b"Traceback (most recent call last):\n")
    truncated = False

    def truncate_on_progress(detail: str) -> None:
        nonlocal truncated
        if truncated or not detail.startswith("audit_log_scan_progress:"):
            return
        truncated = True
        path.write_bytes(b"short")

    monkeypatch.setattr(campaign, "_server_progress", truncate_on_progress)
    monkeypatch.setattr(campaign_module, "LOG_SCAN_CHUNK_CHARACTERS", 64)
    monkeypatch.setattr(campaign_module, "LOG_SCAN_PROGRESS_CHARACTERS", 64)
    result = campaign.scan_server_logs([campaign.primary])["primary"]

    assert truncated is True
    assert result["errors"]
    assert any("truncated" in row["code"] for row in result["errors"])


def test_server_log_snapshot_detects_rotation_without_losing_open_fd_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    log_dir = campaign.primary.runtime_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "server.log"
    rotated = log_dir / "server.log.1"
    path.write_bytes(b"x" * 70 + b"database is locked\n" + b"y" * 200)
    replaced = False

    def rotate_on_progress(detail: str) -> None:
        nonlocal replaced
        if replaced or not detail.startswith("audit_log_scan_progress:"):
            return
        replaced = True
        path.replace(rotated)
        path.write_bytes(b"new log")

    monkeypatch.setattr(campaign, "_server_progress", rotate_on_progress)
    monkeypatch.setattr(campaign_module, "LOG_SCAN_CHUNK_CHARACTERS", 64)
    monkeypatch.setattr(campaign_module, "LOG_SCAN_OVERLAP_CHARACTERS", 48)
    monkeypatch.setattr(campaign_module, "LOG_SCAN_PROGRESS_CHARACTERS", 64)
    result = campaign.scan_server_logs([campaign.primary])["primary"]

    assert result["counts"]["database_locked"] == 1
    assert "server_log_replaced_or_rotated_during_snapshot" in {
        row["code"] for row in result["errors"]
    }


def test_server_log_snapshot_fails_closed_on_append_after_initial_length(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    log_dir = campaign.primary.runtime_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "server.log"
    path.write_bytes(b"x" * 256)
    appended = False

    def append_on_progress(detail: str) -> None:
        nonlocal appended
        if appended or not detail.startswith("audit_log_scan_progress:"):
            return
        appended = True
        with path.open("ab") as handle:
            handle.write(b"Traceback (most recent call last):\n")
            handle.flush()
            os.fsync(handle.fileno())

    monkeypatch.setattr(campaign, "_server_progress", append_on_progress)
    monkeypatch.setattr(campaign_module, "LOG_SCAN_CHUNK_CHARACTERS", 64)
    monkeypatch.setattr(campaign_module, "LOG_SCAN_PROGRESS_CHARACTERS", 64)
    result = campaign.scan_server_logs([campaign.primary])["primary"]

    assert appended is True
    assert "server_log_appended_during_snapshot" in {
        row["code"] for row in result["errors"]
    }


def test_server_log_snapshot_detects_same_size_in_place_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    log_dir = campaign.primary.runtime_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "server.log"
    path.write_bytes(b"x" * 256)
    rewritten = False

    def rewrite_on_progress(detail: str) -> None:
        nonlocal rewritten
        if rewritten or not detail.startswith("audit_log_scan_progress:"):
            return
        rewritten = True
        before = path.stat()
        with path.open("r+b") as handle:
            handle.seek(128)
            handle.write(b"Y")
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(
            path,
            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
        )

    monkeypatch.setattr(campaign, "_server_progress", rewrite_on_progress)
    monkeypatch.setattr(campaign_module, "LOG_SCAN_CHUNK_CHARACTERS", 64)
    monkeypatch.setattr(campaign_module, "LOG_SCAN_PROGRESS_CHARACTERS", 64)
    result = campaign.scan_server_logs([campaign.primary])["primary"]

    assert rewritten is True
    assert "server_log_metadata_changed_during_snapshot" in {
        row["code"] for row in result["errors"]
    }


def test_server_log_discovery_has_hard_file_cap_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    log_dir = campaign.primary.runtime_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        (log_dir / f"server-{index}.log").write_text("clean\n", encoding="utf-8")
    monkeypatch.setattr(campaign_module, "LOG_SCAN_MAX_FILES", 2)

    result = campaign.scan_server_logs([campaign.primary])["primary"]

    assert result["discovery"]["files"] == 2
    assert result["discovery"]["ok"] is False
    assert "server_log_file_limit_exceeded" in {
        row["code"] for row in result["errors"]
    }


def test_server_log_discovery_deduplicates_same_inode(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    log_dir = campaign.primary.runtime_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    first = log_dir / "server.log"
    second = log_dir / "server.out"
    first.write_text("database is locked\n", encoding="utf-8")
    os.link(first, second)

    result = campaign.scan_server_logs([campaign.primary])["primary"]

    assert result["discovery"]["files"] == 1
    assert len(result["discovery"]["duplicate_paths"]) == 1
    assert result["counts"]["database_locked"] == 1


def test_final_server_log_scan_is_after_all_server_touching_cleanup() -> None:
    source = inspect.getsource(Campaign.run)

    assert source.index("account_cleanup = self.cleanup_campaign_accounts") < source.index(
        "server_logs = self.scan_server_logs"
    )
    assert source.index("security_final = self.production_security_sentinel_check") < source.index(
        "server_logs = self.scan_server_logs"
    )
    assert source.index('controller.stop(reason="final_evidence_log_seal")') < source.index(
        "server_logs = self.scan_server_logs"
    )


def test_server_stop_reports_progress_and_fails_when_process_group_remains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    controller = campaign.primary
    progress: list[str] = []
    controller.progress_callback = progress.append
    monkeypatch.setattr(controller, "pid", lambda: 4242)
    monkeypatch.setattr(controller, "_pid_matches_runtime", lambda _pid: True)

    class ProcPath:
        def __init__(self, value: object) -> None:
            self.value = str(value)

        def exists(self) -> bool:
            return True

    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = Clock()
    signals: list[int] = []
    monkeypatch.setattr(campaign_module, "Path", ProcPath)
    monkeypatch.setattr(campaign_module.os, "getpgid", lambda _pid: 4242)
    monkeypatch.setattr(
        campaign_module.os,
        "killpg",
        lambda _pgid, signum: signals.append(int(signum)),
    )
    monkeypatch.setattr(campaign_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(campaign_module.time, "sleep", clock.sleep)

    result = controller.stop(reason="adversarial_orphan")

    assert result["ok"] is False
    assert result["master_process_remaining"] is True
    assert result["process_group_remaining"] is True
    assert signal.SIGTERM in signals
    assert signal.SIGKILL in signals
    assert any("stop_waiting_for_process_group" in item for item in progress)
    assert progress[-1].endswith(":0")


def test_child_report_loader_rejects_oversize_before_reading(tmp_path: Path) -> None:
    path = tmp_path / "oversize.json"
    path.write_bytes(b"{" + b" " * 255 + b"}")

    payload, validation = campaign_module.load_bounded_child_report(
        path,
        expected_schema=campaign_module.OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
        max_bytes=64,
    )

    assert payload["ok"] is False
    assert validation["scanned_bytes"] == 0
    assert "child_report_size_limit_exceeded" in {
        row["code"] for row in validation["errors"]
    }


def test_child_report_loader_enforces_schema_and_nested_cardinality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "core.json"
    path.write_text(
        json.dumps({
            "schema_version": campaign_module.OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
            "ok": True,
            "accounts": [],
            "round_runs": [],
            "partial_round_runs": [],
            "browser_runs": [],
            "effective_load": {
                "target_load_samples": [{"ok": True}, {"ok": True}],
                "ramp": {"stages": {}},
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setitem(
        campaign_module.CORE_REPORT_CARDINALITY_LIMITS,
        "target_load_samples",
        1,
    )

    _payload, validation = campaign_module.load_bounded_child_report(
        path,
        expected_schema=campaign_module.OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
    )

    assert validation["ok"] is False
    assert any(
        row["code"] == "child_report_cardinality_limit_exceeded"
        and row["field"] == "effective_load.target_load_samples"
        for row in validation["errors"]
    )


def test_child_report_loader_accepts_bounded_valid_report_and_publishes_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "core.json"
    path.write_text(
        json.dumps({
            "schema_version": campaign_module.OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
            "ok": True,
            "accounts": [],
            "round_runs": [],
            "partial_round_runs": [],
            "browser_runs": [],
            "effective_load": {
                "target_load_samples": [],
                "ramp": {"stages": {}},
            },
        }),
        encoding="utf-8",
    )
    progress: list[str] = []
    monkeypatch.setattr(campaign_module, "CORE_REPORT_READ_CHUNK_BYTES", 16)
    monkeypatch.setattr(campaign_module, "CORE_REPORT_PROGRESS_BYTES", 16)

    payload, validation = campaign_module.load_bounded_child_report(
        path,
        expected_schema=campaign_module.OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
        progress_callback=progress.append,
    )

    assert payload["ok"] is True
    assert validation["ok"] is True
    assert validation["scanned_bytes"] == path.stat().st_size
    assert progress


def test_child_report_loader_caps_ramp_stage_cardinality_at_four(tmp_path: Path) -> None:
    path = tmp_path / "core.json"
    path.write_text(
        json.dumps({
            "schema_version": campaign_module.OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
            "ok": True,
            "accounts": [],
            "round_runs": [],
            "partial_round_runs": [],
            "browser_runs": [],
            "effective_load": {
                "target_load_samples": [],
                "ramp": {
                    "stages": {
                        str(stage): {"round_evidence": []}
                        for stage in (4, 8, 16, 32, 64)
                    },
                },
            },
        }),
        encoding="utf-8",
    )

    _payload, validation = campaign_module.load_bounded_child_report(
        path,
        expected_schema=campaign_module.OPERATIONAL_SOAK_REPORT_SCHEMA_VERSION,
    )

    assert campaign_module.CORE_REPORT_CARDINALITY_LIMITS["ramp_stages"] == 4
    assert validation["ok"] is False
    assert any(
        row["code"] == "child_report_cardinality_limit_exceeded"
        and row["field"] == "effective_load.ramp.stages"
        and row["actual"] == 5
        for row in validation["errors"]
    )
