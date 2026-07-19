from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time

import pytest

from scripts.testing.campaign_comfyui_backend import (
    CampaignComfyUIBackend,
    ComfyUIBackendConfig,
    ComfyUIBackendError,
)
from scripts.testing.campaign_cgroup import capture_process_identity


FAKE_CONFINEMENT_WRAPPER = r"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys

proof_fd = int(sys.argv[1])
nonce = sys.argv[2]
cgroup_path = sys.argv[3]
write_roots = json.loads(sys.argv[4])
separator = sys.argv.index('--')
command = sys.argv[separator + 1:]
if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0) != 0:
    raise SystemExit(125)
def record(path):
    value = Path(path)
    metadata = value.lstat()
    return {
        'path': str(value),
        'device': metadata.st_dev,
        'inode': metadata.st_ino,
        'mode': metadata.st_mode,
        'uid': metadata.st_uid,
        'gid': metadata.st_gid,
    }
root_records = [record(path) for path in write_roots]
caps = {name: '0000000000000000' for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')}
denied = {name: index for index, name in enumerate((
    'io_uring_setup', 'io_uring_enter', 'io_uring_register', 'setns', 'unshare',
    'mount', 'umount2', 'move_mount', 'mount_setattr', 'pidfd_getfd', 'ptrace',
    'recvmsg',
))}
privileges = {
    'capability_sets': caps,
    'securebits_locked': True,
    'no_new_privileges': True,
    'seccomp': {'mode': 2, 'unconditional_denied_syscalls': denied, 'ok': True},
    'ok': True,
}
namespace_links = {name: 'fixture:[' + name + ']' for name in ('user', 'mnt', 'cgroup', 'pid')}
child = os.fork()
if child == 0:
    os.close(proof_fd)
    os.execvpe(command[0], command, os.environ.copy())
payload = {
    'schema_version': 'hackme.campaign-comfyui-sandbox.v1',
    'nonce': nonce,
    'actual_execution': True,
    'simulated': False,
    'adopted_external_process': False,
    'shell': False,
    'fixed_command': command,
    'fixed_command_sha256': hashlib.sha256(json.dumps(command, separators=(',', ':')).encode()).hexdigest(),
    'environment_keys': sorted(os.environ),
    'expected_host_cgroup_path': cgroup_path,
    'allowed_write_roots': root_records,
    'launcher': {
        'host_pid': os.getpid(),
        'host_process_group': os.getpgrp(),
        'host_session': os.getsid(0),
        'process_group_leader': True,
    },
    'host_transition': {
        'schema_version': 'hackme.campaign-comfyui-host-transition.v1',
        'nonce': nonce,
        'pid': os.getpid(),
        'start_ticks': 1,
        'boot_id': '00000000-0000-0000-0000-000000000000',
        'cgroup_path': cgroup_path,
        'allowed_write_roots': root_records,
        'ok': True,
    },
    'namespace': {
        'uid_map': [[0, os.geteuid(), 1]],
        'gid_map': [[0, os.getegid(), 1]],
        'setgroups': 'deny',
        'ok': True,
    },
    'namespace_links': namespace_links,
    'mounts': {
        'proc': {'filesystem_type': 'proc', 'root': '/'},
        'cgroup2': {
            'filesystem_type': 'cgroup2', 'root': '/',
            'mount_options': ['ro'], 'super_options': ['nsdelegate'],
        },
        'cgroup_namespace_path': '/',
        'leaf_kernel_objects_match': True,
        'hidden_runtime_paths': [
            {'path': '/run', 'hidden': True},
            {'path': '/mnt/wslg/run', 'hidden': True},
        ],
        'ok': True,
    },
    'landlock': {'abi': 3, 'allowed_write_roots': root_records, 'irreversible': True, 'ok': True},
    'cgroup_write_denial': {'write_open_succeeded': False, 'errno': 13, 'ok': True},
    'workload_delegation_capability': False,
    'workload_delegation_confinement': {
        'workload_delegation_capability': False,
        'namespace_rooted_cgroup2': True,
        'cgroup2_read_only': True,
        'capability_sets_zero': True,
        'namespace_and_mount_syscalls_denied': True,
        'ok': True,
    },
    'reaper': {
        'namespace_pid': 1, 'trusted_pid1_reaper': True,
        'open_fds_after_sync': [0, 1, 2], 'privileges': privileges, 'ok': True,
    },
    'payload': {'namespace_pid': 2},
    'privileges': privileges,
    'descriptor_contract': {
        'proof_pipe': {'is_fifo': True, 'is_socket': False},
        'stdio': [{'is_socket': False} for _ in range(3)],
    },
    'proof_written_before_exec': True,
    'outer_launcher_preserves_process_group': True,
    'reaper_preserves_wait_status': True,
    'ok': True,
}
os.write(proof_fd, (json.dumps(payload, sort_keys=True) + '\n').encode())
os.close(proof_fd)
_, status = os.waitpid(child, 0)
if os.WIFEXITED(status):
    raise SystemExit(os.WEXITSTATUS(status))
raise SystemExit(128 + os.WTERMSIG(status))
"""


def _current_cgroup() -> str:
    for row in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
        parts = row.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            return "/" + parts[2].strip().lstrip("/")
    raise AssertionError("unified cgroup is unavailable")


class FakeCampaignCgroup:
    def __init__(self, working_root: Path, scope_path: str | None = None) -> None:
        self.created = True
        self.stopped = False
        self.scope_path = scope_path or _current_cgroup()
        self.working_root = working_root
        self.registered: list[tuple[str, int]] = []

    def create_managed_leaf(self, role: str) -> dict[str, object]:
        assert role == "comfyui"
        return {
            "role": role,
            "cgroup_path": self.scope_path,
            "subtree_control": [],
            "subtree_controllers_enabled": False,
            "descendant_cgroups": 0,
            "workload_delegation_capability": "pending_sandbox",
            "initial_populated": 0,
            "ok": True,
        }

    def wrap_command(
        self,
        command: tuple[str, ...],
        *,
        role: str,
        managed_leaf: str,
        sandbox_allow_write_roots: tuple[Path, ...],
        sandbox_proof_fd: int,
        sandbox_nonce: str,
    ) -> list[str]:
        assert role == "comfyui"
        assert managed_leaf == "comfyui"
        return [
            sys.executable,
            "-c",
            FAKE_CONFINEMENT_WRAPPER,
            str(sandbox_proof_fd),
            sandbox_nonce,
            self.scope_path,
            json.dumps([str(path) for path in sandbox_allow_write_roots]),
            "--",
            *command,
        ]

    def register_pid(self, role: str, pid: int) -> dict[str, object]:
        self.registered.append((role, pid))
        identity = capture_process_identity(Path("/proc"), pid)
        if identity.cgroup_path != self.scope_path:
            raise ComfyUIBackendError("ComfyUI backend is outside its managed cgroup leaf")
        return {
            "role": role,
            "pid": pid,
            "start_ticks": identity.start_ticks,
            "campaign_cgroup": identity.cgroup_path,
            "ok": True,
        }

    def managed_leaf_state(self, role: str) -> dict[str, object]:
        assert role == "comfyui"
        def live(pid: int) -> bool:
            try:
                state = Path(f"/proc/{pid}/stat").read_text(
                    encoding="utf-8"
                ).rsplit(") ", 1)[1].split()[0]
                return state != "Z"
            except Exception:
                return False

        pids = {
            pid
            for _registered_role, pid in self.registered
            if live(pid)
        }
        process_groups = set()
        for pid in tuple(pids):
            try:
                process_groups.add(os.getpgid(pid))
            except ProcessLookupError:
                pass
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                if os.getpgid(int(path.name)) in process_groups:
                    pids.add(int(path.name))
            except (ProcessLookupError, PermissionError):
                continue
        orphan_file = self.working_root / "orphan.pid"
        if orphan_file.exists():
            try:
                orphan_pid = int(orphan_file.read_text(encoding="utf-8"))
                if live(orphan_pid):
                    pids.add(orphan_pid)
            except Exception:
                pass
        return {
            "role": role,
            "cgroup_path": self.scope_path,
            "pids": sorted(pids),
            "populated": 1 if pids else 0,
            "consistent": True,
            "subtree_control": [],
            "descendant_cgroups": 0,
            "topology_intact": True,
            "ok": True,
        }

    def kill_managed_leaf(self, role: str) -> dict[str, object]:
        before = self.managed_leaf_state(role)
        for pid in before["pids"]:
            try:
                os.kill(int(pid), 9)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 2.0
        after = self.managed_leaf_state(role)
        while after["pids"] and time.monotonic() < deadline:
            time.sleep(0.02)
            after = self.managed_leaf_state(role)
        return {"role": role, "before": before, "after": after, "ok": not after["pids"]}


class SyntheticSandboxBackend(CampaignComfyUIBackend):
    """Exercise backend lifecycle while namespace mechanics stay in sandbox tests."""

    def _process_contract(self, pid: int) -> dict[str, object]:
        identity = capture_process_identity(Path("/proc"), pid)
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
        cwd = Path(f"/proc/{pid}/cwd").resolve(strict=True)
        command = [
            value.decode("utf-8", errors="surrogateescape")
            for value in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if value
        ]
        if executable != self.config.python_executable:
            raise ComfyUIBackendError("ComfyUI backend executable differs")
        if cwd != self.config.working_root:
            raise ComfyUIBackendError("ComfyUI backend cwd differs")
        if command != list(self.command()):
            raise ComfyUIBackendError("ComfyUI backend cmdline differs from the fixed reviewed command")
        if os.getpgid(pid) != self.process_group:
            raise ComfyUIBackendError("ComfyUI backend left its process group")
        metadata = self.config.models_root.lstat()
        zero_caps = {
            name: "0000000000000000"
            for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        }
        return {
            "pid": identity.pid,
            "start_ticks": identity.start_ticks,
            "boot_id": identity.boot_id,
            "cgroup_path": identity.cgroup_path,
            "cwd": str(cwd),
            "executable": str(executable),
            "process_group": self.process_group,
            "no_new_privileges": True,
            "seccomp_mode": 2,
            "capability_sets": zero_caps,
            "namespace_pids": [pid, 2],
            "namespace_links": {
                name: f"fixture:[{name}]"
                for name in ("user", "mnt", "cgroup", "pid")
            },
            "models_binding": {
                "entry_path": str(self.config.working_root / "models"),
                "realpath": str(self.config.models_root),
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "mode": stat.S_IMODE(metadata.st_mode),
                "uid": metadata.st_uid,
                "gid": metadata.st_gid,
                "symlink": False,
                "ok": True,
            },
            "ok": True,
        }

    def _live_sandbox_authority(
        self,
        *,
        backend_pid: int,
        process_evidence: dict[str, object],
    ) -> dict[str, object]:
        assert self.process is not None
        state = self.cgroup.managed_leaf_state("comfyui")
        return {
            "launcher_pid": self.process.pid,
            "backend_host_pid": backend_pid,
            "process_group": self.process_group,
            "namespace_links": process_evidence["namespace_links"],
            "namespace_pid": 2,
            "leaf_pids": state["pids"],
            "workload_delegation_capability": False,
            "ok": True,
        }


def _free_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()
    return port


def _fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    (root / "models").mkdir()
    (root / "main.py").write_text(
        """
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--listen', required=True)
parser.add_argument('--port', required=True, type=int)
parser.add_argument('--disable-auto-launch', action='store_true')
args = parser.parse_args()

if Path('spawn_orphan').exists():
    orphan = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(60)'],
        start_new_session=True,
        env={},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    Path('orphan.pid').write_text(str(orphan.pid), encoding='utf-8')

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/system_stats':
            self.send_response(404)
            self.end_headers()
            return
        payload = (
            {}
            if Path('invalid_readiness').exists()
            else {
                'system': {'python_version': sys.version},
                'devices': [{'name': 'campaign-fake-cpu', 'type': 'cpu'}],
            }
        )
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
    def log_message(self, *_args):
        pass

bind_host = '0.0.0.0' if Path('wildcard_listener').exists() else args.listen
HTTPServer((bind_host, args.port), Handler).serve_forever()
""".lstrip(),
        encoding="utf-8",
    )
    return root


def _config(
    root: Path,
    *,
    port: int | None = None,
    timeout: float = 5.0,
) -> ComfyUIBackendConfig:
    selected_port = port or _free_port()
    return ComfyUIBackendConfig(
        python_executable=Path(sys.executable).resolve(strict=True),
        main_path=(root / "main.py").resolve(strict=True),
        working_root=root.resolve(strict=True),
        models_root=(root / "models").resolve(strict=True),
        api_url=f"http://127.0.0.1:{selected_port}",
        port=selected_port,
        readiness_timeout_seconds=timeout,
        poll_interval_seconds=0.02,
    )


def _backend(tmp_path: Path, config: ComfyUIBackendConfig, *, scope: str | None = None):
    cgroup = FakeCampaignCgroup(config.working_root, scope)
    backend = SyntheticSandboxBackend(
        config=config,
        campaign_cgroup=cgroup,
        evidence_root=(tmp_path / "evidence").resolve(),
    )
    return backend, cgroup


def test_managed_backend_proves_process_listener_readiness_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _fixture_root(tmp_path)
    backend, cgroup = _backend(tmp_path, _config(root))

    ready = backend.start()

    assert ready["ok"] is True
    assert ready["actual_execution"] is True
    assert ready["adopted_external_pid"] is False
    assert cgroup.registered == [("comfyui", ready["backend_pid"])]
    assert backend.check_live()["listener"]["loopback_only"] is True
    assert backend.runner_environment() == {
        "HACKME_CAMPAIGN_COMFYUI_API_URL": backend.config.api_url,
        "HACKME_CAMPAIGN_COMFYUI_MODELS_ROOT": str(root / "models"),
        "HACKME_CAMPAIGN_COMFYUI_BACKEND_PID": str(ready["backend_pid"]),
    }
    assert stat.S_IMODE(backend.ready_receipt_path.stat().st_mode) == 0o400
    receipt = json.loads(backend.ready_receipt_path.read_text(encoding="utf-8"))
    models_metadata = (root / "models").stat()
    assert receipt["models_binding"]["realpath"] == str(root / "models")
    assert receipt["models_binding"]["inode"] == models_metadata.st_ino
    assert receipt["models_binding"]["device"] == models_metadata.st_dev
    assert "delegated" not in receipt["managed_leaf"]
    assert receipt["managed_leaf"]["workload_delegation_capability"] is False

    reviewed_command = backend.command()
    monkeypatch.setattr(
        backend,
        "command",
        lambda: (*reviewed_command, "--unexpected-argument"),
    )
    with pytest.raises(ComfyUIBackendError, match="fixed reviewed command"):
        backend.check_live()
    monkeypatch.setattr(backend, "command", lambda: reviewed_command)

    stopped = backend.stop(reason="test_complete")

    assert stopped["ok"] is True
    assert stopped["process_group_empty"] is True
    assert stopped["port_released"] is True
    assert stopped["orphan_free"] is True


def test_ready_receipt_rejects_symlink_replacement_on_contract_readback(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    backend, _cgroup = _backend(tmp_path, _config(root))
    backend.start()
    replacement = tmp_path / "replacement-ready.json"
    replacement.write_text("{}\n", encoding="utf-8")
    backend.ready_receipt_path.unlink()
    backend.ready_receipt_path.symlink_to(replacement)

    with pytest.raises(ComfyUIBackendError, match="nofollow"):
        backend.contract_evidence()

    backend.ready_receipt_path.unlink()
    stopped = backend.stop(reason="receipt_tamper_test")
    assert stopped["ok"] is True


def test_config_rejects_path_escape_and_symlink(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    outside = tmp_path / "main.py"
    outside.write_text("pass\n", encoding="utf-8")
    with pytest.raises(ComfyUIBackendError, match="directly inside"):
        ComfyUIBackendConfig(
            python_executable=Path(sys.executable).resolve(strict=True),
            main_path=outside.resolve(strict=True),
            working_root=root.resolve(strict=True),
            models_root=(root / "models").resolve(strict=True),
            api_url="http://127.0.0.1:8188",
            port=8188,
        )
    link = root / "linked-main.py"
    link.symlink_to(root / "main.py")
    with pytest.raises(ComfyUIBackendError, match="canonical realpath"):
        ComfyUIBackendConfig(
            python_executable=Path(sys.executable).resolve(strict=True),
            main_path=link,
            working_root=root.resolve(strict=True),
            models_root=(root / "models").resolve(strict=True),
            api_url="http://127.0.0.1:8188",
            port=8188,
        )


def test_config_binds_real_working_models_directory_without_symlink(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    unrelated_models = tmp_path / "unrelated-models"
    unrelated_models.mkdir()
    with pytest.raises(ComfyUIBackendError, match="exactly working_root/models"):
        ComfyUIBackendConfig(
            python_executable=Path(sys.executable).resolve(strict=True),
            main_path=(root / "main.py").resolve(strict=True),
            working_root=root.resolve(strict=True),
            models_root=unrelated_models.resolve(strict=True),
            api_url="http://127.0.0.1:8188",
            port=8188,
        )

    (root / "models").rmdir()
    (root / "models").symlink_to(unrelated_models, target_is_directory=True)
    with pytest.raises(ComfyUIBackendError, match="cannot be a symlink"):
        ComfyUIBackendConfig(
            python_executable=Path(sys.executable).resolve(strict=True),
            main_path=(root / "main.py").resolve(strict=True),
            working_root=root.resolve(strict=True),
            models_root=unrelated_models.resolve(strict=True),
            api_url="http://127.0.0.1:8188",
            port=8188,
        )

@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:8188",
        "http://192.168.1.2:8188",
        "http://localhost:8188",
        "https://127.0.0.1:8188",
        "http://user:pass@127.0.0.1:8188",
    ],
)
def test_config_rejects_non_loopback_or_non_origin_url(tmp_path: Path, url: str) -> None:
    root = _fixture_root(tmp_path)
    with pytest.raises(ComfyUIBackendError):
        ComfyUIBackendConfig(
            python_executable=Path(sys.executable).resolve(strict=True),
            main_path=(root / "main.py").resolve(strict=True),
            working_root=root.resolve(strict=True),
            models_root=(root / "models").resolve(strict=True),
            api_url=url,
            port=8188,
        )


def test_backend_rejects_preexisting_wrong_listener(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    backend, _cgroup = _backend(tmp_path, _config(root, port=port))
    try:
        with pytest.raises(ComfyUIBackendError, match="already has a listener"):
            backend.start()
    finally:
        listener.close()
    assert backend.process is None


def test_backend_rejects_preexisting_ipv6_listener_on_same_port(
    tmp_path: Path,
) -> None:
    if not socket.has_ipv6:
        pytest.skip("IPv6 is unavailable")
    root = _fixture_root(tmp_path)
    listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        listener.bind(("::1", 0))
        listener.listen()
    except OSError as exc:
        listener.close()
        pytest.skip(f"IPv6 loopback listener is unavailable: {exc}")
    port = int(listener.getsockname()[1])
    backend, _cgroup = _backend(tmp_path, _config(root, port=port))
    try:
        with pytest.raises(ComfyUIBackendError, match="already has a listener"):
            backend.start()
    finally:
        listener.close()
    assert backend.process is None


def test_backend_rejects_wrong_cgroup_and_cleans_process(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    backend, _cgroup = _backend(
        tmp_path,
        _config(root, timeout=0.2),
        scope="/definitely-not-the-current-cgroup",
    )

    try:
        with pytest.raises(ComfyUIBackendError, match="readiness timed out"):
            backend.start()
    finally:
        # Ownership-safe fixture cleanup: only the launcher-created process
        # group and its campaign leaf can be touched if the assertion itself
        # regresses and start() unexpectedly returns ready.
        if backend.process is not None and backend.process.poll() is None:
            backend.stop(reason="wrong_cgroup_fixture_finalizer")

    assert backend.process is not None
    assert backend.process.poll() is not None
    assert backend._process_group_pids(backend.process_group) == set()
    assert not backend._instance_pids()
    assert not [
        row
        for row in backend._tcp_listeners()
        if row["port"] == backend.config.port
    ]
    leaf_state = backend.cgroup.managed_leaf_state("comfyui")
    assert leaf_state["pids"] == []
    assert leaf_state["populated"] == 0


def test_backend_readiness_timeout_is_fail_closed(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    (root / "invalid_readiness").touch()
    backend, _cgroup = _backend(tmp_path, _config(root, timeout=0.25))

    with pytest.raises(ComfyUIBackendError, match="readiness timed out"):
        backend.start()

    assert backend.process is not None
    assert backend.process.poll() is not None
    assert not backend._instance_pids()


def test_backend_rejects_non_loopback_listener_even_when_readiness_responds(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    (root / "wildcard_listener").touch()
    backend, _cgroup = _backend(tmp_path, _config(root, timeout=0.25))

    with pytest.raises(ComfyUIBackendError, match="readiness timed out"):
        backend.start()

    lifecycle = json.loads(backend.lifecycle_path.read_text(encoding="utf-8"))
    assert lifecycle["events"][0]["action"] == "startup_failed"
    assert backend.process is not None and backend.process.poll() is not None


def test_backend_detects_and_removes_escaped_orphan(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    (root / "spawn_orphan").touch()
    backend, _cgroup = _backend(tmp_path, _config(root))
    backend.start()
    try:
        orphan_pid = int((root / "orphan.pid").read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2.0
        while orphan_pid in backend._instance_pids() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert orphan_pid not in backend._instance_pids()
        assert orphan_pid in set(
            backend.cgroup.managed_leaf_state("comfyui")["pids"]
        )

        stopped = backend.stop(reason="orphan_test")
    finally:
        if backend.process is not None and backend.process.poll() is None:
            backend.stop(reason="orphan_test_finalizer")

    assert stopped["ok"] is False
    assert stopped["escaped_managed_leaf_pids"]
    assert stopped["managed_leaf_after"]["pids"] == []
    assert stopped["managed_leaf_after"]["populated"] == 0
    assert backend._instance_pids() == set()


def test_cleanup_rejects_same_instance_process_outside_managed_leaf(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    backend, _cgroup = _backend(tmp_path, _config(root))
    backend.start()
    outsider = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={
            "HACKME_CAMPAIGN_COMFYUI_INSTANCE_ID": backend.instance_id,
        },
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    try:
        stopped = backend.stop(reason="outside_instance_test")
        assert stopped["ok"] is False
        assert outsider.pid in stopped["remaining_instance_pids"]
    finally:
        if outsider.poll() is None:
            os.killpg(outsider.pid, signal.SIGKILL)
            outsider.wait(timeout=5)


def test_cleanup_rejects_backend_that_exited_before_planned_stop(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    backend, _cgroup = _backend(tmp_path, _config(root))
    backend.start()
    os.killpg(backend.process_group, signal.SIGKILL)
    assert backend.process is not None
    backend.process.wait(timeout=5)

    stopped = backend.stop(reason="planned_cleanup_after_unexpected_exit")

    assert stopped["ok"] is False
    assert stopped["unexpected_pre_stop_exit"] is True


def test_backend_environment_is_allowlisted_and_contains_no_supervisor_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _fixture_root(tmp_path)
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "must-not-cross-boundary")
    monkeypatch.setenv("HACKME_CAMPAIGN_ROOT_PASSWORD", "must-not-cross-boundary")
    backend, _cgroup = _backend(tmp_path, _config(root))

    environment = backend.controlled_environment()

    assert "SUPER_SECRET_TOKEN" not in environment
    assert "HACKME_CAMPAIGN_ROOT_PASSWORD" not in environment
    assert "must-not-cross-boundary" not in environment.values()
    assert not any("PASSWORD" in key or "TOKEN" in key or "SECRET" in key for key in environment)
    assert "/bin/sh" not in backend.command()
