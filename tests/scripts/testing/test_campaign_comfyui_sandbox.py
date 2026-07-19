from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import signal
import socket
import stat
import sys

import pytest

from scripts.testing import campaign_comfyui_sandbox as sandbox
from scripts.testing.campaign_comfyui_sandbox import (
    ComfyUISandboxConfig,
    ComfyUISandboxError,
    HOST_TRANSITION_SCHEMA_VERSION,
    SANDBOX_PROOF_SCHEMA_VERSION,
    build_seccomp_filter,
    config_from_args,
    evaluate_seccomp_filter_for_test,
    validate_host_transition_payload,
)


NONCE = "0123456789abcdef0123456789abcdef"
CGROUP = "/campaign.scope/comfyui"
BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"


def _identity(path: Path) -> dict[str, object]:
    metadata = path.lstat()
    return {
        "path": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": metadata.st_mode,
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
    }


def _leaf_identity(pid: int) -> dict[str, object]:
    root = Path(f"/sys/fs/cgroup{CGROUP}")
    base = {
        "device": 41,
        "uid": 1000,
        "gid": 1000,
    }
    return {
        "root": {
            **base,
            "path": str(root),
            "inode": 101,
            "mode": stat.S_IFDIR | 0o755,
        },
        "cgroup_procs": {
            **base,
            "path": str(root / "cgroup.procs"),
            "inode": 102,
            "mode": stat.S_IFREG | 0o644,
        },
        "cgroup_events": {
            **base,
            "path": str(root / "cgroup.events"),
            "inode": 103,
            "mode": stat.S_IFREG | 0o444,
        },
        "cgroup_type": "domain",
        "subtree_control": [],
        "subtree_controllers_enabled": False,
        "descendant_cgroups": 0,
        "workload_delegation_capability": "pending_sandbox",
        "current_pid_present": True,
        "ok": True,
    }


def _transition(root: Path, *, pid: int = 4242) -> dict[str, object]:
    return {
        "schema_version": HOST_TRANSITION_SCHEMA_VERSION,
        "nonce": NONCE,
        "pid": pid,
        "role": "comfyui",
        "cgroup_path": CGROUP,
        "leaf_identity": _leaf_identity(pid),
        "process": {
            "pid": pid,
            "start_ticks": 987654,
            "boot_id": BOOT_ID,
            "cgroup_path": CGROUP,
        },
        "placement": {
            "pid": pid,
            "campaign_cgroup": CGROUP,
            "exact_leaf": True,
            "ok": True,
        },
        "cgroup_write": {
            "target": f"/sys/fs/cgroup{CGROUP}/cgroup.procs",
            "attempted": True,
            "completed": True,
            "verified_after_write": True,
            "written_pid": pid,
        },
        "allowed_write_roots": [_identity(root)],
        "created_monotonic_ns": 123456789,
        "actual_execution": True,
        "simulated": False,
        "ok": True,
    }


def _config(tmp_path: Path, **changes: object) -> ComfyUISandboxConfig:
    executable = Path(sys.executable).resolve(strict=True)
    values: dict[str, object] = {
        "host_transition": _transition(tmp_path),
        "nonce": NONCE,
        "expected_cgroup_path": CGROUP,
        "allowed_write_roots": (tmp_path,),
        "command": (str(executable), "-V"),
        "cwd": tmp_path,
        "proof_fd": 9,
        "environment": {"PATH": "/usr/bin", "LANG": "C.UTF-8"},
    }
    values.update(changes)
    return ComfyUISandboxConfig(**values)  # type: ignore[arg-type]


def test_schema_and_seccomp_contract_are_versioned_and_deterministic() -> None:
    assert SANDBOX_PROOF_SCHEMA_VERSION == "hackme.campaign-comfyui-sandbox.v1"
    assert HOST_TRANSITION_SCHEMA_VERSION == "hackme.campaign-comfyui-host-transition.v1"
    first = build_seccomp_filter()
    second = build_seccomp_filter()
    assert first == second
    assert len(first) > 40


