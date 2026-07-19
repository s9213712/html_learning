from __future__ import annotations

import inspect
import json
import os
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path

import pytest

from scripts.testing import campaign_control_channel as control_channel
from scripts.testing import operational_campaign_supervisor as supervisor_module
from scripts.testing.campaign_control_channel import (
    ControlChannelError,
    PeerIdentity,
    create_server,
    peer_identity,
    send_hello,
    sign_authenticated_payload,
    validate_hello,
    verify_authenticated_payload,
)
from scripts.testing.campaign_watchdog import (
    CgroupIdentity,
    ExternalCampaignWatchdog,
    WatchdogConfig,
    WatchdogError,
    WatchdogPaths,
    build_watchdog_command,
    capture_process_identity,
)
from scripts.testing.operational_campaign_24h import (
    validate_supervised_runtime_contract,
)
from scripts.testing.operational_campaign_supervisor import (
    OperationalCampaignSupervisor,
    SupervisorConfig,
    SupervisorError,
)


CAMPAIGN_UUID = "campaign-control-security-001"
COMMIT = "a" * 40


@pytest.fixture
def short_tmp_path() -> Path:
    """Keep AF_UNIX socket names well below Linux's 107-byte path limit."""

    with tempfile.TemporaryDirectory(prefix="hw-cc-", dir="/tmp") as directory:
        yield Path(directory)


def _watchdog_config(
    tmp_path: Path,
    *,
    production: bool,
    auth_socket: Path | None,
) -> WatchdogConfig:
    root = tmp_path / "control"
    paths = WatchdogPaths(
        campaign_root=root,
        state=root / "checkpoint" / "state.json",
        control=root / "checkpoint" / "control.json",
        heartbeat=root / "checkpoint" / "heartbeat.json",
        checkpoint=root / "checkpoint" / "checkpoint.json",
        ready=root / "checkpoint" / "ready.json",
        evidence=root / "artifacts" / "watchdog",
        process_lock=root / "checkpoint" / "watchdog.lock",
    )
    return WatchdogConfig(
        campaign_uuid=CAMPAIGN_UUID,
        paths=paths,
        orchestrator_pid=4242,
        orchestrator_start_ticks=777,
        orchestrator_boot_id="boot-control-security-001",
        orchestrator_cgroup="/test.slice/runner.scope",
        campaign_cgroup=CgroupIdentity(
            "/test.slice/campaign.scope", 1, 2
        ),
        production=production,
        auth_socket=auth_socket,
    )


def test_so_peercred_is_kernel_derived_and_claim_mismatch_is_rejected() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        peer = peer_identity(left)
        assert peer == PeerIdentity(os.getpid(), os.getuid(), os.getgid())
        payload = {
            "schema_version": "hackme.campaign-control-auth.v1",
            "campaign_uuid": CAMPAIGN_UUID,
            "pid": peer.pid,
            "uid": peer.uid,
            "gid": peer.gid,
            "challenge_nonce": "1" * 64,
            "client_nonce": "2" * 64,
        }
        validate_hello(
            payload,
            expected_campaign=CAMPAIGN_UUID,
            peer=peer,
            expected_challenge="1" * 64,
        )
        with pytest.raises(ControlChannelError, match="pid"):
            validate_hello(
                {**payload, "pid": peer.pid + 1},
                expected_campaign=CAMPAIGN_UUID,
                peer=peer,
                expected_challenge="1" * 64,
            )
    finally:
        left.close()
        right.close()


def test_server_socket_is_private_socket_and_descriptor_is_not_inheritable(
    short_tmp_path: Path,
) -> None:
    parent = short_tmp_path / "private"
    parent.mkdir(mode=0o700)
    socket_path = parent / "control.sock"
    server = create_server(socket_path)
    try:
        info = socket_path.lstat()
        assert stat.S_ISSOCK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert server.get_inheritable() is False
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)


