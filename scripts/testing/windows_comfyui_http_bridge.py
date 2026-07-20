#!/usr/bin/env python3
"""Bridge WSL HTTP requests to a Windows-hosted ComfyUI through curl.exe.

This is a local test/development compatibility helper for WSL installations
where Windows loopback works from Windows processes but is unreachable from
Linux sockets.  The upstream target is fixed at startup and the listener
defaults to WSL loopback, so this is not an open forward proxy.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.parse


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _validated_target(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise argparse.ArgumentTypeError("target must be an http(s) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("target must not contain credentials, query, or fragment")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _last_response_headers(raw: bytes) -> tuple[int, list[tuple[str, str]]]:
    blocks = raw.replace(b"\r\n", b"\n").split(b"\n\n")
    candidates = [block for block in blocks if block.startswith(b"HTTP/")]
    if not candidates:
        raise ValueError("upstream did not return HTTP response headers")
    lines = candidates[-1].decode("iso-8859-1", errors="replace").splitlines()
    parts = lines[0].split(None, 2)
    status = int(parts[1])
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        if name.strip().lower() not in HOP_BY_HOP_HEADERS:
            headers.append((name.strip(), value.strip()))
    return status, headers


def resolve_cleanup_target(input_dir: str | os.PathLike[str], request_path: str) -> Path | None:
    """Resolve only exact ``input/<run-id>/<filename>`` DELETE targets."""
    parsed = urllib.parse.urlsplit(request_path)
    if parsed.path != "/view":
        return None
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if (query.get("type") or [""])[0] != "input":
        return None
    subfolder = (query.get("subfolder") or [""])[0]
    filename = (query.get("filename") or [""])[0]
    if not re.fullmatch(r"[a-f0-9]{32}", subfolder):
        raise ValueError("cleanup subfolder is not an exact run id")
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename or "\x00" in filename:
        raise ValueError("cleanup filename is unsafe")
    root = Path(input_dir).expanduser().resolve(strict=True)
    target = (root / subfolder / filename).resolve(strict=False)
    target.relative_to(root)
    return target


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    server_version = "HackmeWindowsComfyUIBridge/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def do_DELETE(self) -> None:  # noqa: N802
        input_dir = self.server.comfyui_input_dir  # type: ignore[attr-defined]
        if input_dir:
            try:
                target = resolve_cleanup_target(input_dir, self.path)
            except (OSError, ValueError) as exc:
                self.send_error(403, f"refusing unsafe ComfyUI input cleanup: {exc}")
                return
            if target is not None:
                if target.exists():
                    if not target.is_file() or target.is_symlink():
                        self.send_error(403, "refusing non-regular ComfyUI input cleanup target")
                        return
                    target.unlink()
                    deleted = True
                else:
                    deleted = False
                try:
                    target.parent.rmdir()
                except OSError:
                    pass
                payload = json.dumps({"ok": True, "deleted": deleted, "missing": not deleted}).encode("utf-8")
                self.send_response(200 if deleted else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Hackme-ComfyUI-Bridge", "windows-input-cleanup")
                self.end_headers()
                self.wfile.write(payload)
                print(f"verified ComfyUI input cleanup: {target}", flush=True)
                return
        self._forward()

    def do_HEAD(self) -> None:  # noqa: N802
        self._forward()

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        request_body = self.rfile.read(length) if length else b""
        target = f"{self.server.upstream_target}{self.path}"  # type: ignore[attr-defined]
        curl_path = self.server.curl_path  # type: ignore[attr-defined]
        timeout_seconds = self.server.upstream_timeout  # type: ignore[attr-defined]

        header_fd, header_path = tempfile.mkstemp(prefix="hackme-comfy-bridge-headers-")
        body_fd, body_path = tempfile.mkstemp(prefix="hackme-comfy-bridge-body-")
        os.close(header_fd)
        os.close(body_fd)
        try:
            command = [
                curl_path,
                "--noproxy",
                "*",
                "-sS",
                "--max-time",
                str(timeout_seconds),
                "-X",
                self.command,
                "-D",
                header_path,
                "-o",
                body_path,
            ]
            for name in ("Accept", "Content-Type"):
                value = self.headers.get(name)
                if value:
                    command.extend(["-H", f"{name}: {value}"])
            if request_body:
                command.extend(["--data-binary", "@-"])
            command.append(target)
            completed = subprocess.run(
                command,
                input=request_body,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds + 5,
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()[:500]
                self.send_error(502, f"Windows ComfyUI bridge failed: {detail}")
                return

            with open(header_path, "rb") as stream:
                status, headers = _last_response_headers(stream.read())
            with open(body_path, "rb") as stream:
                response_body = stream.read()
            self.send_response(status)
            for name, value in headers:
                if name.lower() not in {"content-length", "server", "date"}:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("X-Hackme-ComfyUI-Bridge", "windows-curl")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response_body)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self.send_error(502, f"Windows ComfyUI bridge error: {str(exc)[:500]}")
        finally:
            for path in (header_path, body_path):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)


class ThreadingBridgeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8189)
    parser.add_argument("--target", type=_validated_target, default="http://127.0.0.1:8188")
    parser.add_argument("--curl-path", default="/mnt/c/Windows/System32/curl.exe")
    parser.add_argument("--upstream-timeout", type=int, default=120)
    parser.add_argument(
        "--comfyui-input-dir",
        default="",
        help="Optional exact Windows ComfyUI input directory used only for verified run-scoped DELETE /view cleanup.",
    )
    args = parser.parse_args()

    server = ThreadingBridgeServer((args.listen_host, args.listen_port), BridgeHandler)
    server.upstream_target = args.target
    server.curl_path = args.curl_path
    server.upstream_timeout = max(1, args.upstream_timeout)
    server.comfyui_input_dir = str(Path(args.comfyui_input_dir).expanduser().resolve(strict=True)) if args.comfyui_input_dir else ""
    print(
        f"Windows ComfyUI bridge listening on http://{args.listen_host}:{args.listen_port} "
        f"-> {args.target}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
