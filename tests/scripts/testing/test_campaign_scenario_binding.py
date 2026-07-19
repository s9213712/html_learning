from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Mapping

import pytest

from scripts.testing.campaign_contract import (
    SCENARIO_CONTRACT_SCHEMA_VERSION,
    FormalResultStatus,
)
from scripts.testing.campaign_native_evidence import attach_native_evidence
from scripts.testing.campaign_scenario_binding import (
    AUDITED_NATIVE_BINDING_BLOCKERS,
    FORMAL_BINDING_GATE_SCHEMA_VERSION,
    FORMAL_SCENARIO_BINDINGS,
    NATIVE_ARTIFACT_BUNDLE_SCHEMA_VERSION,
    NATIVE_EVIDENCE_MANIFEST_SCHEMA_VERSION,
    NATIVE_RUNNER_RESULT_SCHEMA_VERSION,
    NATIVE_RUNTIME_PIPELINE_SCHEMA_VERSION,
    RUNTIME_RECEIPT_SCHEMA_VERSION,
    BindingValidationError,
    FormalScenarioBinding,
    NativeEvidenceAdapterRegistration,
    ScenarioRunnerRegistration,
    ScenarioValidatorRegistration,
    ValidatorKind,
    build_and_validate_formal_scenario_bindings,
    build_formal_scenario_contracts,
    build_strict_native_adapter_registry,
    build_strict_native_validator_registry,
    execute_registered_native_scenario,
    native_evidence_manifest_validation_errors,
    strict_native_runtime_pipeline_verified,
    validate_formal_scenario_bindings,
    validate_scenario_runtime_receipt,
)
from scripts.testing.operation_coverage import (
    CAMPAIGN_SCENARIO_CONTRACTS,
    CampaignScenarioContract,
)


