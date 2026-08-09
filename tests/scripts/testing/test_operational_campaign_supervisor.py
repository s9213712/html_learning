from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from scripts.testing.operational_campaign_supervisor import (
    FORMAL_AUTHORIZATION_SCHEMA_VERSION,
    GATE_BUNDLE_SCHEMA_VERSION,
    MIN_FORMAL_SECONDS,
    REQUIRED_FORMAL_GATES,
    OperationalCampaignSupervisor,
    SupervisorConfig,
    SupervisorError,
    validate_formal_authorization,
    validate_gate_bundle,
)
from scripts.testing import operational_campaign_supervisor as supervisor_module
from scripts.testing import operational_campaign_runner_admission as admission_module
from scripts.testing.campaign_control_channel import sign_authenticated_payload
from scripts.testing.campaign_comfyui_backend import ComfyUIBackendConfig
from scripts.testing.campaign_watchdog import capture_process_identity
from services.server.database import get_audit_db
from services.system import audit as audit_service


COMMIT = "a" * 40
SOURCE_DIGEST = "b" * 64


def comfyui_config(tmp_path: Path) -> ComfyUIBackendConfig:
    root = tmp_path / "managed-comfyui-fixture"
    root.mkdir(parents=True, exist_ok=True)
    (root / "models").mkdir(exist_ok=True)
    (root / "main.py").write_text("# configuration-only fixture\n", encoding="utf-8")
    return ComfyUIBackendConfig(
        python_executable=Path(sys.executable).resolve(strict=True),
        main_path=(root / "main.py").resolve(strict=True),
        working_root=root.resolve(strict=True),
        models_root=(root / "models").resolve(strict=True),
        api_url="http://127.0.0.1:48188",
        port=48188,
    )


def record_safe_source_capture_checkpoint(
    supervisor: OperationalCampaignSupervisor,
) -> None:
    supervisor.source_capture_safety_checkpoint_count = 1
    supervisor.source_capture_safety_checkpoints = [{
        "stage": "manifest_write:source_freeze.json",
        "at": "2026-07-18T00:00:00Z",
        "ok": True,
        "io": {"avg10": 1.0, "avg60": 1.0},
        "hard_limit_exceeded": False,
        "waited_seconds": 0.0,
        "sample_count": 1,
        "tripped": [],
    }]


def write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def staged_import_receipt(
    supervisor: OperationalCampaignSupervisor,
    *,
    profile: str,
    identity: object,
) -> dict[str, object]:
    order = (
        "site",
        *admission_module.PROFILE_MODULES[profile],
    )
    order_digest = hashlib.sha256(
        json.dumps(
            list(order),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    binding = {
        "campaign_uuid": supervisor.campaign_uuid,
        "profile": profile,
        "pid": identity.pid,
        "process_start_ticks": identity.start_ticks,
        "target_module": admission_module.TARGET_MODULES[profile],
        "module_order_sha256": order_digest,
    }
    binding_digest = hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    admission = {
        "sample_count": 1,
        "waited_seconds": 0.0,
        "maximum": {"avg10": 2.0, "avg60": 2.5},
        "admitted": {"avg10": 2.0, "avg60": 2.5},
    }
    return {
        "schema_version": admission_module.SCHEMA_VERSION,
        "verified": True,
        "status": "PASS",
        "campaign_uuid": supervisor.campaign_uuid,
        "profile": profile,
        "pid": identity.pid,
        "process_start_ticks": identity.start_ticks,
        "python_no_site": True,
        "bootstrap_collector": "direct_proc_pressure_io",
        "site_initialization_mode": "site_paths_only_no_pth_or_customization",
        "time_module_bootstrap": "preloaded_by_interpreter",
        "target_module": admission_module.TARGET_MODULES[profile],
        "module_order": list(order),
        "module_order_sha256": order_digest,
        "binding_sha256": binding_digest,
        "completed_module_count": len(order),
        "soft_io_pressure_maximum": admission_module.SOFT_IO_PRESSURE_MAXIMUM,
        "hard_io_pressure_maximum": admission_module.HARD_IO_PRESSURE_MAXIMUM,
        "import_pacing_seconds": admission_module.IMPORT_PACING_SECONDS,
        "stage_timeout_seconds": admission_module.DEFAULT_STAGE_TIMEOUT_SECONDS,
        "poll_seconds": admission_module.DEFAULT_POLL_SECONDS,
        "collector_mode": (
            "campaign_observability"
            if "scripts.testing.campaign_observability" in order
            else "direct_proc_pressure_io"
        ),
        "nested_import_guard": {
            "mode": admission_module.NESTED_IMPORT_GUARD_MODE,
            "call_count": 12,
            "calls_loading_modules": 6,
            "maximum_io_pressure": {"avg10": 2.0, "avg60": 2.5},
            "pacing_seconds": admission_module.IMPORT_PACING_SECONDS,
            "restored_before_receipt": True,
        },
        "stages": [
            {
                "sequence": index,
                "module": module,
                "pre_admission": dict(admission),
                "post_admission": dict(admission),
            }
            for index, module in enumerate(order)
        ],
        "runner_main_invoked": False,
        "pre_receipt_io_barrier": admission_module.PRE_RECEIPT_BARRIER_MODE,
        "post_receipt_io_barrier": admission_module.POST_RECEIPT_BARRIER_MODE,
        "failure_reason": "",
        "failed_module": "",
    }


def authorization(tmp_path: Path, **changes) -> Path:
    payload = {
        "schema_version": FORMAL_AUTHORIZATION_SCHEMA_VERSION,
        "formal_24h_authorized": True,
        "commit": COMMIT,
        "duration_seconds": MIN_FORMAL_SECONDS,
        "authorized_by": "user",
        "authorized_at": "2026-07-12T00:00:00Z",
    }
    payload.update(changes)
    return write(tmp_path / "authorization.json", payload)


def gate_bundle(tmp_path: Path, **gate_changes) -> Path:
    gates = {
        name: {"status": "PASS", "machine_verified": True, "artifact": f"{name}.json"}
        for name in REQUIRED_FORMAL_GATES
    }
    gates.update(gate_changes)
    return write(tmp_path / "gates.json", {
        "schema_version": GATE_BUNDLE_SCHEMA_VERSION,
        "commit": COMMIT,
        "gates": gates,
        "ok": True,
    })


def test_formal_config_requires_exact_day_authorization_and_gate_bundle(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="authorization_file"):
        SupervisorConfig(tmp_path / "formal", "formal", MIN_FORMAL_SECONDS)
    with pytest.raises(ValueError, match="exactly"):
        SupervisorConfig(
            tmp_path / "formal",
            "formal",
            MIN_FORMAL_SECONDS + 1,
            authorization_file=tmp_path / "auth",
            gate_bundle_file=tmp_path / "gates",
        )
    with pytest.raises(ValueError, match="cannot keep its cgroup scope"):
        SupervisorConfig(
            tmp_path / "formal-preserve",
            "formal",
            MIN_FORMAL_SECONDS,
            authorization_file=tmp_path / "auth",
            gate_bundle_file=tmp_path / "gates",
            keep_scope_on_failure=True,
            comfyui_backend=comfyui_config(tmp_path),
        )
    with pytest.raises(ValueError, match="managed ComfyUI"):
        SupervisorConfig(
            tmp_path / "formal-no-comfyui",
            "formal",
            MIN_FORMAL_SECONDS,
            authorization_file=tmp_path / "auth",
            gate_bundle_file=tmp_path / "gates",
        )


def test_authorization_is_commit_duration_and_identity_bound(tmp_path: Path) -> None:
    result = validate_formal_authorization(
        authorization(tmp_path),
        commit=COMMIT,
        campaign_uuid="new-attempt",
    )
    assert result["formal_24h_authorized"] is True

    with pytest.raises(SupervisorError, match="commit"):
        validate_formal_authorization(
            authorization(tmp_path, commit="b" * 40),
            commit=COMMIT,
            campaign_uuid="new-attempt",
        )


def test_handwritten_machine_gate_rows_cannot_start_formal_campaign(tmp_path: Path) -> None:
    with pytest.raises(SupervisorError, match="not formal-ready"):
        validate_gate_bundle(
            gate_bundle(tmp_path),
            commit=COMMIT,
            source_authority={},
        )


def test_smoke_and_rehearsal_durations_are_fixed(tmp_path: Path) -> None:
    SupervisorConfig(tmp_path / "smoke", "smoke", 180)
    with pytest.raises(ValueError, match="managed ComfyUI"):
        SupervisorConfig(tmp_path / "rehearsal-missing", "rehearsal", 3600)
    SupervisorConfig(
        tmp_path / "rehearsal",
        "rehearsal",
        3600,
        comfyui_backend=comfyui_config(tmp_path),
    )
    with pytest.raises(ValueError, match="smoke duration"):
        SupervisorConfig(tmp_path / "smoke2", "smoke", 179)
    with pytest.raises(ValueError, match="managed ComfyUI"):
        SupervisorConfig(tmp_path / "soak-missing", "soak", MIN_FORMAL_SECONDS)
    SupervisorConfig(
        tmp_path / "soak",
        "soak",
        MIN_FORMAL_SECONDS,
        comfyui_backend=comfyui_config(tmp_path),
    )
    with pytest.raises(ValueError, match="soak duration"):
        SupervisorConfig(
            tmp_path / "soak-short",
            "soak",
            MIN_FORMAL_SECONDS - 1,
            comfyui_backend=comfyui_config(tmp_path),
        )


def test_partial_managed_comfyui_cli_is_rejected_before_campaign_creation(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit, match="managed ComfyUI requires all"):
        supervisor_module.main([
            "--campaign-root",
            str(tmp_path / "smoke-partial"),
            "--level",
            "smoke",
            "--comfyui-api-url",
            "http://127.0.0.1:48188",
        ])
    assert not (tmp_path / "smoke-partial").exists()


def test_short_smoke_can_omit_backend_but_cannot_claim_formal_comfyui_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "smoke-no-comfyui", "smoke", 180)
    )

    supervisor.prepare()

    gate = supervisor.gates["comfyui_backend_lifecycle_verified"]
    assert gate["status"] == "NOT_EVALUATED"
    assert gate["machine_verified"] is False
    assert gate["evidence"]["formal_eligible"] is False
    assert (
        supervisor.freezer.content_evidence_mode
        == supervisor_module.METADATA_CONTENT_EVIDENCE
    )
    assert supervisor.cgroup.allow_idle_io_fallback is True
    assert supervisor.managed_exec_gate_path != supervisor.activation_gate_path
    assert supervisor.managed_exec_gate_path.name == "campaign.exec.json"


