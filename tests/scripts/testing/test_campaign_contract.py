from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.testing.campaign_contract import (
    SCENARIO_CONTRACT_SCHEMA_VERSION,
    SCENARIO_ROLLUP_SCHEMA_VERSION,
    ContractValidationError,
    FormalResultStatus,
    ScenarioContract,
    ScenarioResult,
    build_formal_rollup,
    contract_set_errors,
    rollup_formal_status,
)
from scripts.testing.operation_coverage import CAMPAIGN_SCENARIO_CONTRACTS


def contract() -> ScenarioContract:
    return ScenarioContract(
        scenario_id="cloud_drive_share_001",
        domain="cloud_drive",
        mandatory=True,
        role="user",
        preconditions=("primary_ready",),
        steps=("upload_file", "create_share", "revoke_share"),
        expected_terminal_state="success",
        side_effect_assertions=("share_row_created", "revoked_share_denied"),
        cleanup_assertions=("fixture_deleted",),
        artifacts=("cloud_drive_trace_001",),
        deadline_seconds=180,
        earliest_start=0,
        preferred_window=(30, 120),
        hard_deadline=240,
        resource_class=("disk_light", "browser"),
        conflicts_with=(),
    )


def passing_result() -> ScenarioResult:
    return ScenarioResult(
        scenario_id="cloud_drive_share_001",
        status=FormalResultStatus.PASS,
        terminal_state="success",
        elapsed_seconds=12.5,
        side_effect_assertions={"share_row_created": True, "revoked_share_denied": True},
        cleanup_assertions={"fixture_deleted": True},
        artifact_ids=("cloud_drive_trace_001",),
    )


def test_contract_round_trip_preserves_every_required_field() -> None:
    original = contract()
    payload = original.to_dict()

    assert payload["schema_version"] == SCENARIO_CONTRACT_SCHEMA_VERSION
    assert set(payload) == {
        "schema_version",
        "scenario_id",
        "domain",
        "mandatory",
        "role",
        "preconditions",
        "steps",
        "expected_terminal_state",
        "side_effect_assertions",
        "cleanup_assertions",
        "artifacts",
        "deadline_seconds",
        "earliest_start",
        "preferred_window",
        "hard_deadline",
        "resource_class",
        "conflicts_with",
    }
    assert ScenarioContract.from_dict(payload) == original
    assert original.id == original.scenario_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("steps", ()),
        ("side_effect_assertions", ()),
        ("cleanup_assertions", ()),
        ("artifacts", ()),
        ("deadline_seconds", 0),
        ("deadline_seconds", float("nan")),
        ("role", ""),
        ("mandatory", 1),
        ("earliest_start", -1),
        ("preferred_window", (100, 50)),
        ("preferred_window", (10,)),
        ("hard_deadline", 100),
        ("resource_class", ()),
        ("conflicts_with", ("cloud_drive_share_001",)),
    ],
)
def test_contract_rejects_incomplete_or_ambiguous_design(field: str, value: object) -> None:
    with pytest.raises(ContractValidationError):
        replace(contract(), **{field: value})


def test_contract_from_dict_rejects_unknown_schema_or_shape() -> None:
    payload = contract().to_dict()
    payload["schema_version"] = "future"
    with pytest.raises(ContractValidationError, match="unsupported"):
        ScenarioContract.from_dict(payload)

    payload = contract().to_dict()
    payload["silent_skip"] = True
    with pytest.raises(ContractValidationError, match="shape mismatch"):
        ScenarioContract.from_dict(payload)


