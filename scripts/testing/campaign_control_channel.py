"""Authenticated one-shot supervisor/watchdog control handshake.

The transport is a private filesystem Unix ``SOCK_SEQPACKET`` socket.  The
supervisor trusts only kernel supplied ``SO_PEERCRED`` and sends a fresh
challenge for every accepted connection.  The watchdog binds its hello to that
challenge and a fresh client nonce, then requires a matching acknowledgement.
An old hello therefore cannot be replayed and a same-UID process cannot stand
in for the exact watchdog PID launched by the supervisor.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


CHALLENGE_SCHEMA_VERSION = "hackme.campaign-control-challenge.v1"
HELLO_SCHEMA_VERSION = "hackme.campaign-control-auth.v1"
ACK_SCHEMA_VERSION = "hackme.campaign-control-auth-ack.v1"
MESSAGE_AUTH_SCHEMA_VERSION = "hackme.campaign-message-auth.v1"
MAX_PACKET_BYTES = 8 * 1024
NONCE_BYTES = 32
SESSION_SECRET_BYTES = 32
RUNNER_KEY_DERIVATION_CONTEXT = b"hackme.campaign.runner-message-auth.v1"
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")


class ControlChannelError(RuntimeError):
    """The local authenticated-control contract was not proven."""


@dataclass(frozen=True)
class PeerIdentity:
    pid: int
    uid: int
    gid: int


@dataclass(frozen=True)
class ProcessBinding:
    pid: int
    start_ticks: int
    boot_id: str
    cgroup_path: str
    state: str


def peer_identity(conn: socket.socket) -> PeerIdentity:
    if not hasattr(socket, "SO_PEERCRED"):
        raise ControlChannelError("SO_PEERCRED is required on the production platform")
    size = struct.calcsize("iII")
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    if len(raw) != size:
        raise ControlChannelError("SO_PEERCRED returned an invalid credential record")
    pid, uid, gid = struct.unpack("iII", raw)
    if pid <= 1:
        raise ControlChannelError("unsafe peer pid")
    return PeerIdentity(int(pid), int(uid), int(gid))


def capture_process_binding(
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
) -> ProcessBinding:
    """Capture a Linux process identity while closing the PID-reuse read race."""

    process_id = _claimed_integer(pid, label="process pid")
    if process_id <= 1:
        raise ControlChannelError("unsafe process pid")
    root = Path(proc_root)
    process_root = root / str(process_id)

    def stat_identity() -> tuple[int, str]:
        try:
            tail = (process_root / "stat").read_text(encoding="utf-8").rstrip().rsplit(") ", 1)[1].split()
            state = str(tail[0])
            start_ticks = int(tail[19])
        except Exception as exc:
            raise ControlChannelError("cannot capture server process stat identity") from exc
        if start_ticks <= 0 or state in {"Z", "X", "x"}:
            raise ControlChannelError("server process is not live")
        return start_ticks, state

    first_ticks, first_state = stat_identity()
    try:
        boot_id = (root / "sys" / "kernel" / "random" / "boot_id").read_text(
            encoding="ascii"
        ).strip()
        cgroup_rows = (process_root / "cgroup").read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        raise ControlChannelError("cannot capture server boot/cgroup identity") from exc
    cgroup_path = ""
    for row in cgroup_rows:
        fields = row.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            cgroup_path = "/" + fields[2].strip().lstrip("/")
            cgroup_path = cgroup_path.rstrip("/") or "/"
            break
    second_ticks, second_state = stat_identity()
    if first_ticks != second_ticks:
        raise ControlChannelError("server process identity changed during inspection")
    if not boot_id or not cgroup_path:
        raise ControlChannelError("server process boot/cgroup identity is incomplete")
    return ProcessBinding(
        process_id,
        second_ticks,
        boot_id,
        cgroup_path,
        second_state or first_state,
    )


def _nonce(value: Any, *, label: str) -> str:
    nonce = str(value or "")
    if not _NONCE_RE.fullmatch(nonce):
        raise ControlChannelError(f"{label} is not a 256-bit lowercase hexadecimal nonce")
    return nonce


def _claimed_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise ControlChannelError(f"{label} is not an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ControlChannelError(f"{label} is not an integer") from exc
    return parsed


def _payload_bytes(payload: Mapping[str, Any]) -> bytes:
    data = (json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_PACKET_BYTES:
        raise ControlChannelError("control packet exceeds the maximum size")
    return data


def _session_secret(value: bytes) -> bytes:
    secret = bytes(value)
    if len(secret) != SESSION_SECRET_BYTES:
        raise ControlChannelError("control session secret must contain exactly 256 bits")
    return secret


def derive_runner_auth_key(watchdog_auth_key: bytes) -> bytes:
    """Derive the runner-message key without letting the runner recover the master."""

    master = _session_secret(watchdog_auth_key)
    return hmac.new(master, RUNNER_KEY_DERIVATION_CONTEXT, hashlib.sha256).digest()


def _canonical_message_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    unsigned = dict(payload)
    unsigned.pop("authentication", None)
    try:
        encoded = json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ControlChannelError("authenticated payload is not canonical JSON") from exc
    return unsigned, encoded


def sign_authenticated_payload(
    payload: Mapping[str, Any],
    *,
    session_secret: bytes,
    campaign_uuid: str,
    stream: str,
    sequence: int,
    monotonic_ns: int,
) -> dict[str, Any]:
    """Return a canonical HMAC envelope without persisting the session key."""

    secret = _session_secret(session_secret)
    stream_name = str(stream or "").strip()
    campaign = str(campaign_uuid or "").strip()
    if not campaign or not stream_name:
        raise ControlChannelError("authenticated payload requires campaign and stream binding")
    sequence_value = _claimed_integer(sequence, label="authentication sequence")
    monotonic_value = _claimed_integer(monotonic_ns, label="authentication monotonic time")
    if sequence_value <= 0 or monotonic_value <= 0:
        raise ControlChannelError("authentication sequence and monotonic time must be positive")
    unsigned, encoded = _canonical_message_payload(payload)
    if str(unsigned.get("campaign_uuid") or "") != campaign:
        raise ControlChannelError("authenticated payload campaign identity mismatch")
    payload_sha256 = hashlib.sha256(encoded).hexdigest()
    mac_input = "\0".join((
        MESSAGE_AUTH_SCHEMA_VERSION,
        campaign,
        stream_name,
        str(sequence_value),
        str(monotonic_value),
        payload_sha256,
    )).encode("utf-8")
    result = dict(unsigned)
    result["authentication"] = {
        "schema_version": MESSAGE_AUTH_SCHEMA_VERSION,
        "algorithm": "HMAC-SHA256",
        "campaign_uuid": campaign,
        "stream": stream_name,
        "sequence": sequence_value,
        "monotonic_ns": monotonic_value,
        "payload_sha256": payload_sha256,
        "mac": hmac.new(secret, mac_input, hashlib.sha256).hexdigest(),
    }
    return result


def verify_authenticated_payload(
    payload: Mapping[str, Any],
    *,
    session_secret: bytes,
    expected_campaign_uuid: str,
    expected_stream: str,
    previous_sequence: int = 0,
    previous_payload_sha256: str = "",
) -> dict[str, Any]:
    """Verify HMAC, campaign/stream binding, and monotonic sequence replay rules."""

    secret = _session_secret(session_secret)
    authentication = payload.get("authentication")
    if not isinstance(authentication, Mapping):
        raise ControlChannelError("authenticated payload envelope is missing")
    if str(authentication.get("schema_version") or "") != MESSAGE_AUTH_SCHEMA_VERSION:
        raise ControlChannelError("authenticated payload schema is unsupported")
    if str(authentication.get("algorithm") or "") != "HMAC-SHA256":
        raise ControlChannelError("authenticated payload algorithm is unsupported")
    campaign = str(authentication.get("campaign_uuid") or "")
    stream = str(authentication.get("stream") or "")
    if campaign != str(expected_campaign_uuid) or str(payload.get("campaign_uuid") or "") != campaign:
        raise ControlChannelError("authenticated payload campaign binding mismatch")
    if stream != str(expected_stream):
        raise ControlChannelError("authenticated payload stream binding mismatch")
    sequence = _claimed_integer(authentication.get("sequence"), label="authentication sequence")
    monotonic_value = _claimed_integer(
        authentication.get("monotonic_ns"),
        label="authentication monotonic time",
    )
    if sequence <= 0 or monotonic_value <= 0:
        raise ControlChannelError("authentication sequence and monotonic time must be positive")
    unsigned, encoded = _canonical_message_payload(payload)
    payload_sha256 = hashlib.sha256(encoded).hexdigest()
    claimed_payload_sha256 = str(authentication.get("payload_sha256") or "")
    if not hmac.compare_digest(claimed_payload_sha256, payload_sha256):
        raise ControlChannelError("authenticated payload digest mismatch")
    mac_input = "\0".join((
        MESSAGE_AUTH_SCHEMA_VERSION,
        campaign,
        stream,
        str(sequence),
        str(monotonic_value),
        payload_sha256,
    )).encode("utf-8")
    expected_mac = hmac.new(secret, mac_input, hashlib.sha256).hexdigest()
    claimed_mac = str(authentication.get("mac") or "")
    if not hmac.compare_digest(claimed_mac, expected_mac):
        raise ControlChannelError("authenticated payload MAC mismatch")
    previous = _claimed_integer(previous_sequence, label="previous authentication sequence")
    if sequence < previous:
        raise ControlChannelError("authenticated payload replay sequence regressed")
    if sequence == previous and previous > 0 and not hmac.compare_digest(
        str(previous_payload_sha256 or ""),
        payload_sha256,
    ):
        raise ControlChannelError("authenticated payload sequence was reused")
    return {
        "schema_version": MESSAGE_AUTH_SCHEMA_VERSION,
        "campaign_uuid": campaign,
        "stream": stream,
        "sequence": sequence,
        "monotonic_ns": monotonic_value,
        "payload_sha256": payload_sha256,
        "mac_verified": True,
        "replay_checked": True,
        "ok": True,
    }


def send_packet(conn: socket.socket, payload: Mapping[str, Any]) -> None:
    data = _payload_bytes(payload)
    try:
        sent = conn.send(data)
    except (OSError, TimeoutError) as exc:
        raise ControlChannelError(
            f"control packet send failed: {exc.__class__.__name__}"
        ) from exc
    if sent != len(data):
        raise ControlChannelError(f"short control packet write: {sent}/{len(data)}")


def receive_packet(conn: socket.socket) -> dict[str, Any]:
    try:
        data, _ancillary, flags, _address = conn.recvmsg(MAX_PACKET_BYTES)
    except (OSError, TimeoutError) as exc:
        raise ControlChannelError(f"control packet receive failed: {exc.__class__.__name__}") from exc
    if flags & getattr(socket, "MSG_TRUNC", 0):
        raise ControlChannelError("control packet was truncated")
    if not data:
        raise ControlChannelError("control peer closed before sending a packet")
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise ControlChannelError("control packet is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ControlChannelError("control packet must be a JSON object")
    return payload


def socket_permissions(
    path: Path,
    *,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> dict[str, Any]:
    path = Path(path)
    try:
        metadata = path.lstat()
    except Exception as exc:
        raise ControlChannelError(f"cannot stat authenticated control socket: {exc}") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    uid = os.getuid() if expected_uid is None else int(expected_uid)
    gid = os.getgid() if expected_gid is None else int(expected_gid)
    errors: list[str] = []
    if not stat.S_ISSOCK(metadata.st_mode):
        errors.append("not_socket")
    if mode != 0o600:
        errors.append(f"mode={oct(mode)}")
    if int(metadata.st_uid) != uid:
        errors.append(f"uid={metadata.st_uid}")
    if int(metadata.st_gid) != gid:
        errors.append(f"gid={metadata.st_gid}")
    if errors:
        raise ControlChannelError(
            "authenticated control socket permissions are unsafe: " + ", ".join(errors)
        )
    return {
        "transport": "unix_sock_seqpacket",
        "mode": "0o600",
        "uid": int(metadata.st_uid),
        "gid": int(metadata.st_gid),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "path_sha256": hashlib.sha256(str(path).encode()).hexdigest(),
        "ok": True,
    }


def create_server(path: Path) -> socket.socket:
    path = Path(path)
    if len(os.fsencode(str(path))) >= 108:
        raise ControlChannelError("authenticated control socket path exceeds AF_UNIX limit")
    parent = path.parent
    try:
        parent_metadata = parent.lstat()
    except Exception as exc:
        raise ControlChannelError(f"authenticated control socket parent is unavailable: {exc}") from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or int(parent_metadata.st_uid) != os.getuid()
        or stat.S_IMODE(parent_metadata.st_mode) & 0o077
    ):
        raise ControlChannelError("authenticated control socket parent is not private and owned")
    if path.exists() or path.is_symlink():
        raise ControlChannelError("authenticated control socket path has a pre-existing entry")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        server.bind(str(path))
        os.chmod(path, 0o600)
        socket_permissions(path)
        server.listen(8)
        return server
    except Exception:
        server.close()
        try:
            if path.lstat() and stat.S_ISSOCK(path.lstat().st_mode):
                path.unlink()
        except FileNotFoundError:
            pass
        raise


def validate_challenge(
    payload: Mapping[str, Any],
    *,
    expected_campaign: str,
    expected_role: str | None = None,
) -> str:
    if str(payload.get("schema_version")) != CHALLENGE_SCHEMA_VERSION:
        raise ControlChannelError("unsupported control challenge schema")
    if str(payload.get("campaign_uuid")) != str(expected_campaign):
        raise ControlChannelError("control challenge campaign identity mismatch")
    if expected_role is not None and str(payload.get("role") or "") != str(expected_role):
        raise ControlChannelError("control challenge role mismatch")
    return _nonce(payload.get("challenge_nonce"), label="challenge nonce")


def validate_hello(
    payload: Mapping[str, Any],
    *,
    expected_campaign: str,
    peer: PeerIdentity,
    expected_challenge: str,
    expected_role: str | None = None,
) -> str:
    if str(payload.get("schema_version")) != HELLO_SCHEMA_VERSION:
        raise ControlChannelError("unsupported control hello schema")
    if str(payload.get("campaign_uuid")) != str(expected_campaign):
        raise ControlChannelError("campaign identity mismatch")
    if expected_role is not None and str(payload.get("role") or "") != str(expected_role):
        raise ControlChannelError("control hello role mismatch")
    if _claimed_integer(payload.get("pid"), label="hello pid") != peer.pid:
        raise ControlChannelError("hello pid does not match SO_PEERCRED")
    if (
        _claimed_integer(payload.get("uid"), label="hello uid") != peer.uid
        or _claimed_integer(payload.get("gid"), label="hello gid") != peer.gid
    ):
        raise ControlChannelError("hello credentials do not match SO_PEERCRED")
    if _nonce(payload.get("challenge_nonce"), label="hello challenge nonce") != _nonce(
        expected_challenge,
        label="expected challenge nonce",
    ):
        raise ControlChannelError("hello challenge nonce mismatch")
    return _nonce(payload.get("client_nonce"), label="client nonce")


def authenticate_connection(
    conn: socket.socket,
    *,
    expected_campaign: str,
    expected_peer: PeerIdentity,
    timeout: float,
    challenge_nonce: str | None = None,
    expected_role: str | None = None,
    session_secret: bytes | None = None,
) -> dict[str, Any]:
    """Perform one challenge/hello/ack exchange on an accepted connection."""

    conn.settimeout(max(0.01, float(timeout)))
    peer = peer_identity(conn)
    if peer != expected_peer:
        raise ControlChannelError(
            "unexpected SO_PEERCRED identity: "
            f"pid={peer.pid},uid={peer.uid},gid={peer.gid}"
        )
    challenge = _nonce(
        challenge_nonce or secrets.token_hex(NONCE_BYTES),
        label="challenge nonce",
    )
    challenge_payload = {
        "schema_version": CHALLENGE_SCHEMA_VERSION,
        "campaign_uuid": expected_campaign,
        "challenge_nonce": challenge,
    }
    if expected_role is not None:
        challenge_payload["role"] = str(expected_role)
    send_packet(conn, challenge_payload)
    hello = receive_packet(conn)
    client_nonce = validate_hello(
        hello,
        expected_campaign=expected_campaign,
        peer=peer,
        expected_challenge=challenge,
        expected_role=expected_role,
    )
    acknowledgement = {
        "schema_version": ACK_SCHEMA_VERSION,
        "campaign_uuid": expected_campaign,
        "challenge_nonce": challenge,
        "client_nonce": client_nonce,
        "accepted": True,
    }
    if expected_role is not None:
        acknowledgement["role"] = str(expected_role)
    secret_hash = ""
    if session_secret is not None:
        secret = _session_secret(session_secret)
        acknowledgement["session_secret"] = secret.hex()
        secret_hash = hashlib.sha256(secret).hexdigest()
    send_packet(conn, acknowledgement)
    return {
        "schema_version": HELLO_SCHEMA_VERSION,
        "peer": {"pid": peer.pid, "uid": peer.uid, "gid": peer.gid},
        "challenge_sha256": hashlib.sha256(challenge.encode()).hexdigest(),
        "client_nonce_sha256": hashlib.sha256(client_nonce.encode()).hexdigest(),
        "challenge_bytes": NONCE_BYTES,
        "client_nonce_bytes": NONCE_BYTES,
        "role": str(expected_role or "unspecified"),
        "session_secret_delivered": session_secret is not None,
        "session_secret_bytes": SESSION_SECRET_BYTES if session_secret is not None else 0,
        "session_secret_sha256": secret_hash or None,
        "acknowledged": True,
        "one_time": True,
        "ok": True,
    }


def send_hello(
    path: Path,
    *,
    campaign_uuid: str,
    timeout: float = 5.0,
    role: str | None = None,
    require_session_secret: bool = False,
    expected_server_peer: PeerIdentity | None = None,
    expected_server_process: ProcessBinding | Mapping[str, Any] | None = None,
) -> dict[str, Any] | tuple[dict[str, Any], bytes]:
    """Connect as watchdog and complete the one-shot authenticated hello."""

    socket_evidence = socket_permissions(path)
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    conn.settimeout(max(0.01, float(timeout)))
    try:
        conn.connect(str(path))
        server_peer = peer_identity(conn)
        if expected_server_peer is not None and server_peer != expected_server_peer:
            raise ControlChannelError(
                "authenticated control server identity mismatch: "
                f"pid={server_peer.pid},uid={server_peer.uid},gid={server_peer.gid}"
            )
        server_binding: ProcessBinding | None = None
        if expected_server_process is not None:
            expected = (
                {
                    "pid": expected_server_process.pid,
                    "start_ticks": expected_server_process.start_ticks,
                    "boot_id": expected_server_process.boot_id,
                    "cgroup_path": expected_server_process.cgroup_path,
                }
                if isinstance(expected_server_process, ProcessBinding)
                else dict(expected_server_process)
            )
            if _claimed_integer(expected.get("pid"), label="expected server pid") != server_peer.pid:
                raise ControlChannelError("authenticated control server PID binding mismatch")
            server_binding = capture_process_binding(server_peer.pid)
            mismatches = [
                field
                for field in ("pid", "start_ticks", "boot_id", "cgroup_path")
                if getattr(server_binding, field) != expected.get(field)
            ]
            if mismatches:
                raise ControlChannelError(
                    "authenticated control server process binding mismatch: "
                    + ", ".join(mismatches)
                )
        challenge_payload = receive_packet(conn)
        challenge = validate_challenge(
            challenge_payload,
            expected_campaign=campaign_uuid,
            expected_role=role,
        )
        ident = PeerIdentity(os.getpid(), os.getuid(), os.getgid())
        client_nonce = secrets.token_hex(NONCE_BYTES)
        hello_payload = {
            "schema_version": HELLO_SCHEMA_VERSION,
            "campaign_uuid": campaign_uuid,
            "pid": ident.pid,
            "uid": ident.uid,
            "gid": ident.gid,
            "challenge_nonce": challenge,
            "client_nonce": client_nonce,
        }
        if role is not None:
            hello_payload["role"] = str(role)
        send_packet(conn, hello_payload)
        acknowledgement = receive_packet(conn)
        if str(acknowledgement.get("schema_version")) != ACK_SCHEMA_VERSION:
            raise ControlChannelError("unsupported control acknowledgement schema")
        if str(acknowledgement.get("campaign_uuid")) != str(campaign_uuid):
            raise ControlChannelError("control acknowledgement campaign identity mismatch")
        if acknowledgement.get("accepted") is not True:
            raise ControlChannelError("control acknowledgement rejected")
        if role is not None and str(acknowledgement.get("role") or "") != str(role):
            raise ControlChannelError("control acknowledgement role mismatch")
        if _nonce(
            acknowledgement.get("challenge_nonce"),
            label="ack challenge nonce",
        ) != challenge:
            raise ControlChannelError("control acknowledgement challenge mismatch")
        if _nonce(
            acknowledgement.get("client_nonce"),
            label="ack client nonce",
        ) != client_nonce:
            raise ControlChannelError("control acknowledgement client nonce mismatch")
        session_secret: bytes | None = None
        raw_session_secret = acknowledgement.get("session_secret")
        if require_session_secret:
            session_secret_hex = _nonce(
                raw_session_secret,
                label="control session secret",
            )
            session_secret = bytes.fromhex(session_secret_hex)
            _session_secret(session_secret)
        elif raw_session_secret is not None:
            raise ControlChannelError("unexpected control session secret")
        evidence = {
            "schema_version": ACK_SCHEMA_VERSION,
            "socket": socket_evidence,
            "server_peer": {
                "pid": server_peer.pid,
                "uid": server_peer.uid,
                "gid": server_peer.gid,
            },
            "server_process": (
                {
                    "pid": server_binding.pid,
                    "start_ticks": server_binding.start_ticks,
                    "boot_id": server_binding.boot_id,
                    "cgroup_path": server_binding.cgroup_path,
                    "state": server_binding.state,
                }
                if server_binding is not None
                else None
            ),
            "server_identity_verified": bool(
                expected_server_peer is not None and server_binding is not None
            ),
            "challenge_sha256": hashlib.sha256(challenge.encode()).hexdigest(),
            "client_nonce_sha256": hashlib.sha256(client_nonce.encode()).hexdigest(),
            "acknowledged": True,
            "role": str(role or "unspecified"),
            "session_secret_received": session_secret is not None,
            "session_secret_bytes": len(session_secret) if session_secret is not None else 0,
            "session_secret_sha256": (
                hashlib.sha256(session_secret).hexdigest()
                if session_secret is not None
                else None
            ),
            "ok": True,
        }
        if session_secret is not None:
            return evidence, session_secret
        return evidence
    finally:
        conn.close()