def _native_handler(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("binding validation must not execute registered handlers")


_NATIVE_HANDLER_REF = f"{_native_handler.__module__}:{_native_handler.__name__}"
_NO_BINDING_BLOCKERS = {
    scenario_id: () for scenario_id in CAMPAIGN_SCENARIO_CONTRACTS
}
_PIPELINE_NATIVE_RESULT: dict[str, object] = {}


def _pipeline_native_runner() -> object:
    result = dict(_PIPELINE_NATIVE_RESULT)
    started = datetime.now(timezone.utc)
    finished = max(
        datetime.now(timezone.utc),
        started + timedelta(microseconds=1),
    )
    result["started_at"] = started.isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    result["finished_at"] = finished.isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    result["elapsed_seconds"] = (finished - started).total_seconds()
    return result


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def scenario_authority(binding: FormalScenarioBinding) -> dict[str, object]:
    return {
        "qualification_campaign_uuid": "qualification-campaign-0001",
        "campaign_uuid": "runtime-campaign-0001",
        "campaign_attempt_uuid": "runtime-attempt-0001",
        "scenario_attempt_uuid": f"scenario-attempt-{binding.scenario_id}",
        "native_invocation_id": "native-invocation-0001",
        "commit": "a" * 40,
        "source_digest": "b" * 64,
        "protected_source_digest": "c" * 64,
        "started_at": "2026-07-14T00:00:00Z",
        "finished_at": "2026-07-14T00:00:01Z",
        "started_monotonic_ns": 1_000_000_000,
        "finished_monotonic_ns": 2_000_000_000,
    }


def scenario_authority_identity(
    binding: FormalScenarioBinding,
) -> dict[str, object]:
    authority = scenario_authority(binding)
    for field_name in (
        "started_at",
        "finished_at",
        "started_monotonic_ns",
        "finished_monotonic_ns",
    ):
        authority.pop(field_name)
    return authority


def native_pipeline_fixture(
    tmp_path: Path,
    binding: FormalScenarioBinding,
    *,
    weak_pointer: bool = False,
    handwritten_receipt: bool = False,
) -> tuple[
    dict[str, ScenarioRunnerRegistration],
    Mapping[str, NativeEvidenceAdapterRegistration],
    Mapping[str, ScenarioValidatorRegistration],
]:
    artifact_id = f"native.source.{binding.scenario_id}.domain_probe"
    source = tmp_path / "domain_probe.json"
    source_payload = {
        "domain": {
            "terminal_state": "success",
            "residual_fixture_count": 0,
            "evidence": {
                evidence_id: {"observed": True}
                for evidence_id in binding.evidence_adapter_ids
            },
        },
        "ok": True,
    }
    source.write_text(json.dumps(source_payload), encoding="utf-8")
    raw_runner_result = {
        "schema_version": NATIVE_RUNNER_RESULT_SCHEMA_VERSION,
        "scenario_id": binding.scenario_id,
        "started_at": "2026-07-14T00:00:00Z",
        "finished_at": "2026-07-14T00:00:01Z",
        "elapsed_seconds": 1.0,
        "terminal_state": "success",
        "execution_succeeded": True,
        "ok": True,
        "steps": [{
            "step_id": "domain_probe",
            "timed_out": False,
            "returncode": 0,
            "evidence_errors": [],
            "secret_leak_labels": [],
        }],
        "artifacts": [{
            "artifact_id": artifact_id,
            "path": str(source.resolve()),
            "artifact_type": "json",
        }],
        "formal_evidence_manifest": "",
    }
    attached = attach_native_evidence(
        raw_runner_result,
        scenario_id=binding.scenario_id,
        output_dir=tmp_path,
        scenario_assertions={
            evidence_id: True for evidence_id in binding.evidence_adapter_ids
        },
        terminal_assertions={"domain_terminal_success": True},
        cleanup_assertions={"fixture_cleanup_complete": True},
        details={"source_kind": "runtime_probe"},
    )
    if weak_pointer:
        manifest_path = Path(str(attached["formal_evidence_manifest"]))
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        first_evidence = next(iter(binding.evidence_adapter_ids))
        manifest_payload["evidence"][first_evidence][0]["json_pointer"] = "/ok"
        manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
        manifest_path.chmod(0o600)
    _PIPELINE_NATIVE_RESULT.clear()
    _PIPELINE_NATIVE_RESULT.update(attached)
    if handwritten_receipt:
        _PIPELINE_NATIVE_RESULT["runtime_receipt"] = passing_receipt(binding)
    handler_ref = f"{_pipeline_native_runner.__module__}:{_pipeline_native_runner.__name__}"
    runners = {
        binding.runner_id: ScenarioRunnerRegistration(
            runner_id=binding.runner_id,
            scenario_id=binding.scenario_id,
            implementation_ref=handler_ref,
            handler=_pipeline_native_runner,
        )
    }
    return (
        runners,
        build_strict_native_adapter_registry(),
        build_strict_native_validator_registry(),
    )


def complete_registries() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    adapters: dict[str, object] = {}
    runners: dict[str, object] = {}
    validators: dict[str, object] = {}
    for scenario_id, binding in FORMAL_SCENARIO_BINDINGS.items():
        runners[binding.runner_id] = ScenarioRunnerRegistration(
            runner_id=binding.runner_id,
            scenario_id=scenario_id,
            implementation_ref=_NATIVE_HANDLER_REF,
            handler=_native_handler,
        )
        for evidence_id, adapter_id in binding.evidence_adapter_ids.items():
            adapters[adapter_id] = NativeEvidenceAdapterRegistration(
                adapter_id=adapter_id,
                scenario_id=scenario_id,
                evidence_id=evidence_id,
                implementation_ref=_NATIVE_HANDLER_REF,
                handler=_native_handler,
            )
        for kind, validator_ids in (
            (ValidatorKind.TERMINAL, binding.terminal_validator_ids),
            (ValidatorKind.CLEANUP, binding.cleanup_validator_ids),
            (ValidatorKind.ARTIFACT, binding.artifact_validator_ids),
        ):
            for validator_id in validator_ids:
                validators[validator_id] = ScenarioValidatorRegistration(
                    validator_id=validator_id,
                    scenario_id=scenario_id,
                    kind=kind,
                    implementation_ref=_NATIVE_HANDLER_REF,
                    handler=_native_handler,
                )
    return adapters, runners, validators


def passing_receipt(binding: FormalScenarioBinding) -> dict[str, object]:
    return {
        "schema_version": RUNTIME_RECEIPT_SCHEMA_VERSION,
        "scenario_id": binding.scenario_id,
        "runner_id": binding.runner_id,
        "status": "PASS",
        "terminal_state": "success",
        "authority": scenario_authority(binding),
        "evidence_receipts": {
            evidence_id: {
                "evidence_id": evidence_id,
                "adapter_id": adapter_id,
                "validated": True,
                "native_observation_ids": (f"observation.{evidence_id}",),
            }
            for evidence_id, adapter_id in binding.evidence_adapter_ids.items()
        },
        "terminal_validator_results": {
            validator_id: True for validator_id in binding.terminal_validator_ids
        },
        "cleanup_validator_results": {
            validator_id: True for validator_id in binding.cleanup_validator_ids
        },
        "artifact_validator_results": {
            validator_id: True for validator_id in binding.artifact_validator_ids
        },
        "artifact_ids": (f"native.artifact.bundle.{binding.scenario_id}",),
        "artifact_bundle": {
            "artifact_id": f"native.artifact.bundle.{binding.scenario_id}",
            "content_schema_version": NATIVE_ARTIFACT_BUNDLE_SCHEMA_VERSION,
            "path": f"/tmp/hackme-campaign-artifacts/{binding.scenario_id}.json",
            "sha256": "1" * 64,
            "size_bytes": 4096,
            "manifest_sha256": "2" * 64,
            "member_inventory_sha256": "3" * 64,
            "member_count": 2,
            "artifact_archive_id": f"native.artifact.archive.{binding.scenario_id}",
            "artifact_archive_sha256": "4" * 64,
            "artifact_archive_size_bytes": 10240,
        },
        "diagnostics": (),
    }


def test_manifest_is_exactly_the_thirteen_reviewed_scenarios_and_91_evidence_items() -> None:
    assert len(CAMPAIGN_SCENARIO_CONTRACTS) == 13
    assert set(FORMAL_SCENARIO_BINDINGS) == set(CAMPAIGN_SCENARIO_CONTRACTS)
    assert sum(len(item.evidence_adapter_ids) for item in FORMAL_SCENARIO_BINDINGS.values()) == 91

    all_adapter_ids: list[str] = []
    for scenario_id, reviewed in CAMPAIGN_SCENARIO_CONTRACTS.items():
        binding = FORMAL_SCENARIO_BINDINGS[scenario_id]
        assert binding.scenario_id == scenario_id
        assert binding.category == reviewed.category
        assert binding.scheduled_fraction == reviewed.scheduled_fraction
        assert set(binding.evidence_adapter_ids) == set(reviewed.required_evidence)
        assert all(binding.evidence_adapter_ids.values())
        assert binding.runner_id
        assert binding.terminal_validator_ids
        assert binding.cleanup_validator_ids
        assert binding.artifact_validator_ids
        all_adapter_ids.extend(binding.evidence_adapter_ids.values())
    assert len(all_adapter_ids) == len(set(all_adapter_ids)) == 91


def test_builds_thirteen_campaign_contract_v2_objects_without_claiming_execution() -> None:
    contracts = build_formal_scenario_contracts()

    assert len(contracts) == 13
    for scenario_id, contract in contracts.items():
        reviewed = CAMPAIGN_SCENARIO_CONTRACTS[scenario_id]
        binding = FORMAL_SCENARIO_BINDINGS[scenario_id]
        assert contract.to_dict()["schema_version"] == SCENARIO_CONTRACT_SCHEMA_VERSION
        assert contract.scenario_id == scenario_id
        assert contract.domain == reviewed.category
        assert set(contract.side_effect_assertions) == set(reviewed.required_evidence)
        assert set(binding.terminal_validator_ids) <= set(contract.steps)
        assert set(binding.cleanup_validator_ids) == set(contract.cleanup_assertions)
        assert contract.mandatory is True


def test_current_unwired_native_bindings_are_explicit_fail_harness() -> None:
    gate = build_and_validate_formal_scenario_bindings()
    payload = gate.to_dict()

    assert gate.status is FormalResultStatus.FAIL_HARNESS
    assert gate.passed is False
    assert len(gate.contracts) == 13
    assert payload["schema_version"] == FORMAL_BINDING_GATE_SCHEMA_VERSION
    assert payload["gate_pass"] is False
    assert payload["formal_campaign_pass"] is False
    assert payload["reviewed_scenario_count"] == 13
    assert payload["required_evidence_count"] == 91
    assert any(error.startswith("adapter_registrations_missing:") for error in gate.errors)
    assert any(error.startswith("runner_registrations_missing:") for error in gate.errors)
    assert any(error.startswith("validator_registrations_missing:") for error in gate.errors)


def test_complete_exact_callable_registrations_pass_only_the_binding_gate() -> None:
    adapters, runners, validators = complete_registries()
    gate = build_and_validate_formal_scenario_bindings(
        adapter_registry=adapters,
        runner_registry=runners,
        validator_registry=validators,
        binding_blockers=_NO_BINDING_BLOCKERS,
        runtime_execution_pipeline_verified=True,
    )

    assert gate.status is FormalResultStatus.PASS
    assert gate.passed is True
    assert gate.errors == ()
    assert gate.to_dict()["formal_campaign_pass"] is False
    assert gate.to_dict()["runtime_execution_pipeline_verified"] is True


def test_all_audited_blockers_cleared_but_unverified_pipeline_keeps_gate_closed() -> None:
    adapters, runners, validators = complete_registries()
    gate = build_and_validate_formal_scenario_bindings(
        adapter_registry=adapters,
        runner_registry=runners,
        validator_registry=validators,
    )
    payload = gate.to_dict()

    assert set(AUDITED_NATIVE_BINDING_BLOCKERS) == set(CAMPAIGN_SCENARIO_CONTRACTS)
    assert AUDITED_NATIVE_BINDING_BLOCKERS["pointschain_hft_invariants"] == ()
    assert AUDITED_NATIVE_BINDING_BLOCKERS["media_proxy_cross_browser"] == ()
    assert AUDITED_NATIVE_BINDING_BLOCKERS["final_ui_mobile_prelaunch"] == ()
    assert AUDITED_NATIVE_BINDING_BLOCKERS["media_long_hls_share"] == ()
    assert AUDITED_NATIVE_BINDING_BLOCKERS["wallet_incident_governance"] == ()
    assert AUDITED_NATIVE_BINDING_BLOCKERS["backup_restore_restart"] == ()
    assert AUDITED_NATIVE_BINDING_BLOCKERS["server_emergency_incident"] == ()
    assert AUDITED_NATIVE_BINDING_BLOCKERS["trading_background_custom_workflow"] == ()
    assert AUDITED_NATIVE_BINDING_BLOCKERS["cloud_drive_share_stream"] == ()
    assert AUDITED_NATIVE_BINDING_BLOCKERS["community_governance_operations"] == ()
    assert AUDITED_NATIVE_BINDING_BLOCKERS["comfyui_real_workflows"] == ()
    assert AUDITED_NATIVE_BINDING_BLOCKERS["ai_agent_positive_operations"] == ()
    assert AUDITED_NATIVE_BINDING_BLOCKERS["bt_download_stream_restart"] == ()
    assert all(blockers == () for blockers in AUDITED_NATIVE_BINDING_BLOCKERS.values())
    assert gate.status is FormalResultStatus.FAIL_HARNESS
    assert payload["fully_bound_scenario_count"] == 0
    assert payload["runtime_execution_pipeline_verified"] is False
    assert "native_runtime_execution_pipeline_not_verified" in gate.errors
    assert all(
        item["registrations_complete"] is True
        for item in payload["registration_coverage"].values()
    )
    assert payload["gate_pass"] is False
    assert payload["binding_blockers"]["bt_download_stream_restart"] == []
    assert not any(error.startswith("native_binding_blockers_present:") for error in gate.errors)


def test_binding_blocker_manifest_missing_extra_or_invalid_scenario_fails_closed() -> None:
    blockers = dict(_NO_BINDING_BLOCKERS)
    blockers.pop("media_long_hls_share")
    blockers["unreviewed_alias"] = ()

    errors = validate_formal_scenario_bindings(binding_blockers=blockers)

    assert any(error.startswith("native_binding_blocker_scenarios_missing:") for error in errors)
    assert any(error.startswith("native_binding_blocker_scenarios_extra:") for error in errors)


@pytest.mark.parametrize("registry_name", ["adapter", "runner", "validator"])
def test_missing_and_extra_registration_ids_fail_closed(registry_name: str) -> None:
    adapters, runners, validators = complete_registries()
    registries = {
        "adapter": adapters,
        "runner": runners,
        "validator": validators,
    }
    selected = registries[registry_name]
    selected.pop(next(iter(selected)))
    selected[f"native.{registry_name}.unreviewed_extra"] = object()

    errors = validate_formal_scenario_bindings(
        adapter_registry=adapters,
        runner_registry=runners,
        validator_registry=validators,
    )

    assert any(error.startswith(f"{registry_name}_registrations_missing:") for error in errors)
    assert any(error.startswith(f"{registry_name}_registrations_extra:") for error in errors)


def test_alias_scenario_id_is_missing_plus_extra_not_a_match() -> None:
    first_id = next(iter(FORMAL_SCENARIO_BINDINGS))
    aliased = dict(FORMAL_SCENARIO_BINDINGS)
    original = aliased.pop(first_id)
    alias_id = f"{first_id}_alias"
    aliased[alias_id] = replace(original, scenario_id=alias_id)

    errors = validate_formal_scenario_bindings(bindings=aliased)

    assert any(error.startswith("binding_scenario_ids_missing:") for error in errors)
    assert any(error.startswith("binding_scenario_ids_extra:") for error in errors)


def test_adapter_alias_category_fraction_and_evidence_drift_all_fail_exact_match() -> None:
    scenario_id = next(iter(FORMAL_SCENARIO_BINDINGS))
    original = FORMAL_SCENARIO_BINDINGS[scenario_id]
    evidence_id = next(iter(original.evidence_adapter_ids))

    changed_adapters = dict(original.evidence_adapter_ids)
    changed_adapters[evidence_id] = f"{changed_adapters[evidence_id]}.alias"
    bindings = dict(FORMAL_SCENARIO_BINDINGS)
    bindings[scenario_id] = replace(original, evidence_adapter_ids=changed_adapters)
    assert f"binding_evidence_adapter_ids_mismatch:{scenario_id}" in validate_formal_scenario_bindings(bindings=bindings)

    bindings[scenario_id] = replace(original, category=f"{original.category}.alias")
    assert f"binding_category_mismatch:{scenario_id}" in validate_formal_scenario_bindings(bindings=bindings)

    bindings[scenario_id] = replace(original, scheduled_fraction=original.scheduled_fraction + 0.001)
    assert f"binding_scheduled_fraction_mismatch:{scenario_id}" in validate_formal_scenario_bindings(bindings=bindings)

    reviewed = dict(CAMPAIGN_SCENARIO_CONTRACTS)
    source = reviewed[scenario_id]
    reviewed[scenario_id] = replace(
        source,
        required_evidence=frozenset(set(source.required_evidence) - {evidence_id}),
    )
    assert f"reviewed_required_evidence_mismatch:{scenario_id}" in validate_formal_scenario_bindings(reviewed_contracts=reviewed)


def test_reviewed_alias_missing_extra_category_and_fraction_fail_closed() -> None:
    scenario_id = next(iter(CAMPAIGN_SCENARIO_CONTRACTS))
    source = CAMPAIGN_SCENARIO_CONTRACTS[scenario_id]
    reviewed = dict(CAMPAIGN_SCENARIO_CONTRACTS)
    reviewed.pop(scenario_id)
    reviewed[f"{scenario_id}_alias"] = source
    errors = validate_formal_scenario_bindings(reviewed_contracts=reviewed)
    assert any(error.startswith("reviewed_scenario_ids_missing:") for error in errors)
    assert any(error.startswith("reviewed_scenario_ids_extra:") for error in errors)

    reviewed = dict(CAMPAIGN_SCENARIO_CONTRACTS)
    reviewed[scenario_id] = replace(source, category=f"{source.category}.alias")
    assert f"reviewed_category_mismatch:{scenario_id}" in validate_formal_scenario_bindings(reviewed_contracts=reviewed)

    reviewed[scenario_id] = replace(source, scheduled_fraction=source.scheduled_fraction + 0.001)
    assert f"reviewed_scheduled_fraction_mismatch:{scenario_id}" in validate_formal_scenario_bindings(reviewed_contracts=reviewed)


def test_registration_mapping_key_and_native_identity_cannot_alias() -> None:
    adapters, runners, validators = complete_registries()
    adapter_id = next(iter(adapters))
    registration = adapters[adapter_id]
    assert isinstance(registration, NativeEvidenceAdapterRegistration)
    adapters[adapter_id] = replace(registration, evidence_id=f"{registration.evidence_id}.alias")

    runner_id = next(iter(runners))
    runner = runners[runner_id]
    assert isinstance(runner, ScenarioRunnerRegistration)
    runners[runner_id] = replace(runner, scenario_id=f"{runner.scenario_id}.alias")

    validator_id = next(iter(validators))
    validator = validators[validator_id]
    assert isinstance(validator, ScenarioValidatorRegistration)
    wrong_kind = ValidatorKind.ARTIFACT if validator.kind is not ValidatorKind.ARTIFACT else ValidatorKind.CLEANUP
    validators[validator_id] = replace(validator, kind=wrong_kind)

    errors = validate_formal_scenario_bindings(
        adapter_registry=adapters,
        runner_registry=runners,
        validator_registry=validators,
    )
    assert f"adapter_registration_evidence_mismatch:{adapter_id}" in errors
    assert f"runner_registration_scenario_mismatch:{runner_id}" in errors
    assert f"validator_registration_kind_mismatch:{validator_id}" in errors


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        [],
        True,
        0,
        "ok",
        {"ok": True},
        {"raw_ok": True},
        {"json_ok": True},
        {"raw_json": {}},
        {"rc": 0},
        {"returncode": 0},
        {"status_code": 200},
        {"http_status": 202},
        {"skip": True},
        {"skipped": True},
        {"expected_gap": True},
    ],
)
def test_empty_json_raw_ok_rc0_http_acceptance_skip_and_expected_gap_fail_closed(
    receipt: object,
) -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    result = validate_scenario_runtime_receipt(receipt, binding)

    assert result.status is FormalResultStatus.FAIL_HARNESS
    assert result.valid is False
    assert result.contract_pass is False
    assert result.errors