@pytest.mark.parametrize(
    "argument",
    (
        "--duration-seconds",
        "--duration-seconds=180",
        "--duration",
        "--allow-short-duration",
        "--campaign-uuid=forged",
        "--control-root=/tmp/forged-control",
        "--cgroup-path",
        "--checkpoint-mirror-path=/tmp/forged.json",
        "--supervisor-contract=/tmp/forged.json",
        "--concurrency=1",
        "--concur=1",
        "--round-ops",
        "--max-ordinary-p95-ms=999999",
        "--max-server-busy-rate=1",
        "--minimum-free-gb=0",
        "--resource-interval=999",
        "--keep-servers",
    ),
)
def test_runner_extra_args_cannot_override_supervisor_contract(
    tmp_path: Path,
    argument: str,
) -> None:
    with pytest.raises(ValueError, match="supervisor-owned"):
        SupervisorConfig(
            tmp_path / "smoke",
            "smoke",
            180,
            runner_extra_args=(argument,),
        )


def test_runner_command_is_supervised_and_never_contains_authorization_material(tmp_path: Path) -> None:
    supervisor = OperationalCampaignSupervisor(SupervisorConfig(tmp_path / "smoke", "smoke", 180))
    supervisor.source_h0 = {"artifact_root": str(tmp_path / "source")}
    supervisor.cgroup.scope_path = "/user.slice/test.scope"
    command = supervisor._runner_command()

    assert command[:3] == [
        sys.executable,
        "-S",
        str(
            supervisor_module.ROOT
            / "scripts"
            / "testing"
            / "operational_campaign_runner_admission.py"
        ),
    ]
    assert command[command.index("--profile") + 1] == "runner"
    assert command[command.index("--evidence-path") + 1] == str(
        supervisor.runner_import_evidence_path
    )
    separator = command.index("--")
    assert command.index("--supervised") > separator
    assert "--supervised" in command
    assert "--activation-gate" in command
    assert "--supervisor-contract" in command
    assert command[command.index("--auth-socket") + 1] == str(supervisor.auth_socket_path)
    assert "--checkpoint-mirror-path" in command
    assert command[command.index("--control-root") + 1] == str(supervisor.control_root)
    assert supervisor.control_root.parent == supervisor.root.parent
    assert supervisor.control_root != supervisor.root
    mirror = Path(command[command.index("--checkpoint-mirror-path") + 1])
    assert mirror == supervisor.checkpoint_mirror_path
    assert mirror.is_relative_to(Path.home() / "logs" / "hackme_web_campaign_24h")
    assert "--allow-short-duration" in command
    assert command[command.index("--concurrency") + 1] == "2"
    assert command[command.index("--round-ops") + 1] == "50"
    assert command[command.index("--max-ordinary-p95-ms") + 1] == "30000.0"
    assert command[command.index("--minimum-free-gb") + 1] == "20.0"
    assert "--authorization-file" not in command
    assert "--gate-bundle-file" not in command


def test_soak_runner_command_is_full_day_but_not_capacity_signoff(
    tmp_path: Path,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(
            tmp_path / "soak",
            "soak",
            MIN_FORMAL_SECONDS,
            comfyui_backend=comfyui_config(tmp_path),
        )
    )
    supervisor.source_h0 = {"artifact_root": str(tmp_path / "source")}
    supervisor.cgroup.scope_path = "/user.slice/soak.scope"

    command = supervisor._runner_command()

    assert command[command.index("--duration-seconds") + 1] == "86400"
    assert command[command.index("--workers") + 1] == "1"
    assert command[command.index("--threads") + 1] == "16"
    assert command[command.index("--concurrency") + 1] == "4"
    assert "--allow-short-duration" in command
    assert "--authorization-file" not in command
    assert supervisor.cgroup.allow_idle_io_fallback is True


def test_staged_import_receipt_is_identity_order_and_threshold_bound(
    tmp_path: Path,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    supervisor.checkpoint_dir.mkdir(parents=True)
    identity = capture_process_identity(os.getpid())
    payload = staged_import_receipt(
        supervisor,
        profile="runner",
        identity=identity,
    )
    supervisor.runner_import_evidence_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    os.chmod(supervisor.runner_import_evidence_path, 0o600)

    evidence = supervisor._verify_staged_import_evidence(
        profile="runner",
        process_identity=identity,
    )

    assert evidence["ok"] is True
    assert evidence["process_start_ticks"] == identity.start_ticks
    assert evidence["stage_count"] == len(
        admission_module.PROFILE_MODULES["runner"]
    ) + 1
    assert evidence["maximum_io_pressure"] == {
        "avg10": 2.0,
        "avg60": 2.5,
    }
    gate = supervisor.gates["runner_import_staged_verified"]
    assert gate["status"] == "PASS"
    assert gate["machine_verified"] is True


@pytest.mark.parametrize(
    "mutation,match",
    (
        ("runner_main_invoked", "runner_main_invoked"),
        ("process_start_ticks", "process_start_ticks"),
        ("admitted_above_soft_limit", "threshold"),
        ("maximum_below_admitted", "threshold"),
        ("waited_beyond_timeout", "threshold"),
        ("shared_deadline_exceeded", "shared_deadline"),
        ("multiple_samples_all_safe", "sample_consistency"),
        ("boolean_encoded_as_integer", "type:verified"),
        ("nested_guard_zero_calls", "nested_import_guard_shape"),
        ("module_order", "module_order"),
    ),
)
def test_staged_import_receipt_tampering_fails_closed(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / mutation, "smoke", 180)
    )
    supervisor.checkpoint_dir.mkdir(parents=True)
    identity = capture_process_identity(os.getpid())
    payload = staged_import_receipt(
        supervisor,
        profile="runner",
        identity=identity,
    )
    if mutation == "runner_main_invoked":
        payload["runner_main_invoked"] = True
    elif mutation == "process_start_ticks":
        payload["process_start_ticks"] = identity.start_ticks + 1
    elif mutation == "admitted_above_soft_limit":
        payload["stages"][0]["post_admission"]["admitted"]["avg10"] = 3.01
    elif mutation == "maximum_below_admitted":
        payload["stages"][0]["post_admission"]["maximum"]["avg10"] = 1.0
    elif mutation == "waited_beyond_timeout":
        payload["stages"][0]["post_admission"]["waited_seconds"] = (
            admission_module.DEFAULT_STAGE_TIMEOUT_SECONDS
            + admission_module.DEFAULT_POLL_SECONDS
            + 0.01
        )
    elif mutation == "shared_deadline_exceeded":
        payload["stages"][0]["pre_admission"]["waited_seconds"] = 70.0
        payload["stages"][0]["post_admission"]["waited_seconds"] = 70.0
    elif mutation == "multiple_samples_all_safe":
        payload["stages"][0]["post_admission"]["sample_count"] = 2
    elif mutation == "boolean_encoded_as_integer":
        payload["verified"] = 1
    elif mutation == "nested_guard_zero_calls":
        payload["nested_import_guard"]["call_count"] = 0
        payload["nested_import_guard"]["calls_loading_modules"] = 0
    elif mutation == "module_order":
        payload["module_order"] = list(reversed(payload["module_order"]))
    supervisor.runner_import_evidence_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    os.chmod(supervisor.runner_import_evidence_path, 0o600)

    with pytest.raises(SupervisorError, match=match):
        supervisor._verify_staged_import_evidence(
            profile="runner",
            process_identity=identity,
        )

    gate = supervisor.gates["runner_import_staged_verified"]
    assert gate["status"] == "FAIL"
    assert gate["machine_verified"] is False