def test_production_watchdog_requires_authenticated_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchdog = ExternalCampaignWatchdog(
        _watchdog_config(tmp_path, production=True, auth_socket=None)
    )
    monkeypatch.setattr(watchdog, "_validate_paths", lambda: None)
    monkeypatch.setattr(watchdog, "_validate_configuration", lambda: None)
    with pytest.raises(WatchdogError, match="authenticated control socket"):
        watchdog.run()


def test_hello_without_fresh_challenge_binding_is_rejected() -> None:
    peer = PeerIdentity(os.getpid(), os.getuid(), os.getgid())
    weak_hello = {
        "schema_version": "hackme.campaign-control-auth.v1",
        "campaign_uuid": CAMPAIGN_UUID,
        "pid": peer.pid,
        "uid": peer.uid,
        "gid": peer.gid,
        "client_nonce": "2" * 64,
    }
    with pytest.raises(ControlChannelError, match="challenge nonce"):
        validate_hello(
            weak_hello,
            expected_campaign=CAMPAIGN_UUID,
            peer=peer,
            expected_challenge="1" * 64,
        )


def test_hello_from_a_previous_challenge_is_rejected() -> None:
    peer = PeerIdentity(os.getpid(), os.getuid(), os.getgid())
    payload = {
        "schema_version": "hackme.campaign-control-auth.v1",
        "campaign_uuid": CAMPAIGN_UUID,
        "pid": peer.pid,
        "uid": peer.uid,
        "gid": peer.gid,
        "challenge_nonce": "1" * 64,
        "client_nonce": "2" * 64,
    }
    with pytest.raises(ControlChannelError, match="challenge nonce mismatch"):
        validate_hello(
            payload,
            expected_campaign=CAMPAIGN_UUID,
            peer=peer,
            expected_challenge="3" * 64,
        )


def test_server_rejects_world_traversable_parent(short_tmp_path: Path) -> None:
    parent = short_tmp_path / "untrusted"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)
    socket_path = parent / "control.sock"
    server: socket.socket | None = None
    try:
        with pytest.raises(ControlChannelError, match="private"):
            server = create_server(socket_path)
    finally:
        if server is not None:
            server.close()
        socket_path.unlink(missing_ok=True)


def test_server_refuses_to_unlink_preexisting_non_socket(short_tmp_path: Path) -> None:
    parent = short_tmp_path / "private"
    parent.mkdir(mode=0o700)
    socket_path = parent / "control.sock"
    socket_path.write_bytes(b"do-not-delete")
    server: socket.socket | None = None
    try:
        with pytest.raises(ControlChannelError, match="pre-existing"):
            server = create_server(socket_path)
        assert socket_path.read_bytes() == b"do-not-delete"
    finally:
        if server is not None:
            server.close()
        socket_path.unlink(missing_ok=True)


def test_client_rejects_blackhole_server_without_authenticated_ack(
    short_tmp_path: Path,
) -> None:
    parent = short_tmp_path / "private"
    parent.mkdir(mode=0o700)
    socket_path = parent / "control.sock"
    server = create_server(socket_path)
    received = threading.Event()

    def blackhole() -> None:
        connection, _address = server.accept()
        try:
            control_channel.send_packet(connection, {
                "schema_version": control_channel.CHALLENGE_SCHEMA_VERSION,
                "campaign_uuid": CAMPAIGN_UUID,
                "challenge_nonce": "1" * 64,
            })
            control_channel.receive_packet(connection)
            received.set()
        finally:
            connection.close()

    thread = threading.Thread(target=blackhole, daemon=True)
    thread.start()
    try:
        with pytest.raises(ControlChannelError, match="closed|ack"):
            send_hello(socket_path, campaign_uuid=CAMPAIGN_UUID, timeout=1.0)
        assert received.wait(1.0)
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)
        thread.join(timeout=1.0)


def test_watchdog_command_serializes_authenticated_socket(tmp_path: Path) -> None:
    auth_socket = tmp_path / "control" / "auth.sock"
    command = build_watchdog_command(
        _watchdog_config(
            tmp_path,
            production=True,
            auth_socket=auth_socket,
        )
    )
    assert command[command.index("--auth-socket") + 1] == str(auth_socket)


