from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from scripts.testing import operational_campaign_runner_admission as admission


SAFE = {"avg10": 1.0, "avg60": 2.0}


def observability_evidence(*, avg10: float = 1.0, avg60: float = 2.0) -> dict:
    tripped = []
    if avg10 > 3.0 or avg60 > 3.0:
        tripped.append("HOST_IO_PRESSURE_HIGH")
    if avg10 > 10.0 or avg60 > 10.0:
        tripped.append("HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED")
    return {
        "errors": {},
        "tripped": tripped,
        "checks": {
            "host_io_pressure": {
                "value": {"avg10": avg10, "avg60": avg60},
            },
        },
    }


def test_parse_io_pressure_requires_one_finite_full_row() -> None:
    assert admission.parse_io_pressure(
        "some avg10=4.00 avg60=3.00 avg300=2.00 total=1\n"
        "full avg10=1.25 avg60=2.50 avg300=3.00 total=4\n"
    ) == {"avg10": 1.25, "avg60": 2.5}

    for invalid in (
        "some avg10=1 avg60=1 total=1\n",
        "full avg10=1 total=1\n",
        "full avg10=nan avg60=1 total=1\n",
        "full avg10=-1 avg60=1 total=1\n",
        "full avg10=1 avg60=1\nfull avg10=1 avg60=1\n",
    ):
        with pytest.raises(admission.StagedImportError):
            admission.parse_io_pressure(invalid)


def test_process_start_ticks_parses_parentheses_and_rejects_malformed_stat(
    tmp_path,
) -> None:
    fields = ["S"] + ["0"] * 49
    fields[19] = "987654"
    valid = tmp_path / "stat"
    valid.write_text(
        "4242 (worker ) with spaces) " + " ".join(fields) + "\n",
        encoding="ascii",
    )

    assert admission.read_process_start_ticks(
        str(valid),
        expected_pid=4242,
    ) == 987654

    malformed_rows = (
        "4242 worker S 0 0\n",
        "4242 (worker) S 0 0\n",
        "4242 (worker) " + " ".join(["S"] + ["0"] * 49) + "\n",
    )
    for index, row in enumerate(malformed_rows):
        malformed = tmp_path / f"malformed-{index}.stat"
        malformed.write_text(row, encoding="ascii")
        with pytest.raises(admission.StagedImportError, match="PROCESS_STAT_INVALID"):
            admission.read_process_start_ticks(str(malformed), expected_pid=4242)
    with pytest.raises(admission.StagedImportError, match="PROCESS_STAT_INVALID"):
        admission.read_process_start_ticks(str(valid), expected_pid=4243)


def test_wait_for_io_headroom_waits_only_below_hard_limit() -> None:
    samples = iter((
        {"avg10": 5.0, "avg60": 4.0},
        {"avg10": 2.5, "avg60": 2.75},
    ))
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    result = admission.wait_for_io_headroom(
        stage="test",
        deadline=10.0,
        collector=lambda: next(samples),
        clock=lambda: now[0],
        sleeper=sleep,
        poll_seconds=0.25,
    )

    assert result["sample_count"] == 2
    assert result["maximum"] == {"avg10": 5.0, "avg60": 4.0}
    assert result["admitted"] == {"avg10": 2.5, "avg60": 2.75}
    assert sleeps == [0.25]


def test_wait_for_io_headroom_aborts_hard_pressure_immediately() -> None:
    sleeps: list[float] = []

    with pytest.raises(
        admission.HardIOPressureError,
        match="HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
    ):
        admission.wait_for_io_headroom(
            stage="test",
            deadline=120.0,
            collector=lambda: {"avg10": 10.01, "avg60": 1.0},
            clock=lambda: 0.0,
            sleeper=sleeps.append,
        )

    assert sleeps == []