def test_transition_contract_accepts_exact_live_binding(tmp_path: Path) -> None:
    payload = _transition(tmp_path)
    result = validate_host_transition_payload(
        payload,
        nonce=NONCE,
        expected_cgroup_path=CGROUP,
        allowed_write_roots=(tmp_path,),
        current_pid=4242,
        current_cgroup_path=CGROUP,
        current_monotonic_ns=123456999,
        current_start_ticks=987654,
        current_boot_id=BOOT_ID,
        current_leaf_identity=_leaf_identity(4242),
    )
    assert result["ok"] is True
    assert result["pid"] == 4242
    assert result["cgroup_path"] == CGROUP
    assert len(result["receipt_sha256"]) == 64
    assert result["allowed_write_roots"] == [_identity(tmp_path)]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"nonce": "f" * 32}, "nonce"),
        ({"pid": 4243}, "PID"),
        ({"role": "primary"}, "role"),
        ({"cgroup_path": "/campaign.scope/other"}, "cgroup"),
        ({"actual_execution": False}, "actual execution"),
        ({"simulated": True}, "simulated"),
        ({"ok": False}, "did not pass"),
    ],
)
def test_transition_contract_rejects_false_authority(
    tmp_path: Path,
    mutation: dict[str, object],
    message: str,
) -> None:
    payload = _transition(tmp_path)
    payload.update(mutation)
    with pytest.raises(ComfyUISandboxError, match=message):
        validate_host_transition_payload(
            payload,
            nonce=NONCE,
            expected_cgroup_path=CGROUP,
            allowed_write_roots=(tmp_path,),
            current_pid=4242,
            current_cgroup_path=CGROUP,
            current_monotonic_ns=123456999,
        )


def test_transition_requires_exact_cgroup_write_target(tmp_path: Path) -> None:
    payload = _transition(tmp_path)
    payload["cgroup_write"] = {
        **payload["cgroup_write"],  # type: ignore[arg-type]
        "target": "/sys/fs/cgroup/not-campaign.scope/comfyui/cgroup.procs",
    }
    with pytest.raises(ComfyUISandboxError, match="cgroup write"):
        validate_host_transition_payload(
            payload,
            nonce=NONCE,
            expected_cgroup_path=CGROUP,
            allowed_write_roots=(tmp_path,),
            current_pid=4242,
            current_cgroup_path=CGROUP,
            current_monotonic_ns=123456999,
        )


def test_transition_pins_write_root_device_inode_and_mode(tmp_path: Path) -> None:
    payload = _transition(tmp_path)
    payload["allowed_write_roots"] = [{**_identity(tmp_path), "inode": tmp_path.stat().st_ino + 1}]
    with pytest.raises(ComfyUISandboxError, match="write-root identities"):
        validate_host_transition_payload(
            payload,
            nonce=NONCE,
            expected_cgroup_path=CGROUP,
            allowed_write_roots=(tmp_path,),
            current_pid=4242,
            current_cgroup_path=CGROUP,
            current_monotonic_ns=123456999,
        )


def test_transition_rejects_stale_receipt_and_changed_leaf_inode(tmp_path: Path) -> None:
    payload = _transition(tmp_path)
    with pytest.raises(ComfyUISandboxError, match="stale"):
        validate_host_transition_payload(
            payload,
            nonce=NONCE,
            expected_cgroup_path=CGROUP,
            allowed_write_roots=(tmp_path,),
            current_pid=4242,
            current_cgroup_path=CGROUP,
            current_monotonic_ns=123456789 + sandbox.MAX_HOST_TRANSITION_AGE_NS + 1,
        )
    changed = _leaf_identity(4242)
    changed["root"] = {**changed["root"], "inode": 999}  # type: ignore[arg-type]
    with pytest.raises(ComfyUISandboxError, match="identity changed"):
        validate_host_transition_payload(
            payload,
            nonce=NONCE,
            expected_cgroup_path=CGROUP,
            allowed_write_roots=(tmp_path,),
            current_pid=4242,
            current_cgroup_path=CGROUP,
            current_monotonic_ns=123456999,
            current_leaf_identity=changed,
        )


def test_host_transition_cannot_claim_workload_delegation_is_already_disabled(
    tmp_path: Path,
) -> None:
    payload = _transition(tmp_path)
    leaf = dict(payload["leaf_identity"])  # type: ignore[arg-type]
    leaf["delegated"] = False
    leaf["workload_delegation_capability"] = False
    payload["leaf_identity"] = leaf
    with pytest.raises(
        ComfyUISandboxError,
        match="pending|self-assert delegated",
    ):
        validate_host_transition_payload(
            payload,
            nonce=NONCE,
            expected_cgroup_path=CGROUP,
            allowed_write_roots=(tmp_path,),
            current_pid=4242,
            current_cgroup_path=CGROUP,
            current_monotonic_ns=123456999,
        )


