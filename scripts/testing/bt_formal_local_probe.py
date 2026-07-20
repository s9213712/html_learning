#!/usr/bin/env python3
"""Fail-closed, single-host BitTorrent formal evidence probe.

The probe deliberately does not depend on public trackers, DHT, LPD, or PEX.
It creates a metadata-exchange-capable torrent, runs a tiny in-process HTTP tracker on the host's
private interface, and starts isolated Transmission seed/client daemons. RPC
remains bound to 127.0.0.1. The magnet
run is paused, resumed, and resumed again across a real client daemon restart.
The .torrent run goes through hackme_web's production storage download helper.

The process exits zero only when every mandatory check is backed by terminal
state, hashes, parseable video output, and verified cleanup evidence.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = "hackme.bt.formal.local.v1"
PROBE_NAME = "bt_formal_local_probe"
TORRENT_PIECE_SIZE_BYTES = 64 * 1024
MANDATORY_CHECK_IDS = (
    "controlled_local_seed",
    "magnet_terminal_success",
    "torrent_file_terminal_success",
    "payload_sha256_exact",
    "pause_resume_progress",
    "bt_client_service_restart_resume",
    "downloaded_video_parseable",
    "precise_process_and_fixture_cleanup",
)
TRANSMISSION_STATUS_NAMES = {
    0: "stopped",
    1: "check_wait",
    2: "checking",
    3: "download_wait",
    4: "downloading",
    5: "seed_wait",
    6: "seeding",
}


class ProbeFailure(RuntimeError):
    """A formal probe condition failed and must not be converted to a skip."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class TraceRecorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sequence = 0

    def emit(self, event: str, **fields: Any) -> None:
        with self._lock:
            self._sequence += 1
            record = {
                "schema_version": SCHEMA_VERSION,
                "sequence": self._sequence,
                "observed_at": utc_now(),
                "monotonic_ns": time.monotonic_ns(),
                "event": str(event),
                **fields,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
                handle.flush()


def _bencode(value: Any) -> bytes:
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        encoded_items: list[tuple[bytes, Any]] = []
        for key, item in value.items():
            key_bytes = key if isinstance(key, bytes) else str(key).encode("utf-8")
            encoded_items.append((key_bytes, item))
        encoded_items.sort(key=lambda pair: pair[0])
        return b"d" + b"".join(_bencode(key) + _bencode(item) for key, item in encoded_items) + b"e"
    raise TypeError(f"cannot bencode {type(value).__name__}")


def _raw_query_parameters(query: str) -> dict[str, list[bytes]]:
    output: dict[str, list[bytes]] = {}
    for item in str(query or "").split("&"):
        if not item:
            continue
        key_raw, separator, value_raw = item.partition("=")
        if not separator:
            value_raw = ""
        key = urllib.parse.unquote_plus(key_raw)
        value = urllib.parse.unquote_to_bytes(value_raw.replace("+", " "))
        output.setdefault(key, []).append(value)
    return output


class LocalTracker:
    """Minimal compact-peer tracker restricted to registered host-private endpoints."""

    def __init__(self, trace: TraceRecorder, *, bind_ip: str, advertised_peer_ip: str) -> None:
        self.trace = trace
        bind_address = ipaddress.ip_address(str(bind_ip))
        peer_address = ipaddress.ip_address(str(advertised_peer_ip))
        if not isinstance(bind_address, ipaddress.IPv4Address) or not bind_address.is_private:
            raise ProbeFailure(f"tracker bind address must be a host-local private IPv4 address: {bind_address}")
        if not isinstance(peer_address, ipaddress.IPv4Address) or not peer_address.is_private or peer_address.is_loopback:
            raise ProbeFailure(f"tracker peer address must be a non-loopback host-private IPv4 address: {peer_address}")
        self.bind_ip = str(bind_address)
        self.advertised_peer_ip = str(peer_address)
        self._lock = threading.Lock()
        self._peers: dict[bytes, dict[bytes, dict[str, Any]]] = {}
        self._announces: list[dict[str, Any]] = []
        self._peer_ip_by_port: dict[int, str] = {}
        self._source_proof_by_port: dict[int, dict[str, Any]] = {}
        self._pending_source_proofs: dict[str, dict[str, Any]] = {}
        self._source_route_proofs: list[dict[str, Any]] = []
        tracker = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "HackmeFormalPrivateTracker/2"

            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                tracker._handle_get(self)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer((self.bind_ip, 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="bt-formal-tracker", daemon=True)
        self.started = False

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def announce_url(self) -> str:
        return f"http://{self.bind_ip}:{self.port}/announce"

    def start(self) -> None:
        if self.started:
            raise ProbeFailure("local tracker was started twice")
        self.thread.start()
        self.started = True
        self.trace.emit(
            "tracker_started",
            bind_ip=self.bind_ip,
            advertised_peer_ip=self.advertised_peer_ip,
            port=self.port,
            announce_url=self.announce_url,
        )

    def prove_routed_source(self, peer_ip: str, *, timeout: float = 5.0) -> dict[str, Any]:
        """Prove the routed/NAT source produced by a locally bound peer socket."""

        address = ipaddress.ip_address(str(peer_ip))
        if not isinstance(address, ipaddress.IPv4Address) or not address.is_private or address.is_loopback:
            raise ProbeFailure(f"invalid peer source-proof address: {peer_ip}")
        token = secrets.token_urlsafe(32)
        token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        pending = {
            "expected_peer_ip": str(address),
            "token_sha256": token_sha256,
            "created_monotonic": time.monotonic(),
        }
        with self._lock:
            self._pending_source_proofs[token] = pending
        response = b""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(float(timeout))
                client.bind((str(address), 0))
                client.connect((self.bind_ip, self.port))
                request = (
                    f"GET /source-proof/{token} HTTP/1.1\r\n"
                    f"Host: {self.bind_ip}:{self.port}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                client.sendall(request)
                while len(response) < 4096:
                    chunk = client.recv(4096 - len(response))
                    if not chunk:
                        break
                    response += chunk
        except OSError as exc:
            with self._lock:
                self._pending_source_proofs.pop(token, None)
            raise ProbeFailure(f"peer route source proof failed for {address}: {exc}") from exc
        finally:
            token = ""
        if not response.startswith((b"HTTP/1.0 200 ", b"HTTP/1.1 200 ")):
            raise ProbeFailure(f"peer route source proof HTTP response was not 200 for {address}")
        with self._lock:
            proof = next(
                (dict(row) for row in reversed(self._source_route_proofs) if row.get("token_sha256") == token_sha256),
                None,
            )
        if not proof or proof.get("ok") is not True or proof.get("expected_peer_ip") != str(address):
            raise ProbeFailure(f"peer route source proof receipt missing for {address}")
        return proof

    def register_peer_endpoint(self, port: int, peer_ip: str, *, source_proof: dict[str, Any]) -> None:
        endpoint_port = int(port)
        address = ipaddress.ip_address(str(peer_ip))
        observed_source = ipaddress.ip_address(str((source_proof or {}).get("observed_source_ip") or "0.0.0.0"))
        proof_hash = str((source_proof or {}).get("token_sha256") or "")
        if (
            not isinstance(address, ipaddress.IPv4Address)
            or not address.is_private
            or address.is_loopback
            or not isinstance(observed_source, ipaddress.IPv4Address)
            or not observed_source.is_private
            or observed_source.is_loopback
            or observed_source.is_unspecified
            or (source_proof or {}).get("ok") is not True
            or (source_proof or {}).get("expected_peer_ip") != str(address)
            or len(proof_hash) != 64
            or any(char not in "0123456789abcdef" for char in proof_hash)
            or (source_proof or {}).get("one_time_challenge_consumed") is not True
            or not (1 <= endpoint_port <= 65535)
        ):
            raise ProbeFailure(f"invalid managed tracker peer endpoint: {peer_ip}:{port}")
        with self._lock:
            trusted_proof = next(
                (row for row in self._source_route_proofs if row.get("token_sha256") == proof_hash),
                None,
            )
            if trusted_proof != source_proof:
                raise ProbeFailure("managed peer endpoint source proof was not issued by this tracker")
            existing = self._peer_ip_by_port.get(endpoint_port)
            if existing and existing != str(address):
                raise ProbeFailure(f"managed peer port {endpoint_port} was registered to two addresses")
            self._peer_ip_by_port[endpoint_port] = str(address)
            self._source_proof_by_port[endpoint_port] = dict(source_proof)
        self.trace.emit(
            "tracker_peer_endpoint_registered",
            peer_ip=str(address),
            port=endpoint_port,
            observed_source_ip=str(observed_source),
            source_proof_sha256=proof_hash,
        )

    def unregister_peer_endpoint(self, port: int) -> None:
        endpoint_port = int(port)
        with self._lock:
            removed = self._peer_ip_by_port.pop(endpoint_port, None)
            self._source_proof_by_port.pop(endpoint_port, None)
        self.trace.emit("tracker_peer_endpoint_unregistered", peer_ip=removed or "", port=endpoint_port)

    def stop(self) -> None:
        if not self.started:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise ProbeFailure("local tracker thread did not stop")
        self.started = False
        self.trace.emit("tracker_stopped", port=self.port)

    def _reply(self, request: BaseHTTPRequestHandler, status: int, payload: bytes) -> None:
        request.send_response(status)
        request.send_header("Content-Type", "text/plain")
        request.send_header("Content-Length", str(len(payload)))
        request.send_header("Cache-Control", "no-store")
        request.end_headers()
        request.wfile.write(payload)

    def _failure(self, request: BaseHTTPRequestHandler, message: str) -> None:
        self.trace.emit(
            "tracker_request_rejected",
            reason=str(message),
            remote_ip=str(request.client_address[0]),
        )
        self._reply(request, 400, _bencode({b"failure reason": message}))

    def _handle_source_proof(self, request: BaseHTTPRequestHandler, token: str) -> None:
        now = time.monotonic()
        with self._lock:
            pending = self._pending_source_proofs.pop(token, None)
            if pending is not None and now - float(pending.get("created_monotonic") or 0) <= 5.0:
                observed = ipaddress.ip_address(str(request.client_address[0]))
                valid_source = bool(
                    isinstance(observed, ipaddress.IPv4Address)
                    and observed.is_private
                    and not observed.is_loopback
                    and not observed.is_unspecified
                )
                proof = {
                    "ok": valid_source,
                    "expected_peer_ip": pending["expected_peer_ip"],
                    "observed_source_ip": str(observed),
                    "token_sha256": pending["token_sha256"],
                    "proof_age_seconds": round(now - float(pending["created_monotonic"]), 6),
                    "one_time_challenge_consumed": True,
                }
                self._source_route_proofs.append(proof)
            else:
                proof = None
        if not proof or proof.get("ok") is not True:
            self._failure(request, "invalid or expired peer source proof")
            return
        self.trace.emit("tracker_source_route_proved", **proof)
        self._reply(request, 200, b"ok")

    def _handle_get(self, request: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlsplit(request.path)
        if parsed.path.startswith("/source-proof/"):
            self._handle_source_proof(request, parsed.path.rsplit("/", 1)[-1])
            return
        if parsed.path == "/scrape":
            self._reply(request, 200, _bencode({b"files": {}}))
            return
        if parsed.path != "/announce":
            self._reply(request, 404, b"not found")
            return
        params = _raw_query_parameters(parsed.query)
        info_hash = (params.get("info_hash") or [b""])[0]
        peer_id = (params.get("peer_id") or [b""])[0]
        if len(info_hash) != 20 or len(peer_id) != 20:
            self._failure(request, "invalid info_hash or peer_id")
            return
        try:
            port = int(((params.get("port") or [b"0"])[0]).decode("ascii"))
            left = int(((params.get("left") or [b"0"])[0]).decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            self._failure(request, "invalid port or left")
            return
        if not (1 <= port <= 65535) or left < 0:
            self._failure(request, "invalid peer endpoint")
            return
        event = ((params.get("event") or [b""])[0]).decode("ascii", errors="replace")
        with self._lock:
            managed_peer_ip = self._peer_ip_by_port.get(port)
            source_proof = self._source_proof_by_port.get(port)
            if not managed_peer_ip or not source_proof:
                self._failure(request, "unregistered managed peer endpoint")
                return
            # Transmission's peer listener obeys --bind-address-ipv4, while
            # its HTTP tracker client follows the host's primary route.  A
            # rotated peer may therefore announce from the tracker bind IP
            # while advertising its separately registered listener address.
            # The accepted source is the exact address observed by a one-time
            # local bind/connect challenge.  This remains fail closed when a
            # host bridge or WSL NAT rewrites the peer's tracker connection.
            if request.client_address[0] != source_proof.get("observed_source_ip"):
                self._failure(request, "managed peer source address mismatch")
                return
            swarm = self._peers.setdefault(info_hash, {})
            if event == "stopped":
                swarm.pop(peer_id, None)
            else:
                swarm[peer_id] = {
                    "ip": managed_peer_ip,
                    "port": port,
                    "left": left,
                    "last_seen_monotonic": time.monotonic(),
                }
            compact = b""
            returned = 0
            for other_peer_id, peer in sorted(swarm.items(), key=lambda item: item[0]):
                if other_peer_id == peer_id:
                    continue
                compact += socket.inet_aton(peer["ip"]) + int(peer["port"]).to_bytes(2, "big")
                returned += 1
            record = {
                "observed_at": utc_now(),
                "remote_ip": request.client_address[0],
                "advertised_peer_ip": managed_peer_ip,
                "source_route_proof_sha256": source_proof.get("token_sha256"),
                "info_hash": info_hash.hex(),
                "peer_id_sha256": hashlib.sha256(peer_id).hexdigest(),
                "port": port,
                "left": left,
                "tracker_event": event,
                "returned_peer_count": returned,
            }
            self._announces.append(record)
        self.trace.emit("tracker_announce", **record)
        complete = sum(1 for peer in swarm.values() if int(peer.get("left") or 0) == 0)
        incomplete = max(0, len(swarm) - complete)
        self._reply(
            request,
            200,
            _bencode(
                {
                    b"complete": complete,
                    b"incomplete": incomplete,
                    b"interval": 1,
                    b"min interval": 1,
                    b"peers": compact,
                }
            ),
        )

    def snapshot(self, info_hash_hex: str) -> dict[str, Any]:
        expected = str(info_hash_hex or "").lower()
        with self._lock:
            announces = [dict(item) for item in self._announces if item.get("info_hash") == expected]
            peers = []
            try:
                info_hash = bytes.fromhex(expected)
            except ValueError:
                info_hash = b""
            for peer in self._peers.get(info_hash, {}).values():
                peers.append({key: value for key, value in peer.items() if key != "last_seen_monotonic"})
            registered = {
                str(port): peer_ip
                for port, peer_ip in sorted(self._peer_ip_by_port.items())
            }
            registered_sources = {
                str(port): {
                    "expected_peer_ip": proof.get("expected_peer_ip"),
                    "observed_source_ip": proof.get("observed_source_ip"),
                    "source_proof_sha256": proof.get("token_sha256"),
                }
                for port, proof in sorted(self._source_proof_by_port.items())
            }
            source_route_proofs = [dict(row) for row in self._source_route_proofs]
        advertised_peer_ips = sorted({str(item.get("advertised_peer_ip") or "") for item in announces if item.get("advertised_peer_ip")})
        return {
            "bind_ip": self.bind_ip,
            "advertised_peer_ip": self.advertised_peer_ip,
            "advertised_peer_ip_private": ipaddress.ip_address(self.advertised_peer_ip).is_private,
            "advertised_peer_ips": advertised_peer_ips,
            "all_advertised_peer_ips_private": bool(advertised_peer_ips) and all(
                ipaddress.ip_address(value).is_private and not ipaddress.ip_address(value).is_loopback
                for value in advertised_peer_ips
            ),
            "registered_peer_endpoints": registered,
            "registered_announce_sources": registered_sources,
            "source_route_proofs": source_route_proofs,
            "announce_url": self.announce_url,
            "info_hash": expected,
            "announce_count": len(announces),
            "announces": announces,
            "active_peers": peers,
            "all_announces_host_local": bool(announces) and all(
                len(str(item.get("source_route_proof_sha256") or "")) == 64
                for item in announces
            ),
            "seed_announce_seen": any(item.get("left") is not None and int(item["left"]) == 0 for item in announces),
            "peer_response_seen": any(int(item.get("returned_peer_count") or 0) > 0 for item in announces),
        }


class TransmissionRPC:
    def __init__(self, port: int) -> None:
        self.port = int(port)
        self.url = f"http://127.0.0.1:{self.port}/transmission/rpc"
        self.session_id = ""

    def call(self, method: str, arguments: dict[str, Any] | None = None, *, timeout: float = 10) -> dict[str, Any]:
        payload = json.dumps({"method": method, "arguments": arguments or {}}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.session_id:
            headers["X-Transmission-Session-Id"] = self.session_id
        request = urllib.request.Request(self.url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                new_session_id = str(exc.headers.get("X-Transmission-Session-Id") or "")
                if new_session_id and new_session_id != self.session_id:
                    self.session_id = new_session_id
                    return self.call(method, arguments, timeout=timeout)
            raise ProbeFailure(f"Transmission RPC {method} returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProbeFailure(f"Transmission RPC {method} unavailable: {exc}") from exc
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProbeFailure(f"Transmission RPC {method} returned non-JSON") from exc
        if decoded.get("result") != "success":
            raise ProbeFailure(f"Transmission RPC {method} failed: {decoded.get('result')!r}")
        return dict(decoded.get("arguments") or {})


def _host_private_ipv4() -> str:
    candidates: list[str] = []
    try:
        for row in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            candidates.append(str(row[4][0]))
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # UDP connect selects a route but sends no packet.
            sock.connect(("192.0.2.1", 9))
            candidates.append(str(sock.getsockname()[0]))
    except OSError:
        pass
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv4Address) and address.is_private and not address.is_loopback and not address.is_link_local:
            return str(address)
    raise ProbeFailure("no non-loopback private IPv4 interface is available for host-local BT peers")


def _host_private_ipv4_candidates() -> list[str]:
    primary = _host_private_ipv4()
    candidates = [primary]
    ip_executable = shutil.which("ip")
    if ip_executable:
        try:
            completed = subprocess.run(
                [ip_executable, "-j", "-4", "addr", "show", "scope", "global"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
            rows = json.loads(completed.stdout) if completed.returncode == 0 else []
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            rows = []
        for row in rows if isinstance(rows, list) else []:
            if str(row.get("ifname") or "") == "lo":
                continue
            for address_row in row.get("addr_info") or []:
                value = str(address_row.get("local") or "")
                try:
                    address = ipaddress.ip_address(value)
                except ValueError:
                    continue
                if (
                    isinstance(address, ipaddress.IPv4Address)
                    and address.is_private
                    and not address.is_loopback
                    and not address.is_link_local
                    and value not in candidates
                ):
                    try:
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                            probe.bind((value, 0))
                    except OSError:
                        continue
                    candidates.append(value)
    return candidates


def _allocate_bind_port(bind_ip: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((str(bind_ip), 0))
        return int(sock.getsockname()[1])


def _pid_exists(pid: int | None) -> bool:
    if not pid or int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tcp_endpoint_open(ip: str, port: int, *, timeout: float = 0.2) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((str(ip), int(port))) == 0


@dataclass
class StopEvidence:
    role: str
    pid: int | None
    rpc_close_requested: bool
    graceful_exit: bool
    sigterm_used: bool
    sigkill_used: bool
    pid_remaining: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "pid": self.pid,
            "rpc_close_requested": self.rpc_close_requested,
            "graceful_exit": self.graceful_exit,
            "sigterm_used": self.sigterm_used,
            "sigkill_used": self.sigkill_used,
            "pid_remaining": self.pid_remaining,
        }


class TransmissionDaemon:
    def __init__(
        self,
        *,
        role: str,
        executable: str,
        runtime_dir: Path,
        download_dir: Path,
        log_path: Path,
        rpc_port: int,
        peer_port: int,
        peer_bind_ip: str,
        trace: TraceRecorder,
    ) -> None:
        self.role = role
        self.executable = executable
        self.runtime_dir = runtime_dir
        self.download_dir = download_dir
        self.log_path = log_path
        self.rpc_port = int(rpc_port)
        self.peer_port = int(peer_port)
        self.peer_bind_ip = str(peer_bind_ip)
        self.trace = trace
        self.rpc = TransmissionRPC(self.rpc_port)
        self.process: subprocess.Popen[bytes] | None = None
        self._log_handle: Any = None
        self.generation = 0
        self.started_pids: list[int] = []
        self.stop_evidence: list[dict[str, Any]] = []

    def command(self) -> list[str]:
        return [
            self.executable,
            "--foreground",
            "--config-dir", str(self.runtime_dir),
            "--download-dir", str(self.download_dir),
            "--no-incomplete-dir",
            "--port", str(self.rpc_port),
            "--peerport", str(self.peer_port),
            "--rpc-bind-address", "127.0.0.1",
            "--bind-address-ipv4", self.peer_bind_ip,
            "--allowed", "127.0.0.1",
            "--no-auth",
            "--no-dht",
            "--no-lpd",
            "--no-portmap",
            "--no-blocklist",
            "--no-global-seedratio",
            "--log-level", "info",
        ]

    def start(self, *, timeout: float = 20) -> int:
        if self.process is not None and self.process.poll() is None:
            raise ProbeFailure(f"{self.role} daemon is already running")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("ab", buffering=0)
        self._log_handle.write(f"\n--- {self.role} generation {self.generation + 1} {utc_now()} ---\n".encode("utf-8"))
        self.process = subprocess.Popen(
            self.command(),
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        self.generation += 1
        self.started_pids.append(int(self.process.pid))
        self.trace.emit(
            "transmission_daemon_started",
            role=self.role,
            generation=self.generation,
            pid=self.process.pid,
            rpc_port=self.rpc_port,
            peer_port=self.peer_port,
            peer_bind_ip=self.peer_bind_ip,
            command=self.command(),
        )
        deadline = time.monotonic() + timeout
        last_error = ""
        ready_streak = 0
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise ProbeFailure(f"{self.role} daemon exited early with {self.process.returncode}")
            try:
                self.rpc.call("session-get", timeout=1)
                if not _tcp_endpoint_open(self.peer_bind_ip, self.peer_port, timeout=0.2):
                    ready_streak = 0
                    last_error = "peer listener is not accepting host-local connections"
                    time.sleep(0.2)
                    continue
                session_stats = self.rpc.call("session-stats", timeout=2)
                seconds_active = int((session_stats.get("current-stats") or {}).get("secondsActive") or 0)
                if seconds_active < 2:
                    ready_streak = 0
                    last_error = "Transmission session has not remained active for two seconds"
                    time.sleep(0.2)
                    continue
                self.rpc.call(
                    "session-set",
                    {
                        "dht-enabled": False,
                        "lpd-enabled": False,
                        # Transmission 4 needs extension messaging enabled for
                        # magnet ut_metadata. DHT/LPD remain disabled and the
                        # controlled tracker is still the only bootstrap.
                        "pex-enabled": True,
                        "port-forwarding-enabled": False,
                        "peer-port-random-on-start": False,
                    },
                    timeout=2,
                )
                session = self.rpc.call("session-get", timeout=2)
                if any(bool(session.get(key)) for key in ("dht-enabled", "lpd-enabled", "port-forwarding-enabled")):
                    raise ProbeFailure(f"{self.role} peer discovery isolation settings were not enforced")
                if session.get("pex-enabled") is not True:
                    raise ProbeFailure(f"{self.role} ut_metadata/PEX extension messaging was not enabled")
                if int(session.get("peer-port") or 0) != self.peer_port:
                    raise ProbeFailure(f"{self.role} session peer port diverged from the managed listener")
                ready_streak += 1
                if ready_streak < 2:
                    time.sleep(0.25)
                    continue
                self.trace.emit(
                    "transmission_daemon_ready",
                    role=self.role,
                    generation=self.generation,
                    pid=self.process.pid,
                    session=_session_evidence(session),
                    seconds_active=seconds_active,
                )
                return int(self.process.pid)
            except ProbeFailure as exc:
                last_error = str(exc)
                time.sleep(0.2)
        raise ProbeFailure(f"{self.role} daemon did not become ready: {last_error}")

    def stop(self, *, timeout: float = 12) -> StopEvidence:
        process = self.process
        pid = int(process.pid) if process is not None else None
        evidence = StopEvidence(
            role=self.role,
            pid=pid,
            rpc_close_requested=False,
            graceful_exit=False,
            sigterm_used=False,
            sigkill_used=False,
            pid_remaining=False,
        )
        if process is None:
            self._close_log()
            self.stop_evidence.append(evidence.as_dict())
            return evidence
        if process.poll() is None:
            try:
                self.rpc.call("session-close", timeout=3)
                evidence.rpc_close_requested = True
            except Exception as exc:
                self.trace.emit("transmission_rpc_close_failed", role=self.role, pid=pid, error=str(exc))
            try:
                process.wait(timeout=timeout)
                evidence.graceful_exit = True
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    evidence.sigterm_used = True
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                        evidence.sigkill_used = True
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
        evidence.pid_remaining = _pid_exists(pid)
        self.trace.emit("transmission_daemon_stopped", **evidence.as_dict())
        self.stop_evidence.append(evidence.as_dict())
        self.process = None
        self.rpc = TransmissionRPC(self.rpc_port)
        self._close_log()
        return evidence

    def _close_log(self) -> None:
        if self._log_handle is not None:
            with contextlib.suppress(Exception):
                self._log_handle.close()
        self._log_handle = None


def _session_evidence(session: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "version",
        "rpc-version",
        "rpc-version-minimum",
        "peer-port",
        "peer-port-random-on-start",
        "dht-enabled",
        "lpd-enabled",
        "pex-enabled",
        "port-forwarding-enabled",
        "download-dir",
    )
    return {key: session.get(key) for key in keys}


def _run_command(command: list[str], *, timeout: float, trace: TraceRecorder, event: str) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    trace.emit(f"{event}_started", command=command)
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        trace.emit(f"{event}_timeout", command=command, timeout_seconds=timeout)
        raise ProbeFailure(f"{event} timed out after {timeout} seconds") from exc
    trace.emit(
        f"{event}_finished",
        command=command,
        returncode=completed.returncode,
        duration_seconds=round(time.monotonic() - started, 6),
        stdout_tail=completed.stdout[-1200:],
        stderr_tail=completed.stderr[-1200:],
    )
    if completed.returncode != 0:
        raise ProbeFailure(f"{event} failed with exit {completed.returncode}: {(completed.stderr or completed.stdout)[-500:]}")
    return completed


def _generate_video_payload(
    path: Path,
    *,
    target_bytes: int,
    ffmpeg: str,
    ffprobe: str,
    trace: TraceRecorder,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_command(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-y",
            "-f", "lavfi",
            "-i", "testsrc2=size=640x360:rate=30",
            "-t", "5",
            "-c:v", "mpeg2video",
            "-b:v", "2500k",
            "-muxrate", "3500k",
            "-f", "mpegts",
            str(path),
        ],
        timeout=90,
        trace=trace,
        event="ffmpeg_payload_generation",
    )
    null_packet = b"\x47\x1f\xff\x10" + (b"\xff" * 184)
    current = path.stat().st_size
    if current < target_bytes:
        missing_packets = (target_bytes - current + len(null_packet) - 1) // len(null_packet)
        with path.open("ab") as handle:
            for _ in range(missing_packets):
                handle.write(null_packet)
        trace.emit(
            "mpegts_null_packets_appended",
            original_bytes=current,
            target_bytes=target_bytes,
            final_bytes=path.stat().st_size,
            packet_count=missing_packets,
        )
    probe = _ffprobe_video(path, ffprobe=ffprobe, trace=trace, event="ffprobe_source_payload")
    if not probe.get("ok"):
        raise ProbeFailure("generated BT fixture is not a parseable video")
    return probe


def _ffprobe_video(path: Path, *, ffprobe: str, trace: TraceRecorder, event: str) -> dict[str, Any]:
    completed = _run_command(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,duration",
            "-of", "json",
            str(path),
        ],
        timeout=30,
        trace=trace,
        event=event,
    )
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeFailure(f"ffprobe returned invalid JSON for {path}") from exc
    streams = parsed.get("streams") or []
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    result = {
        "ok": bool(video_streams),
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "format": parsed.get("format") or {},
        "streams": streams,
        "video_stream_count": len(video_streams),
    }
    if not result["ok"]:
        raise ProbeFailure(f"downloaded file has no parseable video stream: {path}")
    return result


def _torrent_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    status_code = int(item.get("status") or 0)
    files = []
    for file_row in item.get("files") or []:
        files.append(
            {
                "name": str(file_row.get("name") or ""),
                "length": int(file_row.get("length") or 0),
                "bytes_completed": int(file_row.get("bytesCompleted") or 0),
            }
        )
    peers = []
    peer_keys = (
        "address",
        "clientName",
        "clientIsChoked",
        "clientIsInterested",
        "flagStr",
        "isDownloadingFrom",
        "isEncrypted",
        "isIncoming",
        "isUTP",
        "isUploadingTo",
        "peerIsChoked",
        "peerIsInterested",
        "port",
        "progress",
        "rateToClient",
        "rateToPeer",
    )
    for peer_row in item.get("peers") or []:
        peers.append({key: peer_row.get(key) for key in peer_keys})
    tracker_stats = []
    tracker_keys = (
        "announce",
        "host",
        "id",
        "lastAnnouncePeerCount",
        "lastAnnounceResult",
        "lastAnnounceSucceeded",
        "lastAnnounceTime",
        "nextAnnounceTime",
        "seederCount",
        "leecherCount",
        "tier",
    )
    for tracker_row in item.get("trackerStats") or []:
        tracker_stats.append({key: tracker_row.get(key) for key in tracker_keys})
    return {
        "observed_at": utc_now(),
        "id": item.get("id"),
        "name": str(item.get("name") or ""),
        "hash_string": str(item.get("hashString") or "").lower(),
        "status_code": status_code,
        "status": TRANSMISSION_STATUS_NAMES.get(status_code, "unknown"),
        "metadata_percent_complete": float(item.get("metadataPercentComplete") or 0),
        "percent_done": float(item.get("percentDone") or 0),
        "downloaded_bytes": int(item.get("downloadedEver") or 0),
        "left_bytes": int(item.get("leftUntilDone") or 0),
        "rate_download_bytes_per_sec": int(item.get("rateDownload") or 0),
        "error_code": int(item.get("error") or 0),
        "error_string": str(item.get("errorString") or ""),
        "peers_connected": int(item.get("peersConnected") or 0),
        "is_finished": bool(item.get("isFinished")),
        "download_dir": str(item.get("downloadDir") or ""),
        "files": files,
        "peers": peers,
        "peers_from": dict(item.get("peersFrom") or {}),
        "tracker_stats": tracker_stats,
    }


TORRENT_FIELDS = [
    "id",
    "name",
    "hashString",
    "status",
    "metadataPercentComplete",
    "percentDone",
    "downloadedEver",
    "leftUntilDone",
    "rateDownload",
    "error",
    "errorString",
    "peersConnected",
    "isFinished",
    "downloadDir",
    "files",
    "peers",
    "peersFrom",
    "trackerStats",
]


def _get_torrent(rpc: TransmissionRPC, torrent_ref: int | str) -> dict[str, Any]:
    response = rpc.call("torrent-get", {"ids": [torrent_ref], "fields": TORRENT_FIELDS})
    torrents = response.get("torrents") or []
    if not torrents:
        raise ProbeFailure(f"torrent {torrent_ref!r} disappeared before terminal state")
    return _torrent_snapshot(dict(torrents[0]))


def _wait_torrent_absent(
    rpc: TransmissionRPC,
    torrent_ref: int | str,
    *,
    timeout: float,
    trace: TraceRecorder,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = rpc.call("torrent-get", {"ids": [torrent_ref], "fields": ["id"]})
        if not (response.get("torrents") or []):
            trace.emit("torrent_absence_verified", torrent_ref=torrent_ref)
            return True
        time.sleep(0.1)
    return False


def _snapshot_completed_bytes(snapshot: dict[str, Any]) -> int:
    return sum(int(row.get("bytes_completed") or 0) for row in snapshot.get("files") or [])


def _wait_for_snapshot(
    rpc: TransmissionRPC,
    torrent_ref: int | str,
    *,
    description: str,
    predicate: Callable[[dict[str, Any]], bool],
    timeout: float,
    trace: TraceRecorder,
    poll_seconds: float = 0.4,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    last_trace = 0.0
    while time.monotonic() < deadline:
        last = _get_torrent(rpc, torrent_ref)
        if int(last.get("error_code") or 0) != 0:
            raise ProbeFailure(f"{description} failed: {last.get('error_string') or last.get('error_code')}")
        if predicate(last):
            trace.emit("torrent_wait_satisfied", description=description, snapshot=last)
            return last
        if time.monotonic() - last_trace >= 2:
            trace.emit("torrent_wait_progress", description=description, snapshot=last)
            last_trace = time.monotonic()
        time.sleep(poll_seconds)
    raise ProbeFailure(f"timed out waiting for {description}; last={json.dumps(last, sort_keys=True)}")


def _terminal_snapshot(snapshot: dict[str, Any], *, expected_hash: str) -> bool:
    return bool(
        str(snapshot.get("hash_string") or "").lower() == str(expected_hash or "").lower()
        and float(snapshot.get("metadata_percent_complete") or 0) >= 1.0
        and float(snapshot.get("percent_done") or 0) >= 1.0
        and int(snapshot.get("left_bytes") or 0) == 0
        and int(snapshot.get("error_code") or 0) == 0
        and snapshot.get("status") in {"seeding", "seed_wait", "stopped"}
    )


def _wait_path_absent(path: Path, *, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not path.exists():
            return True
        time.sleep(0.1)
    return not path.exists()


def _wait_endpoint_closed(ip: str, port: int, *, timeout: float = 8) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _tcp_endpoint_open(ip, port):
            return True
        time.sleep(0.1)
    return not _tcp_endpoint_open(ip, port)


def _artifact_record(artifact_id: str, path: Path, *, artifact_type: str, validated: bool) -> dict[str, Any]:
    exists = path.is_file()
    return {
        "artifact_id": artifact_id,
        "type": artifact_type,
        "path": str(path.resolve()),
        "exists": exists,
        "size_bytes": path.stat().st_size if exists else 0,
        "sha256": sha256_file(path) if exists else "",
        "validated": bool(exists and validated),
        "retention": "formal_evidence",
    }


def derive_checks(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Convert exact raw evidence into fail-closed formal check records."""

    local = raw.get("local_seed") or {}
    tracker = raw.get("tracker") or {}
    magnet = raw.get("magnet") or {}
    torrent_file = raw.get("torrent_file") or {}
    cleanup = raw.get("cleanup") or {}
    payload = raw.get("payload") or {}
    pause = magnet.get("pause_resume") or {}
    restart = magnet.get("service_restart") or {}

    def route_proof_valid(proof: Any) -> bool:
        if not isinstance(proof, dict):
            return False
        try:
            expected_ip = ipaddress.ip_address(str(proof.get("expected_peer_ip") or ""))
            observed_ip = ipaddress.ip_address(str(proof.get("observed_source_ip") or ""))
            proof_age = float(proof.get("proof_age_seconds") or -1)
        except (TypeError, ValueError):
            return False
        return bool(
            proof.get("ok") is True
            and isinstance(expected_ip, ipaddress.IPv4Address)
            and expected_ip.is_private
            and not expected_ip.is_loopback
            and isinstance(observed_ip, ipaddress.IPv4Address)
            and observed_ip.is_private
            and not observed_ip.is_loopback
            and not observed_ip.is_unspecified
            and len(str(proof.get("token_sha256") or "")) == 64
            and all(char in "0123456789abcdef" for char in str(proof.get("token_sha256") or ""))
            and proof.get("one_time_challenge_consumed") is True
            and 0 <= proof_age <= 5.0
        )

    source_route_proofs = tracker.get("source_route_proofs") or []
    proof_hashes = {
        str(row.get("token_sha256") or "")
        for row in source_route_proofs
        if isinstance(row, dict)
    }
    proof_by_hash = {
        str(row.get("token_sha256") or ""): row
        for row in source_route_proofs
        if isinstance(row, dict)
    }
    registered_endpoints = tracker.get("registered_peer_endpoints") or {}
    registered_sources = tracker.get("registered_announce_sources") or {}
    registered_sources_ok = bool(
        set(registered_sources) == set(registered_endpoints)
        and all(
            isinstance(row, dict)
            and str(row.get("expected_peer_ip") or "") == str(registered_endpoints.get(port) or "")
            and str(row.get("source_proof_sha256") or "") in proof_by_hash
            and str((proof_by_hash.get(str(row.get("source_proof_sha256") or "")) or {}).get("expected_peer_ip") or "")
            == str(row.get("expected_peer_ip") or "")
            and str((proof_by_hash.get(str(row.get("source_proof_sha256") or "")) or {}).get("observed_source_ip") or "")
            == str(row.get("observed_source_ip") or "")
            for port, row in registered_sources.items()
        )
    )
    route_announces_ok = bool(
        tracker.get("announces")
        and all(
            isinstance(row, dict)
            and str(row.get("source_route_proof_sha256") or "") in proof_by_hash
            and str(row.get("remote_ip") or "")
            == str((proof_by_hash.get(str(row.get("source_route_proof_sha256") or "")) or {}).get("observed_source_ip") or "")
            and str(row.get("advertised_peer_ip") or "")
            == str((proof_by_hash.get(str(row.get("source_route_proof_sha256") or "")) or {}).get("expected_peer_ip") or "")
            for row in tracker.get("announces") or []
        )
    )
    initial_source_proof = local.get("initial_source_route_proof") or {}
    source_routes_ok = bool(
        len(source_route_proofs) >= 2
        and all(route_proof_valid(row) for row in source_route_proofs)
        and len(proof_hashes) >= 2
        and len(registered_sources) >= 2
        and registered_sources_ok
        and route_announces_ok
        and route_proof_valid(initial_source_proof)
        and str(initial_source_proof.get("token_sha256") or "") in proof_hashes
    )

    session_rows = local.get("session_isolation") or []
    sessions_isolated = len(session_rows) >= 4 and all(
        row.get("dht-enabled") is False
        and row.get("lpd-enabled") is False
        and row.get("pex-enabled") is True
        and row.get("port-forwarding-enabled") is False
        for row in session_rows
    )
    local_ok = bool(
        local.get("discovery_isolated") is True
        and local.get("private_torrent") is False
        and int(local.get("torrent_tracker_count") or 0) == 1
        and local.get("seed_terminal") is True
        and local.get("seed_hash") == payload.get("info_hash")
        and tracker.get("bind_ip") == local.get("peer_bind_ip")
        and tracker.get("advertised_peer_ip_private") is True
        and tracker.get("all_advertised_peer_ips_private") is True
        and len(tracker.get("advertised_peer_ips") or []) >= 2
        and len(tracker.get("registered_peer_endpoints") or {}) >= 2
        and source_routes_ok
        and tracker.get("advertised_peer_ip") == local.get("peer_bind_ip")
        and tracker.get("all_announces_host_local") is True
        and tracker.get("seed_announce_seen") is True
        and tracker.get("peer_response_seen") is True
        and tracker.get("info_hash") == payload.get("info_hash")
        and sessions_isolated
    )

    magnet_terminal = magnet.get("terminal") or {}
    magnet_ok = bool(
        magnet.get("source_type") == "magnet"
        and magnet.get("terminal_state") == "success"
        and _terminal_snapshot(magnet_terminal, expected_hash=str(payload.get("info_hash") or ""))
        and magnet.get("download_path_exists") is True
    )

    torrent_terminal = torrent_file.get("terminal") or {}
    torrent_ok = bool(
        torrent_file.get("source_type") == "torrent_file"
        and torrent_file.get("implementation") == "services.storage.remote_downloads.download_torrent_file_with_aria2"
        and torrent_file.get("terminal_state") == "success"
        and torrent_terminal.get("phase") == "downloaded"
        and int(torrent_terminal.get("loaded_bytes") or 0) == int(payload.get("size_bytes") or -1)
        and int(torrent_terminal.get("total_bytes") or 0) == int(payload.get("size_bytes") or -1)
        and torrent_file.get("download_path_exists") is True
    )

    digest_values = {
        str(payload.get("source_sha256") or ""),
        str(magnet.get("download_sha256") or ""),
        str(torrent_file.get("download_sha256") or ""),
    }
    hashes_ok = bool(
        len(digest_values) == 1
        and "" not in digest_values
        and int(payload.get("size_bytes") or 0) > 0
        and int(magnet.get("download_size_bytes") or -1) == int(payload.get("size_bytes") or 0)
        and int(torrent_file.get("download_size_bytes") or -1) == int(payload.get("size_bytes") or 0)
    )

    before_pause = pause.get("before_pause") or {}
    stable_pause = pause.get("stable_during_pause") or {}
    after_resume = pause.get("after_resume") or {}
    resume_recovery = pause.get("resume_recovery") or {}
    recovery_before = resume_recovery.get("before_recreate") or {}
    recovery_after = resume_recovery.get("after_verify") or {}
    stable_completed = _snapshot_completed_bytes(stable_pause)
    after_resume_completed = _snapshot_completed_bytes(after_resume)
    seed_ip_rotation = resume_recovery.get("seed_ip_rotation") or {}
    piece_size = int(resume_recovery.get("piece_size_bytes") or 0)
    verified_piece_floor = stable_completed - (stable_completed % piece_size) if piece_size > 0 else -1
    incomplete_piece_loss = stable_completed - verified_piece_floor
    try:
        old_seed_ip = ipaddress.ip_address(str(seed_ip_rotation.get("old_ip") or ""))
        new_seed_ip = ipaddress.ip_address(str(seed_ip_rotation.get("new_ip") or ""))
        seed_ips_valid = bool(
            isinstance(old_seed_ip, ipaddress.IPv4Address)
            and isinstance(new_seed_ip, ipaddress.IPv4Address)
            and old_seed_ip.is_private
            and new_seed_ip.is_private
            and not old_seed_ip.is_loopback
            and not new_seed_ip.is_loopback
            and old_seed_ip != new_seed_ip
        )
    except ValueError:
        seed_ips_valid = False
    seed_rotation_ok = bool(
        seed_ip_rotation.get("strategy") == "seed_restart_on_distinct_host_private_ip"
        and seed_ips_valid
        and int(seed_ip_rotation.get("old_port") or 0) > 0
        and int(seed_ip_rotation.get("new_port") or 0) > 0
        and seed_ip_rotation.get("old_port") != seed_ip_rotation.get("new_port")
        and int(seed_ip_rotation.get("old_pid") or 0) > 0
        and int(seed_ip_rotation.get("new_pid") or 0) > 0
        and seed_ip_rotation.get("old_pid") != seed_ip_rotation.get("new_pid")
        and seed_ip_rotation.get("old_pid_exited") is True
        and seed_ip_rotation.get("old_listener_closed") is True
        and seed_ip_rotation.get("new_listener_open") is True
        and seed_ip_rotation.get("torrent_persisted") is True
        and seed_ip_rotation.get("tracker_updated") is True
        and int(seed_ip_rotation.get("seed_generation") or 0) >= 2
        and (seed_ip_rotation.get("stop_evidence") or {}).get("pid_remaining") is False
        and route_proof_valid(seed_ip_rotation.get("source_route_proof"))
        and str((seed_ip_rotation.get("source_route_proof") or {}).get("token_sha256") or "") in proof_hashes
    )
    pause_ok = bool(
        pause.get("stop_rpc_success") is True
        and pause.get("start_rpc_success") is True
        and stable_pause.get("status") == "stopped"
        and int(before_pause.get("downloaded_bytes") or 0) > 0
        and int(stable_pause.get("downloaded_bytes") or -1) == int(before_pause.get("downloaded_bytes") or -2)
        and stable_completed > 0
        and resume_recovery.get("strategy") == "torrent_remove_readd_verify_preserve_partial"
        and resume_recovery.get("remove_rpc_success") is True
        and resume_recovery.get("old_torrent_absent") is True
        and resume_recovery.get("readd_rpc_success") is True
        and resume_recovery.get("same_info_hash") is True
        and _snapshot_completed_bytes(recovery_before) == stable_completed
        and piece_size == TORRENT_PIECE_SIZE_BYTES
        and verified_piece_floor > 0
        and 0 <= incomplete_piece_loss < piece_size
        and _snapshot_completed_bytes(recovery_after) == verified_piece_floor
        and int(resume_recovery.get("preserved_completed_bytes") or -1) == stable_completed
        and int(resume_recovery.get("verified_completed_bytes") or -1) == verified_piece_floor
        and int(
            resume_recovery.get("discarded_incomplete_piece_bytes")
            if resume_recovery.get("discarded_incomplete_piece_bytes") is not None
            else -1
        ) == incomplete_piece_loss
        and resume_recovery.get("partial_path_exists") is True
        and seed_rotation_ok
        and after_resume_completed >= verified_piece_floor + piece_size
        and float(after_resume.get("percent_done") or 0) < 1.0
    )

    before_restart = restart.get("before_restart") or {}
    after_restart = restart.get("after_restart") or {}
    after_restart_resume = restart.get("after_restart_resume") or {}
    restart_ok = bool(
        restart.get("old_pid_exited") is True
        and int(restart.get("old_pid") or 0) > 0
        and int(restart.get("new_pid") or 0) > 0
        and restart.get("old_pid") != restart.get("new_pid")
        and restart.get("torrent_persisted") is True
        and restart.get("same_info_hash") is True
        and int(after_restart.get("downloaded_bytes") or -1) == int(before_restart.get("downloaded_bytes") or -2)
        and int(after_restart_resume.get("downloaded_bytes") or 0) > int(after_restart.get("downloaded_bytes") or 0)
        and int(restart.get("client_generation") or 0) >= 2
    )

    video_rows = payload.get("video_probes") or []
    video_ok = bool(
        len(video_rows) == 3
        and {row.get("role") for row in video_rows} == {"source", "magnet_download", "torrent_file_download"}
        and all(row.get("ok") is True and int(row.get("video_stream_count") or 0) >= 1 for row in video_rows)
    )

    process_rows = cleanup.get("processes") or []
    cleanup_ok = bool(
        cleanup.get("tracker_stopped") is True
        and cleanup.get("runtime_removed") is True
        and cleanup.get("product_download_cleanup_dir_removed") is True
        and cleanup.get("all_ports_released") is True
        and len(process_rows) >= 4
        and all(row.get("pid_remaining") is False for row in process_rows)
        and not cleanup.get("orphan_pids")
    )

    evidence = {
        "controlled_local_seed": (local_ok, {"local_seed": local, "tracker": tracker}),
        "magnet_terminal_success": (magnet_ok, {"terminal": magnet_terminal, "source_type": magnet.get("source_type")}),
        "torrent_file_terminal_success": (torrent_ok, {"terminal": torrent_terminal, "implementation": torrent_file.get("implementation")}),
        "payload_sha256_exact": (
            hashes_ok,
            {
                "source_sha256": payload.get("source_sha256"),
                "magnet_sha256": magnet.get("download_sha256"),
                "torrent_file_sha256": torrent_file.get("download_sha256"),
            },
        ),
        "pause_resume_progress": (pause_ok, pause),
        "bt_client_service_restart_resume": (restart_ok, restart),
        "downloaded_video_parseable": (video_ok, {"video_probes": video_rows}),
        "precise_process_and_fixture_cleanup": (cleanup_ok, cleanup),
    }
    return {
        check_id: {"check_id": check_id, "mandatory": True, "ok": bool(ok), "evidence": detail}
        for check_id, (ok, detail) in evidence.items()
    }


def validate_machine_report(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    if report.get("probe") != PROBE_NAME:
        errors.append("probe mismatch")
    checks = report.get("checks")
    if not isinstance(checks, dict):
        errors.append("checks must be an object")
        checks = {}
    if set(checks) != set(MANDATORY_CHECK_IDS):
        errors.append("mandatory check set mismatch")
    for check_id in MANDATORY_CHECK_IDS:
        row = checks.get(check_id)
        if not isinstance(row, dict):
            errors.append(f"missing check {check_id}")
            continue
        if type(row.get("ok")) is not bool:  # noqa: E721 - strict JSON boolean contract
            errors.append(f"check {check_id} ok must be boolean")
        if row.get("mandatory") is not True:
            errors.append(f"check {check_id} is not mandatory")
        if not isinstance(row.get("evidence"), dict):
            errors.append(f"check {check_id} evidence must be object")
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("artifact index missing")
    else:
        ids = {row.get("artifact_id") for row in artifacts if isinstance(row, dict)}
        mandatory_artifacts = {"source_video", "torrent_metainfo", "magnet_download", "torrent_file_download", "event_trace"}
        if not mandatory_artifacts.issubset(ids):
            errors.append("mandatory artifact index entries missing")
        for row in artifacts:
            if not isinstance(row, dict) or row.get("exists") is not True or row.get("validated") is not True:
                errors.append(f"artifact invalid: {(row or {}).get('artifact_id') if isinstance(row, dict) else 'unknown'}")
    if report.get("ok") is True and any(not (checks.get(check_id) or {}).get("ok") for check_id in MANDATORY_CHECK_IDS):
        errors.append("report ok conflicts with failed mandatory check")
    if report.get("terminal_state") not in {"success", "failed"}:
        errors.append("invalid terminal_state")
    return errors


class FormalBTProbe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_id = str(args.run_id or uuid.uuid4())
        self.out = Path(args.out).expanduser().resolve()
        artifact_parent = Path(args.artifact_dir).expanduser().resolve() if args.artifact_dir else self.out.parent / "bt_formal_artifacts"
        self.artifact_dir = artifact_parent / self.run_id
        runtime_parent = Path(args.runtime_root).expanduser().resolve() if args.runtime_root else Path(tempfile.gettempdir())
        self.runtime_dir = runtime_parent / f"hackme_bt_formal_{self.run_id}"
        self.trace_path = self.artifact_dir / "bt_event_trace.jsonl"
        self.trace: TraceRecorder | None = None
        self.tracker: LocalTracker | None = None
        self.seed: TransmissionDaemon | None = None
        self.client: TransmissionDaemon | None = None
        self.raw: dict[str, Any] = {
            "payload": {},
            "local_seed": {},
            "tracker": {},
            "magnet": {},
            "torrent_file": {},
            "cleanup": {},
        }
        self.errors: list[dict[str, Any]] = []
        self.started_at = utc_now()
        self.started_monotonic = time.monotonic()
        self.product_cleanup_dir: Path | None = None
        self.peer_bind_ip = ""
        self.peer_bind_ips: list[str] = []
        self.extra_endpoints: list[tuple[str, int]] = []

    def _prepare(self) -> dict[str, str]:
        if self.out.exists():
            raise ProbeFailure(f"refusing to overwrite existing report: {self.out}")
        if self.artifact_dir.exists():
            raise ProbeFailure(f"refusing to reuse artifact directory: {self.artifact_dir}")
        if self.runtime_dir.exists():
            raise ProbeFailure(f"refusing to reuse runtime directory: {self.runtime_dir}")
        self.artifact_dir.mkdir(parents=True, mode=0o700)
        self.runtime_dir.mkdir(parents=True, mode=0o700)
        self.trace = TraceRecorder(self.trace_path)
        self.peer_bind_ips = _host_private_ipv4_candidates()
        self.peer_bind_ip = self.peer_bind_ips[0]
        if len(self.peer_bind_ips) < 2:
            raise ProbeFailure("formal BT resume requires two distinct host-private IPv4 peer interfaces")
        required = {}
        for name in ("transmission-daemon", "transmission-create", "transmission-show", "ffmpeg", "ffprobe", "aria2c"):
            path = shutil.which(name)
            if not path:
                raise ProbeFailure(f"required executable is unavailable: {name}")
            required[name] = str(Path(path).resolve())
        self.trace.emit(
            "probe_prepared",
            run_id=self.run_id,
            runtime_dir=str(self.runtime_dir),
            artifact_dir=str(self.artifact_dir),
            peer_bind_ip=self.peer_bind_ip,
            peer_bind_ips=self.peer_bind_ips,
            executables=required,
        )
        return required

    def _create_fixture(self, executables: dict[str, str]) -> tuple[Path, Path, str, str, dict[str, Any]]:
        assert self.trace is not None
        self.tracker = LocalTracker(
            self.trace,
            bind_ip=self.peer_bind_ip,
            advertised_peer_ip=self.peer_bind_ip,
        )
        self.tracker.start()
        payload = self.artifact_dir / f"bt-formal-{self.run_id}.ts"
        source_probe = _generate_video_payload(
            payload,
            target_bytes=int(self.args.payload_bytes),
            ffmpeg=executables["ffmpeg"],
            ffprobe=executables["ffprobe"],
            trace=self.trace,
        )
        torrent = self.artifact_dir / f"bt-formal-{self.run_id}.torrent"
        _run_command(
            [
                executables["transmission-create"],
                "--piecesize", "64",
                "--tracker", self.tracker.announce_url,
                "--outfile", str(torrent),
                str(payload),
            ],
            timeout=30,
            trace=self.trace,
            event="torrent_metainfo_create",
        )
        if not torrent.is_file() or torrent.stat().st_size <= 0:
            raise ProbeFailure("transmission-create did not produce torrent metainfo")
        magnet_result = _run_command(
            [executables["transmission-show"], "--magnet", str(torrent)],
            timeout=10,
            trace=self.trace,
            event="torrent_magnet_extract",
        )
        magnet = next((line.strip() for line in magnet_result.stdout.splitlines() if line.strip().startswith("magnet:?")), "")
        if not magnet:
            raise ProbeFailure("transmission-show did not return a magnet URI")
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
        xt = str((params.get("xt") or [""])[0])
        info_hash = xt.rsplit(":", 1)[-1].lower() if xt.lower().startswith("urn:btih:") else ""
        if len(info_hash) != 40 or any(char not in "0123456789abcdef" for char in info_hash):
            raise ProbeFailure(f"torrent info hash is not canonical SHA-1 hex: {info_hash!r}")
        if self.tracker.announce_url not in (params.get("tr") or []):
            raise ProbeFailure("magnet URI does not retain the controlled host-local tracker")
        torrent_details = _run_command(
            [executables["transmission-show"], str(torrent)],
            timeout=10,
            trace=self.trace,
            event="torrent_metainfo_inspect",
        ).stdout
        private_torrent = "Private torrent: Yes" in torrent_details or "Privacy: Private torrent" in torrent_details
        public_torrent = "Privacy: Public torrent" in torrent_details
        if private_torrent or not public_torrent:
            raise ProbeFailure("controlled magnet torrent must permit metadata exchange")
        source_sha = sha256_file(payload)
        self.raw["payload"] = {
            "source_path": str(payload),
            "torrent_path": str(torrent),
            "source_sha256": source_sha,
            "torrent_sha256": sha256_file(torrent),
            "size_bytes": payload.stat().st_size,
            "info_hash": info_hash,
            "magnet_uri": magnet,
            "tracker_url": self.tracker.announce_url,
            "video_probes": [{"role": "source", **source_probe}],
        }
        atomic_write_json(self.artifact_dir / "source_ffprobe.json", source_probe)
        self.trace.emit("fixture_ready", payload=self.raw["payload"])
        return payload, torrent, magnet, info_hash, source_probe

    def _start_daemons(self, executables: dict[str, str], payload: Path, torrent: Path, info_hash: str) -> None:
        assert self.trace is not None
        allocated = {self.tracker.port if self.tracker else 0}

        def unique_port(bind_ip: str) -> int:
            for _ in range(32):
                candidate = _allocate_bind_port(bind_ip)
                if candidate not in allocated:
                    allocated.add(candidate)
                    return candidate
            raise ProbeFailure("could not allocate a unique managed BT port")

        seed_rpc_port = unique_port("127.0.0.1")
        seed_peer_port = unique_port(self.peer_bind_ip)
        client_rpc_port = unique_port("127.0.0.1")
        client_peer_port = unique_port(self.peer_bind_ip)
        self.seed = TransmissionDaemon(
            role="seed",
            executable=executables["transmission-daemon"],
            runtime_dir=self.runtime_dir / "seed-config",
            download_dir=payload.parent,
            log_path=self.artifact_dir / "transmission_seed.log",
            rpc_port=seed_rpc_port,
            peer_port=seed_peer_port,
            peer_bind_ip=self.peer_bind_ip,
            trace=self.trace,
        )
        self.client = TransmissionDaemon(
            role="client",
            executable=executables["transmission-daemon"],
            runtime_dir=self.runtime_dir / "client-config",
            download_dir=self.runtime_dir / "client-downloads",
            log_path=self.artifact_dir / "transmission_client.log",
            rpc_port=client_rpc_port,
            peer_port=client_peer_port,
            peer_bind_ip=self.peer_bind_ip,
            trace=self.trace,
        )
        if self.tracker is None:
            raise ProbeFailure("tracker unavailable while registering managed peer endpoints")
        primary_source_proof = self.tracker.prove_routed_source(self.peer_bind_ip)
        self.tracker.register_peer_endpoint(
            seed_peer_port,
            self.seed.peer_bind_ip,
            source_proof=primary_source_proof,
        )
        self.tracker.register_peer_endpoint(
            client_peer_port,
            self.client.peer_bind_ip,
            source_proof=primary_source_proof,
        )
        self.seed.start()
        seed_session = _session_evidence(self.seed.rpc.call("session-get"))
        metainfo = base64.b64encode(torrent.read_bytes()).decode("ascii")
        added = self.seed.rpc.call(
            "torrent-add",
            {"metainfo": metainfo, "download-dir": str(payload.parent), "paused": False},
        )
        seed_row = added.get("torrent-added") or added.get("torrent-duplicate") or {}
        seed_id = seed_row.get("id")
        if seed_id is None:
            raise ProbeFailure("seed daemon did not return torrent id")
        # Adding metainfo over an existing payload makes Transmission enter its
        # real checking state.  Do not enqueue a second torrent-verify here:
        # Transmission pauses a torrent after an explicit verify request,
        # creating a race with the first tracker announce.
        self.seed.rpc.call("torrent-start", {"ids": [seed_id]})
        seed_terminal = _wait_for_snapshot(
            self.seed.rpc,
            seed_id,
            description="controlled seed terminal verification",
            predicate=lambda row: _terminal_snapshot(row, expected_hash=info_hash),
            timeout=60,
            trace=self.trace,
        )
        if self.tracker is None:
            raise ProbeFailure("tracker unavailable after seed start")
        deadline = time.monotonic() + 20
        tracker_state: dict[str, Any] = {}
        last_reannounce = 0.0
        while time.monotonic() < deadline:
            seed_state = _get_torrent(self.seed.rpc, seed_id)
            if seed_state.get("status") == "stopped":
                self.seed.rpc.call("torrent-start", {"ids": [seed_id]})
                self.trace.emit("seed_restarted_after_verify_pause", snapshot=seed_state)
            if time.monotonic() - last_reannounce >= 1.0:
                self.seed.rpc.call("torrent-reannounce", {"ids": [seed_id]})
                last_reannounce = time.monotonic()
            tracker_state = self.tracker.snapshot(info_hash)
            if tracker_state.get("seed_announce_seen"):
                break
            time.sleep(0.2)
        if not tracker_state.get("seed_announce_seen"):
            raise ProbeFailure("controlled seed did not announce to the host-local tracker")
        self.client.start()
        client_session = _session_evidence(self.client.rpc.call("session-get"))
        self.raw["local_seed"] = {
            "private_torrent": False,
            "discovery_isolated": True,
            "torrent_tracker_count": 1,
            "seed_terminal": _terminal_snapshot(seed_terminal, expected_hash=info_hash),
            "seed_hash": seed_terminal.get("hash_string"),
            "seed_torrent_id": seed_id,
            "seed_terminal_snapshot": seed_terminal,
            "seed_pid": self.seed.started_pids[-1],
            "client_initial_pid": self.client.started_pids[-1],
            "peer_bind_ip": self.peer_bind_ip,
            "initial_source_route_proof": primary_source_proof,
            "session_isolation": [seed_session, client_session],
            "ports": {
                "tracker": self.tracker.port,
                "seed_rpc": seed_rpc_port,
                "seed_peer": seed_peer_port,
                "client_rpc": client_rpc_port,
                "client_peer": client_peer_port,
            },
        }

    def _rotate_seed_peer_ip_for_resume(self, info_hash: str) -> dict[str, Any]:
        assert self.trace is not None and self.seed is not None and self.client is not None and self.tracker is not None
        old_ip = self.seed.peer_bind_ip
        old_port = int(self.seed.peer_port)
        old_pid = self.seed.started_pids[-1]
        alternate_ip = next((value for value in self.peer_bind_ips if value != old_ip), "")
        if not alternate_ip:
            raise ProbeFailure("no alternate host-private peer IP is available for resume recovery")
        reserved = {
            int(self.tracker.port),
            int(self.seed.rpc_port),
            int(self.client.rpc_port),
            int(self.client.peer_port),
            old_port,
        }
        new_port = 0
        for _ in range(32):
            candidate = _allocate_bind_port(alternate_ip)
            if candidate not in reserved:
                new_port = candidate
                break
        if new_port <= 0:
            raise ProbeFailure("could not allocate alternate controlled seed peer port")

        alternate_source_proof = self.tracker.prove_routed_source(alternate_ip)

        stop_evidence = self.seed.stop()
        if stop_evidence.pid_remaining:
            raise ProbeFailure("old seed pid remained during alternate-IP recovery")
        self.extra_endpoints.append((old_ip, old_port))
        self.tracker.unregister_peer_endpoint(old_port)
        self.seed.peer_bind_ip = alternate_ip
        self.seed.peer_port = new_port
        self.tracker.register_peer_endpoint(
            new_port,
            alternate_ip,
            source_proof=alternate_source_proof,
        )
        new_pid = self.seed.start()
        if new_pid == old_pid:
            raise ProbeFailure("alternate-IP seed recovery reused the old pid")
        seed_session = _session_evidence(self.seed.rpc.call("session-get"))
        self.raw["local_seed"]["session_isolation"].append(seed_session)
        seed_state = _wait_for_snapshot(
            self.seed.rpc,
            info_hash,
            description="seed torrent persisted after alternate-IP restart",
            predicate=lambda item: _terminal_snapshot(item, expected_hash=info_hash),
            timeout=30,
            trace=self.trace,
        )
        seed_id = seed_state.get("id")
        self.seed.rpc.call("torrent-start", {"ids": [seed_id]})
        self.seed.rpc.call("torrent-reannounce", {"ids": [seed_id]})
        deadline = time.monotonic() + 15
        tracker_state: dict[str, Any] = {}
        tracker_updated = False
        while time.monotonic() < deadline:
            tracker_state = self.tracker.snapshot(info_hash)
            tracker_updated = any(
                str(peer.get("ip") or "") == alternate_ip
                and int(peer.get("port") or 0) == new_port
                and int(peer.get("left") if peer.get("left") is not None else -1) == 0
                for peer in tracker_state.get("active_peers") or []
            )
            if tracker_updated:
                break
            self.seed.rpc.call("torrent-reannounce", {"ids": [seed_id]})
            time.sleep(0.2)
        if not tracker_updated:
            raise ProbeFailure("tracker did not publish the alternate seed IP for resume recovery")
        evidence = {
            "strategy": "seed_restart_on_distinct_host_private_ip",
            "old_ip": old_ip,
            "new_ip": alternate_ip,
            "old_port": old_port,
            "new_port": new_port,
            "old_pid": old_pid,
            "new_pid": new_pid,
            "old_pid_exited": not _pid_exists(old_pid),
            "old_listener_closed": not _tcp_endpoint_open(old_ip, old_port),
            "new_listener_open": _tcp_endpoint_open(alternate_ip, new_port),
            "torrent_persisted": seed_state.get("hash_string") == info_hash,
            "tracker_updated": tracker_updated,
            "source_route_proof": alternate_source_proof,
            "seed_generation": self.seed.generation,
            "stop_evidence": stop_evidence.as_dict(),
            "tracker_snapshot": tracker_state,
        }
        self.raw["local_seed"]["ports"]["seed_peer_after_ip_rotation"] = new_port
        self.trace.emit("seed_alternate_ip_recovery_ready", evidence=evidence)
        return evidence

    def _run_magnet(self, payload: Path, magnet: str, info_hash: str, *, ffprobe: str) -> None:
        assert self.trace is not None and self.client is not None
        download_dir = self.runtime_dir / "magnet-download"
        download_dir.mkdir(parents=True, exist_ok=True)
        added = self.client.rpc.call(
            "torrent-add",
            {"filename": magnet, "download-dir": str(download_dir), "paused": False},
            timeout=20,
        )
        row = added.get("torrent-added") or added.get("torrent-duplicate") or {}
        torrent_id = row.get("id")
        if torrent_id is None:
            raise ProbeFailure("magnet add did not return a torrent id")
        self.client.rpc.call(
            "torrent-set",
            {
                "ids": [torrent_id],
                "downloadLimited": True,
                "downloadLimit": int(self.args.download_limit_kib_per_second),
                "honorsSessionLimits": True,
            },
        )
        self.client.rpc.call("torrent-reannounce", {"ids": [torrent_id]})
        before_pause = _wait_for_snapshot(
            self.client.rpc,
            torrent_id,
            description="magnet metadata and initial payload progress",
            predicate=lambda item: (
                item.get("hash_string") == info_hash
                and float(item.get("metadata_percent_complete") or 0) >= 1
                and int(item.get("downloaded_bytes") or 0) >= int(self.args.pause_after_bytes)
                and float(item.get("percent_done") or 0) < 0.8
            ),
            timeout=float(self.args.timeout_seconds),
            trace=self.trace,
        )
        self.client.rpc.call("torrent-stop", {"ids": [torrent_id]})
        stopped = _wait_for_snapshot(
            self.client.rpc,
            torrent_id,
            description="magnet stopped state",
            predicate=lambda item: item.get("status") == "stopped",
            timeout=20,
            trace=self.trace,
        )
        time.sleep(float(self.args.pause_observation_seconds))
        stable = _get_torrent(self.client.rpc, torrent_id)
        if int(stable.get("downloaded_bytes") or -1) != int(stopped.get("downloaded_bytes") or -2):
            raise ProbeFailure("magnet progress changed while torrent was stopped")
        self.raw["magnet"] = {
            "source_type": "magnet",
            "torrent_id": torrent_id,
            "pause_resume": {
                "stop_rpc_success": True,
                "start_rpc_success": False,
                "before_pause": stopped,
                "stable_during_pause": stable,
                "after_resume": {},
                "observation_seconds": float(self.args.pause_observation_seconds),
                "resume_recovery": {},
            },
        }
        # Transmission 4.0.5 retains peer-IP reconnect backoff across a plain
        # stop/start and even a daemon restart.  Recreate the torrent object
        # from trusted metainfo while retaining its partial file, then verify
        # every preserved piece before admitting resumed progress.
        stable_completed = _snapshot_completed_bytes(stable)
        if stable_completed <= 0:
            raise ProbeFailure("paused magnet has no verified completed pieces to preserve")
        verified_piece_floor = stable_completed - (stable_completed % TORRENT_PIECE_SIZE_BYTES)
        if verified_piece_floor <= 0:
            raise ProbeFailure("paused magnet has not completed one full torrent piece")
        partial_path = download_dir / payload.name
        if not partial_path.is_file():
            raise ProbeFailure("paused magnet partial file is missing before recovery")
        self.client.rpc.call("torrent-remove", {"ids": [torrent_id], "delete-local-data": False})
        old_torrent_absent = _wait_torrent_absent(
            self.client.rpc,
            torrent_id,
            timeout=10,
            trace=self.trace,
        )
        if not old_torrent_absent:
            raise ProbeFailure("old paused torrent object remained after non-destructive remove")
        metainfo_path = Path(str((self.raw.get("payload") or {}).get("torrent_path") or ""))
        if not metainfo_path.is_file():
            raise ProbeFailure("trusted torrent metainfo disappeared before resume recovery")
        readded = self.client.rpc.call(
            "torrent-add",
            {
                "metainfo": base64.b64encode(metainfo_path.read_bytes()).decode("ascii"),
                "download-dir": str(download_dir),
                "paused": True,
            },
            timeout=20,
        )
        readded_row = readded.get("torrent-added") or readded.get("torrent-duplicate") or {}
        recovered_torrent_id = readded_row.get("id")
        if recovered_torrent_id is None:
            raise ProbeFailure("resume recovery re-add did not return a torrent id")
        self.client.rpc.call(
            "torrent-set",
            {
                "ids": [recovered_torrent_id],
                "downloadLimited": True,
                "downloadLimit": int(self.args.download_limit_kib_per_second),
                "honorsSessionLimits": True,
            },
        )
        self.client.rpc.call("torrent-verify", {"ids": [recovered_torrent_id]})
        recovered = _wait_for_snapshot(
            self.client.rpc,
            recovered_torrent_id,
            description="piece-verified partial magnet after resume recovery",
            predicate=lambda item: (
                item.get("status") == "stopped"
                and item.get("hash_string") == info_hash
                and float(item.get("metadata_percent_complete") or 0) >= 1
                and _snapshot_completed_bytes(item) == verified_piece_floor
            ),
            timeout=60,
            trace=self.trace,
        )
        seed_ip_rotation = self._rotate_seed_peer_ip_for_resume(info_hash)
        self.client.rpc.call("torrent-start", {"ids": [recovered_torrent_id]})
        self.client.rpc.call("torrent-reannounce", {"ids": [recovered_torrent_id]})
        self.raw["magnet"]["pause_resume"]["start_rpc_success"] = True
        after_resume = _wait_for_snapshot(
            self.client.rpc,
            recovered_torrent_id,
            description="magnet progress after pause resume recovery",
            predicate=lambda item: (
                _snapshot_completed_bytes(item) >= verified_piece_floor + TORRENT_PIECE_SIZE_BYTES
                and float(item.get("percent_done") or 0) < 0.95
            ),
            timeout=60,
            trace=self.trace,
        )
        self.raw["magnet"]["pause_resume"]["after_resume"] = after_resume
        self.raw["magnet"]["pause_resume"]["resume_recovery"] = {
            "strategy": "torrent_remove_readd_verify_preserve_partial",
            "remove_rpc_success": True,
            "old_torrent_absent": old_torrent_absent,
            "readd_rpc_success": True,
            "old_torrent_id": torrent_id,
            "recovered_torrent_id": recovered_torrent_id,
            "same_info_hash": recovered.get("hash_string") == info_hash,
            "before_recreate": stable,
            "after_verify": recovered,
            "preserved_completed_bytes": stable_completed,
            "verified_completed_bytes": _snapshot_completed_bytes(recovered),
            "piece_size_bytes": TORRENT_PIECE_SIZE_BYTES,
            "discarded_incomplete_piece_bytes": stable_completed - _snapshot_completed_bytes(recovered),
            "partial_path": str(partial_path),
            "partial_path_exists": partial_path.is_file(),
            "seed_ip_rotation": seed_ip_rotation,
        }
        torrent_id = recovered_torrent_id

        self.client.rpc.call("torrent-stop", {"ids": [torrent_id]})
        before_restart = _wait_for_snapshot(
            self.client.rpc,
            torrent_id,
            description="stable pre-restart stopped state",
            predicate=lambda item: item.get("status") == "stopped",
            timeout=20,
            trace=self.trace,
        )
        old_pid = self.client.started_pids[-1]
        stop_evidence = self.client.stop()
        if stop_evidence.pid_remaining:
            raise ProbeFailure("old Transmission client pid remained after service stop")
        new_pid = self.client.start()
        if new_pid == old_pid:
            raise ProbeFailure("Transmission client restart reused the old pid")
        client_session = _session_evidence(self.client.rpc.call("session-get"))
        self.raw["local_seed"]["session_isolation"].append(client_session)
        after_restart = _wait_for_snapshot(
            self.client.rpc,
            info_hash,
            description="persisted magnet torrent after client restart",
            predicate=lambda item: item.get("status") == "stopped" and item.get("hash_string") == info_hash,
            timeout=30,
            trace=self.trace,
        )
        if int(after_restart.get("downloaded_bytes") or -1) != int(before_restart.get("downloaded_bytes") or -2):
            raise ProbeFailure("Transmission restart did not preserve exact downloaded byte progress")
        restarted_torrent_id = after_restart.get("id")
        self.client.rpc.call("torrent-start", {"ids": [restarted_torrent_id]})
        self.client.rpc.call("torrent-reannounce", {"ids": [restarted_torrent_id]})
        after_restart_resume = _wait_for_snapshot(
            self.client.rpc,
            restarted_torrent_id,
            description="magnet progress after client service restart",
            predicate=lambda item: int(item.get("downloaded_bytes") or 0) >= int(after_restart.get("downloaded_bytes") or 0) + 64 * 1024,
            timeout=60,
            trace=self.trace,
        )
        terminal = _wait_for_snapshot(
            self.client.rpc,
            restarted_torrent_id,
            description="magnet terminal download success",
            predicate=lambda item: _terminal_snapshot(item, expected_hash=info_hash),
            timeout=float(self.args.timeout_seconds),
            trace=self.trace,
        )
        downloaded = download_dir / payload.name
        if not downloaded.is_file():
            candidates = [path for path in download_dir.rglob("*") if path.is_file()]
            if len(candidates) != 1:
                raise ProbeFailure(f"magnet terminal state did not produce exactly one payload: {candidates}")
            downloaded = candidates[0]
        preserved = self.artifact_dir / f"magnet-downloaded-{payload.name}"
        shutil.copy2(downloaded, preserved)
        video_probe = _ffprobe_video(preserved, ffprobe=ffprobe, trace=self.trace, event="ffprobe_magnet_download")
        atomic_write_json(self.artifact_dir / "magnet_download_ffprobe.json", video_probe)
        self.raw["payload"]["video_probes"].append({"role": "magnet_download", **video_probe})
        self.raw["magnet"].update(
            {
                "terminal_state": "success",
                "terminal": terminal,
                "download_path": str(preserved),
                "download_path_exists": preserved.is_file(),
                "download_size_bytes": preserved.stat().st_size,
                "download_sha256": sha256_file(preserved),
                "service_restart": {
                    "old_pid": old_pid,
                    "new_pid": new_pid,
                    "old_pid_exited": not _pid_exists(old_pid),
                    "torrent_persisted": True,
                    "same_info_hash": before_restart.get("hash_string") == after_restart.get("hash_string") == info_hash,
                    "before_restart": before_restart,
                    "after_restart": after_restart,
                    "after_restart_resume": after_restart_resume,
                    "client_generation": self.client.generation,
                    "stop_evidence": stop_evidence.as_dict(),
                },
            }
        )
        self.client.rpc.call("torrent-remove", {"ids": [restarted_torrent_id], "delete-local-data": True})
        if not _wait_path_absent(downloaded):
            raise ProbeFailure("magnet fixture data remained after torrent-remove delete-local-data")
        self.trace.emit("magnet_run_complete", raw=self.raw["magnet"])

    def _run_torrent_file(self, torrent: Path, payload: Path, *, ffprobe: str) -> None:
        assert self.trace is not None and self.client is not None
        project_root = Path(__file__).resolve().parents[2]
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        from services.storage.remote_downloads import download_torrent_file_with_aria2  # pylint: disable=import-outside-toplevel

        staging = self.runtime_dir / "product-download-staging"
        staging.mkdir(parents=True, exist_ok=True)
        progress: list[dict[str, Any]] = []

        def progress_callback(item: dict[str, Any]) -> None:
            exact = json.loads(json.dumps(item))
            exact["observed_at"] = utc_now()
            progress.append(exact)
            self.trace.emit("product_torrent_file_progress", progress=exact)

        overrides = {
            "HACKME_BT_BACKEND": "transmission",
            "HACKME_TRANSMISSION_RPC_URL": self.client.rpc.url,
            "HACKME_TRANSMISSION_RPC_USERNAME": "",
            "HACKME_TRANSMISSION_RPC_PASSWORD": "",
            "HACKME_BT_DOWNLOAD_STAGING_DIR": str(staging),
            "HACKME_BT_PROGRESS_INTERVAL_SECONDS": "0.5",
            "HACKME_BT_IDLE_TIMEOUT_SECONDS": str(max(30, int(self.args.timeout_seconds))),
            "HACKME_BT_MAX_RUNTIME_SECONDS": str(max(60, int(self.args.timeout_seconds))),
        }
        previous = {key: os.environ.get(key) for key in overrides}
        os.environ.update(overrides)
        downloaded: Any = None
        try:
            downloaded = download_torrent_file_with_aria2(
                str(torrent),
                display_name=torrent.name,
                timeout_seconds=int(self.args.timeout_seconds),
                max_bytes=int(payload.stat().st_size + 1024),
                progress_callback=progress_callback,
                rate_limit_kb_per_sec=None,
                owner_user_id=0,
                task_id=f"formal-{self.run_id}",
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        if downloaded is None:
            raise ProbeFailure("product torrent-file helper returned no DownloadedFile")
        product_path = Path(str(downloaded.path)).resolve()
        self.product_cleanup_dir = Path(str(downloaded.cleanup_dir)).resolve() if downloaded.cleanup_dir else None
        if not product_path.is_file():
            raise ProbeFailure("product torrent-file helper returned a missing payload path")
        preserved = self.artifact_dir / f"torrent-file-downloaded-{payload.name}"
        shutil.copy2(product_path, preserved)
        video_probe = _ffprobe_video(preserved, ffprobe=ffprobe, trace=self.trace, event="ffprobe_torrent_file_download")
        atomic_write_json(self.artifact_dir / "torrent_file_download_ffprobe.json", video_probe)
        self.raw["payload"]["video_probes"].append({"role": "torrent_file_download", **video_probe})
        terminal_progress = next((item for item in reversed(progress) if item.get("phase") == "downloaded"), {})
        self.raw["torrent_file"] = {
            "source_type": "torrent_file",
            "implementation": "services.storage.remote_downloads.download_torrent_file_with_aria2",
            "terminal_state": "success" if terminal_progress else "missing_terminal_progress",
            "terminal": terminal_progress,
            "progress_samples": progress,
            "download_path": str(preserved),
            "download_path_exists": preserved.is_file(),
            "download_size_bytes": preserved.stat().st_size,
            "download_sha256": sha256_file(preserved),
            "helper_filename": str(downloaded.filename),
            "helper_mimetype": str(downloaded.mimetype),
            "helper_cleanup_dir": str(self.product_cleanup_dir) if self.product_cleanup_dir else "",
        }
        if self.product_cleanup_dir:
            shutil.rmtree(self.product_cleanup_dir, ignore_errors=False)
        self.trace.emit("torrent_file_run_complete", raw=self.raw["torrent_file"])

    def _cleanup(self) -> None:
        cleanup_errors: list[str] = []
        process_rows: list[dict[str, Any]] = []
        endpoints: list[tuple[str, int]] = []
        if self.seed is not None:
            endpoints.extend([("127.0.0.1", self.seed.rpc_port), (self.seed.peer_bind_ip, self.seed.peer_port)])
        if self.client is not None:
            endpoints.extend([("127.0.0.1", self.client.rpc_port), (self.client.peer_bind_ip, self.client.peer_port)])
        endpoints.extend(self.extra_endpoints)
        tracker_port = self.tracker.port if self.tracker is not None else None
        if tracker_port:
            endpoints.append((self.tracker.bind_ip, tracker_port))
        for daemon in (self.client, self.seed):
            if daemon is None:
                continue
            try:
                daemon.stop()
            except Exception as exc:  # cleanup evidence must retain every failure
                cleanup_errors.append(f"{daemon.role} stop: {exc}")
            process_rows.extend(daemon.stop_evidence)
        tracker_stopped = self.tracker is None or not self.tracker.started
        if self.tracker is not None and self.tracker.started:
            try:
                self.tracker.stop()
                tracker_stopped = True
            except Exception as exc:
                cleanup_errors.append(f"tracker stop: {exc}")
                tracker_stopped = False
        runtime_removed = False
        try:
            if self.runtime_dir.exists():
                shutil.rmtree(self.runtime_dir)
            runtime_removed = not self.runtime_dir.exists()
        except Exception as exc:
            cleanup_errors.append(f"runtime cleanup: {exc}")
        product_cleanup_removed = self.product_cleanup_dir is None or not self.product_cleanup_dir.exists()
        port_states = {f"{ip}:{port}": _wait_endpoint_closed(ip, port) for ip, port in endpoints}
        started_pids: list[int] = []
        for daemon in (self.seed, self.client):
            if daemon is not None:
                started_pids.extend(daemon.started_pids)
        orphan_pids = [pid for pid in started_pids if _pid_exists(pid)]
        self.raw["cleanup"] = {
            "tracker_stopped": tracker_stopped,
            "runtime_path": str(self.runtime_dir),
            "runtime_removed": runtime_removed,
            "product_download_cleanup_dir": str(self.product_cleanup_dir) if self.product_cleanup_dir else "",
            "product_download_cleanup_dir_removed": product_cleanup_removed,
            "ports_released": port_states,
            "all_ports_released": bool(port_states) and all(port_states.values()),
            "processes": process_rows,
            "started_pids": started_pids,
            "orphan_pids": orphan_pids,
            "cleanup_errors": cleanup_errors,
        }
        if cleanup_errors:
            self.errors.append({"phase": "cleanup", "type": "CleanupFailure", "message": "; ".join(cleanup_errors)})

    def _artifact_index(self) -> list[dict[str, Any]]:
        payload_path = Path(str(self.raw.get("payload", {}).get("source_path") or self.artifact_dir / "missing"))
        torrent_path = Path(str(self.raw.get("payload", {}).get("torrent_path") or self.artifact_dir / "missing"))
        magnet_path = Path(str(self.raw.get("magnet", {}).get("download_path") or self.artifact_dir / "missing"))
        torrent_download_path = Path(str(self.raw.get("torrent_file", {}).get("download_path") or self.artifact_dir / "missing"))
        records = [
            _artifact_record("source_video", payload_path, artifact_type="video/mpegts", validated=bool((self.raw.get("payload") or {}).get("video_probes"))),
            _artifact_record("torrent_metainfo", torrent_path, artifact_type="application/x-bittorrent", validated=bool((self.raw.get("payload") or {}).get("info_hash"))),
            _artifact_record("magnet_download", magnet_path, artifact_type="video/mpegts", validated=bool((self.raw.get("magnet") or {}).get("download_sha256"))),
            _artifact_record("torrent_file_download", torrent_download_path, artifact_type="video/mpegts", validated=bool((self.raw.get("torrent_file") or {}).get("download_sha256"))),
            _artifact_record("event_trace", self.trace_path, artifact_type="application/x-ndjson", validated=self.trace_path.is_file() and self.trace_path.stat().st_size > 0),
        ]
        optional = (
            ("seed_log", self.artifact_dir / "transmission_seed.log", "text/plain"),
            ("client_log", self.artifact_dir / "transmission_client.log", "text/plain"),
            ("source_ffprobe", self.artifact_dir / "source_ffprobe.json", "application/json"),
            ("magnet_ffprobe", self.artifact_dir / "magnet_download_ffprobe.json", "application/json"),
            ("torrent_file_ffprobe", self.artifact_dir / "torrent_file_download_ffprobe.json", "application/json"),
        )
        for artifact_id, path, artifact_type in optional:
            if path.is_file():
                records.append(_artifact_record(artifact_id, path, artifact_type=artifact_type, validated=True))
        return records

    def execute(self) -> tuple[dict[str, Any], int]:
        executables: dict[str, str] = {}
        try:
            executables = self._prepare()
            payload, torrent, magnet, info_hash, _source_probe = self._create_fixture(executables)
            self._start_daemons(executables, payload, torrent, info_hash)
            self._run_magnet(payload, magnet, info_hash, ffprobe=executables["ffprobe"])
            self._run_torrent_file(torrent, payload, ffprobe=executables["ffprobe"])
            if self.tracker is not None:
                self.raw["tracker"] = self.tracker.snapshot(info_hash)
        except Exception as exc:  # report all fail-closed outcomes as machine JSON
            self.errors.append(
                {
                    "phase": "execution",
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                    "traceback": "".join(traceback.format_exception(exc))[-8000:],
                }
            )
            if self.trace is not None:
                self.trace.emit("probe_execution_failed", error_type=exc.__class__.__name__, error=str(exc))
        finally:
            try:
                if self.tracker is not None and self.raw.get("payload", {}).get("info_hash"):
                    self.raw["tracker"] = self.tracker.snapshot(str(self.raw["payload"]["info_hash"]))
            except Exception as exc:
                self.errors.append({"phase": "tracker_snapshot", "type": exc.__class__.__name__, "message": str(exc)})
            self._cleanup()

        checks = derive_checks(self.raw)
        all_checks_ok = all((checks.get(check_id) or {}).get("ok") is True for check_id in MANDATORY_CHECK_IDS)
        artifacts = self._artifact_index()
        all_artifacts_valid = all(item.get("validated") is True for item in artifacts)
        ok = bool(all_checks_ok and all_artifacts_valid and not self.errors)
        report = {
            "schema_version": SCHEMA_VERSION,
            "probe": PROBE_NAME,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "duration_seconds": round(time.monotonic() - self.started_monotonic, 6),
            "terminal_state": "success" if ok else "failed",
            "ok": ok,
            "local_only": True,
            "network_scope": "single_host_private_tracker_and_peer_interface",
            "tracker_host_local_only": True,
            "peer_bind_ip": self.peer_bind_ip,
            "executables": executables,
            "raw": self.raw,
            "checks": checks,
            "artifacts": artifacts,
            "errors": self.errors,
        }
        validation_errors = validate_machine_report(report)
        if validation_errors:
            report["ok"] = False
            report["terminal_state"] = "failed"
            report["errors"].append(
                {"phase": "machine_report_validation", "type": "SchemaValidationFailure", "message": "; ".join(validation_errors)}
            )
        atomic_write_json(self.out, report)
        return report, 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Fresh machine-readable JSON result path")
    parser.add_argument("--artifact-dir", default="", help="Parent directory for retained evidence artifacts")
    parser.add_argument("--runtime-root", default="", help="Parent directory for disposable isolated runtime")
    parser.add_argument("--run-id", default="", help="Optional caller-supplied unique run id")
    parser.add_argument("--timeout-seconds", type=int, default=300, help="Per-download terminal deadline")
    parser.add_argument("--payload-bytes", type=int, default=8 * 1024 * 1024, help="Minimum generated video fixture size")
    parser.add_argument(
        "--download-limit-kib-per-second",
        type=int,
        default=192,
        help="Magnet download throttle in KiB/s used to expose lifecycle state",
    )
    parser.add_argument("--pause-after-bytes", type=int, default=256 * 1024, help="Minimum magnet progress before pause")
    parser.add_argument("--pause-observation-seconds", type=float, default=2.0, help="Stopped-state observation window")
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.timeout_seconds < 60:
        parser.error("--timeout-seconds must be at least 60")
    if args.payload_bytes < 2 * 1024 * 1024:
        parser.error("--payload-bytes must be at least 2 MiB")
    if not (16 <= args.download_limit_kib_per_second <= 4096):
        parser.error("--download-limit-kib-per-second must be between 16 and 4096")
    if not (64 * 1024 <= args.pause_after_bytes < args.payload_bytes // 2):
        parser.error("--pause-after-bytes must be at least 64 KiB and below half the payload")
    if not (0.5 <= args.pause_observation_seconds <= 30):
        parser.error("--pause-observation-seconds must be between 0.5 and 30")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    def abort_on_signal(signum: int, _frame: Any) -> None:
        raise ProbeFailure(f"probe received termination signal {signum}")

    previous_handlers = {
        signum: signal.signal(signum, abort_on_signal)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        report, exit_code = FormalBTProbe(args).execute()
        print(json.dumps({"ok": report["ok"], "terminal_state": report["terminal_state"], "out": str(Path(args.out).resolve())}, sort_keys=True))
        return exit_code
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
