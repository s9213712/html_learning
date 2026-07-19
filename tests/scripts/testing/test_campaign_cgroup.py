from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.testing import campaign_cgroup as cgroup_module
from scripts.testing.campaign_comfyui_sandbox import (
    HOST_TRANSITION_SCHEMA_VERSION,
    validate_host_transition_payload,
)
from scripts.testing.campaign_cgroup import (
    EXEC_FAILURE,
    GIB,
    MIB,
    MANDATORY_MANAGED_ROLES,
    CampaignCgroup,
    CampaignCgroupError,
    CampaignCgroupLimits,
    CgroupUnavailableError,
    CgroupVerificationError,
    _exec_main,
)


SCOPE_PATH = "/user.slice/user-1000.slice/hackme-web-campaign-test.scope"
SUPERVISOR_PATH = "/user.slice/user-1000.slice/session-1.scope"
ANCHOR_PID = 41001
BOOT_ID = "11111111-2222-3333-4444-555555555555"
INVOCATION_ID = "a" * 32


class FakeProcess:
    def __init__(self, pid: int, *, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class FakeRunner:
    def __init__(
        self,
        *,
        scope_path: str,
        scope_fs: Path,
        anchor_pid: int = ANCHOR_PID,
        start_returncode: int | None = None,
        show_returncode: int = 0,
        clear_on_stop: bool = True,
        io_weight: int | str | None = cgroup_module.DEFAULT_IO_WEIGHT,
        io_scheduling_class: str = "idle",
        ionice_output: str = "idle",
    ) -> None:
        self.scope_path = scope_path
        self.scope_fs = scope_fs
        self.anchor_pid = anchor_pid
        self.start_returncode = start_returncode
        self.show_returncode = show_returncode
        self.clear_on_stop = clear_on_stop
        self.io_weight = io_weight
        self.io_scheduling_class = io_scheduling_class
        self.ionice_output = ionice_output
        self.commands: list[list[str]] = []
        self.process: FakeProcess | None = None
        self.stop_calls = 0

    def popen(self, command: list[str], **_kwargs: Any) -> FakeProcess:
        argv = list(command)
        self.commands.append(argv)
        self.process = FakeProcess(self.anchor_pid, returncode=self.start_returncode)
        if self.start_returncode is None:
            pid_file = Path(argv[argv.index("--pid-file") + 1])
            ready_file = Path(argv[argv.index("--ready-file") + 1])
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(f"{self.anchor_pid}\n", encoding="utf-8")
            ready_file.write_text(json.dumps({"pid": self.anchor_pid, "ok": True}), encoding="utf-8")
        return self.process

    def run(self, command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = list(command)
        self.commands.append(argv)
        if "show" in argv:
            properties = [
                "Id=hackme-web-campaign-test.scope",
                "Names=hackme-web-campaign-test.scope",
                "ActiveState=active",
                "SubState=running",
                f"ControlGroup={self.scope_path}",
                f"InvocationID={INVOCATION_ID}",
                "Delegate=yes",
            ]
            if self.io_weight is not None:
                properties.append(f"IOWeight={self.io_weight}")
            properties.append(f"IOSchedulingClass={self.io_scheduling_class}")
            return subprocess.CompletedProcess(
                argv,
                self.show_returncode,
                stdout=(
                    "\n".join(properties)
                    + "\n"
                    if self.show_returncode == 0
                    else ""
                ),
                stderr="" if self.show_returncode == 0 else "no user bus",
            )
        if argv[:2] == ["ionice", "-p"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=self.ionice_output + "\n",
                stderr="",
            )
        if "stop" in argv:
            self.stop_calls += 1
            if self.clear_on_stop and self.scope_fs.exists():
                for procs in self.scope_fs.rglob("cgroup.procs"):
                    procs.write_text("", encoding="utf-8")
                events = self.scope_fs / "cgroup.events"
                if events.exists():
                    counters = {
                        key: value
                        for key, value in (
                            line.split(maxsplit=1)
                            for line in events.read_text(encoding="utf-8").splitlines()
                            if line.strip()
                        )
                    }
                    counters["populated"] = "0"
                    events.write_text(
                        "".join(
                            f"{key} {value}\n"
                            for key, value in sorted(counters.items())
                        ),
                        encoding="utf-8",
                    )
            if self.process is not None:
                self.process.returncode = 0
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {argv}")


def _cgroup_fs(cgroup_root: Path, cgroup_path: str) -> Path:
    return cgroup_root / cgroup_path.lstrip("/")


def _write_proc_identity(
    proc_root: Path,
    pid: int,
    cgroup_path: str,
    *,
    start_ticks: int | None = None,
) -> None:
    proc = proc_root / str(pid)
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "cgroup").write_text(f"0::{cgroup_path}\n", encoding="utf-8")
    stat_tail = [
        "S",
        "1",
        *(["0"] * 17),
        str(start_ticks if start_ticks is not None else pid * 10),
        "0",
        "0",
    ]
    (proc / "stat").write_text(
        f"{pid} (campaign-test) " + " ".join(stat_tail) + "\n",
        encoding="utf-8",
    )
    boot_id = proc_root / "sys" / "kernel" / "random" / "boot_id"
    boot_id.parent.mkdir(parents=True, exist_ok=True)
    boot_id.write_text(BOOT_ID + "\n", encoding="ascii")


def _write_pid_membership(cgroup_root: Path, proc_root: Path, pid: int, cgroup_path: str) -> None:
    _write_proc_identity(proc_root, pid, cgroup_path)
    target = _cgroup_fs(cgroup_root, cgroup_path)
    target.mkdir(parents=True, exist_ok=True)
    procs = target / "cgroup.procs"
    existing = procs.read_text(encoding="utf-8").splitlines() if procs.exists() else []
    values = {int(row) for row in existing if row.strip()}
    values.add(int(pid))
    procs.write_text("".join(f"{value}\n" for value in sorted(values)), encoding="utf-8")


def _prepare_scope(tmp_path: Path, *, scope_path: str = SCOPE_PATH) -> tuple[Path, Path, Path]:
    cgroup_root = tmp_path / "cgroup"
    proc_root = tmp_path / "proc"
    cgroup_root.mkdir()
    proc_root.mkdir()
    (cgroup_root / "cgroup.controllers").write_text("cpu io memory pids\n", encoding="utf-8")
    mountinfo = proc_root / "self" / "mountinfo"
    mountinfo.parent.mkdir(parents=True)
    mountinfo.write_text(
        f"29 23 0:26 / {cgroup_root} rw,nosuid,nodev,noexec,relatime - cgroup2 cgroup2 rw\n",
        encoding="utf-8",
    )
    scope = _cgroup_fs(cgroup_root, scope_path)
    scope.mkdir(parents=True)
    values = {
        "memory.high": str(5 * GIB),
        "memory.max": str(6 * GIB),
        "memory.swap.max": str(512 * MIB),
        "cpu.max": "300000 100000",
        "pids.max": "384",
        "io.weight": f"default {cgroup_module.DEFAULT_IO_WEIGHT}\n",
        "cgroup.events": "populated 1\nfrozen 0\n",
        "cgroup.freeze": "0\n",
        "cgroup.kill": "0\n",
        "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n",
        "memory.swap.events": "high 0\nmax 0\nfail 0\n",
        "pids.events": "max 0\n",
        "cgroup.procs": "",
    }
    for name, value in values.items():
        (scope / name).write_text(value, encoding="utf-8")
    _write_pid_membership(cgroup_root, proc_root, ANCHOR_PID, scope_path)
    _write_pid_membership(cgroup_root, proc_root, os.getpid(), SUPERVISOR_PATH)
    return cgroup_root, proc_root, scope


def _manager(
    tmp_path: Path,
    *,
    scope_path: str = SCOPE_PATH,
    runner_options: dict[str, Any] | None = None,
    manager_options: dict[str, Any] | None = None,
) -> tuple[CampaignCgroup, FakeRunner, Path, Path, Path]:
    cgroup_root, proc_root, scope = _prepare_scope(tmp_path, scope_path=scope_path)
    runner = FakeRunner(scope_path=scope_path, scope_fs=scope, **(runner_options or {}))
    manager = CampaignCgroup(
        campaign_id="test",
        evidence_root=tmp_path / "evidence",
        cgroup_root=cgroup_root,
        proc_root=proc_root,
        runner=runner,
        start_timeout=0.05,
        stop_timeout=0.05,
        poll_interval=0.001,
        **(manager_options or {}),
    )
    return manager, runner, cgroup_root, proc_root, scope


def test_scope_creation_uses_exact_limits_and_proves_actual_cgroup_files(tmp_path: Path) -> None:
    manager, runner, _cgroup_root, _proc_root, _scope = _manager(tmp_path)

    result = manager.create_scope()

    assert result["ok"] is True
    assert result["limit_evidence"]["hard_limit_state"] == "verified"
    assert result["anchor_evidence"]["inside_campaign_scope"] is True
    assert result["supervisor_evidence"]["inside_campaign_scope"] is False
    assert result["scope_identity"]["path"] == SCOPE_PATH
    assert result["scope_identity"]["device"] > 0
    assert result["scope_identity"]["inode"] > 0
    assert result["event_baseline"]["ok"] is True
    assert manager.event_baseline["memory.events"]["oom_kill"] == 0
    command = runner.commands[0]
    for prop in (
        "Delegate=yes",
        f"MemoryHigh={5 * GIB}",
        f"MemoryMax={6 * GIB}",
        f"MemorySwapMax={512 * MIB}",
        "CPUQuota=300%",
        "TasksMax=384",
        f"IOWeight={cgroup_module.DEFAULT_IO_WEIGHT}",
    ):
        assert prop in command
    assert "--scope" in command
    assert "--user" in command
    evidence = json.loads(manager.evidence_file.read_text(encoding="utf-8"))
    assert evidence["sample_schema_version"] == "hackme.campaign-cgroup/v2"
    assert evidence["cgroup_path"] == SCOPE_PATH
    assert evidence["expected_limits"]["io.weight"] == cgroup_module.DEFAULT_IO_WEIGHT
    assert result["limit_evidence"]["checks"]["io.weight"] == {
        "expected": cgroup_module.DEFAULT_IO_WEIGHT,
        "actual": cgroup_module.DEFAULT_IO_WEIGHT,
        "raw": f"default {cgroup_module.DEFAULT_IO_WEIGHT}",
        "ok": True,
    }
    assert result["scope_identity"]["io_weight"] == cgroup_module.DEFAULT_IO_WEIGHT


def test_scope_creation_retries_transient_inactive_systemd_placeholder(
    tmp_path: Path,
) -> None:
    manager, runner, _cgroup_root, _proc_root, _scope = _manager(tmp_path)
    original_run = runner.run
    show_calls = 0

    def transient_run(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal show_calls
        if "show" in command:
            show_calls += 1
            if show_calls == 1:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        "Id=hackme-web-campaign-test.scope\n"
                        "Names=hackme-web-campaign-test.scope\n"
                        "ActiveState=inactive\n"
                        "SubState=dead\n"
                        "ControlGroup=\n"
                        "InvocationID=\n"
                        "Delegate=no\n"
                        "IOWeight=[not set]\n"
                    ),
                    stderr="",
                )
        return original_run(command, **kwargs)

    runner.run = transient_run  # type: ignore[method-assign]

    result = manager.create_scope()

    assert result["ok"] is True
    assert show_calls >= 2


