#!/usr/bin/env python3
"""Level-0, machine-readable fault injection for the campaign harness.

The default profile is deliberately safe to run in a filesystem sandbox.  It
tests the durable state/timer authority, checkpoint recovery and tamper
detection, resource-sample completeness fail-closed behaviour, and Git-backed
source drift detection.  Tests that require a real delegated cgroup-v2 scope
and an external watchdog are never inferred from these simulations: their
formal gates remain ``NOT_RUN`` until a separate sandbox-outside injection has
produced real host evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.campaign_observability import (  # noqa: E402
    RESOURCE_SAMPLE_SCHEMA_VERSION,
    ResourceCollector,
    ResourceCollectorConfig,
)
from scripts.testing.campaign_gate_bundle import REQUIRED_FORMAL_GATES  # noqa: E402
from scripts.testing.campaign_source_freeze import GitSourceFreezer  # noqa: E402
from scripts.testing.campaign_state import (  # noqa: E402
    STATE_SCHEMA_VERSION,
    CampaignState,
    CampaignStateError,
    CampaignStateMachine,
)


FAULT_INJECTION_SCHEMA_VERSION = "hackme.harness-fault-injection.v1"
GATE_BUNDLE_SCHEMA_VERSION = "hackme.harness-gate-bundle.v1"
RECOVERY_CHECKPOINT_SCHEMA_VERSION = "hackme.harness-recovery-checkpoint.v1"

CORE_PROBE_TO_GATE = {
    "hard_stop_state_admission_clock": "hard_stop_injection_verified",
    "checkpoint_recovery_and_tamper": "checkpoint_recovery_verified",
    "sample_completeness_empty_collector": "sample_schema_completeness_verified",
    "source_drift_isolated_git": "source_drift_detection_verified",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_payload(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class FaultInjectionError(RuntimeError):
    """A Level-0 assertion failed or its evidence could not be trusted."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise FaultInjectionError(message)


@dataclass(frozen=True)
class ProbeResult:
    probe_id: str
    status: str
    machine_verified: bool
    started_at: str
    finished_at: str
    duration_seconds: float
    evidence: Mapping[str, Any]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "status": self.status,
            "machine_verified": self.machine_verified,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "evidence": dict(self.evidence),
            "error": self.error,
        }


def run_probe(probe_id: str, probe: Callable[[], Mapping[str, Any]]) -> ProbeResult:
    started_at = utc_now()
    started_ns = time.monotonic_ns()
    try:
        evidence = dict(probe())
        status = "PASS"
        machine_verified = True
        error = ""
    except Exception as exc:
        evidence = {}
        status = "FAIL"
        machine_verified = False
        error = f"{exc.__class__.__name__}: {exc}"
    return ProbeResult(
        probe_id=probe_id,
        status=status,
        machine_verified=machine_verified,
        started_at=started_at,
        finished_at=utc_now(),
        duration_seconds=round((time.monotonic_ns() - started_ns) / 1_000_000_000, 6),
        evidence=evidence,
        error=error,
    )


def _active_conditions() -> dict[str, bool]:
    return {
        "source_frozen": True,
        "primary_ready": True,
        "recovery_ready": True,
        "watchdog_alive": True,
        "monitor_alive": True,
        "load_generator_alive": True,
        "no_hard_stop": True,
        "campaign_state_active": True,
    }


def _new_state_machine(path: Path, *, campaign_uuid: str, required_seconds: int = 100) -> CampaignStateMachine:
    machine = CampaignStateMachine(path)
    machine.initialize(
        campaign_uuid=campaign_uuid,
        required_active_seconds=required_seconds,
        orchestrator_pid=os.getpid(),
        orchestrator_start_ticks=1,
    )
    machine.transition(CampaignState.PREFLIGHT, reason="level0_preflight")
    machine.mark_frozen(
        source={"verified": True, "probe": "isolated-level0-source"},
        containment={"verified": True, "probe": "isolated-level0-containment"},
    )
    return machine


