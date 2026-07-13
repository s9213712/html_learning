from __future__ import annotations

import json
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


COMMIT = "a" * 40
SOURCE_DIGEST = "b" * 64


def write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


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
    SupervisorConfig(tmp_path / "rehearsal", "rehearsal", 3600)
    with pytest.raises(ValueError, match="smoke duration"):
        SupervisorConfig(tmp_path / "smoke2", "smoke", 179)


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

    assert "--supervised" in command
    assert "--activation-gate" in command
    assert "--supervisor-contract" in command
    assert "--checkpoint-mirror-path" in command
    assert command[command.index("--control-root") + 1] == str(supervisor.control_root)
    assert supervisor.control_root.parent == supervisor.root.parent
    assert supervisor.control_root != supervisor.root
    mirror = Path(command[command.index("--checkpoint-mirror-path") + 1])
    assert mirror == supervisor.checkpoint_mirror_path
    assert mirror.is_relative_to(Path.home() / "logs" / "hackme_web_campaign_24h")
    assert "--allow-short-duration" in command
    assert command[command.index("--concurrency") + 1] == "32"
    assert command[command.index("--round-ops") + 1] == "1000"
    assert command[command.index("--max-ordinary-p95-ms") + 1] == "3000.0"
    assert command[command.index("--minimum-free-gb") + 1] == "20.0"
    assert "--authorization-file" not in command
    assert "--gate-bundle-file" not in command


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
    assert gate["evidence"]["implemented"] is False
    with pytest.raises(SupervisorError, match="authenticated supervisor control"):
        supervisor._require_authenticated_control_channel()


def test_pre_activation_exception_runs_authoritative_fail_closed_finalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    monkeypatch.setattr(
        supervisor,
        "_clean_repo_caches",
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


def test_exception_finalizer_failure_still_writes_external_safe_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    monkeypatch.setattr(
        supervisor,
        "_clean_repo_caches",
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
                "artifact_root": str(tmp_path / "source"),
                "git_status_empty": False,
            }

        def lightweight_drift_check(self) -> dict[str, object]:
            return {
                "verified": True,
                "monitor": {"machine_verified": True, "formal_eligible": True},
            }

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
    ))
    supervisor.commit = COMMIT

    class Freezer:
        def capture(self, *, label: str, require_clean: bool) -> dict[str, object]:
            assert label == "H0"
            assert require_clean is True
            return {
                "commit": COMMIT,
                "tracked_content_digest": SOURCE_DIGEST,
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

    supervisor.freezer = Freezer()  # type: ignore[assignment]
    monkeypatch.setattr(supervisor_module, "validate_gate_bundle", strict_validate)
    supervisor._capture_source()

    assert observed == {
        "path": gate_path,
        "commit": COMMIT,
        "source_authority": supervisor.source_h0,
    }
    assert supervisor.gates["prior_harness_gate_bundle_verified"]["status"] == "PASS"


def test_rehearsal_rejects_metadata_only_source_monitor(tmp_path: Path) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "rehearsal", "rehearsal", 3600)
    )

    class Freezer:
        def capture(self, *, label: str, require_clean: bool) -> dict[str, object]:
            assert label == "H0"
            assert require_clean is False
            return {
                "commit": COMMIT,
                "tracked_content_digest": SOURCE_DIGEST,
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
