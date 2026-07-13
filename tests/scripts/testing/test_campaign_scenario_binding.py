from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.testing.campaign_contract import (
    SCENARIO_CONTRACT_SCHEMA_VERSION,
    FormalResultStatus,
)
from scripts.testing.campaign_scenario_binding import (
    AUDITED_NATIVE_BINDING_BLOCKERS,
    FORMAL_BINDING_GATE_SCHEMA_VERSION,
    FORMAL_SCENARIO_BINDINGS,
    RUNTIME_RECEIPT_SCHEMA_VERSION,
    BindingValidationError,
    FormalScenarioBinding,
    NativeEvidenceAdapterRegistration,
    ScenarioRunnerRegistration,
    ScenarioValidatorRegistration,
    ValidatorKind,
    build_and_validate_formal_scenario_bindings,
    build_formal_scenario_contracts,
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


def test_audited_product_blockers_are_machine_readable_and_keep_gate_closed() -> None:
    adapters, runners, validators = complete_registries()
    gate = build_and_validate_formal_scenario_bindings(
        adapter_registry=adapters,
        runner_registry=runners,
        validator_registry=validators,
    )
    payload = gate.to_dict()

    assert set(AUDITED_NATIVE_BINDING_BLOCKERS) == set(CAMPAIGN_SCENARIO_CONTRACTS)
    assert all(AUDITED_NATIVE_BINDING_BLOCKERS.values())
    assert gate.status is FormalResultStatus.FAIL_HARNESS
    assert payload["fully_bound_scenario_count"] == 0
    assert payload["runtime_execution_pipeline_verified"] is False
    assert "native_runtime_execution_pipeline_not_verified" in gate.errors
    assert all(
        item["registrations_complete"] is True
        for item in payload["registration_coverage"].values()
    )
    assert payload["gate_pass"] is False
    assert payload["binding_blockers"]["bt_download_stream_restart"] == [
        "native_scenario_runner_missing",
        "magnet_torrent_hash_pause_resume_restart_evidence_adapters_missing",
    ]
    assert any(
        error.startswith("native_binding_blockers_present:bt_download_stream_restart:")
        for error in gate.errors
    )


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