def test_complete_native_runtime_receipt_is_contract_pass() -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    result = validate_scenario_runtime_receipt(passing_receipt(binding), binding)

    assert result.status is FormalResultStatus.PASS
    assert result.valid is True
    assert result.contract_pass is True
    assert result.errors == ()


@pytest.mark.parametrize(
    ("field", "value"),
    (("size_bytes", 200), ("artifact_archive_size_bytes", 202)),
)
def test_bundle_sizes_equal_to_http_status_numbers_are_not_transport_shortcuts(
    field: str,
    value: int,
) -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    payload = passing_receipt(binding)
    payload["artifact_bundle"][field] = value

    result = validate_scenario_runtime_receipt(payload, binding)

    assert result.status is FormalResultStatus.PASS
    assert result.valid is True
    assert result.contract_pass is True
    assert result.errors == ()


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    (
        (
            lambda payload: payload.update({"schema_version": "obsolete"}),
            "native_evidence_manifest_schema_mismatch",
        ),
        (
            lambda payload: payload.update({"artifact_ids": ["native.other"]}),
            "native_evidence_manifest_artifact_ids_mismatch",
        ),
        (
            lambda payload: payload.update({"skip": True}),
            "native_shortcut_key_forbidden:native_evidence_manifest.skip",
        ),
    ),
)
def test_in_memory_native_manifest_validation_is_complete_and_fail_closed(
    mutation,
    expected_error: str,
) -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    artifact_id = f"native.artifact.{binding.scenario_id}.proof"
    manifest = {
        "schema_version": NATIVE_EVIDENCE_MANIFEST_SCHEMA_VERSION,
        "scenario_id": binding.scenario_id,
        "artifact_ids": [artifact_id],
        "evidence": {
            evidence_id: [] for evidence_id in binding.evidence_adapter_ids
        },
        "terminal": {"state": "success", "observations": []},
        "cleanup": {"state": "clean", "observations": []},
    }
    assert native_evidence_manifest_validation_errors(
        manifest,
        binding,
        artifact_ids={artifact_id},
    ) == ()

    mutation(manifest)
    errors = native_evidence_manifest_validation_errors(
        manifest,
        binding,
        artifact_ids={artifact_id},
    )

    assert expected_error in errors