def test_missing_user_slice_io_controller_uses_verified_idle_fallback(
    tmp_path: Path,
) -> None:
    manager, runner, _cgroup_root, _proc_root, scope = _manager(
        tmp_path,
        manager_options={"allow_idle_io_fallback": True},
    )
    (scope / "io.weight").unlink()

    result = manager.create_scope()

    assert result["ok"] is True
    assert result["limit_evidence"]["io_safety_mode"] == "process_idle"
    io_check = result["limit_evidence"]["checks"]["io.weight"]
    assert io_check["ok"] is True
    assert io_check["cgroup_controller_available"] is False
    assert io_check["fallback"]["expected_class"] == "idle"
    assert io_check["fallback"]["stdout"] == "idle"
    separator = runner.commands[0].index("--")
    assert runner.commands[0][separator + 1 : separator + 4] == [
        "ionice",
        "-c",
        "3",
    ]
    assert any(command[:2] == ["ionice", "-p"] for command in runner.commands)


def test_idle_io_fallback_fails_closed_when_live_anchor_is_not_idle(
    tmp_path: Path,
) -> None:
    manager, runner, _cgroup_root, _proc_root, scope = _manager(
        tmp_path,
        runner_options={"ionice_output": "best-effort: prio 4"},
        manager_options={"allow_idle_io_fallback": True},
    )
    (scope / "io.weight").unlink()

    with pytest.raises(CgroupVerificationError, match="idle I/O priority"):
        manager.create_scope()

    assert runner.stop_calls == 1


