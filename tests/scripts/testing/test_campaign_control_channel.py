from __future__ import annotations

import os
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from scripts.testing.campaign_control_channel import (
    HELLO_SCHEMA_VERSION,
    ControlChannelError,
    PeerIdentity,
    authenticate_connection,
    capture_process_binding,
    create_server,
    receive_packet,
    send_hello,
    send_packet,
    sign_authenticated_payload,
    socket_permissions,
    verify_authenticated_payload,
)
from scripts.testing.campaign_watchdog import capture_process_identity
from scripts.testing.operational_campaign_supervisor import (
    OperationalCampaignSupervisor,
    SupervisorConfig,
    SupervisorError,
)


CAMPAIGN_UUID = "campaign-control-channel-test-001"
CHALLENGE = "a" * 64
CLIENT_NONCE = "b" * 64


@pytest.fixture
def short_socket_root() -> Any:
    # AF_UNIX paths are limited to 107 usable bytes on Linux.  The repository's
    # isolated pytest wrapper intentionally creates a descriptive (long) TMPDIR,
    # so control-channel tests need a separately bounded socket root.
    with tempfile.TemporaryDirectory(prefix=".hcc-", dir="/tmp") as root:
        yield Path(root)


def private_server(root: Path) -> tuple[socket.socket, Path]:
    parent = root / "private"
    parent.mkdir(mode=0o700)
    path = parent / "control.sock"
    return create_server(path), path