@pytest.mark.parametrize(
    "field,value,expected_error",
    [
        (
            "campaign_uuid",
            "other",
            "runtime_receipt_authority_campaign_uuid_invalid",
        ),
        (
            "commit",
            "d" * 39,
            "runtime_receipt_authority_commit_invalid",
        ),
        (
            "source_digest",
            "g" * 64,
            "runtime_receipt_authority_source_digest_invalid",
        ),
        (
            "protected_source_digest",
            "f" * 63,
            "runtime_receipt_authority_protected_source_digest_invalid",
        ),
        (
            "campaign_attempt_uuid",
            "short",
            "runtime_receipt_authority_campaign_attempt_uuid_invalid",
        ),
        (
            "scenario_attempt_uuid",
            "x",
            "runtime_receipt_authority_scenario_attempt_uuid_invalid",
        ),
    ],
)
def test_runtime_receipt_rejects_malformed_campaign_source_and_attempt_authority(
    field: str,
    value: str,
    expected_error: str,
) -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    payload = passing_receipt(binding)
    payload["authority"][field] = value

    result = validate_scenario_runtime_receipt(payload, binding)

    assert result.status is FormalResultStatus.FAIL_HARNESS
    assert result.valid is False
    assert expected_error in result.errors


@pytest.mark.parametrize(
    "mutation,expected_error",
    [
        (
            lambda authority: authority.update(
                {
                    "started_at": "2026-07-14T00:00:02Z",
                    "finished_at": "2026-07-14T00:00:01Z",
                }
            ),
            "runtime_receipt_authority_wall_boundary_invalid",
        ),
        (
            lambda authority: authority.update(
                {
                    "started_monotonic_ns": 3_000_000_000,
                    "finished_monotonic_ns": 2_000_000_000,
                }
            ),
            "runtime_receipt_authority_monotonic_boundary_invalid",
        ),
        (
            lambda authority: authority.update(
                {"finished_monotonic_ns": 12_000_000_000}
            ),
            "runtime_receipt_authority_wall_monotonic_duration_mismatch",
        ),
    ],
)
def test_runtime_receipt_rejects_inverted_or_mismatched_time_bounds(
    mutation,
    expected_error: str,
) -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    payload = passing_receipt(binding)
    mutation(payload["authority"])

    result = validate_scenario_runtime_receipt(payload, binding)

    assert result.status is FormalResultStatus.FAIL_HARNESS
    assert result.valid is False
    assert expected_error in result.errors