def _final_delegation_fixture() -> tuple[dict[str, object], dict[str, object]]:
    leaf = _leaf_identity(1)
    shared: dict[str, object] = {
        "mounts": {
            "cgroup2": {
                "filesystem_type": "cgroup2",
                "root": "/",
                "mount_options": ["nodev", "noexec", "nosuid", "ro"],
            },
            "cgroup_namespace_path": "/",
            "leaf_kernel_objects_match": True,
            "bound_read_only_leaf_identity": leaf,
        },
        "landlock": {"ok": True},
        "cgroup_write_denial": {"ok": True},
    }
    seccomp = {
        **sandbox._seccomp_policy_evidence(build_seccomp_filter()),
        "ok": True,
    }
    privileges: dict[str, object] = {
        "capability_sets": {
            name: "0000000000000000"
            for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        },
        "securebits_locked": True,
        "no_new_privileges": True,
        "seccomp": seccomp,
    }
    return shared, privileges


def test_only_complete_sandbox_evidence_disables_workload_delegation() -> None:
    shared, privileges = _final_delegation_fixture()
    result = sandbox._final_workload_delegation_evidence(shared, privileges)
    assert result["workload_delegation_capability"] is False
    assert result["host_leaf_state_before_sandbox"] == "pending_sandbox"
    assert result["cgroup2_read_only"] is True

    shared["mounts"]["cgroup2"]["mount_options"] = ["rw"]  # type: ignore[index]
    with pytest.raises(ComfyUISandboxError, match="read-only cgroup2"):
        sandbox._final_workload_delegation_evidence(shared, privileges)


def test_config_requires_exact_absolute_no_shell_executable(tmp_path: Path) -> None:
    executable = Path(sys.executable).resolve(strict=True)
    config = _config(tmp_path)
    assert config.command == (str(executable), "-V")
    assert config.environment == {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
    with pytest.raises(ComfyUISandboxError, match="absolute"):
        _config(tmp_path, command=("python3", "-V"))
    with pytest.raises(ComfyUISandboxError, match="empty argument"):
        _config(tmp_path, command=(str(executable), ""))


def test_config_rejects_executable_and_write_root_symlinks(tmp_path: Path) -> None:
    executable = Path(sys.executable).resolve(strict=True)
    executable_link = tmp_path / "python-link"
    executable_link.symlink_to(executable)
    with pytest.raises(ComfyUISandboxError, match="canonical|symlink"):
        _config(tmp_path, command=(str(executable_link), "-V"))

    real_root = tmp_path / "real"
    real_root.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ComfyUISandboxError, match="canonical|symlink"):
        _config(
            tmp_path,
            allowed_write_roots=(root_link,),
            host_transition=_transition(real_root),
        )


@pytest.mark.parametrize(
    "root",
    [
        Path("/"),
        Path("/sys"),
        Path("/sys/fs"),
        Path("/proc"),
        Path("/run"),
        Path("/run/user"),
        Path("/mnt/wslg"),
    ],
)
def test_config_rejects_write_roots_that_expose_kernel_or_runtime(root: Path, tmp_path: Path) -> None:
    with pytest.raises(ComfyUISandboxError, match="protected namespace path"):
        _config(tmp_path, allowed_write_roots=(root,))


@pytest.mark.parametrize(
    "value",
    ["/", "relative/path", "/campaign.scope/../escape", "/campaign.scope//comfyui", "/campaign.scope/comfyui/"],
)
def test_config_rejects_non_leaf_or_noncanonical_cgroup_paths(value: str, tmp_path: Path) -> None:
    with pytest.raises(ComfyUISandboxError, match="cgroup"):
        _config(tmp_path, expected_cgroup_path=value)