@pytest.mark.parametrize("unsafe_kind", ("mode", "symlink", "hardlink"))
def test_staged_import_receipt_requires_private_single_link_regular_file(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / unsafe_kind, "smoke", 180)
    )
    supervisor.checkpoint_dir.mkdir(parents=True)
    identity = capture_process_identity(os.getpid())
    payload = staged_import_receipt(
        supervisor,
        profile="runner",
        identity=identity,
    )
    path = supervisor.runner_import_evidence_path
    if unsafe_kind == "symlink":
        target = supervisor.checkpoint_dir / "real.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(target, 0o600)
        path.symlink_to(target)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, 0o644 if unsafe_kind == "mode" else 0o600)
        if unsafe_kind == "hardlink":
            os.link(path, supervisor.checkpoint_dir / "second-link.json")

    with pytest.raises(SupervisorError, match="metadata is unsafe"):
        supervisor._verify_staged_import_evidence(
            profile="runner",
            process_identity=identity,
        )


def test_missing_authenticated_control_channel_blocks_activation_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )

    supervisor.prepare()

    gate = supervisor.gates["authenticated_control_channel_verified"]
    assert gate["status"] == "FAIL"
    assert gate["machine_verified"] is False
    assert gate["evidence"]["implemented"] is True
    assert gate["evidence"]["verification_state"] == "not_started"
    with pytest.raises(SupervisorError, match="authenticated supervisor control"):
        supervisor._require_authenticated_control_channel()


def test_host_safety_preflight_blocks_campaign_before_repo_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    calls: list[dict[str, object]] = []

    def failed_wait(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "schema_version": "hackme.host-safety-preflight.v1",
            "ok": False,
            "tripped": ["HOST_IO_PRESSURE_HIGH"],
            "checks": {},
            "errors": {},
        }

    monkeypatch.setattr(
        supervisor_module,
        "wait_for_host_safety_preflight",
        failed_wait,
    )
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    supervisor.prepare()

    with pytest.raises(SupervisorError, match="HOST_IO_PRESSURE_HIGH"):
        supervisor._verify_host_safety_preflight()

    gate = supervisor.gates["host_safety_preflight_verified"]
    assert gate["status"] == "FAIL"
    assert gate["machine_verified"] is False
    assert len(calls) == 1
    block_io_sampler = calls[0].pop("block_io_sampler")
    assert isinstance(
        block_io_sampler,
        supervisor_module.HostStartupBlockIoSampler,
    )
    assert block_io_sampler.data_root == supervisor_module.ROOT
    assert calls == [{
        "timeout_seconds": supervisor_module.HOST_SAFETY_PREFLIGHT_TIMEOUT_SECONDS,
        "required_consecutive_safe": (
            supervisor_module.HOST_SAFETY_STARTUP_SETTLE_CONSECUTIVE_SAMPLES
        ),
        "collector": supervisor_module.collect_host_startup_safety_preflight,
    }]


def test_host_safety_activation_gate_blocks_workload_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    calls: list[dict[str, object]] = []

    def failed_wait(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "schema_version": "hackme.host-safety-preflight.v1",
            "ok": False,
            "tripped": ["HOST_IO_PRESSURE_HIGH"],
            "checks": {},
            "errors": {},
        }

    monkeypatch.setattr(
        supervisor_module,
        "wait_for_host_safety_preflight",
        failed_wait,
    )
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )

    with pytest.raises(SupervisorError, match="activation gate"):
        supervisor._verify_host_safety_activation()

    gate = supervisor.gates["host_safety_activation_verified"]
    assert gate["status"] == "FAIL"
    assert gate["machine_verified"] is False
    assert len(calls) == 1
    block_io_sampler = calls[0].pop("block_io_sampler")
    assert isinstance(
        block_io_sampler,
        supervisor_module.HostStartupBlockIoSampler,
    )
    assert calls == [{
        "timeout_seconds": supervisor_module.HOST_SAFETY_ACTIVATION_TIMEOUT_SECONDS,
        "required_consecutive_safe": (
            supervisor_module.HOST_SAFETY_STARTUP_SETTLE_CONSECUTIVE_SAMPLES
        ),
        "collector": supervisor_module.collect_host_startup_safety_preflight,
    }]


def test_runner_import_settle_requires_block_io_quiet_streak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    expected = {
        "schema_version": "hackme.host-safety-preflight.v1",
        "ok": True,
        "tripped": [],
        "checks": {},
        "errors": {},
    }

    def safe_wait(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(
        supervisor_module,
        "wait_for_host_safety_preflight",
        safe_wait,
    )
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )

    result = supervisor._verify_host_safety_runner_import_settled()

    assert result is expected
    assert len(calls) == 1
    block_io_sampler = calls[0].pop("block_io_sampler")
    assert isinstance(
        block_io_sampler,
        supervisor_module.HostStartupBlockIoSampler,
    )
    assert calls == [{
        "timeout_seconds": supervisor_module.HOST_SAFETY_ACTIVATION_TIMEOUT_SECONDS,
        "required_consecutive_safe": (
            supervisor_module.HOST_SAFETY_STARTUP_SETTLE_CONSECUTIVE_SAMPLES
        ),
        "collector": supervisor_module.collect_host_startup_safety_preflight,
    }]
    gate = supervisor.gates["host_safety_runner_import_settled"]
    assert gate["status"] == "PASS"
    assert gate["machine_verified"] is True


def test_gated_heartbeat_is_rate_limited_during_polling_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    state_heartbeats: list[dict[str, int]] = []
    durable_writes: list[Path] = []

    class Runner:
        @staticmethod
        def poll() -> None:
            return None

    class StateMachine:
        @staticmethod
        def heartbeat(**kwargs: int) -> None:
            state_heartbeats.append(kwargs)

    class Identity:
        start_ticks = 321

    supervisor.runner = Runner()  # type: ignore[assignment]
    supervisor.runner_pid = 123
    supervisor.runner_auth_key = b"r" * 32
    supervisor.state_machine = StateMachine()  # type: ignore[assignment]
    supervisor.last_gated_heartbeat_monotonic_ns = 1_000_000_000
    samples = iter((1_500_000_000, 31_500_000_000))
    monkeypatch.setattr(
        supervisor_module.time,
        "monotonic_ns",
        lambda: next(samples),
    )
    monkeypatch.setattr(
        supervisor_module,
        "atomic_write_json",
        lambda path, _payload: durable_writes.append(Path(path)),
    )

    skipped = supervisor._refresh_gated_heartbeat(Identity())
    refreshed = supervisor._refresh_gated_heartbeat(Identity())

    assert skipped["refreshed"] is False
    assert skipped["reason"] == "rate_limited"
    assert refreshed["refreshed"] is True
    assert len(state_heartbeats) == 1
    assert durable_writes == [supervisor.heartbeat_path]
    assert supervisor.last_gated_heartbeat_monotonic_ns == 31_500_000_000