@pytest.mark.parametrize(
    "field,value,expected_error",
    [
        (
            "artifact_id",
            "native.artifact.bundle.other_scenario",
            "runtime_receipt_artifact_bundle_id_mismatch",
        ),
        (
            "content_schema_version",
            "hackme.campaign.native-artifact-bundle/v1",
            "runtime_receipt_artifact_bundle_schema_mismatch",
        ),
        (
            "path",
            "/tmp/hackme-campaign-artifacts/../substituted.json",
            "runtime_receipt_artifact_bundle_path_not_canonical",
        ),
        (
            "sha256",
            "not-a-sha256",
            "runtime_receipt_artifact_bundle_sha256_invalid",
        ),
        (
            "size_bytes",
            0,
            "runtime_receipt_artifact_bundle_size_bytes_invalid",
        ),
        (
            "artifact_archive_id",
            "native.artifact.archive.other_scenario",
            "runtime_receipt_artifact_bundle_archive_id_mismatch",
        ),
        (
            "artifact_archive_size_bytes",
            -1,
            "runtime_receipt_artifact_bundle_artifact_archive_size_bytes_invalid",
        ),
    ],
)
def test_runtime_receipt_rejects_substituted_bundle_reference(
    field: str,
    value: object,
    expected_error: str,
) -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    payload = passing_receipt(binding)
    payload["artifact_bundle"][field] = value

    result = validate_scenario_runtime_receipt(payload, binding)

    assert result.status is FormalResultStatus.FAIL_HARNESS
    assert result.valid is False
    assert expected_error in result.errors