def test_missing_io_weight_without_explicit_fallback_still_fails_closed(
    tmp_path: Path,
) -> None:
    manager, runner, _cgroup_root, _proc_root, scope = _manager(tmp_path)
    (scope / "io.weight").unlink()

    with pytest.raises(CgroupVerificationError, match="io.weight"):
        manager.create_scope()

    assert runner.stop_calls == 1


def test_limit_mismatch_stops_scope_and_fails_closed(tmp_path: Path) -> None:
    manager, runner, _cgroup_root, _proc_root, scope = _manager(tmp_path)
    (scope / "memory.max").write_text(str(9 * GIB), encoding="utf-8")

    with pytest.raises(CgroupVerificationError, match="memory.max"):
        manager.create_scope()

    assert runner.stop_calls == 1
    evidence = json.loads(manager.evidence_file.read_text(encoding="utf-8"))
    assert evidence["events"][-1]["ok"] is False
    assert manager.created is False


def test_io_weight_mismatch_stops_scope_and_fails_closed(tmp_path: Path) -> None:
    manager, runner, _cgroup_root, _proc_root, scope = _manager(tmp_path)
    (scope / "io.weight").write_text("default 100\n", encoding="utf-8")

    with pytest.raises(CgroupVerificationError, match="io.weight"):
        manager.create_scope()

    assert runner.stop_calls == 1
    assert manager.created is False


def test_io_weight_device_override_is_not_accepted_as_default_authority(
    tmp_path: Path,
) -> None:
    manager, runner, _cgroup_root, _proc_root, scope = _manager(tmp_path)
    (scope / "io.weight").write_text(
        f"default {cgroup_module.DEFAULT_IO_WEIGHT}\n8:0 100\n",
        encoding="utf-8",
    )

    with pytest.raises(CgroupVerificationError, match="exactly one default"):
        manager.create_scope()

    assert runner.stop_calls == 1


@pytest.mark.parametrize(
    ("property_value", "error"),
    (
        (None, "IOWeight authority is invalid"),
        ("not-a-weight", "IOWeight authority is invalid"),
        (100, "IOWeight authority mismatch"),
    ),
)
def test_systemd_io_weight_readback_invalid_or_mismatch_fails_closed(
    tmp_path: Path,
    property_value: int | str | None,
    error: str,
) -> None:
    manager, runner, _cgroup_root, _proc_root, _scope = _manager(
        tmp_path,
        runner_options={"io_weight": property_value},
    )

    with pytest.raises(CgroupVerificationError, match=error):
        manager.create_scope()

    assert runner.stop_calls == 1
    assert manager.created is False