def test_supervisor_poll_stops_a_long_staged_import_on_hard_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    monkeypatch.setattr(supervisor_module.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        supervisor_module,
        "collect_host_startup_safety_preflight",
        lambda: {
            "ok": False,
            "errors": {},
            "tripped": [
                "HOST_IO_PRESSURE_HIGH",
                "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
            ],
            "checks": {},
        },
    )

    with pytest.raises(SupervisorError, match="supervisor safety stop"):
        supervisor._poll_staged_import_host_safety(profile="runner")

    gate = supervisor.gates["runner_import_staged_verified"]
    assert gate["status"] == "FAIL"
    assert gate["evidence"]["verification_state"] == "supervisor_safety_stop"
    assert supervisor._host_io_hard_limit_was_exceeded() is True


def test_host_safety_runner_launch_gate_uses_fresh_startup_headroom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    expected = {
        "schema_version": "hackme.host-safety-preflight.v1",
        "ok": True,
        "tripped": [],
        "checks": {},
        "errors": {},
    }

    def safe_wait(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(
        supervisor_module,
        "wait_for_host_safety_preflight",
        safe_wait,
    )
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )

    result = supervisor._verify_host_safety_runner_launch()

    assert result is expected
    assert len(calls) == 1
    block_io_sampler = calls[0].pop("block_io_sampler")
    assert isinstance(
        block_io_sampler,
        supervisor_module.HostStartupBlockIoSampler,
    )
    assert block_io_sampler.data_root == supervisor_module.ROOT
    assert calls == [{
        "timeout_seconds": (
            supervisor_module.HOST_SAFETY_RUNNER_LAUNCH_TIMEOUT_SECONDS
        ),
        "required_consecutive_safe": (
            supervisor_module.HOST_SAFETY_STARTUP_SETTLE_CONSECUTIVE_SAMPLES
        ),
        "collector": supervisor_module.collect_host_startup_safety_preflight,
    }]
    gate = supervisor.gates["host_safety_runner_launch_verified"]
    assert gate["status"] == "PASS"
    assert gate["machine_verified"] is True


def test_source_capture_checkpoint_uses_strict_single_sample_headroom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    pacing_sleeps: list[float] = []

    def safe_wait(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "at": "2026-07-18T00:00:00Z",
            "ok": True,
            "tripped": [],
            "checks": {
                "host_io_pressure": {
                    "value": {"avg10": 1.25, "avg60": 2.5},
                },
                "host_io_pressure_hard_limit": {"exceeded": False},
            },
            "admission_wait": {
                "waited_seconds": 0.0,
                "sample_count": 1,
            },
        }

    monkeypatch.setattr(
        supervisor_module,
        "wait_for_host_safety_preflight",
        safe_wait,
    )
    monkeypatch.setattr(
        supervisor_module.time,
        "sleep",
        pacing_sleeps.append,
    )
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )

    supervisor._source_capture_safety_checkpoint("git_authority:status")

    assert calls == [{
        "timeout_seconds": supervisor_module.SOURCE_CAPTURE_SAFETY_TIMEOUT_SECONDS,
        "required_consecutive_safe": 1,
        "collector": supervisor_module.collect_host_startup_safety_preflight,
    }]
    summary = supervisor._source_capture_safety_summary()
    assert summary["all_safe"] is True
    assert summary["checkpoint_count"] == 1
    assert summary["maximum_io_pressure"] == {
        "avg10": 1.25,
        "avg60": 2.5,
    }
    assert pacing_sleeps == [
        supervisor_module.SOURCE_CAPTURE_IO_PACING_SECONDS
    ]


def test_passed_gate_never_retains_a_stale_error(tmp_path: Path) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )

    gate = supervisor._gate(
        "source_runtime_monitor_verified",
        passed=True,
        evidence={"verified": True},
        error="source monitor self-check failed",
    )

    assert gate["status"] == "PASS"
    assert gate["error"] == ""


def test_hard_io_exception_finalizer_skips_h24_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    supervisor.source_h0 = {"verified": True}
    supervisor.gates["host_safety_runner_launch_verified"] = {
        "evidence": {
            "tripped": ["HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED"],
        },
    }
    closed: list[bool] = []
    monkeypatch.setattr(
        supervisor.freezer,
        "verify_final",
        lambda **_kwargs: pytest.fail("hard I/O abort must not run H24 capture"),
    )
    monkeypatch.setattr(
        supervisor.freezer,
        "close",
        lambda: closed.append(True),
    )

    result = supervisor._exception_source_final()

    assert result == {
        "verified": False,
        "verification_state": "NOT_EVALUATED",
        "reason_code": "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
        "h0_verified": True,
        "h24_capture_skipped": True,
    }
    assert closed == [True]


def test_staged_import_hard_exit_skips_h24_capture_without_a_gate_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    supervisor.source_h0 = {"verified": True}
    closed: list[bool] = []

    class Runner:
        @staticmethod
        def poll() -> int:
            return admission_module.HARD_IO_EXIT_CODE

    supervisor.runner = Runner()  # type: ignore[assignment]
    monkeypatch.setattr(
        supervisor.freezer,
        "verify_final",
        lambda **_kwargs: pytest.fail("hard import abort must not run H24"),
    )
    monkeypatch.setattr(
        supervisor.freezer,
        "close",
        lambda: closed.append(True),
    )

    result = supervisor._exception_source_final()

    assert result["reason_code"] == "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED"
    assert result["h24_capture_skipped"] is True
    assert closed == [True]


