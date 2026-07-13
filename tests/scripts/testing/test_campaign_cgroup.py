from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.testing.campaign_cgroup import (
    EXEC_FAILURE,
    GIB,
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
    ) -> None:
        self.scope_path = scope_path
        self.scope_fs = scope_fs
        self.anchor_pid = anchor_pid
        self.start_returncode = start_returncode
        self.show_returncode = show_returncode
        self.clear_on_stop = clear_on_stop
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
            return subprocess.CompletedProcess(
                argv,
                self.show_returncode,
                stdout=(
                    "\n".join([
                        "Id=hackme-web-campaign-test.scope",
                        "Names=hackme-web-campaign-test.scope",
                        "ActiveState=active",
                        "SubState=running",
                        f"ControlGroup={self.scope_path}",
                        f"InvocationID={INVOCATION_ID}",
                        "Delegate=yes",
                    ])
                    + "\n"
                    if self.show_returncode == 0
                    else ""
                ),
                stderr="" if self.show_returncode == 0 else "no user bus",
            )
        if "stop" in argv:
            self.stop_calls += 1
            if self.clear_on_stop and self.scope_fs.exists():
                for procs in self.scope_fs.rglob("cgroup.procs"):
                    procs.write_text("", encoding="utf-8")
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
        "memory.high": str(7 * GIB),
        "memory.max": str(8 * GIB),
        "memory.swap.max": str(GIB),
        "cpu.max": "600000 100000",
        "pids.max": "768",
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
        f"MemoryHigh={7 * GIB}",
        f"MemoryMax={8 * GIB}",
        f"MemorySwapMax={GIB}",
        "CPUQuota=600%",
        "TasksMax=768",
    ):
        assert prop in command
    assert "--scope" in command
    assert "--user" in command
    evidence = json.loads(manager.evidence_file.read_text(encoding="utf-8"))
    assert evidence["sample_schema_version"] == "hackme.campaign-cgroup/v1"
    assert evidence["cgroup_path"] == SCOPE_PATH


def test_limit_mismatch_stops_scope_and_fails_closed(tmp_path: Path) -> None:
    manager, runner, _cgroup_root, _proc_root, scope = _manager(tmp_path)
    (scope / "memory.max").write_text(str(9 * GIB), encoding="utf-8")

    with pytest.raises(CgroupVerificationError, match="memory.max"):
        manager.create_scope()

    assert runner.stop_calls == 1
    evidence = json.loads(manager.evidence_file.read_text(encoding="utf-8"))
    assert evidence["events"][-1]["ok"] is False
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