def test_runtime_receipt_rejects_symbolic_bundle_id_without_concrete_reference() -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    payload = passing_receipt(binding)
    payload.pop("artifact_bundle")

    result = validate_scenario_runtime_receipt(payload, binding)

    assert result.status is FormalResultStatus.FAIL_HARNESS
    assert result.valid is False
    assert "runtime_receipt_fields_missing:artifact_bundle" in result.errors
    assert "runtime_receipt_artifact_bundle_not_mapping" in result.errors
    assert (
        "runtime_receipt_symbolic_bundle_without_concrete_reference"
        in result.errors
    )


def test_runtime_receipt_requires_the_reviewed_scenario_artifact_bundle() -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    payload = passing_receipt(binding)
    payload["artifact_ids"] = ("native.artifact.bundle.some_other_scenario",)

    result = validate_scenario_runtime_receipt(payload, binding)
    assert result.status is FormalResultStatus.FAIL_HARNESS
    assert "runtime_receipt_reviewed_artifact_bundle_missing" in result.errors


@pytest.mark.parametrize(
    "mutation,expected_error",
    [
        (
            lambda payload, binding: payload.update({"scenario_id": f"{binding.scenario_id}.alias"}),
            "runtime_receipt_scenario_id_mismatch",
        ),
        (
            lambda payload, binding: payload.update({"runner_id": f"{binding.runner_id}.alias"}),
            "runtime_receipt_runner_id_mismatch",
        ),
        (
            lambda payload, _binding: payload.update({"extra": True}),
            "runtime_receipt_fields_extra:extra",
        ),
        (
            lambda payload, _binding: payload.pop("artifact_ids"),
            "runtime_receipt_fields_missing:artifact_ids",
        ),
    ],
)
def test_runtime_alias_missing_and_extra_fields_fail_closed(mutation, expected_error: str) -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    payload = passing_receipt(binding)
    mutation(payload, binding)

    result = validate_scenario_runtime_receipt(payload, binding)
    assert result.status is FormalResultStatus.FAIL_HARNESS
    assert expected_error in result.errors


def test_runtime_missing_extra_or_aliased_evidence_adapter_fails_closed() -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    evidence_id = next(iter(binding.evidence_adapter_ids))

    payload = passing_receipt(binding)
    payload["evidence_receipts"].pop(evidence_id)
    result = validate_scenario_runtime_receipt(payload, binding)
    assert any(error.startswith("evidence_receipts_missing:") for error in result.errors)

    payload = passing_receipt(binding)
    payload["evidence_receipts"]["unreviewed_extra"] = {
        "evidence_id": "unreviewed_extra",
        "adapter_id": "native.adapter.unreviewed_extra",
        "validated": True,
        "native_observation_ids": ("observation.extra",),
    }
    result = validate_scenario_runtime_receipt(payload, binding)
    assert any(error.startswith("evidence_receipts_extra:") for error in result.errors)

    payload = passing_receipt(binding)
    payload["evidence_receipts"][evidence_id]["adapter_id"] += ".alias"
    result = validate_scenario_runtime_receipt(payload, binding)
    assert f"evidence_receipt_adapter_mismatch:{evidence_id}" in result.errors