def probe_hard_stop_state_admission_clock(work_root: Path) -> Mapping[str, Any]:
    """Prove a hard stop closes admission atomically and never credits bad time."""

    root = Path(work_root) / "hard_stop"
    root.mkdir(parents=True, exist_ok=False)
    state_path = root / "campaign.state.json"
    machine = _new_state_machine(state_path, campaign_uuid="level0-hard-stop")
    conditions = _active_conditions()
    machine.start_active(conditions, now_ns=1_000_000_000)
    machine.tick_active(conditions, now_ns=6_000_000_000)
    before = machine.snapshot()
    after = machine.hard_stop(
        reason_code="LEVEL0_INJECTED_DB_LOCK_PRESSURE",
        classification="FAIL_HARNESS",
        evidence={"injected": True, "lock_wait_seconds": 3},
        now_ns=9_000_000_000,
    )

    require(before["clock"]["continuous_active_seconds"] == 5.0, "pre-stop clock did not credit five valid seconds")
    require(after["state"] == CampaignState.STOPPING_LOAD.value, "hard stop did not enter STOPPING_LOAD")
    require(after["control"]["admit_new_jobs"] is False, "hard stop left job admission open")
    require(after["control"]["load_generator_should_run"] is False, "hard stop left load generation enabled")
    require(after["control"]["preserve_evidence_requested"] is True, "hard stop did not request evidence preservation")
    require(after["clock"]["continuous_active_seconds"] == 5.0, "hard stop incorrectly credited detection latency")
    require(after["clock"]["invalid_seconds"] == 3.0, "hard stop did not classify detection latency as invalid")
    require(after["clock"]["formal_segment_valid"] is False, "hard stop left the formal segment valid")
    require(after["clock"]["clock_pause_reason"] == "LEVEL0_INJECTED_DB_LOCK_PRESSURE", "hard-stop reason was not bound to the clock")

    tick_rejected = False
    try:
        machine.tick_active(conditions, now_ns=10_000_000_000)
    except CampaignStateError:
        tick_rejected = True
    require(tick_rejected, "active time advanced after STOPPING_LOAD")

    durable = json.loads(state_path.read_text(encoding="utf-8"))
    require(durable == after, "returned hard-stop state differs from durable state")
    return {
        "state_path": str(state_path),
        "state_revision_before": before["revision"],
        "state_revision_after": after["revision"],
        "state": after["state"],
        "control": after["control"],
        "clock": after["clock"],
        "hard_stop": after["hard_stop"],
        "post_stop_tick_rejected": tick_rejected,
    }