def test_hard_io_run_skips_scan_heavy_exception_finalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    monkeypatch.setattr(supervisor, "_verify_host_safety_preflight", lambda: {})

    def hard_capture() -> None:
        supervisor.source_h0 = {"verified": True}
        supervisor._gate(
            "source_capture_host_safety_verified",
            passed=False,
            evidence={
                "tripped": ["HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED"],
            },
            error="HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
        )
        raise SupervisorError("HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED")

    monkeypatch.setattr(supervisor, "_capture_source", hard_capture)
    monkeypatch.setattr(
        supervisor,
        "_exception_source_final",
        lambda: {
            "verified": False,
            "reason_code": "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
            "h24_capture_skipped": True,
        },
    )
    monkeypatch.setattr(
        supervisor,
        "_terminate_processes_after_hard_io",
        lambda: {"ok": True, "durable_writes_performed": False},
    )
    monkeypatch.setattr(
        supervisor,
        "_cleanup",
        lambda **_kwargs: {
            "authenticated_control_channel": {"ok": True},
            "source_monitor": {"ok": True},
            "comfyui_backend": {"ok": True},
            "watchdog": {"ok": True},
            "scope": {"ok": True, "not_created": True},
        },
    )
    monkeypatch.setattr(
        supervisor,
        "_wait_for_post_hard_io_quiescence",
        lambda: {"ok": True, "required_consecutive_safe": 4},
    )
    monkeypatch.setattr(
        supervisor,
        "_purge_stopped_server_tls_private_keys",
        lambda _cleanup: {"ok": True, "absent": ["all"]},
    )
    monkeypatch.setattr(
        supervisor,
        "_authoritative_final_scan_and_publish",
        lambda *_args, **_kwargs: pytest.fail(
            "hard I/O must not run the scan-heavy finalizer"
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "_publish_fail_closed_secret_scan_receipt",
        lambda **_kwargs: {"ok": False, "reason": "hard-io"},
    )

    returncode = supervisor.run()

    assert returncode == 2
    result = json.loads(supervisor.final_path.read_text(encoding="utf-8"))
    scan = result["authoritative_secret_scan"]
    assert scan["status"] == "SKIPPED_DUE_TO_HOST_IO_HARD_LIMIT"
    assert scan["root_scan_verified"] is False
    assert scan["minimal_report_written"] is True
    assert result["report_mode"] == "hard_io_minimal_allowlisted"
    assert result["contains_raw_error_text"] is False
    assert "gates" not in result
    gate = result["gate_statuses"][
        "hard_io_failure_finalizer_load_suppressed"
    ]
    assert gate["status"] == "PASS"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_hard_io_immediate_termination_includes_dormant_scope_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    signals: list[tuple[int, int]] = []
    scope_kills: list[bool] = []

    class DormantAnchor:
        pid = 43210

        def __init__(self) -> None:
            self.stopped = False

        def poll(self) -> int | None:
            return -9 if self.stopped else None

        def wait(self, *, timeout: float) -> int:
            assert 0.0 <= timeout <= 2.0
            self.stopped = True
            return -9

    anchor = DormantAnchor()
    supervisor.cgroup.anchor_process = anchor  # type: ignore[assignment]

    def kill_process_group(process_group: int, signum: int) -> None:
        if signum == 0:
            raise ProcessLookupError
        signals.append((process_group, signum))

    monkeypatch.setattr(
        supervisor_module.os,
        "killpg",
        kill_process_group,
    )
    monkeypatch.setattr(
        supervisor.cgroup,
        "emergency_kill_scope_without_durable_evidence",
        lambda: (
            scope_kills.append(True)
            or {
                "ok": True,
                "stopped": True,
                "cgroup_empty": True,
                "durable_writes_performed": False,
            }
        ),
    )

    result = supervisor._terminate_processes_after_hard_io()

    assert signals == [(anchor.pid, supervisor_module.signal.SIGKILL)]
    assert scope_kills == [True]
    assert result["scope_anchor"]["stopped"] is True
    assert result["campaign_scope"]["cgroup_empty"] is True
    assert result["durable_writes_performed"] is False
    assert result["ok"] is True


def test_hard_io_minimal_report_is_structurally_allowlisted(
    tmp_path: Path,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    supervisor.commit = COMMIT
    secret = "do-not-serialize-this-secret"
    supervisor.gates = {
        "host_safety_preflight_verified": {
            "status": "FAIL",
            "machine_verified": False,
            "error": secret,
            "evidence": {"raw": secret},
        },
        secret: {"status": "FAIL", "machine_verified": False},
    }
    projected = supervisor._hard_io_minimal_failure_result({
        "started_at": secret,
        "finished_at": "2026-07-18T00:00:00Z",
        "error_code": secret,
        "error_sha256": secret,
        "source_final": {
            "verification_state": secret,
            "reason_code": secret,
        },
        "cleanup": {
            "scope": {"ok": False, "error": secret},
            secret: {"ok": False},
        },
        "authoritative_secret_scan": {
            "status": secret,
            "external_failure_receipt_payload_sha256": secret,
        },
    })

    serialized = json.dumps(projected, sort_keys=True)
    assert secret not in serialized
    assert projected["started_at"] == ""
    assert projected["error_code"] == "SupervisorError"
    assert projected["source_final"]["reason_code"] == "UNKNOWN"
    assert projected["authoritative_secret_scan"]["status"] == "UNKNOWN"
    assert set(projected["gate_statuses"]) == {
        "host_safety_preflight_verified"
    }
    assert set(projected["cleanup_statuses"]) == {"scope"}


def test_hard_io_run_defers_cleanup_and_all_durable_finalization_without_quiescence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    monkeypatch.setattr(supervisor, "_verify_host_safety_preflight", lambda: {})

    def hard_capture() -> None:
        supervisor.source_h0 = {"verified": True}
        supervisor._gate(
            "source_capture_host_safety_verified",
            passed=False,
            evidence={
                "tripped": ["HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED"],
            },
            error="HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
        )
        raise SupervisorError("HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED")

    monkeypatch.setattr(supervisor, "_capture_source", hard_capture)
    monkeypatch.setattr(
        supervisor,
        "_exception_source_final",
        lambda: {
            "verified": False,
            "reason_code": "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
            "h24_capture_skipped": True,
        },
    )
    monkeypatch.setattr(
        supervisor,
        "_terminate_processes_after_hard_io",
        lambda: {"ok": True, "durable_writes_performed": False},
    )
    monkeypatch.setattr(
        supervisor,
        "_wait_for_post_hard_io_quiescence",
        lambda: {"ok": False, "reason": "POST_HARD_IO_QUIESCENCE_TIMEOUT"},
    )
    monkeypatch.setattr(
        supervisor,
        "_cleanup",
        lambda **_kwargs: pytest.fail("cleanup must be deferred without quiescence"),
    )
    monkeypatch.setattr(
        supervisor,
        "_purge_stopped_server_tls_private_keys",
        lambda *_args: pytest.fail("TLS purge must be deferred without quiescence"),
    )
    monkeypatch.setattr(
        supervisor,
        "_publish_fail_closed_secret_scan_receipt",
        lambda **_kwargs: pytest.fail("receipt write must be suppressed"),
    )
    monkeypatch.setattr(
        supervisor,
        "_authoritative_final_scan_and_publish",
        lambda *_args, **_kwargs: pytest.fail("tree scan must be suppressed"),
    )

    assert supervisor.run() == 2
    assert not supervisor.final_path.exists()
    assert not supervisor.final_secret_scan_receipt.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_post_hard_io_quiescence_waits_for_four_safe_samples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    values = iter(
        ((12.0, 4.0), (5.0, 3.5))
        + ((2.0, 2.0),) * (
            supervisor_module.POST_HARD_IO_REQUIRED_SAFE_SAMPLES
        )
    )
    now = [0.0]

    class SafeBlockIoSampler:
        @staticmethod
        def sample() -> dict[str, object]:
            return {
                "status": "safe",
                "safe": True,
                "reason_codes": [],
                "metrics": {},
            }

    block_data_roots: list[Path] = []

    def block_sampler(*, data_root: Path) -> SafeBlockIoSampler:
        block_data_roots.append(data_root)
        return SafeBlockIoSampler()

    def collect() -> dict[str, object]:
        avg10, avg60 = next(values)
        tripped = []
        if avg10 > 3.0 or avg60 > 3.0:
            tripped.append("HOST_IO_PRESSURE_HIGH")
        if avg10 > 10.0 or avg60 > 10.0:
            tripped.append("HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED")
        return {
            "at": f"sample-{now[0]}",
            "ok": not tripped,
            "errors": {},
            "tripped": tripped,
            "checks": {
                "host_io_pressure": {
                    "value": {"avg10": avg10, "avg60": avg60},
                },
            },
        }

    monkeypatch.setattr(
        supervisor_module,
        "collect_host_startup_safety_preflight",
        collect,
    )
    monkeypatch.setattr(
        supervisor_module,
        "HostStartupBlockIoSampler",
        block_sampler,
    )
    monkeypatch.setattr(supervisor_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        supervisor_module.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    result = supervisor._wait_for_post_hard_io_quiescence()

    assert result["ok"] is True
    assert result["required_consecutive_safe"] == (
        supervisor_module.POST_HARD_IO_REQUIRED_SAFE_SAMPLES
    )
    assert result["waited_seconds"] == 61.0
    assert result["maximum_io_pressure"] == {
        "avg10": 12.0,
        "avg60": 4.0,
    }
    assert len(result["samples"]) == 62
    assert block_data_roots == [supervisor_module.ROOT]


def test_pre_activation_exception_runs_authoritative_fail_closed_finalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    monkeypatch.setattr(supervisor, "_verify_host_safety_preflight", lambda: {})
    monkeypatch.setattr(
        supervisor,
        "_capture_source",
        lambda: (_ for _ in ()).throw(
            SupervisorError("injected pre-activation failure")
        ),
    )

    returncode = supervisor.run()

    assert returncode == 2
    result = json.loads(supervisor.final_path.read_text(encoding="utf-8"))
    receipt = json.loads(
        supervisor.final_secret_scan_receipt.read_text(encoding="utf-8")
    )
    assert result["ok"] is False
    assert result["error_code"] == "SupervisorError"
    assert "error" not in result
    assert result["authoritative_secret_scan"]["required"] is True
    assert receipt["ok"] is False
    assert receipt["root_scan"]["scope"] == "recursive_tree"
    assert receipt["post_scan_artifacts"][0]["scan"]["scope"] == "exact_files"
    assert receipt["post_scan_artifacts"][0]["scan"]["files_scanned"] == 1
    assert supervisor.root not in supervisor.final_secret_scan_receipt.parents


def test_keyboard_interrupt_runs_fail_closed_cleanup_instead_of_orphaning_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    monkeypatch.setattr(
        supervisor,
        "_verify_host_safety_preflight",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    returncode = supervisor.run()

    assert returncode == 2
    result = json.loads(supervisor.final_path.read_text(encoding="utf-8"))
    assert result["error_code"] == "KeyboardInterrupt"
    assert result["cleanup"]["scope"]["not_created"] is True
    assert result["cleanup"]["scope"]["ok"] is True


def test_exception_finalizer_failure_still_writes_external_safe_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    monkeypatch.setattr(supervisor, "_verify_host_safety_preflight", lambda: {})
    monkeypatch.setattr(
        supervisor,
        "_capture_source",
        lambda: (_ for _ in ()).throw(
            SupervisorError("injected pre-activation failure")
        ),
    )
    injected_message = "injected-finalizer-sensitive-message"
    monkeypatch.setattr(
        supervisor,
        "_authoritative_final_scan_and_publish",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(injected_message)
        ),
    )

    returncode = supervisor.run()

    assert returncode == 2
    result = json.loads(supervisor.final_path.read_text(encoding="utf-8"))
    receipt_bytes = supervisor.final_secret_scan_receipt.read_bytes()
    receipt = json.loads(receipt_bytes)
    assert result["authoritative_secret_scan"]["status"] == "FAIL_CLOSED"
    assert result["authoritative_secret_scan"]["root_scan_verified"] is False
    assert receipt["ok"] is False
    assert receipt["finalizer"]["status"] == "FAIL"
    assert receipt["finalizer"]["error_class"] == "RuntimeError"
    assert injected_message.encode("utf-8") not in receipt_bytes
    assert supervisor.final_secret_scan_receipt.stat().st_mode & 0o777 == 0o600


def test_runner_release_requires_and_serializes_verified_cgroup_event_baseline(
    tmp_path: Path,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "smoke", "smoke", 180)
    )
    supervisor.checkpoint_dir.mkdir(parents=True)
    supervisor.source_h0 = {
        "commit": COMMIT,
        "tracked_content_digest": SOURCE_DIGEST,
        "artifact_root": str(tmp_path / "source"),
    }
    baseline = {
        "memory.events": {"max": 0, "oom": 0, "oom_kill": 0},
        "pids.events": {"max": 0},
    }

    class Cgroup:
        scope_path = "/campaign.scope"
        event_baseline = baseline

    class StateMachine:
        @staticmethod
        def mark_frozen(**_kwargs: object) -> dict[str, int]:
            return {"revision": 7}

    class Watchdog:
        pid = 12345

    supervisor.cgroup = Cgroup()  # type: ignore[assignment]
    supervisor.state_machine = StateMachine()  # type: ignore[assignment]
    supervisor.watchdog = Watchdog()  # type: ignore[assignment]
    supervisor.runner_pid = 23456
    supervisor.runner_auth_key = b"k" * 32
    supervisor.watchdog_auth_key = b"w" * 32
    for name in (
        "authenticated_control_channel_verified",
        "runner_control_channel_authenticated",
        "watchdog_reciprocal_liveness_verified",
        "runner_import_staged_verified",
        "watchdog_import_staged_verified",
        "host_safety_runner_import_settled",
        "host_safety_state_initialization_settled",
    ):
        supervisor._gate(name, passed=True, evidence={"ok": True})

    with pytest.raises(SupervisorError, match="verified cgroup event baseline"):
        supervisor._release_runner(
            cgroup_evidence={"ok": True},
            watchdog_evidence={"ok": True},
            placement={"ok": True},
        )

    supervisor._gate(
        "cgroup_event_baseline_verified",
        passed=True,
        evidence={"ok": True, "baseline": baseline},
    )
    with pytest.raises(SupervisorError, match="host safety activation gate"):
        supervisor._release_runner(
            cgroup_evidence={"ok": True},
            watchdog_evidence={"ok": True},
            placement={"ok": True},
        )

    supervisor._gate(
        "host_safety_activation_verified",
        passed=True,
        evidence={"ok": True},
    )
    supervisor._release_runner(
        cgroup_evidence={"ok": True},
        watchdog_evidence={"ok": True},
        placement={"ok": True},
    )

    contract = json.loads(supervisor.contract_path.read_text(encoding="utf-8"))
    assert contract["cgroup_event_baseline"] == baseline
    assert contract["gates"]["cgroup_event_baseline_verified"]["status"] == "PASS"
    assert supervisor.activation_gate_path.is_file()


def test_cleanup_proves_scope_empty_before_stopping_external_watchdog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "smoke", "smoke", 180)
    )
    calls: list[str] = []

    class Freezer:
        @staticmethod
        def close() -> None:
            calls.append("source_monitor")

    class Cgroup:
        scope_path = "/campaign.scope"

        @staticmethod
        def stop_scope() -> dict[str, object]:
            calls.append("scope")
            return {"ok": True, "cgroup_empty": True}

    supervisor.freezer = Freezer()  # type: ignore[assignment]
    supervisor.cgroup = Cgroup()  # type: ignore[assignment]
    monkeypatch.setattr(
        supervisor,
        "_stop_watchdog",
        lambda: calls.append("watchdog") or {"ok": True, "returncode": 0},
    )

    cleanup = supervisor._cleanup(normal=True)

    assert calls == ["source_monitor", "scope", "watchdog"]
    assert cleanup["scope"]["cgroup_empty"] is True
    assert cleanup["watchdog"]["ok"] is True

    calls.clear()
    diagnostic = OperationalCampaignSupervisor(
        SupervisorConfig(
            tmp_path / "diagnostic",
            "smoke",
            180,
            keep_scope_on_failure=True,
        )
    )
    diagnostic.freezer = Freezer()  # type: ignore[assignment]
    diagnostic.cgroup = Cgroup()  # type: ignore[assignment]
    monkeypatch.setattr(
        diagnostic,
        "_stop_watchdog",
        lambda: calls.append("watchdog") or {"ok": True, "returncode": 0},
    )

    preserved = diagnostic._cleanup(normal=False)

    assert calls == ["source_monitor", "watchdog"]
    assert preserved["scope"]["preserved_for_diagnosis"] is True