def test_missing_watchdog_control_stops_scope_and_fails_closed(tmp_path: Path) -> None:
    manager, runner, _cgroup_root, _proc_root, scope = _manager(tmp_path)
    (scope / "cgroup.kill").unlink()

    with pytest.raises(CgroupVerificationError, match="cgroup.kill"):
        manager.create_scope()

    assert runner.stop_calls == 1


def test_missing_v2_controller_never_launches_unconstrained_process(tmp_path: Path) -> None:
    manager, runner, cgroup_root, _proc_root, _scope = _manager(tmp_path)
    (cgroup_root / "cgroup.controllers").write_text("cpu pids\n", encoding="utf-8")

    with pytest.raises(CgroupUnavailableError, match="memory"):
        manager.create_scope()

    assert not any("systemd-run" in command[0] for command in runner.commands)


def test_missing_io_controller_never_launches_formal_scope(tmp_path: Path) -> None:
    manager, runner, cgroup_root, _proc_root, _scope = _manager(tmp_path)
    (cgroup_root / "cgroup.controllers").write_text(
        "cpu memory pids\n",
        encoding="utf-8",
    )

    with pytest.raises(CgroupUnavailableError, match="io"):
        manager.create_scope()

    assert not any("systemd-run" in command[0] for command in runner.commands)


def test_non_cgroup2_mount_never_launches_unconstrained_process(tmp_path: Path) -> None:
    manager, runner, _cgroup_root, proc_root, _scope = _manager(tmp_path)
    (proc_root / "self" / "mountinfo").write_text(
        f"29 23 0:26 / {manager.cgroup_root} rw - tmpfs tmpfs rw\n",
        encoding="utf-8",
    )

    with pytest.raises(CgroupUnavailableError, match="not proven to be a cgroup2 mount"):
        manager.create_scope()

    assert not any("systemd-run" in command[0] for command in runner.commands)


def test_systemd_scope_start_failure_has_no_fallback(tmp_path: Path) -> None:
    manager, runner, _cgroup_root, _proc_root, _scope = _manager(
        tmp_path,
        runner_options={"start_returncode": 1},
    )

    with pytest.raises(CgroupUnavailableError, match="exited before"):
        manager.create_scope()

    assert runner.stop_calls == 1
    assert manager.created is False


def test_control_group_parent_traversal_is_rejected(tmp_path: Path) -> None:
    manager, runner, _cgroup_root, _proc_root, _scope = _manager(tmp_path)
    runner.scope_path = "/user.slice/../escaped.scope"

    with pytest.raises(CgroupVerificationError, match="parent traversal"):
        manager.create_scope()

    assert runner.stop_calls == 1


def test_all_required_roles_inside_and_watchdog_outside_are_machine_verified(tmp_path: Path) -> None:
    manager, _runner, cgroup_root, proc_root, _scope = _manager(tmp_path)
    manager.create_scope()
    role_pids: dict[str, list[int]] = {}
    for index, role in enumerate(sorted(MANDATORY_MANAGED_ROLES), start=1):
        pid = 42000 + index
        _write_pid_membership(cgroup_root, proc_root, pid, SCOPE_PATH)
        role_pids[role] = [pid]
    watchdog_pid = 43000
    _write_pid_membership(cgroup_root, proc_root, watchdog_pid, SUPERVISOR_PATH)

    result = manager.verify_process_placement(role_pids, watchdog_pid=watchdog_pid)

    assert result["ok"] is True
    assert set(result["placements"]) == MANDATORY_MANAGED_ROLES
    assert all(rows[0]["inside_campaign_scope"] for rows in result["placements"].values())
    assert result["watchdog"]["inside_campaign_scope"] is False


def test_missing_managed_role_fails_closed(tmp_path: Path) -> None:
    manager, _runner, cgroup_root, proc_root, _scope = _manager(tmp_path)
    manager.create_scope()
    watchdog_pid = 43001
    _write_pid_membership(cgroup_root, proc_root, watchdog_pid, SUPERVISOR_PATH)

    with pytest.raises(CgroupVerificationError, match="missing mandatory roles"):
        manager.verify_process_placement({"primary": [ANCHOR_PID]}, watchdog_pid=watchdog_pid)


def test_misplaced_managed_pid_and_in_scope_watchdog_both_fail(tmp_path: Path) -> None:
    manager, _runner, cgroup_root, proc_root, _scope = _manager(tmp_path)
    manager.create_scope()
    misplaced_pid = 44001
    watchdog_pid = 44002
    _write_pid_membership(cgroup_root, proc_root, misplaced_pid, SUPERVISOR_PATH)
    _write_pid_membership(cgroup_root, proc_root, watchdog_pid, SCOPE_PATH)

    with pytest.raises(CgroupVerificationError) as captured:
        manager.verify_process_placement(
            {"primary": [misplaced_pid]},
            watchdog_pid=watchdog_pid,
            required_roles={"primary"},
        )

    message = str(captured.value)
    assert "primary" in message
    assert "external_watchdog" in message