def test_pass_requires_all_native_terminal_cleanup_and_artifact_validators() -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    payload = passing_receipt(binding)
    evidence_id = next(iter(binding.evidence_adapter_ids))
    payload["evidence_receipts"][evidence_id]["validated"] = False
    payload["terminal_validator_results"][binding.terminal_validator_ids[0]] = False
    payload["cleanup_validator_results"][binding.cleanup_validator_ids[0]] = False
    payload["artifact_validator_results"][binding.artifact_validator_ids[0]] = False

    result = validate_scenario_runtime_receipt(payload, binding)
    assert result.status is FormalResultStatus.FAIL_HARNESS
    assert "runtime_receipt_native_evidence_not_all_validated" in result.errors
    assert "runtime_receipt_validators_not_all_passed" in result.errors


def test_nonpass_receipt_requires_diagnostics_and_never_becomes_contract_pass() -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    payload = passing_receipt(binding)
    payload["status"] = "FAIL_PRODUCT"
    payload["terminal_state"] = "failed"
    payload["diagnostics"] = ("domain_invariant_failed",)

    result = validate_scenario_runtime_receipt(payload, binding)
    assert result.status is FormalResultStatus.FAIL_PRODUCT
    assert result.valid is True
    assert result.contract_pass is False

    payload["diagnostics"] = ()
    result = validate_scenario_runtime_receipt(payload, binding)
    assert result.status is FormalResultStatus.FAIL_HARNESS
    assert "runtime_receipt_nonpass_diagnostics_missing" in result.errors


def test_registration_constructors_reject_empty_alias_like_or_noncallable_implementations() -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))
    evidence_id, adapter_id = next(iter(binding.evidence_adapter_ids.items()))
    with pytest.raises(BindingValidationError):
        NativeEvidenceAdapterRegistration(
            adapter_id=adapter_id,
            scenario_id=binding.scenario_id,
            evidence_id=evidence_id,
            implementation_ref="",
            handler=_native_handler,
        )
    with pytest.raises(BindingValidationError):
        NativeEvidenceAdapterRegistration(
            adapter_id=adapter_id,
            scenario_id=binding.scenario_id,
            evidence_id=evidence_id,
            implementation_ref="tests.native:evidence_adapter",
            handler=None,
        )


def test_registration_rejects_forged_callable_provenance() -> None:
    binding = next(iter(FORMAL_SCENARIO_BINDINGS.values()))

    with pytest.raises(BindingValidationError, match="does not match handler provenance"):
        ScenarioRunnerRegistration(
            runner_id=binding.runner_id,
            scenario_id=binding.scenario_id,
            implementation_ref="scripts.testing.operational_campaign_24h:scenario_media_long",
            handler=_native_handler,
        )


def test_shared_strict_runtime_registry_covers_all_91_evidence_and_39_validators() -> None:
    adapters = build_strict_native_adapter_registry()
    validators = build_strict_native_validator_registry()

    assert len(adapters) == 91
    assert len(validators) == 39
    assert strict_native_runtime_pipeline_verified(
        adapter_registry=adapters,
        validator_registry=validators,
    ) is True
    assert {
        registration.kind for registration in validators.values()
    } == {ValidatorKind.TERMINAL, ValidatorKind.CLEANUP, ValidatorKind.ARTIFACT}


def test_native_pipeline_invokes_runner_and_derives_pass_receipt_from_reopened_artifacts(
    tmp_path: Path,
) -> None:
    binding = FORMAL_SCENARIO_BINDINGS["media_long_hls_share"]
    runners, adapters, validators = native_pipeline_fixture(tmp_path, binding)
    registration = runners[binding.runner_id]
    invocation_count = 0

    def counted_runner() -> object:
        nonlocal invocation_count
        invocation_count += 1
        return registration.handler()

    runners = {
        binding.runner_id: replace(
            registration,
            implementation_ref=(
                f"{counted_runner.__module__}:{counted_runner.__name__}"
            ),
            handler=counted_runner,
        )
    }

    result = execute_registered_native_scenario(
        binding=binding,
        runner_registry=runners,
        adapter_registry=adapters,
        validator_registry=validators,
        artifact_root=tmp_path,
        authority=scenario_authority_identity(binding),
    )

    assert result["schema_version"] == NATIVE_RUNTIME_PIPELINE_SCHEMA_VERSION
    assert result["runner_invoked"] is True
    assert invocation_count == 1
    assert result["classification"] == "PASS"
    assert result["ok"] is True
    assert result["runtime_receipt_validation"]["contract_pass"] is True
    assert set(result["runtime_receipt"]["evidence_receipts"]) == set(
        binding.evidence_adapter_ids
    )
    assert all(
        receipt["validated"] is True
        and receipt["native_observation_ids"]
        for receipt in result["runtime_receipt"]["evidence_receipts"].values()
    )
    bundle = Path(result["artifact_bundle_path"])
    assert bundle.is_file()
    assert bundle.stat().st_mode & 0o077 == 0
    assert len(result["artifact_bundle_sha256"]) == 64


def test_native_pipeline_missing_authority_fails_before_invoking_runner(
    tmp_path: Path,
) -> None:
    binding = FORMAL_SCENARIO_BINDINGS["media_long_hls_share"]
    runners, adapters, validators = native_pipeline_fixture(tmp_path, binding)

    result = execute_registered_native_scenario(
        binding=binding,
        runner_registry=runners,
        adapter_registry=adapters,
        validator_registry=validators,
        artifact_root=tmp_path,
    )

    assert result["classification"] == "FAIL_HARNESS"
    assert result["ok"] is False
    assert result["runner_invoked"] is False
    assert any(
        item.startswith("native_pipeline_authority_")
        for item in result["diagnostics"]
    )