def test_ephemeral_tls_private_keys_are_removed_only_after_scope_is_empty(
    tmp_path: Path,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "smoke", "smoke", 180)
    )
    keys = []
    for name in ("primary", "recovery"):
        path = supervisor.root / name / "runtime" / "key.pem"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ephemeral-test-private-key")
        os.chmod(path, 0o600)
        keys.append(path)

    blocked = supervisor._purge_stopped_server_tls_private_keys(
        {"scope": {"ok": False, "cgroup_empty": False}}
    )

    assert blocked["ok"] is False
    assert all(path.is_file() for path in keys)

    result = supervisor._purge_stopped_server_tls_private_keys(
        {"scope": {"ok": True, "cgroup_empty": True}}
    )

    assert result["ok"] is True
    assert result["removed"] == [
        "primary/runtime/key.pem",
        "recovery/runtime/key.pem",
    ]
    assert result["absent"] == ["security_sentinel/runtime/key.pem"]
    assert all(not path.exists() for path in keys)
    gate = supervisor.gates["ephemeral_tls_private_keys_purged"]
    assert gate["status"] == "PASS"


def test_watchdog_handled_incident_is_clean_teardown(
    tmp_path: Path,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "smoke", "smoke", 180)
    )

    class Watchdog:
        returncode = supervisor_module.INCIDENT_EXIT_CODE
        pid = 12345

        @staticmethod
        def poll() -> int:
            return supervisor_module.INCIDENT_EXIT_CODE

    supervisor.watchdog = Watchdog()  # type: ignore[assignment]
    supervisor.watchdog_ready_path.parent.mkdir(parents=True, exist_ok=True)
    write(
        supervisor.watchdog_ready_path,
        {
            "ok": True,
            "incident_id": "watchdog-incident-1",
            "reason": "EXTERNAL_HARD_STOP_REQUESTED",
        },
    )

    result = supervisor._stop_watchdog()

    assert result["ok"] is True
    assert result["handled_incident"] is True
    assert result["returncode"] == supervisor_module.INCIDENT_EXIT_CODE


def _supervisor_audit_fixture(
    runtime: Path,
    *,
    suffix: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for directory in (runtime / "database", runtime / "logs", runtime / "anchors"):
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
        f"supervisor_fixture_{suffix}",
        "127.0.0.1",
        user="campaign",
        success=True,
        ua="pytest",
        detail="post-scope-seal",
    )