def test_execute_profile_imports_in_order_switches_collector_and_calls_same_pid(
    monkeypatch,
) -> None:
    target_name = admission.TARGET_MODULES["runner"]
    modules = (
        "os",
        "json",
        "hashlib",
        "scripts.testing.campaign_observability",
        target_name,
    )
    monkeypatch.setitem(admission.PROFILE_MODULES, "runner", modules)
    imports: list[str] = []
    site_calls: list[bool] = []
    target_calls: list[tuple[int, list[str]]] = []
    direct_calls: list[bool] = []
    reviewed_calls: list[bool] = []
    receipts: list[dict] = []
    sleeps: list[float] = []
    now = [0.0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    class Target:
        @staticmethod
        def main(argv: list[str]) -> int:
            target_calls.append((os.getpid(), argv))
            return 17

    observed = SimpleNamespace(
        collect_host_startup_safety_preflight=lambda: (
            reviewed_calls.append(True) or observability_evidence()
        ),
    )
    mapping = {
        "site": SimpleNamespace(
            getsitepackages=lambda: (
                site_calls.append(True) or ["/tmp/test-site-packages"]
            ),
            main=lambda: pytest.fail(
                "site.main must not execute .pth or customization hooks"
            ),
        ),
        "os": os,
        "json": json,
        "hashlib": hashlib,
        "scripts.testing.campaign_observability": observed,
        target_name: Target,
    }

    def importer(name: str, *_args, **_kwargs):
        imports.append(name)
        return mapping[name]

    def writer(_path: str, payload: dict, **_kwargs) -> None:
        receipts.append(payload)

    result = admission.execute_profile(
        profile="runner",
        campaign_uuid="campaign-test-001",
        evidence_path="/tmp/private-run/import.json",
        target_argv=["--password", "do-not-record"],
        initial_sample=SAFE,
        importer=importer,
        direct_collector=lambda: direct_calls.append(True) or dict(SAFE),
        clock=lambda: now[0],
        sleeper=sleep,
        evidence_writer=writer,
    )

    expected_order = ("site",) + modules
    assert result == 17
    assert imports == list(expected_order)
    assert site_calls == [True]
    assert direct_calls
    assert reviewed_calls
    assert target_calls == [(os.getpid(), ["--password", "do-not-record"])]
    assert sleeps.count(admission.IMPORT_PACING_SECONDS) == len(expected_order)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["verified"] is True
    assert receipt["status"] == "PASS"
    assert receipt["campaign_uuid"] == "campaign-test-001"
    assert receipt["profile"] == "runner"
    assert receipt["pid"] == target_calls[0][0]
    assert receipt["process_start_ticks"] > 0
    assert receipt["runner_main_invoked"] is False
    assert receipt["pre_receipt_io_barrier"] == (
        admission.PRE_RECEIPT_BARRIER_MODE
    )
    assert receipt["post_receipt_io_barrier"] == (
        admission.POST_RECEIPT_BARRIER_MODE
    )
    assert receipt["target_module"] == target_name
    assert receipt["module_order"] == list(expected_order)
    expected_digest = hashlib.sha256(
        json.dumps(
            list(expected_order),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    assert receipt["module_order_sha256"] == expected_digest
    expected_binding = {
        "campaign_uuid": "campaign-test-001",
        "profile": "runner",
        "pid": os.getpid(),
        "process_start_ticks": receipt["process_start_ticks"],
        "target_module": target_name,
        "module_order_sha256": expected_digest,
    }
    assert receipt["binding_sha256"] == hashlib.sha256(
        json.dumps(
            expected_binding,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    assert receipt["collector_mode"] == "campaign_observability"
    assert receipt["nested_import_guard"] == {
        "mode": "custom_importer_not_guarded",
        "call_count": 0,
        "calls_loading_modules": 0,
        "maximum_io_pressure": {"avg10": 0.0, "avg60": 0.0},
        "pacing_seconds": admission.IMPORT_PACING_SECONDS,
        "restored_before_receipt": True,
    }
    assert receipt["bootstrap_collector"] == "direct_proc_pressure_io"
    assert receipt["site_initialization_mode"] == (
        "site_paths_only_no_pth_or_customization"
    )
    assert receipt["time_module_bootstrap"] == "preloaded_by_interpreter"
    assert receipt["stage_timeout_seconds"] == 120.0
    serialized = json.dumps(receipt, sort_keys=True)
    assert "do-not-record" not in serialized
    assert "--password" not in serialized
    assert "argv" not in serialized.lower()


def test_execute_profile_hard_failure_exits_without_receipt_or_target(
    monkeypatch,
) -> None:
    target_name = admission.TARGET_MODULES["watchdog"]
    modules = ("os", "json", "hashlib", target_name)
    monkeypatch.setitem(admission.PROFILE_MODULES, "watchdog", modules)
    imports: list[str] = []
    target_calls: list[list[str]] = []
    receipts: list[dict] = []
    collector_calls = [0]

    class Target:
        @staticmethod
        def main(argv: list[str]) -> int:
            target_calls.append(argv)
            return 0

    mapping = {
        "site": SimpleNamespace(
            getsitepackages=lambda: ["/tmp/test-site-packages"]
        ),
        "os": os,
        "json": json,
        "hashlib": hashlib,
        target_name: Target,
    }

    def importer(name: str, *_args, **_kwargs):
        imports.append(name)
        return mapping[name]

    # Initial sample is site pre-admission.  Seven further safe reads complete
    # site + the three receipt-support imports.  The target pre-admission then
    # trips the hard ceiling, before the target module is imported.
    def collector() -> dict[str, float]:
        collector_calls[0] += 1
        if collector_calls[0] == 8:
            return {"avg10": 11.0, "avg60": 2.0}
        return dict(SAFE)

    result = admission.execute_profile(
        profile="watchdog",
        campaign_uuid="campaign-test-002",
        evidence_path="/tmp/private-run/import.json",
        target_argv=["--auth-secret", "do-not-record"],
        initial_sample=SAFE,
        importer=importer,
        direct_collector=collector,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        evidence_writer=lambda _path, payload, **_kwargs: receipts.append(payload),
    )

    assert result == admission.HARD_IO_EXIT_CODE
    assert imports == ["site", "os", "json", "hashlib"]
    assert target_calls == []
    assert receipts == []


@pytest.mark.parametrize(
    "hard_call,expected_receipt_count",
    ((10, 0), (11, 1)),
)
def test_receipt_barriers_block_hard_pressure_before_target_main(
    monkeypatch,
    hard_call: int,
    expected_receipt_count: int,
) -> None:
    target_name = admission.TARGET_MODULES["watchdog"]
    modules = ("os", "json", "hashlib", target_name)
    monkeypatch.setitem(admission.PROFILE_MODULES, "watchdog", modules)
    target_calls: list[list[str]] = []
    receipts: list[dict] = []
    collector_calls = [0]

    class Target:
        @staticmethod
        def main(argv: list[str]) -> int:
            target_calls.append(argv)
            return 0

    mapping = {
        "site": SimpleNamespace(
            getsitepackages=lambda: ["/tmp/test-site-packages"]
        ),
        "os": os,
        "json": json,
        "hashlib": hashlib,
        target_name: Target,
    }

    def collector() -> dict[str, float]:
        collector_calls[0] += 1
        if collector_calls[0] == hard_call:
            return {"avg10": 10.01, "avg60": 1.0}
        return dict(SAFE)

    result = admission.execute_profile(
        profile="watchdog",
        campaign_uuid="campaign-post-receipt-hard",
        evidence_path="/tmp/private-run/import.json",
        target_argv=["--must-not-run"],
        initial_sample=SAFE,
        importer=lambda name, *_args, **_kwargs: mapping[name],
        direct_collector=collector,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        evidence_writer=lambda _path, payload, **_kwargs: receipts.append(payload),
    )

    assert result == admission.HARD_IO_EXIT_CODE
    assert collector_calls == [hard_call]
    assert len(receipts) == expected_receipt_count
    assert target_calls == []


def test_nested_import_guard_aborts_transitive_import_before_receipt_or_main(
    tmp_path,
    monkeypatch,
) -> None:
    parent_name = "staged_guard_parent_fixture"
    child_name = "staged_guard_child_fixture"
    (tmp_path / f"{child_name}.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    (tmp_path / f"{parent_name}.py").write_text(
        f"import {child_name}\n"
        "def main(_argv):\n"
        "    return 29\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(
        admission.PROFILE_MODULES,
        "watchdog",
        ("os", "json", "hashlib", parent_name),
    )
    monkeypatch.setitem(admission.TARGET_MODULES, "watchdog", parent_name)
    receipts: list[dict] = []

    def collector() -> dict[str, float]:
        if child_name in sys.modules:
            return {"avg10": 10.01, "avg60": 1.0}
        return dict(SAFE)

    try:
        result = admission.execute_profile(
            profile="watchdog",
            campaign_uuid="campaign-nested-hard",
            evidence_path="/tmp/private-run/import.json",
            target_argv=[],
            initial_sample=SAFE,
            direct_collector=collector,
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
            evidence_writer=lambda _path, payload, **_kwargs: receipts.append(
                payload
            ),
        )
    finally:
        sys.modules.pop(parent_name, None)
        sys.modules.pop(child_name, None)

    assert result == admission.HARD_IO_EXIT_CODE
    assert receipts == []


def test_execute_profile_import_error_never_calls_target_or_records_error_text(
    monkeypatch,
) -> None:
    target_name = admission.TARGET_MODULES["runner"]
    modules = ("os", "json", "hashlib", "broken.module", target_name)
    monkeypatch.setitem(admission.PROFILE_MODULES, "runner", modules)
    receipts: list[dict] = []
    target_calls: list[list[str]] = []

    class Target:
        @staticmethod
        def main(argv: list[str]) -> int:
            target_calls.append(argv)
            return 0

    mapping = {
        "site": SimpleNamespace(
            getsitepackages=lambda: ["/tmp/test-site-packages"]
        ),
        "os": os,
        "json": json,
        "hashlib": hashlib,
        target_name: Target,
    }

    def importer(name: str, *_args, **_kwargs):
        if name == "broken.module":
            raise RuntimeError("secret-bearing import detail")
        return mapping[name]

    result = admission.execute_profile(
        profile="runner",
        campaign_uuid="campaign-test-003",
        evidence_path="/tmp/private-run/import.json",
        target_argv=[],
        initial_sample=SAFE,
        importer=importer,
        direct_collector=lambda: dict(SAFE),
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        evidence_writer=lambda _path, payload, **_kwargs: receipts.append(payload),
    )

    assert result == 2
    assert target_calls == []
    assert len(receipts) == 1
    assert receipts[0]["failed_module"] == "broken.module"
    assert receipts[0]["failure_reason"] == "RUNTIMEERROR_DURING_STAGED_IMPORT"
    assert "secret-bearing" not in json.dumps(receipts[0])


def test_explicit_time_import_gets_immediate_direct_hard_pressure_check(
    monkeypatch,
) -> None:
    monkeypatch.delitem(admission.sys.modules, "time", raising=False)
    samples: list[bool] = []

    with pytest.raises(admission.HardIOPressureError):
        admission._prepare_time_module(
            initial_sample=dict(SAFE),
            direct_collector=lambda: (
                samples.append(True) or {"avg10": 1.0, "avg60": 10.01}
            ),
        )

    assert samples == [True]


def test_main_returns_dedicated_code_for_initial_hard_pressure(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(admission, "_running_without_site", lambda: True)
    monkeypatch.setattr(
        admission,
        "read_direct_io_pressure",
        lambda: {"avg10": 11.0, "avg60": 1.0},
    )
    monkeypatch.setattr(
        admission,
        "execute_profile",
        lambda **_kwargs: pytest.fail("target staging must not begin"),
    )

    result = admission.main([
        "--profile", "runner",
        "--campaign-uuid", "campaign-hard-initial",
        "--evidence-path", "/tmp/private-run/import.json",
        "--",
    ])

    assert result == admission.HARD_IO_EXIT_CODE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_main_samples_hard_pressure_before_no_site_diagnostic(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(admission, "_running_without_site", lambda: False)
    monkeypatch.setattr(
        admission,
        "read_direct_io_pressure",
        lambda: {"avg10": 1.0, "avg60": 10.01},
    )

    assert admission.main([]) == admission.HARD_IO_EXIT_CODE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_atomic_evidence_is_private_atomic_and_write_once(tmp_path) -> None:
    private_parent = tmp_path / "control"
    private_parent.mkdir(mode=0o700)
    path = private_parent / "staged-import.json"
    payload = {"schema_version": admission.SCHEMA_VERSION, "verified": True}

    admission._atomic_write_evidence(
        str(path),
        payload,
        os_module=os,
        json_module=json,
    )

    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(private_parent.glob(".*.tmp")) == []
    with pytest.raises(FileExistsError):
        admission._atomic_write_evidence(
            str(path),
            {"verified": False},
            os_module=os,
            json_module=json,
        )
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_wrapper_parser_separates_target_arguments_and_defaults() -> None:
    options, target = admission.parse_wrapper_argv([
        "--profile", "watchdog",
        "--campaign-uuid", "campaign-parser-001",
        "--evidence-path", "/tmp/run/private.json",
        "--",
        "--campaign-uuid", "target-value",
        "--auth-secret", "not-for-receipt",
    ])

    assert options == {
        "profile": "watchdog",
        "campaign_uuid": "campaign-parser-001",
        "evidence_path": "/tmp/run/private.json",
        "stage_timeout_seconds": 120.0,
        "poll_seconds": 0.25,
    }
    assert target == [
        "--campaign-uuid", "target-value",
        "--auth-secret", "not-for-receipt",
    ]


def test_reviewed_profiles_are_fixed_unique_and_end_at_their_targets() -> None:
    for profile, modules in admission.PROFILE_MODULES.items():
        assert len(modules) == len(set(modules))
        assert modules[:3] == ("os", "json", "hashlib")
        assert modules[-1] == admission.TARGET_MODULES[profile]
    runner = admission.PROFILE_MODULES["runner"]
    assert runner.index("scripts.testing.campaign_observability") < runner.index(
        admission.TARGET_MODULES["runner"]
    )


def test_target_cannot_reuse_reserved_hard_io_exit_code(monkeypatch) -> None:
    target_name = admission.TARGET_MODULES["watchdog"]
    modules = ("os", "json", "hashlib", target_name)
    monkeypatch.setitem(admission.PROFILE_MODULES, "watchdog", modules)

    class Target:
        @staticmethod
        def main(_argv: list[str]) -> int:
            return admission.HARD_IO_EXIT_CODE

    mapping = {
        "site": SimpleNamespace(
            getsitepackages=lambda: ["/tmp/test-site-packages"]
        ),
        "os": os,
        "json": json,
        "hashlib": hashlib,
        target_name: Target,
    }

    result = admission.execute_profile(
        profile="watchdog",
        campaign_uuid="campaign-reserved-exit",
        evidence_path="/tmp/private-run/import.json",
        target_argv=[],
        initial_sample=SAFE,
        importer=lambda name, *_args, **_kwargs: mapping[name],
        direct_collector=lambda: dict(SAFE),
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        evidence_writer=lambda *_args, **_kwargs: None,
    )

    assert result == 2