def test_procfs_cgroupfs_disagreement_is_rejected(tmp_path: Path) -> None:
    manager, _runner, _cgroup_root, proc_root, scope = _manager(tmp_path)
    manager.create_scope()
    pid = 45001
    proc = proc_root / str(pid)
    _write_proc_identity(proc_root, pid, SCOPE_PATH)
    assert str(pid) not in (scope / "cgroup.procs").read_text(encoding="utf-8")

    with pytest.raises(CgroupVerificationError, match="disagreement"):
        manager.assert_pid_membership(pid, role="scenario")


def test_registered_pid_reuse_is_rejected_by_pinned_start_identity(tmp_path: Path) -> None:
    manager, _runner, cgroup_root, proc_root, _scope = _manager(tmp_path)
    manager.create_scope()
    pid = 45002
    _write_pid_membership(cgroup_root, proc_root, pid, SCOPE_PATH)
    manager.register_pid("scenario", pid)

    _write_proc_identity(proc_root, pid, SCOPE_PATH, start_ticks=pid * 10 + 1)

    with pytest.raises(CgroupVerificationError, match="identity changed"):
        manager.assert_pid_membership(
            pid,
            role="scenario",
            expected_identity=manager.registered_identities["scenario"][pid],
        )


def test_resource_failure_counter_delta_blocks_release_gate(tmp_path: Path) -> None:
    manager, _runner, _cgroup_root, _proc_root, scope = _manager(tmp_path)
    manager.create_scope()
    (scope / "memory.events").write_text(
        "low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n",
        encoding="utf-8",
    )

    with pytest.raises(CgroupVerificationError, match="resource-failure counters changed"):
        manager.verify_event_counters_unchanged()

    evidence = json.loads(manager.evidence_file.read_text(encoding="utf-8"))
    last = evidence["events"][-1]
    assert last["action"] == "verify_event_counters_unchanged"
    assert last["ok"] is False
    assert last["deltas"]["memory.events.oom_kill"] == 1


def test_descendant_cgroup_counts_as_inside_campaign_scope(tmp_path: Path) -> None:
    manager, _runner, cgroup_root, proc_root, _scope = _manager(tmp_path)
    manager.create_scope()
    pid = 46001
    descendant = f"{SCOPE_PATH}/browser-worker"
    _write_pid_membership(cgroup_root, proc_root, pid, descendant)

    result = manager.assert_pid_membership(pid, role="browser")

    assert result["actual_cgroup"] == descendant
    assert result["inside_campaign_scope"] is True


def test_wrap_command_enters_scope_before_real_executable(tmp_path: Path) -> None:
    manager, _runner, _cgroup_root, _proc_root, _scope = _manager(tmp_path)
    manager.create_scope()

    command = manager.wrap_command(["ffmpeg", "-version"], role="ffmpeg")

    assert command[2] == "_exec"
    assert command[command.index("--scope-path") + 1] == SCOPE_PATH
    assert command[command.index("--role") + 1] == "ffmpeg"
    separator = command.index("--")
    assert command[separator + 1 :] == ["ffmpeg", "-version"]


def test_wrap_command_refuses_unverified_scope(tmp_path: Path) -> None:
    manager, _runner, _cgroup_root, _proc_root, _scope = _manager(tmp_path)

    with pytest.raises(CampaignCgroupError, match="active verified"):
        manager.wrap_command(["true"], role="scenario")