def _post_scope_cleanup() -> dict[str, object]:
    return {
        "authenticated_control_channel": {"ok": True, "closed": True},
        "source_monitor": {"ok": True, "closed": True},
        "comfyui_backend": {"ok": True, "stopped": True},
        "watchdog": {"ok": True, "returncode": 0},
        "scope": {
            "ok": True,
            "cgroup_empty": True,
            "terminal_population": {"ok": True, "populated": 0},
        },
    }


def test_supervisor_sealed_triad_runs_only_after_scope_population_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "supervisor-audit", "smoke", 180)
    )
    object.__setattr__(supervisor.config, "level", "formal")
    supervisor.artifact_dir.mkdir(parents=True)
    supervisor.control_path.parent.mkdir(parents=True)
    supervisor.control_path.write_text(
        json.dumps({
            "state": "PASS",
            "admit_new_jobs": False,
            "load_generator_should_run": False,
        }),
        encoding="utf-8",
    )

    class Runner:
        returncode = 0

        @staticmethod
        def poll() -> int:
            return 0

    class Cgroup:
        stopped = True

    supervisor.runner = Runner()  # type: ignore[assignment]
    supervisor.cgroup = Cgroup()  # type: ignore[assignment]
    for suffix, name in enumerate(
        ("primary", "recovery", "security_sentinel"),
        start=1,
    ):
        _supervisor_audit_fixture(
            supervisor.root / name / "runtime",
            suffix=suffix,
            monkeypatch=monkeypatch,
        )

    result = supervisor._capture_post_scope_audit_evidence(_post_scope_cleanup())

    assert result["ok"] is True
    assert result["classification"] == "PASS"
    assert result["writer_barrier"]["scope"]["terminal_population"]["populated"] == 0
    assert set(result["targets"]) == {"primary", "recovery", "security_sentinel"}
    assert all(row["ok"] is True for row in result["targets"].values())
    manifest = json.loads(Path(result["hash_manifest"]["path"]).read_text(encoding="utf-8"))
    assert "writer_barrier.json" in {row["path"] for row in manifest["files"]}


def test_supervisor_sealed_triad_never_captures_when_scope_is_populated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "supervisor-audit-blocked", "smoke", 180)
    )
    object.__setattr__(supervisor.config, "level", "formal")
    supervisor.artifact_dir.mkdir(parents=True)
    supervisor.control_path.parent.mkdir(parents=True)
    supervisor.control_path.write_text(
        json.dumps({
            "state": "PASS",
            "admit_new_jobs": False,
            "load_generator_should_run": False,
        }),
        encoding="utf-8",
    )

    class Runner:
        returncode = 0

        @staticmethod
        def poll() -> int:
            return 0

    class Cgroup:
        stopped = True

    supervisor.runner = Runner()  # type: ignore[assignment]
    supervisor.cgroup = Cgroup()  # type: ignore[assignment]
    calls: list[str] = []
    monkeypatch.setattr(
        supervisor_module,
        "capture_audit_evidence",
        lambda **_kwargs: calls.append("capture"),
    )
    cleanup = _post_scope_cleanup()
    cleanup["scope"]["terminal_population"] = {"ok": False, "populated": 1}

    result = supervisor._capture_post_scope_audit_evidence(cleanup)

    assert result["ok"] is False
    assert result["classification"] == "FAIL_HARNESS"
    assert calls == []
    assert all(
        row["receipt_verdict"] == "BLOCKED_BY_WRITER_BARRIER"
        for row in result["targets"].values()
    )


def test_authoritative_final_scan_covers_sealed_root_and_exact_final_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    supervisor.prepare()
    supervisor.checkpoint_mirror_path = tmp_path / ".mirror-pass" / "campaign.checkpoint.json"
    supervisor.checkpoint_mirror_path.parent.mkdir(mode=0o700)
    supervisor.checkpoint_mirror_path.write_bytes(b'{"revision":1}')
    (supervisor.root / "reports").mkdir()
    (supervisor.root / "reports" / "runner.json").write_bytes(b'{"ok":true}')
    (supervisor.control_root / "checkpoint" / "state.json").write_bytes(
        b'{"state":"PASS"}'
    )
    base = {
        "schema_version": supervisor_module.SUPERVISOR_SCHEMA_VERSION,
        "campaign_uuid": supervisor.campaign_uuid,
        "classification": "PASS",
        "ok": True,
    }

    result, receipt = supervisor._authoritative_final_scan_and_publish(
        base,
        base_ok=True,
        writers_stopped=True,
    )

    assert result["ok"] is True
    assert receipt["ok"] is True
    assert receipt["all_root_writers_stopped"] is True
    assert receipt["artifact_cutoff_at"]
    assert receipt["root_scan"]["scope"] == "recursive_tree"
    assert receipt["post_scan_artifacts"][0]["scan"]["scope"] == "exact_files"
    assert receipt["post_scan_artifacts"][0]["scan"]["files_scanned"] == 1
    assert supervisor.final_path.is_file()
    assert supervisor.final_secret_scan_receipt.is_file()
    assert supervisor.root not in supervisor.final_secret_scan_receipt.parents


def test_authoritative_final_scan_does_not_ignore_external_control_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    supervisor.prepare()
    supervisor.checkpoint_mirror_path = tmp_path / ".mirror-fail" / "campaign.checkpoint.json"
    supervisor.checkpoint_mirror_path.parent.mkdir(mode=0o700)
    supervisor.checkpoint_mirror_path.write_bytes(b'{"revision":1}')
    leaked = supervisor.control_root / "checkpoint" / "leaked.json"
    leaked.write_text(
        json.dumps({"credential": supervisor.credentials.root}),
        encoding="utf-8",
    )
    base = {
        "schema_version": supervisor_module.SUPERVISOR_SCHEMA_VERSION,
        "campaign_uuid": supervisor.campaign_uuid,
        "classification": "PASS",
        "ok": True,
    }

    result, receipt = supervisor._authoritative_final_scan_and_publish(
        base,
        base_ok=True,
        writers_stopped=True,
    )

    assert result["ok"] is False
    assert result["classification"] == "FAIL_PRODUCT"
    assert receipt["ok"] is False
    assert receipt["root_scan"]["hit_count"] == 1
    assert receipt["root_scan"]["hits"][0]["label"] == "root"
    assert "path" not in receipt["root_scan"]["hits"][0]
    snapshot_leak = (
        supervisor.artifact_dir
        / "supervisor_control_snapshot"
        / leaked.relative_to(supervisor.control_root)
    )
    assert snapshot_leak.is_file()


def test_nonformal_dirty_baseline_cannot_claim_clean_worktree_gate(tmp_path: Path) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "smoke", "smoke", 180)
    )

    class Freezer:
        def capture(self, *, label: str, require_clean: bool) -> dict[str, object]:
            assert label == "H0"
            assert require_clean is False
            return {
                "commit": COMMIT,
                "tracked_content_digest": "b" * 64,
                "content_evidence_mode": supervisor_module.METADATA_CONTENT_EVIDENCE,
                "artifact_root": str(tmp_path / "source"),
                "git_status_empty": False,
            }

        def lightweight_drift_check(self) -> dict[str, object]:
            return {
                "verified": True,
                "monitor": {"machine_verified": True, "formal_eligible": True},
            }

    record_safe_source_capture_checkpoint(supervisor)
    supervisor.freezer = Freezer()  # type: ignore[assignment]
    supervisor._capture_source()

    assert supervisor.gates["source_baseline_frozen"]["status"] == "PASS"
    assert supervisor.gates["worktree_clean_and_frozen"]["status"] == "NOT_EVALUATED"
    assert supervisor.gates["worktree_clean_and_frozen"]["machine_verified"] is False