def test_same_uid_forged_heartbeat_without_channel_auth_is_rejected(
    tmp_path: Path,
) -> None:
    config = _watchdog_config(
        tmp_path,
        production=True,
        auth_socket=tmp_path / "auth.sock",
    )
    config.paths.heartbeat.parent.mkdir(parents=True)
    config.paths.heartbeat.write_text(
        json.dumps({
            "campaign_uuid": CAMPAIGN_UUID,
            "heartbeat": {
                "orchestrator_pid": config.orchestrator_pid,
                "orchestrator_start_ticks": config.orchestrator_start_ticks,
                "orchestrator_monotonic_ns": 9_000_000_000,
                "checkpoint_revision": 3,
            },
        }),
        encoding="utf-8",
    )
    config.paths.checkpoint.write_text(
        json.dumps({"campaign_uuid": CAMPAIGN_UUID, "revision": 3}),
        encoding="utf-8",
    )
    watchdog = ExternalCampaignWatchdog(
        config,
        monotonic_ns=lambda: 10_000_000_000,
    )
    watchdog.runner_auth_key = b"R" * 32

    healthy, reason, _details = watchdog._heartbeat_health()

    assert healthy is False
    assert reason == "HEARTBEAT_AUTHENTICATION_INVALID"


def test_authenticated_payload_rejects_tamper_wrong_key_and_sequence_replay() -> None:
    key = b"R" * 32
    payload = sign_authenticated_payload(
        {"campaign_uuid": CAMPAIGN_UUID, "revision": 7},
        session_secret=key,
        campaign_uuid=CAMPAIGN_UUID,
        stream="runner_checkpoint",
        sequence=7,
        monotonic_ns=7_000_000_000,
    )

    verify_authenticated_payload(
        payload,
        session_secret=key,
        expected_campaign_uuid=CAMPAIGN_UUID,
        expected_stream="runner_checkpoint",
    )
    with pytest.raises(ControlChannelError, match="digest"):
        verify_authenticated_payload(
            {**payload, "revision": 8},
            session_secret=key,
            expected_campaign_uuid=CAMPAIGN_UUID,
            expected_stream="runner_checkpoint",
        )
    with pytest.raises(ControlChannelError, match="MAC"):
        verify_authenticated_payload(
            payload,
            session_secret=b"W" * 32,
            expected_campaign_uuid=CAMPAIGN_UUID,
            expected_stream="runner_checkpoint",
        )
    with pytest.raises(ControlChannelError, match="replay sequence regressed"):
        verify_authenticated_payload(
            payload,
            session_secret=key,
            expected_campaign_uuid=CAMPAIGN_UUID,
            expected_stream="runner_checkpoint",
            previous_sequence=8,
        )


def test_watchdog_accepts_signed_fresh_heartbeat_then_rejects_older_sequence(
    tmp_path: Path,
) -> None:
    key = b"R" * 32
    config = _watchdog_config(
        tmp_path,
        production=True,
        auth_socket=tmp_path / "auth.sock",
    )
    config.paths.heartbeat.parent.mkdir(parents=True)

    def write_streams(sequence: int) -> None:
        heartbeat = {
            "campaign_uuid": CAMPAIGN_UUID,
            "heartbeat": {
                "orchestrator_pid": config.orchestrator_pid,
                "orchestrator_start_ticks": config.orchestrator_start_ticks,
                "orchestrator_monotonic_ns": 9_000_000_000,
                "checkpoint_revision": 3,
            },
        }
        checkpoint = {"campaign_uuid": CAMPAIGN_UUID, "revision": 3}
        config.paths.heartbeat.write_text(
            json.dumps(sign_authenticated_payload(
                heartbeat,
                session_secret=key,
                campaign_uuid=CAMPAIGN_UUID,
                stream="runner_heartbeat",
                sequence=sequence,
                monotonic_ns=9_000_000_000,
            )),
            encoding="utf-8",
        )
        config.paths.checkpoint.write_text(
            json.dumps(sign_authenticated_payload(
                checkpoint,
                session_secret=key,
                campaign_uuid=CAMPAIGN_UUID,
                stream="runner_checkpoint",
                sequence=sequence,
                monotonic_ns=9_000_000_000,
            )),
            encoding="utf-8",
        )

    watchdog = ExternalCampaignWatchdog(
        config,
        monotonic_ns=lambda: 10_000_000_000,
    )
    watchdog.runner_auth_key = key
    write_streams(2)
    healthy, reason, _details = watchdog._heartbeat_health()
    assert (healthy, reason) == (True, "HEALTHY")

    write_streams(1)
    healthy, reason, details = watchdog._heartbeat_health()
    assert healthy is False
    assert reason == "HEARTBEAT_AUTHENTICATION_INVALID"
    assert "replay sequence regressed" in details["authentication_error"]