def test_late_managed_environment_accepts_only_reviewed_comfyui_binding(
    tmp_path: Path,
) -> None:
    manager, _runner, _cgroup_root, _proc_root, _scope = _manager(tmp_path)
    activation = tmp_path / "control" / "activation.json"
    manager.configure_managed_command(
        ["runner", "--formal"],
        activation_gate=activation,
        cwd=tmp_path,
        stdout=tmp_path / "runner.stdout",
        environment={"PYTHONPATH": "/reviewed/source"},
    )
    manager.create_scope()

    result = manager.update_managed_environment_before_activation({
        "HACKME_CAMPAIGN_COMFYUI_API_URL": "http://127.0.0.1:8188",
        "HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT": "/models",
        "HACKME_CAMPAIGN_COMFYUI_BACKEND_PID": "4242",
    })

    payload = json.loads(manager.managed_command_file.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["values_logged"] is False
    assert payload["environment"] == {
        "PYTHONPATH": "/reviewed/source",
        "HACKME_CAMPAIGN_COMFYUI_API_URL": "http://127.0.0.1:8188",
        "HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT": "/models",
        "HACKME_CAMPAIGN_COMFYUI_BACKEND_PID": "4242",
    }
    with pytest.raises(CampaignCgroupError, match="unreviewed keys"):
        manager.update_managed_environment_before_activation({
            "HACKME_CAMPAIGN_ROOT_PASSWORD": "must-not-be-written",
        })
    assert "must-not-be-written" not in manager.managed_command_file.read_text(
        encoding="utf-8"
    )

    activation.parent.mkdir(parents=True, exist_ok=True)
    activation.touch()
    with pytest.raises(CampaignCgroupError, match="after activation"):
        manager.update_managed_environment_before_activation({
            "HACKME_CAMPAIGN_COMFYUI_BACKEND_PID": "5252",
        })


def test_managed_exec_gate_releases_runner_without_releasing_campaign_gate(
    tmp_path: Path,
) -> None:
    manager, _runner, _cgroup_root, _proc_root, _scope = _manager(tmp_path)
    managed_exec_gate = tmp_path / "control" / "campaign.exec.json"
    campaign_activation_gate = tmp_path / "control" / "campaign.activation.json"
    manager.configure_managed_command(
        ["runner", "--activation-gate", str(campaign_activation_gate)],
        activation_gate=managed_exec_gate,
        cwd=tmp_path,
        stdout=tmp_path / "runner.stdout",
    )
    manager.create_scope()

    evidence = manager.release_managed_command()

    payload = json.loads(managed_exec_gate.read_text(encoding="utf-8"))
    assert evidence["ok"] is True
    assert payload["ok"] is True
    assert payload["anchor_pid"] == ANCHOR_PID
    assert len(payload["managed_command_sha256"]) == 64
    assert not campaign_activation_gate.exists()
    with pytest.raises(CampaignCgroupError, match="already exists"):
        manager.release_managed_command()


def test_managed_comfyui_leaf_leaves_delegation_pending_and_wrap_targets_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _runner, _cgroup_root, _proc_root, scope = _manager(tmp_path)
    manager.create_scope()
    original_mkdir = Path.mkdir

    def kernel_leaf_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        original_mkdir(path, *args, **kwargs)
        if path.parent == scope and path.name == "comfyui":
            (path / "cgroup.procs").write_text("", encoding="utf-8")
            (path / "cgroup.events").write_text(
                "populated 0\nfrozen 0\n",
                encoding="utf-8",
            )
            (path / "cgroup.kill").write_text("", encoding="utf-8")
            (path / "cgroup.freeze").write_text("0\n", encoding="utf-8")
            (path / "cgroup.type").write_text("domain\n", encoding="utf-8")
            (path / "cgroup.subtree_control").write_text("", encoding="utf-8")

    monkeypatch.setattr(Path, "mkdir", kernel_leaf_mkdir)

    leaf = manager.create_managed_leaf("comfyui")
    command = manager.wrap_command(
        ["python", "main.py"],
        role="comfyui",
        managed_leaf="comfyui",
    )
    sandbox_command = manager.wrap_command(
        [str(Path(sys.executable).resolve(strict=True)), "-V"],
        role="comfyui",
        managed_leaf="comfyui",
        sandbox_allow_write_roots=(tmp_path,),
        sandbox_proof_fd=9,
        sandbox_nonce="ab" * 16,
    )
    state = manager.managed_leaf_state("comfyui")

    assert "delegated" not in leaf
    assert leaf["subtree_control"] == []
    assert leaf["subtree_controllers_enabled"] is False
    assert leaf["descendant_cgroups"] == 0
    assert leaf["workload_delegation_capability"] == "pending_sandbox"
    assert leaf["cgroup_path"] == f"{SCOPE_PATH}/comfyui"
    assert command[command.index("--scope-path") + 1] == f"{SCOPE_PATH}/comfyui"
    assert sandbox_command[sandbox_command.index("--sandbox-proof-fd") + 1] == "9"
    assert sandbox_command[sandbox_command.index("--sandbox-nonce") + 1] == "ab" * 16
    assert sandbox_command[sandbox_command.index("--sandbox-allow-write") + 1] == str(tmp_path)
    assert not any("landlock" in value for value in sandbox_command)
    with pytest.raises(ValueError, match="requires its managed leaf"):
        manager.wrap_command(
            [str(Path(sys.executable).resolve(strict=True)), "-V"],
            role="comfyui",
            managed_leaf="comfyui",
            sandbox_allow_write_roots=(),
        )
    assert state["pids"] == []
    assert state["populated"] == 0
    assert state["ok"] is True

    leaf_fs = scope / "comfyui"
    (leaf_fs / "cgroup.procs").write_text("48001\n", encoding="utf-8")
    (leaf_fs / "cgroup.events").write_text(
        "populated 1\nfrozen 0\n",
        encoding="utf-8",
    )
    original_write_text = Path.write_text

    def kernel_control_write(
        path: Path,
        data: str,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        written = original_write_text(path, data, *args, **kwargs)
        if path == leaf_fs / "cgroup.kill" and data == "1":
            original_write_text(
                leaf_fs / "cgroup.procs",
                "",
                encoding="utf-8",
            )
            original_write_text(
                leaf_fs / "cgroup.events",
                "populated 0\nfrozen 1\n",
                encoding="utf-8",
            )
        return written

    monkeypatch.setattr(Path, "write_text", kernel_control_write)
    cleanup = manager.kill_managed_leaf("comfyui")

    assert cleanup["before"]["pids"] == [48001]
    assert cleanup["after"]["pids"] == []
    assert cleanup["after"]["populated"] == 0
    assert cleanup["ok"] is True


def test_exec_wrapper_proves_membership_before_exec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cgroup_root = tmp_path / "cgroup"
    proc_root = tmp_path / "proc"
    scope = _cgroup_fs(cgroup_root, SCOPE_PATH)
    scope.mkdir(parents=True)
    (cgroup_root / "cgroup.controllers").write_text("cpu memory pids\n", encoding="utf-8")
    (scope / "cgroup.procs").write_text("", encoding="utf-8")
    pid = 47001
    _write_proc_identity(proc_root, pid, SCOPE_PATH)
    monkeypatch.setattr(os, "getpid", lambda: pid)

    class ExecCalled(BaseException):
        pass

    def fake_exec(executable: str, command: list[str], env: dict[str, str]) -> None:
        assert executable == "tool"
        assert command == ["tool", "arg"]
        assert env
        raise ExecCalled

    monkeypatch.setattr(os, "execvpe", fake_exec)
    args = argparse.Namespace(
        command=["--", "tool", "arg"],
        role="scenario",
        cgroup_root=str(cgroup_root),
        proc_root=str(proc_root),
        scope_path=SCOPE_PATH,
        evidence_dir=str(tmp_path / "evidence"),
    )

    with pytest.raises(ExecCalled):
        _exec_main(args)

    evidence = json.loads((tmp_path / "evidence" / f"scenario_{pid}.json").read_text(encoding="utf-8"))
    assert evidence["ok"] is True
    assert evidence["placement"]["inside_campaign_scope"] is True


def test_exec_wrapper_builds_valid_host_transition_and_exact_execs_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cgroup_root = tmp_path / "cgroup"
    proc_root = tmp_path / "proc"
    leaf_path = f"{SCOPE_PATH}/comfyui"
    leaf = _cgroup_fs(cgroup_root, leaf_path)
    leaf.mkdir(parents=True)
    (cgroup_root / "cgroup.controllers").write_text(
        "cpu memory pids\n",
        encoding="utf-8",
    )
    (leaf / "cgroup.procs").write_text("", encoding="utf-8")
    (leaf / "cgroup.events").write_text(
        "populated 1\nfrozen 0\n",
        encoding="utf-8",
    )
    (leaf / "cgroup.type").write_text("domain\n", encoding="utf-8")
    (leaf / "cgroup.subtree_control").write_text("", encoding="utf-8")
    write_root = tmp_path / "write-root"
    write_root.mkdir()
    pid = 47003
    _write_proc_identity(proc_root, pid, leaf_path)
    monkeypatch.setattr(cgroup_module, "DEFAULT_CGROUP_ROOT", cgroup_root)
    monkeypatch.setattr(cgroup_module, "DEFAULT_PROC_ROOT", proc_root)
    monkeypatch.setattr(os, "getpid", lambda: pid)
    captured: dict[str, Any] = {}

    class ExecCalled(BaseException):
        pass

    def fake_execve(
        executable: str,
        command: list[str],
        environment: dict[str, str],
    ) -> None:
        captured.update({
            "executable": executable,
            "command": command,
            "environment": environment,
        })
        raise ExecCalled

    def forbidden_execvpe(
        _executable: str,
        _command: list[str],
        _environment: dict[str, str],
    ) -> None:
        raise AssertionError("sandbox launch must not use PATH-searching execvpe")

    monkeypatch.setattr(os, "execve", fake_execve)
    monkeypatch.setattr(os, "execvpe", forbidden_execvpe)
    nonce = "cd" * 16
    executable = str(Path(sys.executable).resolve(strict=True))
    args = argparse.Namespace(
        command=["--", executable, "-V"],
        role="comfyui",
        cgroup_root=str(cgroup_root),
        proc_root=str(proc_root),
        scope_path=leaf_path,
        evidence_dir=str(tmp_path / "evidence"),
        sandbox_proof_fd=9,
        sandbox_nonce=nonce,
        sandbox_allow_write=[str(write_root)],
    )

    with pytest.raises(ExecCalled):
        _exec_main(args)

    command = captured["command"]
    assert captured["executable"] == executable
    assert command[0] == executable
    assert command[1] == str(
        Path(cgroup_module.__file__).with_name(
            "campaign_comfyui_sandbox.py"
        ).resolve(strict=True)
    )
    assert command[command.index("--proof-fd") + 1] == "9"
    assert command[command.index("--expected-cgroup-path") + 1] == leaf_path
    assert command[command.index("--allow-write-root") + 1] == str(write_root)
    assert command[command.index("--") + 1 :] == [executable, "-V"]
    transition = json.loads(
        command[command.index("--host-transition-json") + 1]
    )
    assert transition["schema_version"] == HOST_TRANSITION_SCHEMA_VERSION
    assert transition["placement"]["exact_leaf"] is True
    assert transition["cgroup_write"] == {
        "target": f"/sys/fs/cgroup{leaf_path}/cgroup.procs",
        "attempted": True,
        "completed": True,
        "verified_after_write": True,
        "written_pid": pid,
    }
    validated = validate_host_transition_payload(
        transition,
        nonce=nonce,
        expected_cgroup_path=leaf_path,
        allowed_write_roots=(write_root,),
        current_pid=pid,
        current_cgroup_path=leaf_path,
        current_monotonic_ns=transition["created_monotonic_ns"],
        current_start_ticks=pid * 10,
        current_boot_id=BOOT_ID,
        current_leaf_identity=transition["leaf_identity"],
    )
    assert validated["ok"] is True
    evidence = json.loads(
        (tmp_path / "evidence" / f"comfyui_{pid}.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["host_transition"] == transition


def test_exec_wrapper_refuses_sandbox_when_placement_is_only_below_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cgroup_root = tmp_path / "cgroup"
    proc_root = tmp_path / "proc"
    leaf_path = f"{SCOPE_PATH}/comfyui"
    descendant_path = f"{leaf_path}/escaped"
    leaf = _cgroup_fs(cgroup_root, leaf_path)
    descendant = _cgroup_fs(cgroup_root, descendant_path)
    descendant.mkdir(parents=True)
    (cgroup_root / "cgroup.controllers").write_text(
        "cpu memory pids\n",
        encoding="utf-8",
    )
    (leaf / "cgroup.procs").write_text("", encoding="utf-8")
    pid = 47004
    _write_proc_identity(proc_root, pid, descendant_path)
    (descendant / "cgroup.procs").write_text(f"{pid}\n", encoding="utf-8")
    write_root = tmp_path / "write-root"
    write_root.mkdir()
    monkeypatch.setattr(cgroup_module, "DEFAULT_CGROUP_ROOT", cgroup_root)
    monkeypatch.setattr(cgroup_module, "DEFAULT_PROC_ROOT", proc_root)
    monkeypatch.setattr(os, "getpid", lambda: pid)
    executed = False

    def forbidden_exec(*_args: Any, **_kwargs: Any) -> None:
        nonlocal executed
        executed = True

    monkeypatch.setattr(os, "execve", forbidden_exec)
    monkeypatch.setattr(os, "execvpe", forbidden_exec)
    args = argparse.Namespace(
        command=["--", str(Path(sys.executable).resolve(strict=True)), "-V"],
        role="comfyui",
        cgroup_root=str(cgroup_root),
        proc_root=str(proc_root),
        scope_path=leaf_path,
        evidence_dir=str(tmp_path / "evidence"),
        sandbox_proof_fd=9,
        sandbox_nonce="ef" * 16,
        sandbox_allow_write=[str(write_root)],
    )

    assert _exec_main(args) == EXEC_FAILURE
    assert executed is False


def test_exec_wrapper_refuses_to_run_when_membership_cannot_be_proven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cgroup_root = tmp_path / "cgroup"
    proc_root = tmp_path / "proc"
    scope = _cgroup_fs(cgroup_root, SCOPE_PATH)
    outside = _cgroup_fs(cgroup_root, SUPERVISOR_PATH)
    scope.mkdir(parents=True)
    outside.mkdir(parents=True)
    (cgroup_root / "cgroup.controllers").write_text("cpu memory pids\n", encoding="utf-8")
    (scope / "cgroup.procs").write_text("", encoding="utf-8")
    pid = 47002
    proc = proc_root / str(pid)
    _write_proc_identity(proc_root, pid, SUPERVISOR_PATH)
    (outside / "cgroup.procs").write_text(f"{pid}\n", encoding="utf-8")
    monkeypatch.setattr(os, "getpid", lambda: pid)
    executed = False

    def fake_exec(_executable: str, _command: list[str], _env: dict[str, str]) -> None:
        nonlocal executed
        executed = True

    monkeypatch.setattr(os, "execvpe", fake_exec)
    args = argparse.Namespace(
        command=["--", "tool"],
        role="scenario",
        cgroup_root=str(cgroup_root),
        proc_root=str(proc_root),
        scope_path=SCOPE_PATH,
        evidence_dir=str(tmp_path / "evidence"),
    )

    assert _exec_main(args) == EXEC_FAILURE
    assert executed is False


def test_stop_scope_requires_empty_cgroup_evidence(tmp_path: Path) -> None:
    manager, runner, _cgroup_root, _proc_root, _scope = _manager(tmp_path)
    manager.create_scope()

    result = manager.stop_scope()

    assert result["ok"] is True
    assert result["cgroup_empty"] is True
    assert runner.stop_calls == 1
    assert manager.stopped is True


def test_stop_scope_fails_when_population_cannot_be_proven_empty(tmp_path: Path) -> None:
    manager, runner, _cgroup_root, _proc_root, _scope = _manager(
        tmp_path,
        runner_options={"clear_on_stop": False},
    )
    manager.create_scope()

    with pytest.raises(CgroupVerificationError, match="cgroup_empty=False"):
        manager.stop_scope()

    assert runner.stop_calls == 1


def test_limit_contract_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="positive"):
        CampaignCgroupLimits(tasks_max=0)
    with pytest.raises(ValueError, match="cannot exceed"):
        CampaignCgroupLimits(memory_high_bytes=9 * GIB, memory_max_bytes=8 * GIB)
    with pytest.raises(ValueError, match="positive"):
        CampaignCgroupLimits(io_weight=0)
    with pytest.raises(ValueError, match="1..10000"):
        CampaignCgroupLimits(io_weight=10_001)
    with pytest.raises(ValueError, match="must be an integer"):
        CampaignCgroupLimits(io_weight=50.0)  # type: ignore[arg-type]

    assert CampaignCgroupLimits().io_weight == 10
    assert "IOWeight=1" in CampaignCgroupLimits(io_weight=1).systemd_properties()
    assert "IOWeight=10000" in CampaignCgroupLimits(
        io_weight=10_000
    ).systemd_properties()