def test_formal_bundle_is_bound_after_clean_source_digest_is_measured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate_path = tmp_path / "gates.json"
    supervisor = OperationalCampaignSupervisor(SupervisorConfig(
        tmp_path / "formal",
        "formal",
        MIN_FORMAL_SECONDS,
        authorization_file=tmp_path / "authorization.json",
        gate_bundle_file=gate_path,
        comfyui_backend=comfyui_config(tmp_path),
    ))
    supervisor.commit = COMMIT

    class Freezer:
        def capture(self, *, label: str, require_clean: bool) -> dict[str, object]:
            assert label == "H0"
            assert require_clean is True
            return {
                "commit": COMMIT,
                "tracked_content_digest": SOURCE_DIGEST,
                "content_evidence_mode": supervisor_module.FULL_CONTENT_EVIDENCE,
                "artifact_root": str(tmp_path / "source"),
                "git_status_empty": True,
            }

        def lightweight_drift_check(self) -> dict[str, object]:
            return {
                "verified": True,
                "monitor": {"machine_verified": True, "formal_eligible": True},
            }

    observed: dict[str, object] = {}

    def strict_validate(
        path: Path,
        *,
        commit: str,
        source_authority: dict[str, object],
    ) -> dict[str, object]:
        observed.update({
            "path": path,
            "commit": commit,
            "source_authority": source_authority,
        })
        return {
            "schema_version": GATE_BUNDLE_SCHEMA_VERSION,
            "bundle_sha256": "c" * 64,
            "qualification_campaign_uuid": "qualification-0001",
            "commit": commit,
            "source_digest": source_authority["tracked_content_digest"],
            "protected_source_digest": "d" * 64,
        }

    record_safe_source_capture_checkpoint(supervisor)
    supervisor.freezer = Freezer()  # type: ignore[assignment]
    monkeypatch.setattr(supervisor_module, "validate_gate_bundle", strict_validate)
    supervisor._capture_source()

    assert observed == {
        "path": gate_path,
        "commit": COMMIT,
        "source_authority": supervisor.source_h0,
    }
    assert supervisor.gates["prior_harness_gate_bundle_verified"]["status"] == "PASS"


def test_formal_source_gate_rejects_metadata_projection_as_binary_authority(
    tmp_path: Path,
) -> None:
    supervisor = OperationalCampaignSupervisor(SupervisorConfig(
        tmp_path / "formal",
        "formal",
        MIN_FORMAL_SECONDS,
        authorization_file=tmp_path / "authorization.json",
        gate_bundle_file=tmp_path / "gates.json",
        comfyui_backend=comfyui_config(tmp_path),
    ))

    class Freezer:
        def capture(self, *, label: str, require_clean: bool) -> dict[str, object]:
            assert label == "H0"
            assert require_clean is True
            return {
                "commit": COMMIT,
                "tracked_content_digest": SOURCE_DIGEST,
                "content_evidence_mode": supervisor_module.METADATA_CONTENT_EVIDENCE,
                "git_change_evidence_mode": (
                    "tracked_status_porcelain_v1_projection"
                ),
                "artifact_root": str(tmp_path / "source"),
                "git_status_empty": True,
            }

        def lightweight_drift_check(self) -> dict[str, object]:
            pytest.fail("metadata projection must fail before monitor acceptance")

    record_safe_source_capture_checkpoint(supervisor)
    supervisor.freezer = Freezer()  # type: ignore[assignment]

    with pytest.raises(SupervisorError, match="evidence mode"):
        supervisor._capture_source()


def test_rehearsal_rejects_metadata_only_source_monitor(tmp_path: Path) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(
            tmp_path / "rehearsal",
            "rehearsal",
            3600,
            comfyui_backend=comfyui_config(tmp_path),
        )
    )

    class Freezer:
        def capture(self, *, label: str, require_clean: bool) -> dict[str, object]:
            assert label == "H0"
            assert require_clean is False
            return {
                "commit": COMMIT,
                "tracked_content_digest": SOURCE_DIGEST,
                "content_evidence_mode": supervisor_module.FULL_CONTENT_EVIDENCE,
                "artifact_root": str(tmp_path / "source"),
                "git_status_empty": True,
            }

        def lightweight_drift_check(self) -> dict[str, object]:
            return {
                "verified": True,
                "monitor": {
                    "mode": "metadata_fallback",
                    "machine_verified": True,
                    "formal_eligible": False,
                },
            }

    record_safe_source_capture_checkpoint(supervisor)
    supervisor.freezer = Freezer()  # type: ignore[assignment]

    with pytest.raises(SupervisorError, match="source monitor"):
        supervisor._capture_source()
    assert supervisor.gates["source_runtime_monitor_verified"]["status"] == "FAIL"


def test_watchdog_clean_exit_before_runner_is_still_campaign_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "smoke", "smoke", 180)
    )

    class Runner:
        returncode = 0

        def __init__(self) -> None:
            self.calls = 0

        def poll(self) -> int | None:
            self.calls += 1
            return None if self.calls == 1 else 0

    class Watchdog:
        returncode = 0

        def poll(self) -> int:
            return 0

    supervisor.runner = Runner()  # type: ignore[assignment]
    supervisor.watchdog = Watchdog()  # type: ignore[assignment]
    hard_stops: list[dict[str, object]] = []
    monkeypatch.setattr(
        supervisor,
        "_request_hard_stop",
        lambda **kwargs: hard_stops.append(kwargs),
    )
    monkeypatch.setattr(supervisor_module.time, "sleep", lambda _seconds: None)

    assert supervisor._monitor_runner() == 0
    assert "watchdog exited before campaign runner terminal state" in supervisor.failure
    assert len(hard_stops) == 1
    assert hard_stops[0]["reason"] == "EXTERNAL_WATCHDOG_EXITED"
    assert hard_stops[0]["evidence"]["watchdog_returncode"] == 0
    assert hard_stops[0]["evidence"]["detected_at"]


def test_authenticated_watchdog_liveness_rejects_stale_signed_sample(tmp_path: Path) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "smoke", "smoke", 180)
    )

    class Watchdog:
        pid = os.getpid()
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

    identity = capture_process_identity(os.getpid())
    supervisor.watchdog = Watchdog()  # type: ignore[assignment]
    supervisor.watchdog_process_identity = identity
    supervisor.watchdog_auth_key = b"l" * 32
    supervisor.watchdog_liveness_path.parent.mkdir(parents=True)

    def publish(sequence: int, monotonic_ns: int) -> None:
        payload = {
            "schema_version": "hackme.campaign-watchdog-liveness.v1",
            "campaign_uuid": supervisor.campaign_uuid,
            "watchdog": {
                "pid": identity.pid,
                "start_ticks": identity.start_ticks,
                "boot_id": identity.boot_id,
                "cgroup": identity.cgroup_path,
                "monotonic_ns": monotonic_ns,
            },
        }
        supervisor_module.atomic_write_json(
            supervisor.watchdog_liveness_path,
            sign_authenticated_payload(
                payload,
                session_secret=supervisor.watchdog_auth_key,
                campaign_uuid=supervisor.campaign_uuid,
                stream="watchdog_liveness",
                sequence=sequence,
                monotonic_ns=monotonic_ns,
            ),
        )

    publish(1, time.monotonic_ns())
    assert supervisor._verify_watchdog_liveness()["ok"] is True

    publish(
        2,
        time.monotonic_ns()
        - int((supervisor_module.WATCHDOG_LIVENESS_TIMEOUT_SECONDS + 1) * 1e9),
    )
    with pytest.raises(SupervisorError, match="stale"):
        supervisor._verify_watchdog_liveness()


def test_watchdog_exit_forces_whole_scope_stop_after_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(
            tmp_path / "smoke",
            "smoke",
            180,
            source_poll_seconds=120.0,
        )
    )

    class Runner:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

    class Watchdog:
        returncode = 9

        def poll(self) -> int:
            return self.returncode

    class Cgroup:
        def __init__(self, runner: Runner) -> None:
            self.runner = runner
            self.stop_calls = 0

        def stop_scope(self) -> dict[str, object]:
            self.stop_calls += 1
            self.runner.returncode = -15
            return {"ok": True, "cgroup_empty": True}

    clock = {"now": 100.0}
    runner = Runner()
    cgroup = Cgroup(runner)
    supervisor.runner = runner  # type: ignore[assignment]
    supervisor.watchdog = Watchdog()  # type: ignore[assignment]
    supervisor.cgroup = cgroup  # type: ignore[assignment]
    monkeypatch.setattr(supervisor, "_request_hard_stop", lambda **_kwargs: None)
    monkeypatch.setattr(supervisor_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        supervisor_module.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    assert supervisor._monitor_runner() == -15
    assert cgroup.stop_calls == 1
    assert supervisor.gates["supervisor_forced_scope_stop"]["status"] == "PASS"