def write_recovery_checkpoint(path: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    require(payload.get("schema_version") == STATE_SCHEMA_VERSION, "checkpoint source state schema is unsupported")
    require(bool(str(payload.get("campaign_uuid") or "")), "checkpoint source state has no campaign UUID")
    require(int(payload.get("revision") or 0) > 0, "checkpoint source state has no durable revision")
    envelope = {
        "schema_version": RECOVERY_CHECKPOINT_SCHEMA_VERSION,
        "campaign_uuid": str(payload["campaign_uuid"]),
        "state_revision": int(payload["revision"]),
        "payload_sha256": sha256_payload(payload),
        "captured_at": utc_now(),
        "payload": payload,
    }
    atomic_write_json(path, envelope)
    return envelope


def load_recovery_checkpoint(
    path: Path,
    *,
    expected_campaign_uuid: str,
    minimum_revision: int,
) -> dict[str, Any]:
    try:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise FaultInjectionError(f"checkpoint is unreadable: {exc.__class__.__name__}: {exc}") from exc
    require(isinstance(envelope, dict), "checkpoint envelope is not an object")
    require(envelope.get("schema_version") == RECOVERY_CHECKPOINT_SCHEMA_VERSION, "checkpoint envelope schema is unsupported")
    payload = envelope.get("payload")
    require(isinstance(payload, dict), "checkpoint payload is not an object")
    require(payload.get("schema_version") == STATE_SCHEMA_VERSION, "checkpoint payload state schema is unsupported")
    require(str(envelope.get("campaign_uuid") or "") == expected_campaign_uuid, "checkpoint envelope campaign UUID mismatch")
    require(str(payload.get("campaign_uuid") or "") == expected_campaign_uuid, "checkpoint payload campaign UUID mismatch")
    revision = int(payload.get("revision") or 0)
    require(int(envelope.get("state_revision") or 0) == revision, "checkpoint envelope revision mismatch")
    require(revision >= int(minimum_revision), "checkpoint revision is older than the required durable revision")
    require(str(envelope.get("payload_sha256") or "") == sha256_payload(payload), "checkpoint payload digest mismatch")
    return dict(payload)


def restore_recovery_checkpoint(
    checkpoint_path: Path,
    destination: Path,
    *,
    expected_campaign_uuid: str,
    minimum_revision: int,
) -> dict[str, Any]:
    payload = load_recovery_checkpoint(
        checkpoint_path,
        expected_campaign_uuid=expected_campaign_uuid,
        minimum_revision=minimum_revision,
    )
    atomic_write_json(destination, payload)
    recovered = CampaignStateMachine(destination).snapshot()
    require(recovered == payload, "checkpoint readback differs after atomic recovery")
    return recovered


def probe_checkpoint_recovery_and_tamper(work_root: Path) -> Mapping[str, Any]:
    """Simulate a torn primary state, then reject a tampered recovery copy."""

    root = Path(work_root) / "checkpoint"
    root.mkdir(parents=True, exist_ok=False)
    campaign_uuid = "level0-checkpoint"
    state_path = root / "campaign.state.json"
    checkpoint_path = root / "campaign.recovery.json"
    machine = _new_state_machine(state_path, campaign_uuid=campaign_uuid)
    state = machine.snapshot()
    envelope = write_recovery_checkpoint(checkpoint_path, state)

    state_path.write_text('{"schema_version":', encoding="utf-8")
    corrupt_primary_rejected = False
    try:
        machine.snapshot()
    except CampaignStateError:
        corrupt_primary_rejected = True
    require(corrupt_primary_rejected, "a torn primary state was silently accepted")

    recovered = restore_recovery_checkpoint(
        checkpoint_path,
        state_path,
        expected_campaign_uuid=campaign_uuid,
        minimum_revision=int(state["revision"]),
    )
    require(recovered["revision"] == state["revision"], "recovery changed the durable state revision")

    tampered_path = root / "campaign.recovery.tampered.json"
    tampered = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    tampered["payload"]["reason"] = "silently-rewritten"
    atomic_write_json(tampered_path, tampered)
    tampered_destination = root / "tampered-destination.json"
    tamper_rejected = False
    try:
        restore_recovery_checkpoint(
            tampered_path,
            tampered_destination,
            expected_campaign_uuid=campaign_uuid,
            minimum_revision=int(state["revision"]),
        )
    except FaultInjectionError:
        tamper_rejected = True
    require(tamper_rejected, "tampered checkpoint payload was silently recovered")
    require(not tampered_destination.exists(), "tampered recovery modified its destination")

    return {
        "state_path": str(state_path),
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_schema_version": envelope["schema_version"],
        "state_revision": state["revision"],
        "payload_sha256": envelope["payload_sha256"],
        "corrupt_primary_rejected": corrupt_primary_rejected,
        "valid_checkpoint_recovered": recovered == state,
        "tampered_checkpoint_rejected": tamper_rejected,
        "tampered_destination_absent": not tampered_destination.exists(),
    }


def probe_sample_completeness_empty_collector(work_root: Path) -> Mapping[str, Any]:
    """Prove empty output and schema-only output cannot satisfy 95% coverage."""

    root = Path(work_root) / "sample_completeness"
    data_root = root / "data"
    data_root.mkdir(parents=True, exist_ok=False)
    collector = ResourceCollector(ResourceCollectorConfig(
        cgroup_path=root / "unused-cgroup",
        sample_path=root / "resource_samples.jsonl",
        runtime_roots={},
        campaign_data_root=data_root,
    ))
    zero_sample = collector.summary(minimum_ratio=0.95)
    require(zero_sample["samples"] == 0, "zero-sample injection unexpectedly created a sample")
    require(zero_sample["mandatory_field_completeness"] == 0.0, "zero samples received non-zero completeness")
    require(zero_sample["ok"] is False, "zero samples were accepted as complete")

    collector.samples.append({
        "sample_schema_version": RESOURCE_SAMPLE_SCHEMA_VERSION,
        "expected_fields": [],
        "valid_fields": [],
        "missing_fields": [],
        "collector_errors": {"injected": "collector returned no fields"},
        "hard_limit_state": {"ok": True, "tripped": []},
    })
    schema_only = collector.summary(minimum_ratio=0.95)
    require(schema_only["samples"] == 1, "schema-only injection was not counted")
    require(schema_only["expected_values"] == 0, "schema-only injection unexpectedly declared fields")
    require(schema_only["mandatory_field_completeness"] == 0.0, "schema-only sample received non-zero completeness")
    require(schema_only["ok"] is False, "schema-only sample was accepted as complete")
    return {
        "resource_sample_schema_version": RESOURCE_SAMPLE_SCHEMA_VERSION,
        "minimum_required_ratio": 0.95,
        "zero_sample_summary": zero_sample,
        "schema_only_summary": schema_only,
        "empty_collector_rejected": True,
        "schema_only_sample_rejected": True,
    }


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise FaultInjectionError(
            f"git {' '.join(args)} returned {completed.returncode}: "
            f"{(completed.stderr or completed.stdout).strip()[:500]}"
        )
    return completed.stdout.strip()


def probe_source_drift_isolated_git(work_root: Path) -> Mapping[str, Any]:
    """Freeze a clean temp repository, then mutate tracked and untracked source."""

    root = Path(work_root) / "source_drift"
    repo = root / "repo"
    evidence_root = root / "evidence"
    repo.mkdir(parents=True, exist_ok=False)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "level0@example.invalid")
    _git(repo, "config", "user.name", "Level Zero Probe")
    tracked = repo / "service.py"
    tracked.write_text("value = 'alpha'\n", encoding="utf-8")
    (repo / "requirements.lock").write_text("dependency==1.0\n", encoding="utf-8")
    _git(repo, "add", "service.py", "requirements.lock")
    _git(repo, "commit", "--quiet", "-m", "isolated baseline")

    freezer = GitSourceFreezer(repo, evidence_root)
    baseline = freezer.capture(label="H0", require_clean=True)
    require(baseline["verified"] is True, "isolated Git baseline was not verified")
    baseline_stat = tracked.stat()
    tracked.write_text("value = 'omega'\n", encoding="utf-8")
    os.utime(tracked, ns=(baseline_stat.st_atime_ns, baseline_stat.st_mtime_ns))
    injected_untracked = repo / "runtime-generated.cfg"
    injected_untracked.write_text("unexpected=true\n", encoding="utf-8")

    drift = freezer.lightweight_drift_check()
    require(drift["verified"] is False, "tracked/untracked drift was silently accepted")
    require("service.py" in drift["tracked_changes"], "tracked source mutation was not identified")
    require("runtime-generated.cfg" in drift["untracked_changes"] or not drift["untracked_paths_unchanged"], "new untracked source was not identified")
    require(drift["status_unchanged"] is False, "Git status drift was not detected")
    return {
        "repo_root": str(repo),
        "baseline_artifact_root": baseline["artifact_root"],
        "drift_artifact_root": drift["artifact_root"],
        "baseline_commit": baseline["commit"],
        "baseline_tracked_content_digest": baseline["tracked_content_digest"],
        "verified_after_injection": drift["verified"],
        "tracked_changes": drift["tracked_changes"],
        "untracked_changes": drift["untracked_changes"],
        "untracked_paths_unchanged": drift["untracked_paths_unchanged"],
        "status_unchanged": drift["status_unchanged"],
    }