def test_challenge_hello_ack_round_trip_is_one_time_and_private(
    short_socket_root: Path,
) -> None:
    server, path = private_server(short_socket_root)
    result: dict[str, Any] = {}

    def accept() -> None:
        connection, _address = server.accept()
        try:
            result.update(authenticate_connection(
                connection,
                expected_campaign=CAMPAIGN_UUID,
                expected_peer=PeerIdentity(os.getpid(), os.getuid(), os.getgid()),
                timeout=2.0,
                challenge_nonce=CHALLENGE,
            ))
        finally:
            connection.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    client = send_hello(path, campaign_uuid=CAMPAIGN_UUID, timeout=2.0)
    thread.join(timeout=2.0)
    try:
        assert not thread.is_alive()
        assert result["ok"] is True
        assert result["one_time"] is True
        assert result["acknowledged"] is True
        assert client["acknowledged"] is True
        permissions = socket_permissions(path)
        assert permissions["mode"] == "0o600"
        assert stat.S_ISSOCK(path.lstat().st_mode)
    finally:
        server.close()
        path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "mutate,match",
    (
        (lambda payload: payload.update(pid=os.getpid() + 1), "pid"),
        (lambda payload: payload.update(campaign_uuid="wrong-campaign"), "campaign"),
        (lambda payload: payload.update(challenge_nonce="c" * 64), "challenge"),
    ),
)
def test_wrong_pid_uuid_or_replayed_nonce_is_rejected(
    mutate: Callable[[dict[str, Any]], None],
    match: str,
) -> None:
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    errors: list[BaseException] = []

    def authenticate() -> None:
        try:
            authenticate_connection(
                server,
                expected_campaign=CAMPAIGN_UUID,
                expected_peer=PeerIdentity(os.getpid(), os.getuid(), os.getgid()),
                timeout=1.0,
                challenge_nonce=CHALLENGE,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=authenticate, daemon=True)
    thread.start()
    challenge = receive_packet(client)
    payload = {
        "schema_version": HELLO_SCHEMA_VERSION,
        "campaign_uuid": CAMPAIGN_UUID,
        "pid": os.getpid(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "challenge_nonce": challenge["challenge_nonce"],
        "client_nonce": CLIENT_NONCE,
    }
    mutate(payload)
    send_packet(client, payload)
    thread.join(timeout=2.0)
    server.close()
    client.close()

    assert len(errors) == 1
    assert isinstance(errors[0], ControlChannelError)
    assert match in str(errors[0]).lower()


def test_same_uid_impostor_is_rejected_by_kernel_pid() -> None:
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        with pytest.raises(ControlChannelError, match="SO_PEERCRED"):
            authenticate_connection(
                server,
                expected_campaign=CAMPAIGN_UUID,
                expected_peer=PeerIdentity(
                    os.getpid() + 100_000,
                    os.getuid(),
                    os.getgid(),
                ),
                timeout=0.1,
            )
    finally:
        server.close()
        client.close()


def test_malformed_identity_claim_is_a_controlled_rejection() -> None:
    peer = PeerIdentity(os.getpid(), os.getuid(), os.getgid())
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    errors: list[BaseException] = []

    def authenticate() -> None:
        try:
            authenticate_connection(
                server,
                expected_campaign=CAMPAIGN_UUID,
                expected_peer=peer,
                timeout=1.0,
                challenge_nonce=CHALLENGE,
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=authenticate, daemon=True)
    thread.start()
    challenge = receive_packet(client)
    send_packet(client, {
        "schema_version": HELLO_SCHEMA_VERSION,
        "campaign_uuid": CAMPAIGN_UUID,
        "pid": [],
        "uid": os.getuid(),
        "gid": os.getgid(),
        "challenge_nonce": challenge["challenge_nonce"],
        "client_nonce": CLIENT_NONCE,
    })
    thread.join(timeout=2.0)
    server.close()
    client.close()

    assert len(errors) == 1
    assert isinstance(errors[0], ControlChannelError)
    assert "hello pid is not an integer" in str(errors[0])


def test_disconnected_peer_send_is_a_controlled_rejection() -> None:
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client.close()
    try:
        with pytest.raises(ControlChannelError, match="send failed"):
            send_packet(server, {"schema_version": "test"})
    finally:
        server.close()


def test_authenticated_payload_rejects_tamper_replay_and_stream_swap() -> None:
    secret = bytes.fromhex("1" * 64)
    payload = {
        "schema_version": "test.v1",
        "campaign_uuid": CAMPAIGN_UUID,
        "value": 7,
    }
    signed = sign_authenticated_payload(
        payload,
        session_secret=secret,
        campaign_uuid=CAMPAIGN_UUID,
        stream="runner_heartbeat",
        sequence=4,
        monotonic_ns=123,
    )
    evidence = verify_authenticated_payload(
        signed,
        session_secret=secret,
        expected_campaign_uuid=CAMPAIGN_UUID,
        expected_stream="runner_heartbeat",
        previous_sequence=3,
    )
    assert evidence["mac_verified"] is True
    with pytest.raises(ControlChannelError, match="digest|MAC"):
        verify_authenticated_payload(
            {**signed, "value": 8},
            session_secret=secret,
            expected_campaign_uuid=CAMPAIGN_UUID,
            expected_stream="runner_heartbeat",
        )
    with pytest.raises(ControlChannelError, match="regressed"):
        verify_authenticated_payload(
            signed,
            session_secret=secret,
            expected_campaign_uuid=CAMPAIGN_UUID,
            expected_stream="runner_heartbeat",
            previous_sequence=5,
        )
    with pytest.raises(ControlChannelError, match="stream"):
        verify_authenticated_payload(
            signed,
            session_secret=secret,
            expected_campaign_uuid=CAMPAIGN_UUID,
            expected_stream="runner_checkpoint",
        )


def test_socket_parent_and_mode_fail_closed(short_socket_root: Path) -> None:
    unsafe = short_socket_root / "unsafe"
    unsafe.mkdir(mode=0o755)
    os.chmod(unsafe, 0o755)
    with pytest.raises(ControlChannelError, match="private"):
        create_server(unsafe / "control.sock")

    private = short_socket_root / "private"
    private.mkdir(mode=0o700)
    occupied = private / "control.sock"
    occupied.write_bytes(b"do-not-delete")
    with pytest.raises(ControlChannelError, match="pre-existing"):
        create_server(occupied)
    assert occupied.read_bytes() == b"do-not-delete"


def test_client_rejects_socket_permission_drift(short_socket_root: Path) -> None:
    server, path = private_server(short_socket_root)
    try:
        os.chmod(path, 0o660)
        with pytest.raises(ControlChannelError, match="permissions are unsafe"):
            send_hello(path, campaign_uuid=CAMPAIGN_UUID, timeout=0.1)
    finally:
        server.close()
        path.unlink(missing_ok=True)


@pytest.mark.parametrize("field", ("start_ticks", "boot_id", "cgroup_path"))
def test_client_binds_server_proc_identity(
    short_socket_root: Path,
    field: str,
) -> None:
    server, path = private_server(short_socket_root)
    binding = capture_process_binding(os.getpid())
    expected = {
        "pid": binding.pid,
        "start_ticks": binding.start_ticks,
        "boot_id": binding.boot_id,
        "cgroup_path": binding.cgroup_path,
    }
    expected[field] = (
        binding.start_ticks + 1 if field == "start_ticks" else str(expected[field]) + "-wrong"
    )
    server_errors: list[BaseException] = []

    def accept() -> None:
        connection, _address = server.accept()
        try:
            authenticate_connection(
                connection,
                expected_campaign=CAMPAIGN_UUID,
                expected_peer=PeerIdentity(os.getpid(), os.getuid(), os.getgid()),
                timeout=1.0,
            )
        except BaseException as exc:
            server_errors.append(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        with pytest.raises(ControlChannelError, match="process binding mismatch"):
            send_hello(
                path,
                campaign_uuid=CAMPAIGN_UUID,
                timeout=1.0,
                expected_server_peer=PeerIdentity(
                    os.getpid(),
                    os.getuid(),
                    os.getgid(),
                ),
                expected_server_process=expected,
            )
    finally:
        thread.join(timeout=2.0)
        server.close()
        path.unlink(missing_ok=True)
    assert not thread.is_alive()
    assert server_errors


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class OutsideCgroup:
    def assert_watchdog_outside(self, pid: int) -> dict[str, Any]:
        identity = capture_process_identity(pid)
        return {
            "pid": identity.pid,
            "start_ticks": identity.start_ticks,
            "boot_id": identity.boot_id,
            "actual_cgroup": identity.cgroup_path,
            "inside_campaign_scope": False,
            "ok": True,
        }


def test_supervisor_gate_passes_only_after_peer_proc_and_cgroup_binding(
    tmp_path: Path,
) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    supervisor.watchdog = FakeProcess(os.getpid())  # type: ignore[assignment]
    supervisor.cgroup = OutsideCgroup()  # type: ignore[assignment]
    supervisor._open_authenticated_control_server()
    supervisor._gate(
        "runner_control_channel_authenticated",
        passed=True,
        evidence={"ok": True},
    )
    identity = capture_process_identity(os.getpid())
    client_error: list[BaseException] = []

    def client() -> None:
        try:
            authentication = send_hello(
                supervisor.auth_socket_path,
                campaign_uuid=supervisor.campaign_uuid,
                timeout=2.0,
                role="watchdog",
                require_session_secret=True,
            )
            assert isinstance(authentication, tuple)
        except BaseException as exc:
            client_error.append(exc)

    thread = threading.Thread(target=client, daemon=True)
    thread.start()
    try:
        evidence = supervisor._authenticate_watchdog_control(
            runner_identity=None,
            watchdog_identity=identity,
            deadline=time.monotonic() + 2.0,
        )
        thread.join(timeout=2.0)
        assert not client_error
        assert evidence["ok"] is True
        assert evidence["peer_credentials"]["pid"] == os.getpid()
        assert evidence["process_identity"]["start_ticks"] == identity.start_ticks
        assert evidence["process_identity"]["boot_id"] == identity.boot_id
        assert evidence["placement"]["inside_campaign_scope"] is False
        gate = supervisor.gates["authenticated_control_channel_verified"]
        assert gate["status"] == "PASS"
        assert gate["machine_verified"] is True
    finally:
        supervisor._close_authenticated_control_server()


def test_supervisor_watchdog_connection_timeout_is_fail_closed(tmp_path: Path) -> None:
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    supervisor.watchdog = FakeProcess(os.getpid())  # type: ignore[assignment]
    supervisor._open_authenticated_control_server()
    identity = capture_process_identity(os.getpid())
    try:
        with pytest.raises(SupervisorError, match="before timeout"):
            supervisor._authenticate_watchdog_control(
                runner_identity=None,
                watchdog_identity=identity,
                deadline=time.monotonic() + 0.05,
            )
        gate = supervisor.gates["authenticated_control_channel_verified"]
        assert gate["status"] == "FAIL"
        assert gate["evidence"]["reason"] == "watchdog_connection_timeout"
    finally:
        supervisor._close_authenticated_control_server()
