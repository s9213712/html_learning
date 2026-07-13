"""Authenticated local control channel for campaign supervisor/watchdog.

The channel is deliberately tiny: a private Unix ``SOCK_SEQPACKET`` socket,
one JSON hello, and kernel supplied peer credentials.  File permissions alone
are not sufficient because a same-UID process could otherwise forge control
messages.
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ControlChannelError(RuntimeError):
    pass


@dataclass(frozen=True)
class PeerIdentity:
    pid: int
    uid: int
    gid: int


def peer_identity(conn: socket.socket) -> PeerIdentity:
    if not hasattr(socket, "SO_PEERCRED"):
        raise ControlChannelError("SO_PEERCRED is required on the production platform")
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    pid = int.from_bytes(raw[0:4], "little", signed=True)
    uid = int.from_bytes(raw[4:8], "little", signed=False)
    gid = int.from_bytes(raw[8:12], "little", signed=False)
    if pid <= 1:
        raise ControlChannelError("unsafe peer pid")
    return PeerIdentity(pid, uid, gid)


def _payload_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n").encode()


def validate_hello(payload: Mapping[str, Any], *, expected_campaign: str, peer: PeerIdentity) -> None:
    if str(payload.get("schema_version")) != "hackme.campaign-control-auth.v1":
        raise ControlChannelError("unsupported control hello schema")
    if str(payload.get("campaign_uuid")) != expected_campaign:
        raise ControlChannelError("campaign identity mismatch")
    if int(payload.get("pid") or 0) != peer.pid:
        raise ControlChannelError("hello pid does not match SO_PEERCRED")
    if int(payload.get("uid") or -1) != peer.uid or int(payload.get("gid") or -1) != peer.gid:
        raise ControlChannelError("hello credentials do not match SO_PEERCRED")


def create_server(path: Path) -> socket.socket:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(1)
    return server


def send_hello(path: Path, *, campaign_uuid: str, timeout: float = 5.0) -> None:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    conn.settimeout(timeout)
    try:
        conn.connect(str(path))
        ident = PeerIdentity(os.getpid(), os.getuid(), os.getgid())
        conn.send(_payload_bytes({
            "schema_version": "hackme.campaign-control-auth.v1",
            "campaign_uuid": campaign_uuid,
            "pid": ident.pid,
            "uid": ident.uid,
            "gid": ident.gid,
        }))
    finally:
        conn.close()

