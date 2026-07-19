from __future__ import annotations

import json

from scripts.testing import operational_campaign_admission as admission
from scripts.testing.operational_campaign_admission import (
    wait_for_dormant_admission,
)


def evidence(
    *,
    ok: bool,
    avg10: float,
    avg60: float,
    tripped: list[str],
) -> dict[str, object]:
    return {
        "at": "now",
        "ok": ok,
        "tripped": tripped,
        "checks": {
            "host_io_pressure": {
                "value": {"avg10": avg10, "avg60": avg60},
            },
        },
    }


def test_dormant_admission_waits_through_hard_io_for_two_safe_samples() -> None:
    samples = iter((
        evidence(
            ok=False,
            avg10=20.0,
            avg60=5.0,
            tripped=[
                "HOST_IO_PRESSURE_HIGH",
                "HOST_IO_PRESSURE_HARD_LIMIT_EXCEEDED",
            ],
        ),
        evidence(ok=True, avg10=2.0, avg60=2.0, tripped=[]),
        evidence(ok=True, avg10=1.0, avg60=1.0, tripped=[]),
    ))
    now = [0.0]

    result = wait_for_dormant_admission(
        timeout_seconds=10,
        collector=lambda: next(samples),
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    assert result["ok"] is True
    assert result["sample_count"] == 3
    assert result["hard_io_samples"] == 1
    assert result["waited_seconds"] == 2.0
    assert result["maximum_io_pressure"] == {"avg10": 20.0, "avg60": 5.0}


def test_dormant_admission_rejects_non_io_failure_immediately() -> None:
    sleeps: list[float] = []
    result = wait_for_dormant_admission(
        timeout_seconds=10,
        collector=lambda: evidence(
            ok=False,
            avg10=0.0,
            avg60=0.0,
            tripped=["HOST_MEMORY_AVAILABLE_LOW"],
        ),
        clock=lambda: 0.0,
        sleeper=sleeps.append,
    )

    assert result["ok"] is False
    assert result["reason"] == "NON_IO_HOST_SAFETY_FAILURE"
    assert result["non_waitable"] == ["HOST_MEMORY_AVAILABLE_LOW"]
    assert result["sample_count"] == 1
    assert sleeps == []


def test_staged_supervisor_loader_gates_each_uncached_dependency(
    monkeypatch,
) -> None:
    target_name = "scripts.testing.operational_campaign_supervisor"
    staged = ("site", "example.supervisor_dependency", target_name)
    monkeypatch.setattr(admission, "_SUPERVISOR_STAGED_MODULES", staged)
    monkeypatch.setattr(admission.sys, "path", list(admission.sys.path))
    for module_name in staged:
        monkeypatch.delitem(admission.sys.modules, module_name, raising=False)

    class Site:
        @staticmethod
        def getsitepackages() -> list[str]:
            return ["/trusted/system-site-packages"]

        @staticmethod
        def main() -> None:
            raise AssertionError("site.main must never be called")

    class Dependency:
        pass

    class Supervisor:
        @staticmethod
        def main(_argv: list[str]) -> int:
            return 0

    modules = {
        "site": Site,
        "example.supervisor_dependency": Dependency,
        target_name: Supervisor,
    }
    imported: list[str] = []

    def import_module(name: str) -> object:
        imported.append(name)
        module = modules[name]
        monkeypatch.setitem(admission.sys.modules, name, module)
        return module

    gate_timeouts: list[float] = []
    monkeypatch.setattr(admission.importlib, "import_module", import_module)
    monkeypatch.setattr(
        admission,
        "_guarded_wait_for_pre_import_safety",
        lambda timeout: gate_timeouts.append(timeout) or True,
    )

    result = admission._load_supervisor_staged(17.0)

    assert result is Supervisor
    assert imported == list(staged)
    assert gate_timeouts == [17.0] * 6
    assert "/trusted/system-site-packages" in admission.sys.path


def test_staged_supervisor_loader_fails_closed_after_import_spike(
    monkeypatch,
) -> None:
    target_name = "scripts.testing.operational_campaign_supervisor"
    dependency_name = "example.spiking_dependency"
    monkeypatch.setattr(
        admission,
        "_SUPERVISOR_STAGED_MODULES",
        (dependency_name, target_name),
    )
    monkeypatch.delitem(admission.sys.modules, dependency_name, raising=False)
    monkeypatch.delitem(admission.sys.modules, target_name, raising=False)
    imported: list[str] = []

    def import_module(name: str) -> object:
        imported.append(name)
        module = object()
        monkeypatch.setitem(admission.sys.modules, name, module)
        return module

    gates = iter((True, False))
    monkeypatch.setattr(admission.importlib, "import_module", import_module)
    monkeypatch.setattr(
        admission,
        "_guarded_wait_for_pre_import_safety",
        lambda _timeout: next(gates),
    )

    assert admission._load_supervisor_staged(17.0) is None
    assert imported == [dependency_name]


def test_main_waits_both_before_and_after_supervisor_import(
    monkeypatch,
    capsys,
) -> None:
    waits: list[dict[str, object]] = []
    supervisor_calls: list[list[str]] = []

    def safe_wait(**kwargs: object) -> dict[str, object]:
        waits.append(kwargs)
        return {
            "ok": True,
            "reason": "HOST_STARTUP_HEADROOM_AVAILABLE",
            "sample_count": 2,
        }

    class Supervisor:
        @staticmethod
        def main(argv: list[str]) -> int:
            supervisor_calls.append(argv)
            return 7

    monkeypatch.setattr(admission, "wait_for_dormant_admission", safe_wait)
    monkeypatch.setattr(
        admission.importlib,
        "import_module",
        lambda name: (
            Supervisor
            if name == "scripts.testing.operational_campaign_supervisor"
            else None
        ),
    )

    result = admission.main(
        [
            "--admission-timeout-seconds",
            "12",
            "--campaign-root",
            "/tmp/campaign",
            "--level",
            "smoke",
        ],
        _allow_unbootstrapped_for_tests=True,
    )

    assert result == 7
    assert waits == [
        {"timeout_seconds": 12.0, "poll_seconds": 1.0},
        {"timeout_seconds": 12.0, "poll_seconds": 1.0},
    ]
    assert supervisor_calls == [[
        "--campaign-root",
        "/tmp/campaign",
        "--level",
        "smoke",
    ]]
    stages = [
        json.loads(line)["stage"]
        for line in capsys.readouterr().out.splitlines()
    ]
    assert stages == ["pre_supervisor_import", "post_supervisor_import"]