def test_existing_operation_coverage_contract_adapts_without_losing_required_evidence() -> None:
    coverage = CAMPAIGN_SCENARIO_CONTRACTS["media_long_hls_share"]
    adapted = ScenarioContract.from_coverage_contract(
        "media_long_hls_share",
        coverage,
        role="user",
        preconditions=("ffmpeg_ready",),
        steps=("upload", "wait_terminal", "verify_hls"),
        expected_terminal_state="ready",
        cleanup_assertions=("media_deleted",),
        artifacts=("media_hls_trace_001",),
        deadline_seconds=3600,
        earliest_start=3600,
        preferred_window=(7200, 10800),
        hard_deadline=14400,
        resource_class=("disk_heavy", "media"),
        conflicts_with=("full_runtime_backup_001",),
    )

    assert adapted.domain == coverage.category
    assert set(adapted.side_effect_assertions) == set(coverage.required_evidence)


def test_schedule_boundaries_and_conflict_references_are_fail_closed() -> None:
    valid = contract()
    assert valid.earliest_start <= valid.preferred_window[0]
    assert valid.hard_deadline >= valid.preferred_window[1]

    other = replace(valid, scenario_id="other_001", conflicts_with=(valid.scenario_id,))
    assert contract_set_errors({valid.scenario_id: valid, other.scenario_id: other}) == ()
    unknown = replace(valid, conflicts_with=("not_reviewed_001",))
    assert contract_set_errors({unknown.scenario_id: unknown}) == (
        "conflicts_reference_unknown_scenarios:cloud_drive_share_001:not_reviewed_001",
    )


def test_formal_result_taxonomy_is_exact_and_only_pass_is_success() -> None:
    assert {status.value for status in FormalResultStatus} == {
        "PASS",
        "FAIL_PRODUCT",
        "FAIL_HARNESS",
        "FAIL_INFRA",
        "FAIL_EXTERNAL",
        "BLOCKED",
        "INVALIDATED",
        "INTERRUPTED",
    }
    assert FormalResultStatus.PASS.is_pass is True
    assert all(not status.is_pass for status in FormalResultStatus if status is not FormalResultStatus.PASS)


@pytest.mark.parametrize(
    "changes",
    [
        {"terminal_state": ""},
        {"side_effect_assertions": {}},
        {"side_effect_assertions": {"share_row_created": False}},
        {"cleanup_assertions": {}},
        {"cleanup_assertions": {"fixture_deleted": False}},
        {"artifact_ids": ()},
        {"diagnostics": ("unexpected",)},
    ],
)
def test_pass_cannot_be_constructed_with_missing_or_failed_evidence(changes: dict[str, object]) -> None:
    with pytest.raises(ContractValidationError):
        replace(passing_result(), **changes)


def test_non_pass_requires_a_diagnostic_reason() -> None:
    with pytest.raises(ContractValidationError, match="requires diagnostics"):
        ScenarioResult(
            scenario_id="cloud_drive_share_001",
            status=FormalResultStatus.FAIL_PRODUCT,
            terminal_state="failed",
            elapsed_seconds=1,
            side_effect_assertions={},
            cleanup_assertions={},
            artifact_ids=(),
        )


def test_result_must_match_contract_terminal_deadline_side_effect_cleanup_and_artifact() -> None:
    result = ScenarioResult(
        scenario_id="cloud_drive_share_001",
        status=FormalResultStatus.PASS,
        terminal_state="wrong_terminal",
        elapsed_seconds=181,
        side_effect_assertions={"different_evidence": True},
        cleanup_assertions={"different_cleanup": True},
        artifact_ids=("different_artifact",),
    )

    assert set(result.contract_errors(contract())) == {
        "terminal_state_mismatch",
        "deadline_exceeded",
        "side_effect_assertions_missing_or_failed:revoked_share_denied,share_row_created",
        "cleanup_assertions_missing_or_failed:fixture_deleted",
        "artifacts_missing:cloud_drive_trace_001",
    }
    assert result.is_contract_pass(contract()) is False
    assert result.to_dict(contract())["contract_pass"] is False


def test_matching_result_is_a_contract_pass() -> None:
    result = passing_result()
    assert result.contract_errors(contract()) == ()
    assert result.is_contract_pass(contract()) is True
    assert result.to_dict(contract())["contract_pass"] is True
    assert ScenarioResult.from_dict(result.to_dict(contract()), contract=contract()) == result