def test_supervisor_cannot_mark_auth_gate_pass_from_static_boolean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        supervisor_module,
        "AUTHENTICATED_CONTROL_CHANNEL_IMPLEMENTED",
        True,
    )
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )

    supervisor.prepare()

    assert supervisor.gates["authenticated_control_channel_verified"][
        "status"
    ] != "PASS"


def test_runner_cannot_possess_watchdog_liveness_signing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    supervisor.prepare()
    try:
        supervisor._open_authenticated_control_server()
        runner_key = getattr(supervisor, "runner_auth_key", None)
        watchdog_key = getattr(supervisor, "watchdog_auth_key", None)
        assert isinstance(runner_key, bytes) and len(runner_key) == 32
        assert isinstance(watchdog_key, bytes) and len(watchdog_key) == 32
        assert runner_key != watchdog_key
        forged_ns = time.monotonic_ns()
        forged = sign_authenticated_payload(
            {
                "schema_version": "hackme.campaign-watchdog-liveness.v1",
                "campaign_uuid": supervisor.campaign_uuid,
                "watchdog": {"monotonic_ns": forged_ns},
            },
            session_secret=runner_key,
            campaign_uuid=supervisor.campaign_uuid,
            stream="watchdog_liveness",
            sequence=1,
            monotonic_ns=forged_ns,
        )
        with pytest.raises(ControlChannelError, match="MAC"):
            verify_authenticated_payload(
                forged,
                session_secret=watchdog_key,
                expected_campaign_uuid=supervisor.campaign_uuid,
                expected_stream="watchdog_liveness",
            )
    finally:
        supervisor._close_authenticated_control_server()


def test_supervisor_cleanup_refuses_replaced_socket_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "git_commit", lambda: COMMIT)
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    supervisor.prepare()
    attacker: socket.socket | None = None
    try:
        supervisor._open_authenticated_control_server()
        assert supervisor.auth_server is not None
        supervisor.auth_server.close()
        supervisor.auth_server = None
        supervisor.auth_socket_path.unlink()
        attacker = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        attacker.bind(str(supervisor.auth_socket_path))
        os.chmod(supervisor.auth_socket_path, 0o600)

        cleanup = supervisor._close_authenticated_control_server()

        assert cleanup["ok"] is False
        assert "socket_path_identity_changed" in cleanup["errors"]
        assert supervisor.auth_socket_path.exists()
    finally:
        if attacker is not None:
            attacker.close()
        supervisor.auth_socket_path.unlink(missing_ok=True)
        try:
            supervisor.auth_socket_dir.rmdir()
        except FileNotFoundError:
            pass