def test_native_pipeline_rejects_runner_wall_interval_outside_measured_envelope(
    tmp_path: Path,
) -> None:
    binding = FORMAL_SCENARIO_BINDINGS["media_long_hls_share"]
    runners, adapters, validators = native_pipeline_fixture(tmp_path, binding)
    registered = runners[binding.runner_id]

    def stale_runner() -> object:
        result = dict(_pipeline_native_runner())
        result["started_at"] = "2000-01-01T00:00:00.000000Z"
        result["finished_at"] = "2000-01-01T00:00:01.000000Z"
        return result

    runners = {
        binding.runner_id: replace(
            registered,
            implementation_ref=f"{stale_runner.__module__}:{stale_runner.__name__}",
            handler=stale_runner,
        )
    }
    result = execute_registered_native_scenario(
        binding=binding,
        runner_registry=runners,
        adapter_registry=adapters,
        validator_registry=validators,
        artifact_root=tmp_path,
        authority=scenario_authority_identity(binding),
    )

    assert result["classification"] == "FAIL_HARNESS"
    assert result["ok"] is False
    assert result["runner_invoked"] is True
    assert "native_runner_authority_wall_interval_mismatch" in result["diagnostics"]


def test_native_pipeline_rejects_ok_field_as_semantic_evidence(
    tmp_path: Path,
) -> None:
    binding = FORMAL_SCENARIO_BINDINGS["media_long_hls_share"]
    runners, adapters, validators = native_pipeline_fixture(
        tmp_path,
        binding,
        weak_pointer=True,
    )

    result = execute_registered_native_scenario(
        binding=binding,
        runner_registry=runners,
        adapter_registry=adapters,
        validator_registry=validators,
        artifact_root=tmp_path,
        authority=scenario_authority_identity(binding),
    )

    assert result["ok"] is False
    assert result["classification"] == "FAIL_HARNESS"
    assert result["runtime_receipt_validation"]["contract_pass"] is False
    assert any("transport/process shortcut" in item for item in result["diagnostics"])


def test_native_pipeline_rejects_skip_or_fallback_semantics_in_probe_manifest(
    tmp_path: Path,
) -> None:
    binding = FORMAL_SCENARIO_BINDINGS["media_long_hls_share"]
    runners, adapters, validators = native_pipeline_fixture(tmp_path, binding)
    manifest_path = Path(str(_PIPELINE_NATIVE_RESULT["formal_evidence_manifest"]))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_evidence = next(iter(binding.evidence_adapter_ids))
    manifest["evidence"][first_evidence][0]["skip"] = False
    manifest["cleanup"]["fallback"] = "clean"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = execute_registered_native_scenario(
        binding=binding,
        runner_registry=runners,
        adapter_registry=adapters,
        validator_registry=validators,
        artifact_root=tmp_path,
        authority=scenario_authority_identity(binding),
    )

    assert result["ok"] is False
    assert result["classification"] == "FAIL_HARNESS"
    assert any("native_shortcut_key_forbidden" in item for item in result["diagnostics"])


def test_native_pipeline_rejects_runner_supplied_handwritten_receipt(
    tmp_path: Path,
) -> None:
    binding = FORMAL_SCENARIO_BINDINGS["media_long_hls_share"]
    runners, adapters, validators = native_pipeline_fixture(
        tmp_path,
        binding,
        handwritten_receipt=True,
    )

    result = execute_registered_native_scenario(
        binding=binding,
        runner_registry=runners,
        adapter_registry=adapters,
        validator_registry=validators,
        artifact_root=tmp_path,
        authority=scenario_authority_identity(binding),
    )

    assert result["ok"] is False
    assert result["classification"] == "FAIL_HARNESS"
    assert "native_runner_result_shape_mismatch" in result["diagnostics"]
    assert any("runtime_receipt" in item for item in result["diagnostics"])


def test_build_rejects_structurally_drifted_reviewed_contract_instead_of_partial_output() -> None:
    scenario_id = next(iter(CAMPAIGN_SCENARIO_CONTRACTS))
    reviewed = dict(CAMPAIGN_SCENARIO_CONTRACTS)
    source = reviewed[scenario_id]
    reviewed[scenario_id] = CampaignScenarioContract(
        category=source.category,
        scheduled_fraction=source.scheduled_fraction,
        required_evidence=frozenset(),
        resource_class=source.resource_class,
    )

    with pytest.raises(BindingValidationError, match="reviewed_required_evidence_mismatch"):
        build_formal_scenario_contracts(reviewed_contracts=reviewed)


def test_invalid_binding_object_remains_serialisable_fail_harness() -> None:
    scenario_id = next(iter(FORMAL_SCENARIO_BINDINGS))
    bindings = dict(FORMAL_SCENARIO_BINDINGS)
    bindings[scenario_id] = object()

    gate = build_and_validate_formal_scenario_bindings(bindings=bindings)
    payload = gate.to_dict()
    assert gate.status is FormalResultStatus.FAIL_HARNESS
    assert payload["gate_pass"] is False
    assert f"binding_type_invalid:{scenario_id}" in payload["errors"]