def test_result_readback_rejects_tampered_derived_contract_evidence() -> None:
    payload = passing_result().to_dict(contract())
    payload["contract_pass"] = False
    with pytest.raises(ContractValidationError, match="contract_pass"):
        ScenarioResult.from_dict(payload, contract=contract())


def test_rollup_fails_closed_for_empty_missing_or_nonpass_results() -> None:
    current_contract = contract()
    other_contract = replace(current_contract, scenario_id="other_001")
    assert rollup_formal_status({}, {current_contract.scenario_id: current_contract}) is FormalResultStatus.FAIL_HARNESS
    assert rollup_formal_status({"cloud_drive_share_001": passing_result()}, {}) is FormalResultStatus.FAIL_HARNESS
    assert rollup_formal_status(
        {"cloud_drive_share_001": passing_result()},
        {current_contract.scenario_id: current_contract, other_contract.scenario_id: other_contract},
    ) is FormalResultStatus.FAIL_HARNESS
    assert rollup_formal_status(
        {"cloud_drive_share_001": passing_result()},
        {"wrong_mapping_key": current_contract},
    ) is FormalResultStatus.FAIL_HARNESS

    interrupted = replace(
        passing_result(),
        status=FormalResultStatus.INTERRUPTED,
        diagnostics=("watchdog_stopped_load",),
    )
    assert rollup_formal_status(
        {"cloud_drive_share_001": interrupted},
        {current_contract.scenario_id: current_contract},
    ) is FormalResultStatus.INTERRUPTED

    superficially_passing = replace(passing_result(), terminal_state="wrong_terminal")
    assert rollup_formal_status(
        {"cloud_drive_share_001": superficially_passing},
        {current_contract.scenario_id: current_contract},
    ) is FormalResultStatus.FAIL_HARNESS


def test_rollup_passes_only_when_all_mandatory_scenarios_pass() -> None:
    current_contract = contract()
    assert rollup_formal_status(
        {"cloud_drive_share_001": passing_result()},
        {current_contract.scenario_id: current_contract},
    ) is FormalResultStatus.PASS
    rollup = build_formal_rollup(
        {"cloud_drive_share_001": passing_result()},
        {current_contract.scenario_id: current_contract},
    )
    assert rollup["schema_version"] == SCENARIO_ROLLUP_SCHEMA_VERSION
    assert rollup["formal_pass"] is True
    assert rollup["mandatory_pass_count"] == rollup["mandatory_count"] == 1


def test_rollup_rejects_unreviewed_extra_results() -> None:
    extra = replace(passing_result(), scenario_id="unreviewed_001")
    contracts = {contract().scenario_id: contract()}
    results = {passing_result().scenario_id: passing_result(), extra.scenario_id: extra}
    assert rollup_formal_status(results, contracts) is FormalResultStatus.FAIL_HARNESS
    rollup = build_formal_rollup(results, contracts)
    assert rollup["formal_pass"] is False
    assert "results_reference_unknown_contracts:unreviewed_001" in rollup["errors"]


def test_rollup_rejects_false_pass_claim_even_for_optional_scenario() -> None:
    required = contract()
    optional = replace(
        required,
        scenario_id="optional_001",
        mandatory=False,
        conflicts_with=(),
    )
    false_optional_pass = replace(
        passing_result(),
        scenario_id=optional.scenario_id,
        terminal_state="not_the_reviewed_terminal",
    )
    contracts = {required.scenario_id: required, optional.scenario_id: optional}
    results = {
        required.scenario_id: passing_result(),
        optional.scenario_id: false_optional_pass,
    }
    assert rollup_formal_status(results, contracts) is FormalResultStatus.FAIL_HARNESS
    rollup = build_formal_rollup(results, contracts)
    assert "passing_contract_mismatch:optional_001" in rollup["errors"]