def test_proof_descriptor_must_be_distinct_write_only_anonymous_pipe() -> None:
    read_fd, write_fd = os.pipe()
    try:
        result = sandbox._validate_descriptor_contract(write_fd)
        assert result["ok"] is True
        assert result["proof_pipe"]["access_mode"] == os.O_WRONLY
        assert result["proof_pipe"]["is_fifo"] is True
        with pytest.raises(ComfyUISandboxError, match="write-only"):
            sandbox._validate_descriptor_contract(read_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    left, right = socket.socketpair()
    try:
        with pytest.raises(ComfyUISandboxError, match="pipe"):
            sandbox._validate_descriptor_contract(left.fileno())
    finally:
        left.close()
        right.close()


def test_seccomp_rejects_compat_and_x32_before_syscall_dispatch() -> None:
    assert evaluate_seccomp_filter_for_test(
        architecture=0x40000003,
        syscall=0,
    ) == sandbox._SECCOMP_RET_KILL_PROCESS
    assert evaluate_seccomp_filter_for_test(
        architecture=sandbox._AUDIT_ARCH_X86_64,
        syscall=sandbox._X32_SYSCALL_BIT,
    ) == sandbox._SECCOMP_RET_KILL_PROCESS


def test_seccomp_denies_every_reviewed_unconditional_syscall() -> None:
    denied = sandbox._SECCOMP_RET_ERRNO | errno.EPERM
    for name in sandbox._UNCONDITIONAL_DENY_NAMES:
        result = evaluate_seccomp_filter_for_test(
            architecture=sandbox._AUDIT_ARCH_X86_64,
            syscall=sandbox._SYSCALLS[name],
        )
        assert result == denied, name


def test_seccomp_denies_clone3_with_enosys_for_libc_fallback() -> None:
    assert "clone3" not in sandbox._UNCONDITIONAL_DENY_NAMES
    assert evaluate_seccomp_filter_for_test(
        architecture=sandbox._AUDIT_ARCH_X86_64,
        syscall=sandbox._SYSCALLS["clone3"],
    ) == sandbox._SECCOMP_RET_ERRNO | errno.ENOSYS


def test_seccomp_denies_unix_sockets_but_keeps_inet_sockets() -> None:
    denied = sandbox._SECCOMP_RET_ERRNO | errno.EPERM
    for name in ("socket", "socketpair"):
        assert evaluate_seccomp_filter_for_test(
            architecture=sandbox._AUDIT_ARCH_X86_64,
            syscall=sandbox._SYSCALLS[name],
            arg0=socket.AF_UNIX,
        ) == denied
        assert evaluate_seccomp_filter_for_test(
            architecture=sandbox._AUDIT_ARCH_X86_64,
            syscall=sandbox._SYSCALLS[name],
            arg0=socket.AF_INET,
        ) == sandbox._SECCOMP_RET_ALLOW


def test_seccomp_denies_namespace_clone_flags_but_allows_plain_clone() -> None:
    denied = sandbox._SECCOMP_RET_ERRNO | errno.EPERM
    for flag in (
        sandbox._CLONE_NEWNS,
        sandbox._CLONE_NEWCGROUP,
        sandbox._CLONE_NEWUTS,
        sandbox._CLONE_NEWIPC,
        sandbox._CLONE_NEWUSER,
        sandbox._CLONE_NEWPID,
        sandbox._CLONE_NEWNET,
        sandbox._CLONE_NEWTIME,
    ):
        assert evaluate_seccomp_filter_for_test(
            architecture=sandbox._AUDIT_ARCH_X86_64,
            syscall=sandbox._SYSCALLS["clone"],
            arg0=signal.SIGCHLD | flag,
        ) == denied
    assert evaluate_seccomp_filter_for_test(
        architecture=sandbox._AUDIT_ARCH_X86_64,
        syscall=sandbox._SYSCALLS["clone"],
        arg0=signal.SIGCHLD,
    ) == sandbox._SECCOMP_RET_ALLOW


def test_cli_contract_preserves_fixed_command_without_shell(tmp_path: Path) -> None:
    executable = Path(sys.executable).resolve(strict=True)
    read_fd, write_fd = os.pipe()
    try:
        arguments = sandbox.build_parser().parse_args([
            "--host-transition-json",
            json.dumps(_transition(tmp_path)),
            "--nonce",
            NONCE,
            "--expected-cgroup-path",
            CGROUP,
            "--allow-write-root",
            str(tmp_path),
            "--cwd",
            str(tmp_path),
            "--proof-fd",
            str(write_fd),
            "--",
            str(executable),
            "-V",
        ])
        config = config_from_args(arguments)
        assert config.command == (str(executable), "-V")
        assert config.proof_fd == write_fd
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_cli_rejects_duplicate_transition_json_keys(tmp_path: Path) -> None:
    executable = Path(sys.executable).resolve(strict=True)
    arguments = sandbox.build_parser().parse_args([
        "--host-transition-json",
        '{"schema_version":"one","schema_version":"two"}',
        "--nonce",
        NONCE,
        "--expected-cgroup-path",
        CGROUP,
        "--allow-write-root",
        str(tmp_path),
        "--cwd",
        str(tmp_path),
        "--proof-fd",
        "9",
        "--",
        str(executable),
        "-V",
    ])
    with pytest.raises(ComfyUISandboxError, match="duplicates key"):
        config_from_args(arguments)
