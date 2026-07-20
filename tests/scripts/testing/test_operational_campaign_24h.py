from __future__ import annotations

import json
import hashlib
import inspect
import os
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable

import pytest

from scripts.testing import operational_campaign_24h as campaign_module
from scripts.testing.operational_campaign_24h import (
    MIN_FORMAL_SECONDS,
    EFFECTIVE_LOAD_EVIDENCE_SCHEMA_VERSION,
    SUPERVISED_LEVEL_DURATIONS,
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
from services.server.database import get_audit_db
from services.system import audit as audit_service


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


def test_reliability_soak_is_full_day_low_concurrency_without_capacity_claim() -> None:
    assert SUPERVISED_LEVEL_DURATIONS["soak"] == 86_400
    assert SUPERVISED_RUNNER_PROFILES["soak"]["workers"] == 2
    assert SUPERVISED_RUNNER_PROFILES["soak"]["threads"] == 2
    assert SUPERVISED_RUNNER_PROFILES["soak"]["concurrency"] == 4
    assert SUPERVISED_RUNNER_PROFILES["soak"]["resource_interval"] == 2.0
    assert SUPERVISED_LOAD_POLICIES["soak"]["ramp_required"] is False
    validation = validate_effective_load_evidence({}, campaign_level="soak")
    assert validation == {"required": False, "ok": True, "errors": []}


def test_smoke_auxiliary_servers_use_one_worker_without_weakening_soak() -> None:
    assert campaign_module.managed_auxiliary_worker_count(
        requested_workers=1,
        supervised=True,
        campaign_level="smoke",
    ) == 1
    assert campaign_module.managed_auxiliary_worker_count(
        requested_workers=2,
        supervised=True,
        campaign_level="soak",
    ) == 2
    assert campaign_module.managed_auxiliary_worker_count(
        requested_workers=2,
        supervised=False,
        campaign_level="smoke",
    ) == 2
    assert campaign_module.managed_strict_readiness(
        supervised=True,
        campaign_level="smoke",
    ) is False
    assert campaign_module.managed_strict_readiness(
        supervised=True,
        campaign_level="soak",
    ) is True


def test_launcher_post_bootstrap_gate_requires_safe_callback_before_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    controller = campaign.primary
    controller.post_bootstrap_safety_callback = lambda: {
        "ok": True,
        "tripped": [],
    }
    controller.launch_count = 1
    controller._prepare_post_bootstrap_gate()
    assert controller.post_bootstrap_ready_file is not None
    assert controller.post_bootstrap_release_file is not None
    environment = controller._env()
    assert environment["HACKME_DEV_POST_BOOTSTRAP_READY_FILE"] == str(
        controller.post_bootstrap_ready_file
    )
    assert environment["HACKME_DEV_POST_BOOTSTRAP_RELEASE_FILE"] == str(
        controller.post_bootstrap_release_file
    )
    assert environment["HACKME_DEV_POST_BOOTSTRAP_NONCE"] == (
        controller.post_bootstrap_nonce
    )
    controller.post_bootstrap_ready_file.write_text(
        controller.post_bootstrap_nonce + "\n",
        encoding="ascii",
    )
    os.chmod(controller.post_bootstrap_ready_file, 0o600)

    class Process:
        pid = os.getpid()

        def poll(self) -> int | None:
            return 0 if controller.post_bootstrap_release_file.exists() else None

        @staticmethod
        def wait(timeout: float | None = None) -> int:
            return -15

    monkeypatch.setattr(
        controller,
        "_launcher_observation",
        lambda _process, _log: ("post-bootstrap",),
    )

    released = controller._wait_launcher(
        Process(),  # type: ignore[arg-type]
        tmp_path / "launcher.log",
        timeout=2.0,
    )

    assert released["returncode"] == 0
    assert released["post_bootstrap"]["ok"] is True
    assert released["host_safety_blocked"] is False
    assert controller.post_bootstrap_release_file.read_text(encoding="ascii").strip() == (
        controller.post_bootstrap_nonce
    )

    controller.post_bootstrap_safety_callback = lambda: {
        "ok": False,
        "tripped": ["HOST_IO_PRESSURE_HIGH"],
    }
    controller.launch_count = 2
    controller._prepare_post_bootstrap_gate()
    assert controller.post_bootstrap_ready_file is not None
    controller.post_bootstrap_ready_file.write_text(
        controller.post_bootstrap_nonce + "\n",
        encoding="ascii",
    )
    os.chmod(controller.post_bootstrap_ready_file, 0o600)
    monkeypatch.setattr(
        campaign_module,
        "terminate_process_group",
        lambda *_args, **_kwargs: None,
    )

    blocked = controller._wait_launcher(
        Process(),  # type: ignore[arg-type]
        tmp_path / "launcher.log",
        timeout=2.0,
    )

    assert blocked["host_safety_blocked"] is True
    assert blocked["post_bootstrap"]["host_safety"]["tripped"] == [
        "HOST_IO_PRESSURE_HIGH"
    ]
    assert controller.post_bootstrap_release_file is not None
    assert not controller.post_bootstrap_release_file.exists()


def test_run_group_fails_closed_without_starting_dependent_steps(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    calls: list[str] = []

    def failed_producer() -> dict[str, object]:
        calls.append("producer")
        return {"ok": False, "error": "terminal_assertion_failed"}

    def unsafe_consumer() -> dict[str, object]:
        calls.append("consumer")
        return {"ok": True}

    result = campaign.run_group(
        "ai_agent_positive_operations",
        [failed_producer, unsafe_consumer],
    )

    assert result["ok"] is False
    assert result["terminal_state"] == "failed"
    assert calls == ["producer"]
    assert len(result["steps"]) == 1


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
    for name, value in SUPERVISED_RUNNER_PROFILES["smoke"].items():
        setattr(args, name, value)
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
    runner_auth_key = b"k" * 32
    control_auth_hash = campaign_module.hashlib.sha256(runner_auth_key).hexdigest()
    setattr(args, "_runner_auth_key", runner_auth_key)
    args.supervisor_pid = 1234
    args.supervisor_start_ticks = 5678
    args.supervisor_boot_id = "boot-test"
    args.supervisor_cgroup = "/supervisor.scope"
    supervisor_identity = {
        "pid": args.supervisor_pid,
        "start_ticks": args.supervisor_start_ticks,
        "boot_id": args.supervisor_boot_id,
        "cgroup_path": args.supervisor_cgroup,
    }
    setattr(args, "_control_auth_evidence", {
        "session_secret_sha256": control_auth_hash,
        "server_identity_verified": True,
        "server_process": supervisor_identity,
    })
    contract = {
        "level": "smoke",
        "duration_seconds": 180,
        "campaign_root": str(campaign_root),
        "control_root": str(control_root),
        "watchdog_liveness_path": str(control_root / "checkpoint" / "watchdog.liveness.json"),
        "runner_auth_key_sha256": control_auth_hash,
        "watchdog_auth_key_sha256": campaign_module.hashlib.sha256(b"w" * 32).hexdigest(),
        "role_separated_auth_keys": True,
        "supervisor_identity": supervisor_identity,
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
                "authenticated_control_channel_verified",
                "runner_control_channel_authenticated",
                "watchdog_reciprocal_liveness_verified",
                "runner_import_staged_verified",
                "watchdog_import_staged_verified",
                "host_safety_runner_import_settled",
                "host_safety_state_initialization_settled",
                "host_safety_activation_verified",
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
        "content_evidence_mode": campaign_module.METADATA_CONTENT_EVIDENCE,
        "require_clean": False,
    }
    args.duration_seconds = 180

    validate_supervised_runtime_contract(args, contract, source)

    staged_gate = contract["gates"].pop(
        "host_safety_runner_import_settled"
    )
    with pytest.raises(
        RuntimeError,
        match="gate:host_safety_runner_import_settled",
    ):
        validate_supervised_runtime_contract(args, contract, source)
    contract["gates"]["host_safety_runner_import_settled"] = staged_gate

    args.duration_seconds = 179
    with pytest.raises(RuntimeError, match="runner_duration_seconds"):
        validate_supervised_runtime_contract(args, contract, source)
    args.duration_seconds = 180
    source["tracked_content_digest"] = "c" * 64
    with pytest.raises(RuntimeError, match="source_digest"):
        validate_supervised_runtime_contract(args, contract, source)
    source["tracked_content_digest"] = contract["source_digest"]
    source["content_evidence_mode"] = campaign_module.FULL_CONTENT_EVIDENCE
    with pytest.raises(RuntimeError, match="source_content_evidence_mode"):
        validate_supervised_runtime_contract(args, contract, source)


def test_rehearsal_contract_requires_exact_managed_comfyui_receipt_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = campaign_args(
        tmp_path,
        "--supervised",
        "--campaign-uuid",
        "campaign-comfyui",
        "--cgroup-path",
        "/test.scope",
        "--checkpoint-mirror-path",
        str(
            Path.home()
            / "logs"
            / "hackme_web_campaign_24h"
            / "campaign-comfyui"
            / "campaign.checkpoint.json"
        ),
    )
    args.duration_seconds = 3600
    args.minimum_free_gb = 20.0
    for name, value in SUPERVISED_RUNNER_PROFILES["rehearsal"].items():
        setattr(args, name, value)
    campaign_root = Path(args.campaign_root).resolve()
    control_root = campaign_root.parent / ".campaign-comfyui-control"
    ready_path = control_root / "artifacts" / "comfyui_backend" / "ready.json"
    lifecycle_path = ready_path.with_name("lifecycle.json")
    stdout_path = ready_path.with_name("backend.stdout")
    ready_path.parent.mkdir(parents=True)
    backend_root = (tmp_path / "managed-comfyui").resolve()
    backend_root.mkdir()
    backend_models = backend_root / "models"
    backend_models.mkdir()
    backend_main = backend_root / "main.py"
    backend_main.write_text("# contract fixture\n", encoding="utf-8")
    backend_python = Path(sys.executable).resolve(strict=True)
    backend_command = [
        str(backend_python),
        str(backend_main),
        "--listen",
        "127.0.0.1",
        "--port",
        "48188",
        "--disable-auto-launch",
    ]
    backend_command_sha256 = campaign_module.hashlib.sha256(
        b"\0".join(value.encode() for value in backend_command)
    ).hexdigest()
    models_metadata = backend_models.stat()
    models_binding = {
        "entry_path": str(backend_models),
        "realpath": str(backend_models),
        "device": int(models_metadata.st_dev),
        "inode": int(models_metadata.st_ino),
        "mode": stat.S_IMODE(models_metadata.st_mode),
        "uid": int(models_metadata.st_uid),
        "gid": int(models_metadata.st_gid),
        "symlink": False,
        "ok": True,
    }
    zero_capabilities = {
        name: "0000000000000000"
        for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    }
    confinement = {
        "schema_version": campaign_module.SANDBOX_PROOF_SCHEMA_VERSION,
        "nonce": "f" * 32,
        "actual_execution": True,
        "simulated": False,
        "adopted_external_process": False,
        "shell": False,
        "fixed_command": backend_command,
        "expected_host_cgroup_path": "/test.scope/comfyui",
        "launcher": {
            "host_pid": 4141,
            "host_process_group": 4141,
            "host_session": 4141,
            "process_group_leader": True,
        },
        "host_transition": {
            "schema_version": campaign_module.HOST_TRANSITION_SCHEMA_VERSION,
            "nonce": "f" * 32,
            "pid": 4141,
            "start_ticks": 123456,
            "boot_id": "launcher-boot-test",
            "cgroup_path": "/test.scope/comfyui",
            "ok": True,
        },
        "mounts": {
            "cgroup_namespace_path": "/",
            "leaf_kernel_objects_match": True,
            "ok": True,
        },
        "privileges": {
            "capability_sets": zero_capabilities,
            "securebits_locked": True,
            "no_new_privileges": True,
            "seccomp": {"mode": 2, "ok": True},
            "ok": True,
        },
        "cgroup_write_denial": {
            "write_open_succeeded": False,
            "errno": 13,
            "ok": True,
        },
        "workload_delegation_capability": False,
        "workload_delegation_confinement": {
            "workload_delegation_capability": False,
            "namespace_rooted_cgroup2": True,
            "cgroup2_read_only": True,
            "capability_sets_zero": True,
            "namespace_and_mount_syscalls_denied": True,
            "ok": True,
        },
        "proof_written_before_exec": True,
        "outer_launcher_preserves_process_group": True,
        "reaper_preserves_wait_status": True,
        "ok": True,
    }
    ready = {
        "schema_version": campaign_module.COMFYUI_BACKEND_READY_SCHEMA_VERSION,
        "ok": True,
        "actual_execution": True,
        "simulated": False,
        "adopted_external_pid": False,
        "api_url": "http://127.0.0.1:48188",
        "python_executable": str(backend_python),
        "main_path": str(backend_main),
        "working_root": str(backend_root),
        "models_root": str(backend_models),
        "models_binding": models_binding,
        "command": backend_command,
        "command_sha256": backend_command_sha256,
        "environment_keys": [
            "HACKME_CAMPAIGN_COMFYUI_API_URL",
            "HACKME_CAMPAIGN_COMFYUI_INSTANCE_ID",
            "HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT",
            "PYTHONUNBUFFERED",
        ],
        "process": {
            "pid": 4242,
            "start_ticks": 987654,
            "boot_id": "backend-boot-test",
            "cgroup_path": "/test.scope/comfyui",
            "cwd": str(backend_root),
            "executable": str(backend_python),
            "process_group": 4141,
            "no_new_privileges": True,
            "seccomp_mode": 2,
            "capability_sets": zero_capabilities,
            "namespace_pids": [4242, 2],
            "namespace_links": {
                name: f"fixture:[{name}]"
                for name in ("user", "mnt", "cgroup", "pid")
            },
            "models_binding": models_binding,
            "ok": True,
        },
        "placement": {
            "pid": 4242,
            "start_ticks": 987654,
            "campaign_cgroup": "/test.scope/comfyui",
            "ok": True,
        },
        "managed_leaf": {
            "cgroup_path": "/test.scope/comfyui",
            "device": 7,
            "inode": 11,
            "subtree_controllers_enabled": False,
            "descendant_cgroups": 0,
            "host_leaf_state_before_sandbox": "pending_sandbox",
            "workload_delegation_capability": False,
            "ok": True,
        },
        "managed_leaf_state": {
            "cgroup_path": "/test.scope/comfyui",
            "pids": [4141, 4242],
            "populated": 1,
            "consistent": True,
            "subtree_control": [],
            "descendant_cgroups": 0,
            "topology_intact": True,
            "workload_delegation_capability": False,
            "ok": True,
        },
        "confinement": confinement,
        "sandbox": confinement,
        "sandbox_live": {
            "launcher_pid": 4141,
            "backend_host_pid": 4242,
            "process_group": 4141,
            "namespace_links": {
                name: f"fixture:[{name}]"
                for name in ("user", "mnt", "cgroup", "pid")
            },
            "namespace_pid": 2,
            "leaf_pids": [4141, 4242],
            "workload_delegation_capability": False,
            "ok": True,
        },
        "launcher": {
            "pid": 4141,
            "process_group": 4141,
            "session": 4141,
            "ok": True,
        },
        "listener": {
            "family": "ipv4",
            "address": "127.0.0.1",
            "port": 48188,
            "socket_inode": 123456,
            "owner_pids": [4242],
            "loopback_only": True,
            "ok": True,
        },
        "listener_stable_across_readiness": True,
        "readiness": {
            "endpoint": "http://127.0.0.1:48188/system_stats",
            "system_fields": ["python_version"],
            "device_count": 1,
            "device_names": ["fixture-device"],
            "ok": True,
        },
    }
    ready_path.write_text(json.dumps(ready), encoding="utf-8")
    ready_path.chmod(0o400)
    ready_metadata = ready_path.stat()
    ready_sha256 = campaign_module.hashlib.sha256(
        ready_path.read_bytes()
    ).hexdigest()
    for name, path in (
        ("state_path", control_root / "checkpoint" / "campaign.state.json"),
        ("control_path", control_root / "checkpoint" / "campaign.control.json"),
        ("heartbeat_path", control_root / "checkpoint" / "campaign.heartbeat.json"),
        ("checkpoint_path", control_root / "checkpoint" / "campaign.checkpoint.json"),
        ("source_freeze_path", control_root / "artifacts" / "source" / "H0" / "source_freeze.json"),
        ("activation_gate", control_root / "checkpoint" / "campaign.activation.json"),
        ("supervisor_contract", control_root / "checkpoint" / "supervisor.contract.json"),
    ):
        setattr(args, name, str(path))
    args.control_root = str(control_root)
    runner_auth_key = b"r" * 32
    runner_auth_hash = campaign_module.hashlib.sha256(runner_auth_key).hexdigest()
    setattr(args, "_runner_auth_key", runner_auth_key)
    args.supervisor_pid = 1234
    args.supervisor_start_ticks = 5678
    args.supervisor_boot_id = "boot-test"
    args.supervisor_cgroup = "/supervisor.scope"
    supervisor_identity = {
        "pid": 1234,
        "start_ticks": 5678,
        "boot_id": "boot-test",
        "cgroup_path": "/supervisor.scope",
    }
    setattr(args, "_control_auth_evidence", {
        "session_secret_sha256": runner_auth_hash,
        "server_identity_verified": True,
        "server_process": supervisor_identity,
    })
    contract = {
        "level": "rehearsal",
        "duration_seconds": 3600,
        "campaign_root": str(campaign_root),
        "control_root": str(control_root),
        "watchdog_liveness_path": str(control_root / "checkpoint" / "watchdog.liveness.json"),
        "runner_auth_key_sha256": runner_auth_hash,
        "watchdog_auth_key_sha256": campaign_module.hashlib.sha256(b"w" * 32).hexdigest(),
        "role_separated_auth_keys": True,
        "supervisor_identity": supervisor_identity,
        "checkpoint_mirror_path": str(Path(args.checkpoint_mirror_path).resolve()),
        "cgroup_path": "/test.scope",
        "cgroup_event_baseline": {
            "memory.events": {"max": 0, "oom": 0, "oom_kill": 0},
            "pids.events": {"max": 0},
        },
        "commit": "a" * 40,
        "source_digest": "b" * 64,
        "runner_profile": dict(SUPERVISED_RUNNER_PROFILES["rehearsal"]),
        "load_policy": dict(SUPERVISED_LOAD_POLICIES["rehearsal"]),
        "comfyui_backend": {
            "status": "ready",
            "ok": True,
            "actual_execution": True,
            "simulated": False,
            "adopted_external_pid": False,
            "api_url": ready["api_url"],
            "python_executable": ready["python_executable"],
            "main_path": ready["main_path"],
            "working_root": ready["working_root"],
            "models_root": ready["models_root"],
            "command": backend_command,
            "command_sha256": backend_command_sha256,
            "backend_pid": 4242,
            "backend_start_ticks": 987654,
            "backend_boot_id": "backend-boot-test",
            "backend_cgroup": "/test.scope/comfyui",
            "launcher_pid": 4141,
            "process_group": 4141,
            "managed_leaf": {
                "cgroup_path": "/test.scope/comfyui",
                "device": 7,
                "inode": 11,
                "subtree_controllers_enabled": False,
                "descendant_cgroups": 0,
                "host_leaf_state_before_sandbox": "pending_sandbox",
                "workload_delegation_capability": False,
                "ok": True,
            },
            "confinement": confinement,
            "sandbox": confinement,
            "ready_receipt": str(ready_path),
            "ready_receipt_sha256": ready_sha256,
            "ready_receipt_identity": {
                "sha256": ready_sha256,
                "device": int(ready_metadata.st_dev),
                "inode": int(ready_metadata.st_ino),
                "size": int(ready_metadata.st_size),
                "mode": stat.S_IMODE(ready_metadata.st_mode),
                "uid": int(ready_metadata.st_uid),
                "gid": int(ready_metadata.st_gid),
                "link_count": int(ready_metadata.st_nlink),
                "mtime_ns": int(ready_metadata.st_mtime_ns),
                "ctime_ns": int(ready_metadata.st_ctime_ns),
                "nofollow_stable": True,
            },
            "lifecycle_path": str(lifecycle_path),
            "stdout_path": str(stdout_path),
        },
        "gates": {
            name: {"status": "PASS", "machine_verified": True}
            for name in (
                "authenticated_control_channel_verified",
                "runner_control_channel_authenticated",
                "watchdog_reciprocal_liveness_verified",
                "runner_import_staged_verified",
                "watchdog_import_staged_verified",
                "host_safety_runner_import_settled",
                "host_safety_state_initialization_settled",
                "host_safety_activation_verified",
                "cgroup_limits_verified",
                "external_watchdog_verified",
                "runner_and_watchdog_placement_verified",
                "cgroup_event_baseline_verified",
                "source_baseline_frozen",
                "comfyui_backend_lifecycle_verified",
                "host_safety_backend_startup_settled",
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
        "content_evidence_mode": campaign_module.FULL_CONTENT_EVIDENCE,
        "require_clean": False,
    }
    monkeypatch.setenv("HACKME_CAMPAIGN_COMFYUI_API_URL", str(ready["api_url"]))
    monkeypatch.setenv("HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT", str(ready["models_root"]))
    monkeypatch.setenv("HACKME_CAMPAIGN_COMFYUI_BACKEND_PID", "4242")
    monkeypatch.setattr(
        campaign_module,
        "validate_live_comfyui_backend_authority",
        lambda contract, payload: {
            "pid": contract["backend_pid"],
            "receipt_pid": payload["process"]["pid"],
            "ok": True,
        },
    )

    validate_supervised_runtime_contract(args, contract, source)

    contract["level"] = "soak"
    contract["duration_seconds"] = MIN_FORMAL_SECONDS
    contract["runner_profile"] = dict(SUPERVISED_RUNNER_PROFILES["soak"])
    contract["load_policy"] = dict(SUPERVISED_LOAD_POLICIES["soak"])
    args.duration_seconds = MIN_FORMAL_SECONDS
    for name, value in SUPERVISED_RUNNER_PROFILES["soak"].items():
        setattr(args, name, value)
    validate_supervised_runtime_contract(args, contract, source)

    contract["level"] = "rehearsal"
    contract["duration_seconds"] = 3600
    contract["runner_profile"] = dict(SUPERVISED_RUNNER_PROFILES["rehearsal"])
    contract["load_policy"] = dict(SUPERVISED_LOAD_POLICIES["rehearsal"])
    args.duration_seconds = 3600
    for name, value in SUPERVISED_RUNNER_PROFILES["rehearsal"].items():
        setattr(args, name, value)

    monkeypatch.setenv("HACKME_CAMPAIGN_COMFYUI_BACKEND_PID", "9999")
    with pytest.raises(RuntimeError, match="comfyui_backend_environment"):
        validate_supervised_runtime_contract(args, contract, source)

    monkeypatch.setenv("HACKME_CAMPAIGN_COMFYUI_BACKEND_PID", "4242")
    contract["comfyui_backend"]["command"] = [
        *backend_command,
        "--tampered",
    ]
    with pytest.raises(RuntimeError, match="comfyui_backend_ready_authority"):
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
    for name, value in SUPERVISED_RUNNER_PROFILES["smoke"].items():
        setattr(args, name, value)
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
                "authenticated_control_channel_verified",
                "runner_control_channel_authenticated",
                "watchdog_reciprocal_liveness_verified",
                "runner_import_staged_verified",
                "watchdog_import_staged_verified",
                "host_safety_runner_import_settled",
                "host_safety_state_initialization_settled",
                "host_safety_activation_verified",
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
        "content_evidence_mode": campaign_module.METADATA_CONTENT_EVIDENCE,
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


def test_supervised_source_identity_reuses_authenticated_h0() -> None:
    source_hashes, digest, file_count, metadata = (
        campaign_module.supervised_source_identity({
            "commit": "a" * 40,
            "branch": "test-branch",
            "tracked_content_digest": "b" * 64,
            "tracked_file_count": 2009,
            "git_status_empty": False,
            "status": {
                "blocked_changes": [
                    {"path": "server.py", "status": "M"},
                    {"path": "new.py", "status": "??"},
                ],
            },
        })
    )

    assert source_hashes == {}
    assert digest == "b" * 64
    assert file_count == 2009
    assert metadata == {
        "target_commit": "a" * 40,
        "target_branch": "test-branch",
        "worktree_dirty": True,
        "worktree_change_count": 2,
        "authority": "supervisor_h0",
    }


def test_runner_host_safety_cold_start_wait_keeps_thresholds_and_extends_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    expected = {"ok": True, "tripped": []}

    def fake_wait(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(campaign_module, "wait_for_host_safety_preflight", fake_wait)

    result = campaign_module.wait_for_runner_host_safety_preflight()

    assert result is expected
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == (
        campaign_module.RUNNER_HOST_SAFETY_TIMEOUT_SECONDS
    )
    assert calls[0]["collector"] is campaign_module.collect_runner_startup_headroom

    collector_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        campaign_module,
        "collect_host_startup_safety_preflight",
        lambda **kwargs: collector_calls.append(kwargs) or expected,
    )

    assert campaign_module.collect_runner_startup_headroom() is expected
    assert collector_calls == [{}]


def test_smoke_preflight_probes_only_the_dependency_it_exercises() -> None:
    smoke = campaign_module.preflight_dependency_commands("smoke")
    formal = campaign_module.preflight_dependency_commands("formal")

    assert set(smoke) == {"gunicorn"}
    assert set(formal) == {"ffmpeg", "ffprobe", "playwright", "gunicorn"}


def test_smoke_preflight_explicitly_omits_repo_scale_runtime_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path] = []
    expected = {
        "schema_version": "hackme.preflight-runtime-scan.v1",
        "ok": True,
        "complete": True,
        "entries_scanned": 1,
        "repo_runtime_pollution": [],
        "errors": [],
    }

    monkeypatch.setattr(
        campaign_module,
        "bounded_repo_runtime_scan",
        lambda root, **_kwargs: calls.append(root) or expected,
    )

    smoke = campaign_module.preflight_repo_runtime_scan(
        tmp_path,
        campaign_level="smoke",
    )
    formal = campaign_module.preflight_repo_runtime_scan(
        tmp_path,
        campaign_level="formal",
    )

    assert smoke == {
        "schema_version": "hackme.preflight-runtime-scan.v1",
        "status": "NOT_APPLICABLE",
        "reason": (
            "level_0_smoke_uses_supervisor_source_freeze_and_isolated_run_root"
        ),
        "required": False,
        "ok": True,
        "complete": False,
        "entries_scanned": 0,
        "repo_runtime_pollution": [],
        "errors": [],
    }
    assert formal == {**expected, "status": "EVALUATED", "required": True}
    assert calls == [tmp_path]


@pytest.mark.parametrize(
    ("actual_cgroup", "expected_ok"),
    (("/campaign.scope", True), ("/other.scope", False)),
)
def test_role_inheritance_uses_one_direct_child_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    actual_cgroup: str,
    expected_ok: bool,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.cgroup_path = "/campaign.scope"
    campaign.reports.mkdir(parents=True)
    popen_calls: list[tuple[list[str], dict[str, object]]] = []
    signals: list[tuple[int, int]] = []

    class Probe:
        pid = 43210
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, *, timeout: float) -> int:
            assert timeout == 5
            self.returncode = -signal.SIGTERM
            return self.returncode

    def fake_popen(
        command: list[str],
        **kwargs: object,
    ) -> Probe:
        popen_calls.append((command, kwargs))
        return Probe()

    monkeypatch.setattr(campaign_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        campaign_module,
        "_current_unified_cgroup",
        lambda _pid: actual_cgroup,
    )
    monkeypatch.setattr(
        campaign_module.os,
        "killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    result = campaign.verify_role_inheritance()

    assert result["ok"] is expected_ok
    assert result["probe_count"] == 1
    assert result["probe_mode"] == "single_direct_child_kernel_inheritance"
    assert result["managed_roles_covered"] == sorted(
        campaign_module.MANDATORY_MANAGED_ROLES
    )
    assert len(popen_calls) == 1
    assert popen_calls[0][0] == ["/bin/sleep", "30"]
    assert signals == [(43210, signal.SIGTERM)]
    if expected_ok:
        assert result["errors"] == []
    else:
        assert result["errors"] == [
            f"direct_child:outside_campaign_scope:{actual_cgroup}"
        ]


def test_server_startup_waits_for_host_safety_before_starting_next_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Controller:
        def __init__(self, name: str) -> None:
            self.name = name

        def start(self) -> dict[str, object]:
            calls.append(self.name)
            return {"ok": True, "name": self.name}

    campaign = Campaign.__new__(Campaign)
    campaign.reports = tmp_path / "reports"
    campaign.reports.mkdir()
    campaign.primary = Controller("primary")  # type: ignore[assignment]
    campaign.recovery = Controller("recovery")  # type: ignore[assignment]
    campaign.security_sentinel = Controller("security_sentinel")  # type: ignore[assignment]
    checkpoints: list[str] = []
    campaign.write_checkpoint = checkpoints.append  # type: ignore[method-assign]
    monkeypatch.setattr(
        campaign_module,
        "wait_for_runner_host_safety_preflight",
        lambda: {
            "ok": False,
            "tripped": ["HOST_IO_PRESSURE_HIGH"],
        },
    )

    result = campaign._start_managed_servers_with_host_safety()

    assert result["ok"] is False
    assert result["classification"] == "FAIL_INFRA"
    assert result["failed_stage"] == "primary_host_safety"
    assert calls == ["primary"]
    assert checkpoints == ["starting_primary", "waiting_for_primary_host_safety"]
    evidence = campaign_module.load_json(
        campaign.reports / "host_safety_after_primary.json"
    )
    assert evidence["tripped"] == ["HOST_IO_PRESSURE_HIGH"]


def test_git_metadata_disables_optional_index_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"command": list(command), **kwargs})
        stdout = ""
        if "rev-parse" in command and "HEAD" in command:
            stdout = "a" * 40 + "\n"
        elif "--abbrev-ref" in command:
            stdout = "test-branch\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(campaign_module.subprocess, "run", fake_run)

    metadata = campaign_module.git_metadata()

    assert metadata["target_commit"] == "a" * 40
    assert calls
    assert all(
        isinstance(call.get("env"), dict)
        and call["env"].get("GIT_OPTIONAL_LOCKS") == "0"  # type: ignore[union-attr]
        for call in calls
    )


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


def test_rehearsal_native_scenario_binding_preflight_is_complete(tmp_path: Path) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.campaign_level = "rehearsal"

    result, required = campaign.formal_scenario_binding_preflight()

    assert required is True
    assert result["status"] == "PASS"
    assert result["gate_pass"] is True
    assert result["reviewed_scenario_count"] == 13
    assert result["required_evidence_count"] == 91
    assert result["registered_runner_count"] == 13
    assert result["registered_evidence_adapter_count"] == 91
    assert result["registered_validator_count"] == 39
    assert result["runtime_execution_pipeline_verified"] is True
    assert result["fully_bound_scenario_count"] == 13
    assert result["fully_bound_scenario_ids"] == [
        "ai_agent_positive_operations",
        "backup_restore_restart",
        "bt_download_stream_restart",
        "cloud_drive_share_stream",
        "comfyui_real_workflows",
        "community_governance_operations",
        "final_ui_mobile_prelaunch",
        "media_long_hls_share",
        "media_proxy_cross_browser",
        "pointschain_hft_invariants",
        "server_emergency_incident",
        "trading_background_custom_workflow",
        "wallet_incident_governance",
    ]
    assert result["registration_coverage"]["media_long_hls_share"]["runner_registered"] is True
    assert result["registration_coverage"]["bt_download_stream_restart"]["runner_registered"] is True


def test_bt_formal_runner_streams_exact_magnet_artifact_without_secret_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.accounts = [("bt-member", "BtMemberPassword!")]
    scenario_id = "bt_download_stream_restart"
    out_dir = campaign.reports / "scenarios" / scenario_id
    out_dir.mkdir(parents=True, exist_ok=True)
    bt_out = out_dir / "bt_formal_local_probe.json"
    stress_out = out_dir / "downloaded_video_hls.json"
    restart_out = out_dir / "downloaded_video_restart_continuity.json"
    magnet = out_dir / "magnet-download.ts"
    magnet.write_bytes(b"retained-magnet-download")
    bt_out.write_text(json.dumps({
        "raw": {"magnet": {"download_path": str(magnet.resolve())}},
        "artifacts": [{
            "artifact_id": "magnet_download",
            "path": str(magnet.resolve()),
            "type": "video/mpegts",
            "exists": True,
            "validated": True,
        }],
    }), encoding="utf-8")
    stress_out.write_text("{}\n", encoding="utf-8")
    restart_out.write_text("{}\n", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def fake_run_step(
        received_scenario_id: str,
        step_id: str,
        command: list[str],
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append({
            "scenario_id": received_scenario_id,
            "step_id": step_id,
            "command": command,
            **kwargs,
        })
        return {"step_id": step_id, "artifact": str(kwargs["artifact"]), "ok": True}

    def fake_callable_step(
        received_scenario_id: str,
        step_id: str,
        artifact: Path,
        _callback: Callable[[], dict[str, object]],
        **_kwargs: object,
    ) -> dict[str, object]:
        assert received_scenario_id == scenario_id
        return {"step_id": step_id, "artifact": str(artifact), "ok": True}

    evidence_ids = set(CAMPAIGN_SCENARIO_CONTRACTS[scenario_id].required_evidence)
    monkeypatch.setattr(campaign, "run_step", fake_run_step)
    monkeypatch.setattr(campaign, "run_native_callable_step", fake_callable_step)
    monkeypatch.setattr(
        campaign_module,
        "bt_download_assertions",
        lambda _probe, _stress, _restart: {
            "scenario_assertions": {key: True for key in evidence_ids},
            "terminal_assertions": {"terminal": True},
            "cleanup_assertions": {"cleanup": True},
            "details": {"source_count": 3},
        },
    )

    result = campaign.native_bt_download_stream_restart()

    assert len(calls) == 2
    bt_call, hls_call = calls
    bt_command = bt_call["command"]
    hls_command = hls_call["command"]
    assert isinstance(bt_command, list) and bt_command[1].endswith("bt_formal_local_probe.py")
    assert bt_call["process_role"] == "bt"
    assert isinstance(hls_command, list) and hls_command[1].endswith("video_hls_quality_stress.py")
    assert hls_command[hls_command.index("--video") + 1] == str(magnet.resolve())
    assert hls_call["process_role"] == "ffmpeg"
    for secret in (campaign.credentials.root, "BtMemberPassword!"):
        assert secret not in bt_command
        assert secret not in hls_command
    assert hls_call["env"] == {
        "HACKME_HLS_STRESS_ACCOUNTS_JSON": json.dumps([
            {"username": "bt-member", "password": "BtMemberPassword!"},
        ]),
        "HACKME_HLS_SHARE_PASSWORD": hls_call["env"]["HACKME_HLS_SHARE_PASSWORD"],
        "HACKME_PROBE_ROOT_PASSWORD": campaign.credentials.root,
    }
    assert result["ok"] is True
    assert Path(result["formal_evidence_manifest"]).is_file()
    assert magnet.resolve() in {Path(row["path"]) for row in result["artifacts"]}
    magnet_declaration = next(
        row for row in result["artifacts"] if Path(row["path"]) == magnet.resolve()
    )
    assert magnet_declaration["artifact_type"] == "auto"
    binding = campaign_module.FORMAL_SCENARIO_BINDINGS[scenario_id]
    assert binding.runner_id in campaign.native_scenario_runner_registry()


def test_comfyui_formal_runner_uses_exact_probe_secret_env_and_artifact_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    scenario_id = "comfyui_real_workflows"
    probe_dir = campaign.reports / "scenarios" / scenario_id / "probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe_out = probe_dir / "formal_comfyui_workflows_probe.json"
    probe_out.write_text('{"ok": true}\n', encoding="utf-8")
    output = probe_dir / "outputs" / "generated.bin"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"formal-comfyui-output")
    artifact_index_out = probe_dir / "artifact_index.json"
    artifact_index_out.write_text(
        json.dumps({
            "artifacts": [
                {"path": str(probe_out.resolve())},
                {"path": str(output.resolve())},
            ],
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HACKME_CAMPAIGN_COMFYUI_API_URL", "http://127.0.0.1:8188")
    captured: dict[str, object] = {}

    def fake_run_step(
        received_scenario_id: str,
        step_id: str,
        command: list[str],
        **kwargs: object,
    ) -> dict[str, object]:
        captured.update({
            "scenario_id": received_scenario_id,
            "step_id": step_id,
            "command": command,
            **kwargs,
        })
        return {
            "step_id": step_id,
            "artifact": str(kwargs["artifact"]),
            "ok": True,
        }

    evidence_ids = set(
        CAMPAIGN_SCENARIO_CONTRACTS[scenario_id].required_evidence
    )
    monkeypatch.setattr(campaign, "run_step", fake_run_step)
    monkeypatch.setattr(
        campaign_module,
        "comfyui_workflow_assertions",
        lambda _probe, _index: {
            "scenario_assertions": {key: True for key in evidence_ids},
            "terminal_assertions": {"terminal": True},
            "cleanup_assertions": {"cleanup": True},
            "details": {"source_count": 3},
        },
    )

    result = campaign.native_comfyui_real_workflows()

    command = captured["command"]
    assert isinstance(command, list)
    assert command[1].endswith("formal_comfyui_workflows_probe.py")
    assert "--feature-timeout" in command
    assert "--official-timeout" in command
    assert campaign.credentials.root not in command
    assert captured["process_role"] == "comfyui"
    assert captured["timeout"] == 16 * 60 * 60
    assert captured["env"] == {
        "HACKME_CAMPAIGN_COMFYUI_API_URL": "http://127.0.0.1:8188",
        "HACKME_PROBE_ROOT_PASSWORD": campaign.credentials.root,
    }
    assert result["ok"] is True
    assert Path(result["formal_evidence_manifest"]).is_file()
    artifact_paths = {Path(row["path"]) for row in result["artifacts"]}
    assert probe_out.resolve() in artifact_paths
    assert artifact_index_out.resolve() in artifact_paths
    assert output.resolve() in artifact_paths
    binding = campaign_module.FORMAL_SCENARIO_BINDINGS[scenario_id]
    assert binding.runner_id in campaign.native_scenario_runner_registry()


def test_ai_agent_formal_runner_keeps_credentials_out_of_argv_and_targets_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    campaign.accounts = [("qa-one", "MemberOne!"), ("qa-two", "MemberTwo!")]
    scenario_id = "ai_agent_positive_operations"
    out_dir = campaign.reports / "scenarios" / scenario_id
    out_dir.mkdir(parents=True, exist_ok=True)
    fixture = out_dir / "artifacts" / "fixture.mp4"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_bytes(b"video")
    probe_out = out_dir / "formal_ai_agent_positive_operations.json"
    restart_out = out_dir / "supervised_restart.json"
    probe_out.write_text(json.dumps({
        "ok": True,
        "video": {"fixture": {"path": str(fixture.resolve())}},
    }), encoding="utf-8")
    restart_out.write_text('{"ok": true}\n', encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run_step(
        received_scenario_id: str,
        step_id: str,
        command: list[str],
        **kwargs: object,
    ) -> dict[str, object]:
        captured.update({
            "scenario_id": received_scenario_id,
            "step_id": step_id,
            "command": command,
            **kwargs,
        })
        return {"step_id": step_id, "artifact": str(kwargs["artifact"]), "ok": True}

    def fake_run_group(received_scenario_id: str, steps: list[object]) -> dict[str, object]:
        first = steps[0]
        assert callable(first)
        first()
        return {
            "schema_version": campaign_module.NATIVE_RUNNER_RESULT_SCHEMA_VERSION,
            "scenario_id": received_scenario_id,
            "ok": True,
            "execution_succeeded": True,
            "terminal_state": "success",
            "artifacts": [
                {"artifact_id": "native.source.ai.probe", "path": str(probe_out), "artifact_type": "json"},
                {"artifact_id": "native.source.ai.restart", "path": str(restart_out), "artifact_type": "json"},
            ],
        }

    evidence_ids = set(CAMPAIGN_SCENARIO_CONTRACTS[scenario_id].required_evidence)
    monkeypatch.setattr(campaign, "run_step", fake_run_step)
    monkeypatch.setattr(campaign, "run_group", fake_run_group)
    monkeypatch.setattr(
        campaign_module,
        "ai_agent_positive_assertions",
        lambda _probe, _restart: {
            "scenario_assertions": {key: True for key in evidence_ids},
            "terminal_assertions": {"terminal": True},
            "cleanup_assertions": {"cleanup": True},
            "details": {"source_count": 3},
        },
    )

    result = campaign.native_ai_agent_positive_operations()

    command = captured["command"]
    assert isinstance(command, list)
    assert command[1].endswith("formal_ai_agent_positive_operations_probe.py")
    for secret in (
        campaign.credentials.root,
        campaign.credentials.manager,
        "MemberOne!",
        "MemberTwo!",
    ):
        assert secret not in command
    assert captured["env"] == {
        "HACKME_PROBE_ROOT_PASSWORD": campaign.credentials.root,
        "HACKME_PROBE_MANAGER_PASSWORD": campaign.credentials.manager,
        "HACKME_PROBE_USER_ONE_PASSWORD": "MemberOne!",
        "HACKME_PROBE_USER_TWO_PASSWORD": "MemberTwo!",
    }
    assert "--base-url" in command
    assert command[command.index("--base-url") + 1] == campaign.recovery.base_url
    assert captured["process_role"] == "ffmpeg"
    assert result["ok"] is True
    binding = campaign_module.FORMAL_SCENARIO_BINDINGS[scenario_id]
    assert binding.runner_id in campaign.native_scenario_runner_registry()


def test_incomplete_native_scenario_cannot_execute_as_formal_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    scenario_id = "cloud_drive_share_stream"
    runner_id = campaign_module.FORMAL_SCENARIO_BINDINGS[scenario_id].runner_id
    complete_registry = campaign.native_scenario_runner_registry()
    monkeypatch.setattr(
        campaign,
        "native_scenario_runner_registry",
        lambda: {
            registered_id: registration
            for registered_id, registration in complete_registry.items()
            if registered_id != runner_id
        },
    )

    result = campaign.run_formal_native_scenario(scenario_id)

    assert result["ok"] is False
    assert result["classification"] == "FAIL_HARNESS"
    assert result["error"] == "formal_native_binding_incomplete"
    assert result["registration_coverage"]["runner_registered"] is False
    assert result["registration_coverage"]["registrations_complete"] is False
    assert result["runtime_execution_pipeline_verified"] is True
    # This failure is injected by removing an otherwise audited runner; the
    # registration coverage, rather than the static audit-blocker list, is the
    # machine-readable reason it must fail closed.
    assert result["binding_blockers"] == []


def test_formal_native_scenario_caller_supplies_exact_unique_eight_field_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    captured: list[dict[str, object]] = []

    def execute_spy(**kwargs: object) -> dict[str, object]:
        captured.append(dict(kwargs["authority"]))
        binding = kwargs["binding"]
        return {
            "ok": False,
            "classification": "FAIL_HARNESS",
            "scenario_id": binding.scenario_id,
            "diagnostics": ["spy_stopped_before_handler"],
        }

    monkeypatch.setattr(
        campaign_module,
        "execute_registered_native_scenario",
        execute_spy,
    )
    scenario_ids = tuple(campaign_module.FORMAL_SCENARIO_BINDINGS)[:2]
    for scenario_id in scenario_ids:
        campaign.run_formal_native_scenario(scenario_id)

    assert len(captured) == 2
    expected_fields = {
        "qualification_campaign_uuid",
        "campaign_uuid",
        "campaign_attempt_uuid",
        "scenario_attempt_uuid",
        "native_invocation_id",
        "commit",
        "source_digest",
        "protected_source_digest",
    }
    assert all(set(authority) == expected_fields for authority in captured)
    assert len({authority["scenario_attempt_uuid"] for authority in captured}) == 2
    assert len({authority["native_invocation_id"] for authority in captured}) == 2


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
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/tmp/host-controlled-pycache")
    campaign = Campaign(campaign_args(tmp_path))

    env = campaign.primary._env()
    probe_env = campaign.base_env()

    assert env["HACKME_DEV_USE_CAPACITY_DEFAULTS"] == "0"
    assert "HACKME_DEV_CAPACITY_DEFAULTS_FILE" not in env
    assert "HACKME_DEV_CAPACITY_REPORT_FILE" not in env
    for child_env in (env, probe_env):
        assert child_env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert "PYTHONPYCACHEPREFIX" not in child_env
    command = campaign.primary.launcher_command()
    assert command[command.index("--gunicorn-workers") + 1] == "2"
    assert command[command.index("--gunicorn-threads") + 1] == "2"


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
    monkeypatch.setattr(
        campaign,
        "validate_online_security_audit_evidence",
        lambda *_args, **_kwargs: {
            "schema_version": "hackme.audit-evidence-triad-online-wiring/v1",
            "ok": True,
            "classification": "PASS",
            "receipt": {},
            "contract": {},
            "errors": [],
        },
    )

    result = campaign.production_security_sentinel_check(phase="final")

    assert result["ok"] is True
    assert progress == ["security_final:request_completed:GET:200"]


def test_online_security_triad_binding_mismatch_is_never_classified_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    output_dir = tmp_path / "online-triad"
    output_dir.mkdir()
    receipt_path = output_dir / "receipt.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    metadata = campaign._stream_file_metadata(receipt_path)
    contract = {
        "schema_version": "hackme.audit-evidence-triad-validation/v1",
        "ok": True,
        "classification": "PASS",
        "errors": [],
        "validated_invariants": [],
        "artifact_files_verified": True,
    }
    monkeypatch.setattr(
        campaign_module,
        "validate_audit_evidence_receipt",
        lambda *_args, **_kwargs: dict(contract),
    )
    reference = {
        "schema_version": "hackme.audit-evidence-triad-reference/v1",
        "receipt_schema_version": campaign_module.AUDIT_EVIDENCE_SCHEMA_VERSION,
        "mode": "online",
        "target": "security_sentinel",
        "receipt_path": metadata["path"],
        "receipt_sha256": "0" * 64,
        "receipt_size_bytes": metadata["size_bytes"],
        "receipt": {},
        "validation": contract,
    }
    result = campaign.validate_online_security_audit_evidence(
        {
            "audit_evidence": reference,
            "checks": [{
                "name": "audit_evidence_triad_online",
                "ok": True,
                "detail": {
                    "receipt_schema_version": campaign_module.AUDIT_EVIDENCE_SCHEMA_VERSION,
                    "mode": "online",
                    "target": "security_sentinel",
                    "receipt_sha256": metadata["sha256"],
                    "receipt_size_bytes": metadata["size_bytes"],
                    "artifact_files_verified": True,
                    "validation_classification": "PASS",
                    "validation_errors": [],
                },
            }],
        },
        output_dir=output_dir,
    )

    assert result["ok"] is False
    assert result["classification"] == "FAIL_HARNESS"
    assert "audit_evidence_reference_hash_mismatch" in result["errors"]


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
        # Exercise the production activation window.  A one-second edge is
        # shorter than the supported five-second lead and makes protocol
        # correctness depend on scheduler/I/O latency on a loaded host.
        lead_seconds=campaign_module.CORE_ACTIVATION_LEAD_SECONDS,
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
    assert source.index("final_audit_evidence = self.capture_final_audit_evidence") < source.index(
        "server_logs = self.scan_server_logs"
    )


def _valid_final_log_seal_stops(campaign: Campaign) -> dict[str, dict[str, object]]:
    controllers = (
        campaign.primary,
        campaign.recovery,
        campaign.security_sentinel,
    )
    for controller in controllers:
        controller.final_evidence_restart_disabled = True
        controller.planned_outage.set()
        controller.registered_identity = None
    return {
        controller.name: {
            "action": "stop",
            "name": controller.name,
            "reason": "final_evidence_log_seal",
            "pid": 0,
            "master_process_remaining": False,
            "process_group_remaining": False,
            "restart_disabled": True,
            "launch_generation": controller.launch_count,
            "ok": True,
        }
        for controller in controllers
    }


def _write_controller_audit_fixture(
    controller: ServerController,
    *,
    suffix: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = controller.runtime_root
    for directory in (
        runtime,
        runtime / "database",
        runtime / "logs",
        runtime / "anchors",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    seed = f"{suffix:02x}" * 24
    key = bytes([suffix]) * 32
    (runtime / ".chain_seed").write_text(seed, encoding="utf-8")
    (runtime / ".integrity_key").write_bytes(key)
    database = runtime / "database" / "audit.db"
    audit_service.configure_audit_service(
        get_db=lambda database=database: get_audit_db(str(database)),
        chain_seed=seed,
        integrity_key=key,
        audit_log_path=str(runtime / "logs" / "audit.log"),
        audit_anchor_path=str(runtime / "anchors" / "audit_head.jsonl"),
        audit_anchor_latest_path=str(runtime / "anchors" / "audit_head_latest.json"),
        audit_anchor_interval_seconds=60,
    )
    monkeypatch.setattr(audit_service, "_last_audit_anchor_at", 0.0)
    audit_service.audit(
        f"final_fixture_{controller.name}",
        "127.0.0.1",
        user="campaign",
        success=True,
        ua="pytest",
        detail="sealed-final",
    )


def test_h24_sealed_triad_requires_writer_seal_and_indexes_all_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    for suffix, controller in enumerate(
        (campaign.primary, campaign.recovery, campaign.security_sentinel),
        start=1,
    ):
        _write_controller_audit_fixture(
            controller,
            suffix=suffix,
            monkeypatch=monkeypatch,
        )
    monkeypatch.setattr(campaign, "_runtime_writer_pids", lambda _root: [])
    stops = _valid_final_log_seal_stops(campaign)

    result = campaign.capture_final_audit_evidence(stops)

    assert result["ok"] is True
    assert result["classification"] == "PASS"
    assert result["capture_attempted"] is True
    assert set(result["targets"]) == {"primary", "recovery", "security_sentinel"}
    assert all(row["ok"] is True for row in result["targets"].values())
    assert all(
        row["heads"]["anchor_latest"]["reason"] == "formal_evidence_seal"
        for row in result["targets"].values()
    )
    index = Path(result["artifact_index"]["path"])
    manifest = Path(result["hash_manifest"]["path"])
    assert index.is_file() and manifest.is_file()
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    names = {row["path"] for row in manifest_payload["files"]}
    assert "artifact_index.json" in names
    assert "audit_evidence_triad.schema.json" in names
    assert {
        "primary/receipt.json",
        "recovery/receipt.json",
        "security_sentinel/receipt.json",
    } <= names


def test_h24_never_calls_sealed_capture_while_runtime_writer_is_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(campaign, "_runtime_writer_pids", lambda _root: [4242])
    monkeypatch.setattr(
        campaign_module,
        "capture_audit_evidence",
        lambda **_kwargs: calls.append("capture"),
    )

    result = campaign.capture_final_audit_evidence(
        _valid_final_log_seal_stops(campaign)
    )

    assert result["ok"] is False
    assert result["classification"] == "FAIL_HARNESS"
    assert result["capture_attempted"] is False
    assert calls == []
    assert all(
        "sealed_capture_forbidden_without_writer_seal" in row["errors"]
        for row in result["targets"].values()
    )


def test_h24_rechecks_for_writers_immediately_before_sealed_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    writer_checks = 0
    captured_targets: list[str] = []

    def runtime_writers(_root: Path) -> list[int]:
        nonlocal writer_checks
        writer_checks += 1
        # The first three calls establish the aggregate stop seal.  A writer
        # appearing immediately before the primary capture must still block it.
        return [4242] if writer_checks == 4 else []

    def capture(**kwargs: object) -> None:
        captured_targets.append(str(kwargs["target"]))
        raise RuntimeError("test capture stops after call observation")

    monkeypatch.setattr(campaign, "_runtime_writer_pids", runtime_writers)
    monkeypatch.setattr(campaign_module, "capture_audit_evidence", capture)

    result = campaign.capture_final_audit_evidence(
        _valid_final_log_seal_stops(campaign)
    )

    assert result["capture_attempted"] is True
    assert result["ok"] is False
    assert result["classification"] == "FAIL_HARNESS"
    assert "primary" not in captured_targets
    assert result["targets"]["primary"]["errors"] == [
        "runtime_writer_detected_immediately_before_sealed_capture"
    ]


def test_h24_manifest_rehashes_index_after_fail_closed_manifest_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    for suffix, controller in enumerate(
        (campaign.primary, campaign.recovery, campaign.security_sentinel),
        start=7,
    ):
        _write_controller_audit_fixture(
            controller,
            suffix=suffix,
            monkeypatch=monkeypatch,
        )
    original = campaign._stream_file_metadata
    schema_calls = 0

    def fail_schema_during_manifest(path: Path) -> dict[str, object]:
        nonlocal schema_calls
        if Path(path).name == "audit_evidence_triad.schema.json":
            schema_calls += 1
            if schema_calls == 2:
                raise RuntimeError("injected manifest read failure")
        return original(path)

    monkeypatch.setattr(campaign, "_stream_file_metadata", fail_schema_during_manifest)
    monkeypatch.setattr(campaign, "_runtime_writer_pids", lambda _root: [])

    result = campaign.capture_final_audit_evidence(
        _valid_final_log_seal_stops(campaign)
    )

    assert result["ok"] is False
    assert result["classification"] == "FAIL_HARNESS"
    index_path = Path(result["artifact_index"]["path"])
    manifest = json.loads(Path(result["hash_manifest"]["path"]).read_text(encoding="utf-8"))
    index_row = next(row for row in manifest["files"] if row["path"] == "artifact_index.json")
    index_bytes = index_path.read_bytes()
    assert index_row["size_bytes"] == len(index_bytes)
    assert index_row["sha256"] == hashlib.sha256(index_bytes).hexdigest()
    assert manifest["ok"] is False


def test_h24_cross_source_integrity_failure_is_classified_as_product(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    for suffix, controller in enumerate(
        (campaign.primary, campaign.recovery, campaign.security_sentinel),
        start=4,
    ):
        _write_controller_audit_fixture(
            controller,
            suffix=suffix,
            monkeypatch=monkeypatch,
        )
    primary_log = campaign.primary.runtime_root / "logs" / "audit.log"
    entry = json.loads(primary_log.read_text(encoding="utf-8"))
    entry["detail"] = "tampered-after-write"
    primary_log.write_text(
        campaign_module.json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(campaign, "_runtime_writer_pids", lambda _root: [])

    result = campaign.capture_final_audit_evidence(
        _valid_final_log_seal_stops(campaign)
    )

    assert result["ok"] is False
    assert result["classification"] == "FAIL_PRODUCT"
    assert result["targets"]["primary"]["classification"] == "FAIL_PRODUCT"
    assert result["targets"]["recovery"]["ok"] is True
    assert result["targets"]["security_sentinel"]["ok"] is True


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


def test_final_evidence_stop_permanently_disables_controller_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign(campaign_args(tmp_path))
    controller = campaign.primary
    monkeypatch.setattr(controller, "pid", lambda: 0)

    stopped = controller.stop(reason="final_evidence_log_seal")
    started = controller.start()
    restarted = controller.restart(reason="forbidden_after_seal")

    assert stopped["ok"] is True
    assert stopped["restart_disabled"] is True
    assert controller.final_evidence_restart_disabled is True
    assert started["ok"] is False
    assert started["error"] == "final_evidence_restart_barrier_active"
    assert restarted["ok"] is False
    assert restarted["restart_disabled"] is True
    assert controller.planned_outage.is_set() is True


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


def test_rehearsal_execution_contract_is_derived_from_strict_runtime_state() -> None:
    scenario_results = {
        scenario_id: {"ok": True, "details": {"fallback": False, "skips": []}}
        for scenario_id in set(campaign_module.REHEARSAL_FEATURE_SCENARIOS.values())
    }

    result = campaign_module.derive_rehearsal_execution_contract(
        scenario_results,
        {"clock": {"continuous_active_seconds": 3600.0, "invalid_seconds": 0.0}},
    )

    assert result == {
        "invalid_seconds": 0.0,
        "mandatory_features_executed": sorted(
            campaign_module.REHEARSAL_FEATURE_SCENARIOS
        ),
        "skips": [],
        "fallbacks": [],
        "expected_gaps": [],
        "errors": [],
    }


def test_rehearsal_execution_contract_never_claims_failed_or_truthy_scenario() -> None:
    scenario_id = campaign_module.REHEARSAL_FEATURE_SCENARIOS["comfyui_real_workflow"]

    failed = campaign_module.derive_rehearsal_execution_contract(
        {scenario_id: {"ok": False}},
        {"clock": {"invalid_seconds": 0}},
    )
    truthy_not_boolean = campaign_module.derive_rehearsal_execution_contract(
        {scenario_id: {"ok": 1}},
        {"clock": {"invalid_seconds": 0}},
    )

    assert "comfyui_real_workflow" not in failed["mandatory_features_executed"]
    assert "comfyui_real_workflow" not in truthy_not_boolean["mandatory_features_executed"]


def test_rehearsal_execution_contract_detects_nested_gap_markers() -> None:
    result = campaign_module.derive_rehearsal_execution_contract(
        {
            "scenario-one": {
                "ok": True,
                "nested": [
                    {"skip": "dependency missing"},
                    {"fallback_error": "provider unavailable"},
                    {"expected-gap": ["mobile UI"]},
                    {"skipped": False, "used_fallback": 0, "expected_gaps": []},
                ],
            }
        },
        {"clock": {"invalid_seconds": 1.25}},
    )

    assert result["invalid_seconds"] == 1.25
    assert [row["marker"] for row in result["skips"]] == ["skip"]
    assert [row["marker"] for row in result["fallbacks"]] == ["fallback_error"]
    assert [row["marker"] for row in result["expected_gaps"]] == ["expected_gap"]


@pytest.mark.parametrize(
    "state_snapshot",
    [
        {},
        {"clock": {}},
        {"clock": {"invalid_seconds": True}},
        {"clock": {"invalid_seconds": -1}},
        {"clock": {"invalid_seconds": float("nan")}},
    ],
)
def test_rehearsal_execution_contract_fails_closed_without_valid_clock(
    state_snapshot: dict[str, object],
) -> None:
    result = campaign_module.derive_rehearsal_execution_contract({}, state_snapshot)

    assert result["invalid_seconds"] is None
    assert result["errors"]