def test_watchdog_client_rejects_same_uid_fake_supervisor(short_tmp_path: Path) -> None:
    parent = short_tmp_path / "private"
    parent.mkdir(mode=0o700)
    socket_path = parent / "control.sock"
    server = create_server(socket_path)

    def fake_supervisor() -> None:
        connection, _address = server.accept()
        try:
            control_channel.send_packet(connection, {
                "schema_version": control_channel.CHALLENGE_SCHEMA_VERSION,
                "campaign_uuid": CAMPAIGN_UUID,
                "challenge_nonce": "1" * 64,
            })
            hello = control_channel.receive_packet(connection)
            control_channel.send_packet(connection, {
                "schema_version": control_channel.ACK_SCHEMA_VERSION,
                "campaign_uuid": CAMPAIGN_UUID,
                "challenge_nonce": hello["challenge_nonce"],
                "client_nonce": hello["client_nonce"],
                "accepted": True,
            })
        except ControlChannelError:
            pass
        finally:
            connection.close()

    thread = threading.Thread(target=fake_supervisor, daemon=True)
    thread.start()
    try:
        with pytest.raises(ControlChannelError, match="server identity"):
            send_hello(
                socket_path,
                campaign_uuid=CAMPAIGN_UUID,
                timeout=1.0,
                expected_server_peer=PeerIdentity(
                    os.getpid() + 100_000,
                    os.getuid(),
                    os.getgid(),
                ),
            )
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)
        thread.join(timeout=1.0)


def test_runner_contract_requires_authenticated_control_gate() -> None:
    source = inspect.getsource(validate_supervised_runtime_contract)
    assert '"authenticated_control_channel_verified"' in source


def test_supervisor_ready_check_binds_expected_watchdog_identity() -> None:
    source = inspect.getsource(
        OperationalCampaignSupervisor._launch_watchdog
    )
    assert 'last.get("watchdog_pid")' in source
    assert 'last.get("watchdog_start_ticks")' in source


def test_supervisor_rejects_stale_signed_watchdog_liveness(
    tmp_path: Path,
) -> None:
    key = b"W" * 32
    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    identity = capture_process_identity(os.getpid())

    class LiveProcess:
        pid = os.getpid()
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

    supervisor.watchdog = LiveProcess()  # type: ignore[assignment]
    supervisor.watchdog_process_identity = identity
    supervisor.watchdog_auth_key = key
    supervisor.watchdog_liveness_path.parent.mkdir(parents=True)
    stale_ns = time.monotonic_ns() - int(
        (supervisor_module.WATCHDOG_LIVENESS_TIMEOUT_SECONDS + 1) * 1_000_000_000
    )
    liveness = sign_authenticated_payload(
        {
            "schema_version": "hackme.campaign-watchdog-liveness.v1",
            "campaign_uuid": supervisor.campaign_uuid,
            "watchdog": {
                "pid": identity.pid,
                "start_ticks": identity.start_ticks,
                "boot_id": identity.boot_id,
                "cgroup": identity.cgroup_path,
                "monotonic_ns": stale_ns,
            },
        },
        session_secret=key,
        campaign_uuid=supervisor.campaign_uuid,
        stream="watchdog_liveness",
        sequence=1,
        monotonic_ns=stale_ns,
    )
    supervisor.watchdog_liveness_path.write_text(
        json.dumps(liveness),
        encoding="utf-8",
    )

    with pytest.raises(SupervisorError, match="stale"):
        supervisor._verify_watchdog_liveness()


def test_supervisor_monitor_requires_fresh_watchdog_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runner:
        returncode = 0

        def __init__(self) -> None:
            self.polls = 0

        def poll(self) -> int | None:
            self.polls += 1
            return None if self.polls == 1 else 0

    class StoppedWatchdog:
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

    supervisor = OperationalCampaignSupervisor(
        SupervisorConfig(tmp_path / "campaign", "smoke", 180)
    )
    supervisor.runner = Runner()  # type: ignore[assignment]
    supervisor.watchdog = StoppedWatchdog()  # type: ignore[assignment]
    hard_stops: list[dict[str, object]] = []
    monkeypatch.setattr(
        supervisor,
        "_verify_watchdog_liveness",
        lambda: (_ for _ in ()).throw(SupervisorError("stale watchdog liveness")),
    )
    monkeypatch.setattr(
        supervisor,
        "_request_hard_stop",
        lambda **request: hard_stops.append(request),
    )
    monkeypatch.setattr(supervisor_module.time, "sleep", lambda _seconds: None)

    supervisor._monitor_runner()

    assert supervisor.failure is not None
    assert "watchdog" in supervisor.failure.lower()
    assert hard_stops[0]["reason"] == "WATCHDOG_LIVENESS_INVALID"