def _git_commit(repo_root: Path) -> str:
    value = _git(Path(repo_root), "rev-parse", "HEAD")
    require(len(value) == 40 and all(character in "0123456789abcdefABCDEF" for character in value), "repository HEAD is not a full Git SHA")
    return value.lower()


def _gate_from_probe(result: ProbeResult) -> dict[str, Any]:
    passed = result.status == "PASS" and result.machine_verified is True
    return {
        "status": "PASS" if passed else "FAIL",
        "machine_verified": passed,
        "checked_at": result.finished_at,
        "evidence": result.to_dict(),
        "error": result.error,
    }


def _not_run_gate(reason: str, *, requirement: str) -> dict[str, Any]:
    return {
        "status": "NOT_RUN",
        "machine_verified": False,
        "checked_at": utc_now(),
        "evidence": {
            "reason": reason,
            "requirement": requirement,
        },
        "error": "",
    }


def _component_only_gate(result: ProbeResult, *, requirement: str) -> dict[str, Any]:
    return {
        "status": "PARTIAL_PASS" if result.status == "PASS" and result.machine_verified else "FAIL",
        "machine_verified": False,
        "checked_at": result.finished_at,
        "evidence": {
            "verification_scope": "component_only",
            "component_result": result.to_dict(),
            "promotion_requirement": requirement,
        },
        "error": result.error,
    }


def run_level0(*, artifact_root: Path, repo_root: Path = ROOT) -> dict[str, Any]:
    destination = Path(artifact_root).resolve(strict=False)
    if destination.exists() and any(destination.iterdir()):
        raise FaultInjectionError(f"artifact root must be absent or empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    work_root = destination / "work"
    work_root.mkdir(parents=True, exist_ok=False)
    started_at = utc_now()
    commit = _git_commit(repo_root)

    probes = [
        run_probe(
            "hard_stop_state_admission_clock",
            lambda: probe_hard_stop_state_admission_clock(work_root),
        ),
        run_probe(
            "checkpoint_recovery_and_tamper",
            lambda: probe_checkpoint_recovery_and_tamper(work_root),
        ),
        run_probe(
            "sample_completeness_empty_collector",
            lambda: probe_sample_completeness_empty_collector(work_root),
        ),
        run_probe(
            "source_drift_isolated_git",
            lambda: probe_source_drift_isolated_git(work_root),
        ),
    ]
    component_gates: dict[str, dict[str, Any]] = {
        CORE_PROBE_TO_GATE[result.probe_id]: _gate_from_probe(result)
        for result in probes
    }
    promotion_requirements = {
        "hard_stop_injection_verified": "inject a hard stop through the real supervisor/watchdog/cgroup path and verify admission, clock, evidence, and process teardown",
        "checkpoint_recovery_verified": "recover a real supervisor campaign checkpoint after runner interruption and verify PID/cgroup/source identity plus non-resumable formal time",
        "sample_schema_completeness_verified": "combine the empty-sample rejection with a supervised live sample stream whose every mandatory field reaches at least 95%",
        "source_drift_detection_verified": "modify frozen source during a supervised managed run and verify immediate INVALIDATED/STOPPING_LOAD plus H0/H24 mismatch evidence",
    }
    gates: dict[str, dict[str, Any]] = {
        name: _component_only_gate(
            next(result for result in probes if CORE_PROBE_TO_GATE[result.probe_id] == name),
            requirement=promotion_requirements[name],
        )
        for name in component_gates
    }
    gates.update({
        "cgroup_limits_verified": _not_run_gate(
            "sandbox Level-0 does not create or mutate the host cgroup-v2 hierarchy",
            requirement="run the real CampaignCgroup scope injection outside the sandbox and verify kernel files plus every mandatory PID",
        ),
        "external_watchdog_verified": _not_run_gate(
            "sandbox Level-0 does not SIGSTOP a real managed campaign runner",
            requirement="outside sandbox: SIGSTOP the real runner, wait for the independent 120-second watchdog, then verify admission closure, evidence preservation, and cgroup kill",
        ),
        "production_security_sentinel_verified": _not_run_gate(
            "owned by the production-security preflight",
            requirement="production-equivalent sentinel target must pass its machine contract",
        ),
        "all_mandatory_dependencies_verified": _not_run_gate(
            "owned by the dependency preflight",
            requirement="real AI provider, ComfyUI, browsers, BT seed, ffmpeg/HLS, storage, and restore dependencies must pass",
        ),
        "180_second_smoke_passed": _not_run_gate(
            "owned by the supervised 180-second smoke campaign",
            requirement="single supervised 180-second smoke must pass on this exact commit",
        ),
        "60_minute_rehearsal_passed": _not_run_gate(
            "owned by the supervised full-feature rehearsal campaign",
            requirement="single supervised 3600-second rehearsal must execute and validate all 13 reviewed scenario contracts on this exact commit",
        ),
        "worktree_clean_and_frozen": _not_run_gate(
            "formal source freeze must be performed immediately before H0",
            requirement="worktree, binary diff, index, submodules, tracked and untracked manifests must be clean and frozen on this exact commit",
        ),
    })
    # Preserve the formal supervisor's complete gate vocabulary even if a
    # future refactor changes construction order above.
    missing_gate_names = sorted(set(REQUIRED_FORMAL_GATES) - set(gates))
    require(not missing_gate_names, "Level-0 bundle omitted formal gates: " + ", ".join(missing_gate_names))
    core_ok = all(row.status == "PASS" and row.machine_verified for row in probes)
    formal_ready = all(
        gates[name]["status"] == "PASS" and gates[name]["machine_verified"] is True
        for name in REQUIRED_FORMAL_GATES
    )
    result = {
        "schema_version": FAULT_INJECTION_SCHEMA_VERSION,
        "started_at": started_at,
        "finished_at": utc_now(),
        "repo_root": str(Path(repo_root).resolve()),
        "commit": commit,
        "profile": "sandbox_level0_core",
        "probes": [row.to_dict() for row in probes],
        "component_gates": component_gates,
        "core_required_probe_ids": list(CORE_PROBE_TO_GATE),
        "core_ok": core_ok,
        "actual_cgroup_watchdog_injection_ran": False,
        "formal_ready": formal_ready,
        "gates": gates,
    }
    result_path = destination / "harness_fault_injection.json"
    gate_bundle_path = destination / "harness_gate_bundle.partial.json"
    result["artifacts"] = {
        "result": str(result_path),
        "partial_gate_bundle": str(gate_bundle_path),
    }
    gate_bundle = {
        "schema_version": GATE_BUNDLE_SCHEMA_VERSION,
        "generated_at": result["finished_at"],
        "commit": commit,
        "source": str(result_path),
        "gates": gates,
        "ok": formal_ready,
    }
    atomic_write_json(result_path, result)
    atomic_write_json(gate_bundle_path, gate_bundle)
    # Readback is part of the result contract: a truncated JSON artifact must
    # never be reported as a successful Level-0 execution.
    for path in (result_path, gate_bundle_path):
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise FaultInjectionError(f"machine artifact readback failed for {path}: {exc}") from exc
        require(isinstance(parsed, dict), f"machine artifact is not an object: {path}")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--repo-root", default=str(ROOT))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_level0(
            artifact_root=Path(args.artifact_root),
            repo_root=Path(args.repo_root),
        )
    except Exception as exc:
        failure = {
            "schema_version": FAULT_INJECTION_SCHEMA_VERSION,
            "finished_at": utc_now(),
            "core_ok": False,
            "formal_ready": False,
            "error": f"{exc.__class__.__name__}: {exc}",
        }
        print(json.dumps(failure, ensure_ascii=False, indent=2), flush=True)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["core_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
