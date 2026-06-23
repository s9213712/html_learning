import base64
import ipaddress
import json
import http.client
import mimetypes
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from services.security.upload_security import safe_public_filename


MAX_BDECODE_DEPTH = 64
BT_DEFAULT_MAX_RUNTIME_SECONDS = 24 * 3600
PUBLIC_BT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://tracker.moeking.me:6969/announce",
]


class RemoteDownloadError(RuntimeError):
    pass


class RemoteDownloadCancelled(RemoteDownloadError):
    pass


class RemoteDownloadPaused(RemoteDownloadError):
    pass


@dataclass
class DownloadedFile:
    path: str
    filename: str
    mimetype: str
    cleanup_dir: str | None = None

    @property
    def stream(self):
        return open(self.path, "rb")


def _bt_backend_preference():
    value = str(os.environ.get("HACKME_BT_BACKEND", "auto") or "auto").strip().lower()
    if value in {"transmission", "transmission-rpc", "rpc"}:
        return "transmission"
    if value in {"aria2", "aria2c"}:
        return "aria2"
    return "auto"


def _transmission_rpc_url():
    return str(os.environ.get("HACKME_TRANSMISSION_RPC_URL", "http://127.0.0.1:9091/transmission/rpc") or "").strip()


def _transmission_rpc_auth_header():
    username = str(os.environ.get("HACKME_TRANSMISSION_RPC_USERNAME", "") or "")
    password = str(os.environ.get("HACKME_TRANSMISSION_RPC_PASSWORD", "") or "")
    if not username and not password:
        return None
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def transmission_rpc_available(*, timeout_seconds=2):
    try:
        _transmission_rpc_call("session-get", timeout_seconds=timeout_seconds)
        return True
    except Exception:
        return False


def remote_download_capabilities():
    aria2c = shutil.which("aria2c")
    transmission_rpc = _transmission_rpc_url()
    transmission_ok = transmission_rpc_available(timeout_seconds=1) if transmission_rpc else False
    bt_available = bool(transmission_ok or aria2c)
    return {
        "direct_link": True,
        "bt_magnet": bt_available,
        "bt_file": bt_available,
        "bt_backend": _bt_backend_preference(),
        "bt_backend_active": "transmission" if transmission_ok else ("aria2" if aria2c else ""),
        "aria2c_path": aria2c or "",
        "transmission_rpc_url": transmission_rpc,
        "transmission_rpc_available": bool(transmission_ok),
    }


def _ip_is_public(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _host_is_public(hostname):
    if not hostname:
        return False
    try:
        _resolve_public_endpoint(hostname, 80)
    except RemoteDownloadError:
        return False
    return True


def _tracker_hostname_is_definitely_private(hostname):
    host = str(hostname or "").strip().rstrip(".").lower()
    if not host:
        return True
    if host in {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}:
        return True
    if host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        return not _ip_is_public(str(ipaddress.ip_address(host)))
    except ValueError:
        return False


def _resolve_public_endpoint(hostname, port):
    if not hostname:
        raise RemoteDownloadError("下載網址缺少主機名稱")
    # If hostname is a literal IP address, block private ones directly
    try:
        literal_ip = ipaddress.ip_address(hostname)
        if not _ip_is_public(str(literal_ip)):
            raise RemoteDownloadError(f"下載網址不可指向 localhost、內網或保留位址（{hostname}）")
        family = socket.AF_INET6 if isinstance(literal_ip, ipaddress.IPv6Address) else socket.AF_INET
        return (family, socket.SOCK_STREAM, 0, (hostname, port))
    except RemoteDownloadError:
        raise
    except ValueError:
        pass  # Not a literal IP — it's a domain name, proceed with DNS
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RemoteDownloadError("下載網址無法解析") from exc
    if not infos:
        raise RemoteDownloadError("下載網址無法解析")
    candidates = []
    blocked = []
    for family, socktype, proto, _, sockaddr in infos:
        address = sockaddr[0]
        if not _ip_is_public(address):
            blocked.append(address)
            continue
        candidates.append((family, socktype, proto, sockaddr))
    if not candidates:
        suffix = ""
        if blocked:
            suffix = f"（{hostname} -> {', '.join(sorted(set(blocked)))}）"
        raise RemoteDownloadError(f"下載網址不可指向 localhost、內網或保留位址{suffix}")
    return candidates[0]


def _validate_tracker_url(tracker_url):
    parsed = urllib.parse.urlparse(str(tracker_url or "").strip())
    if parsed.scheme not in {"http", "https", "udp"} or not parsed.hostname:
        raise RemoteDownloadError("BT tracker URL 格式不支援")
    if _tracker_hostname_is_definitely_private(parsed.hostname):
        raise RemoteDownloadError(f"BT tracker 不可指向 localhost、內網或保留位址（{parsed.hostname}）")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise RemoteDownloadError("BT tracker URL 格式不支援") from exc
    try:
        _resolve_public_endpoint(parsed.hostname, port)
    except RemoteDownloadError as exc:
        if str(exc) == "下載網址無法解析":
            # Tracker DNS can be flaky and aria2 can still use DHT,
            # peer exchange, and supplemental public trackers. Treat
            # unresolvable public-looking tracker domains as
            # unavailable, not unsafe.
            return
        raise


def validate_magnet_trackers(url):
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    trackers = [tracker for tracker in params.get("tr", []) if str(tracker or "").strip()]
    blocked = []
    for tracker in trackers:
        try:
            _validate_tracker_url(tracker)
        except RemoteDownloadError as exc:
            blocked.append({"url": tracker, "reason": str(exc)})
    return {"trackers": trackers, "blocked": blocked}


def _bdecode(data, index=0, depth=0):
    if depth > MAX_BDECODE_DEPTH:
        raise ValueError("bencode nesting depth exceeded")
    if index >= len(data):
        raise ValueError("unexpected end of bencode data")
    marker = data[index:index + 1]
    if marker == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1:end]), end + 1
    if marker == b"l":
        index += 1
        out = []
        while data[index:index + 1] != b"e":
            item, index = _bdecode(data, index, depth + 1)
            out.append(item)
        return out, index + 1
    if marker == b"d":
        index += 1
        out = {}
        while data[index:index + 1] != b"e":
            key, index = _bdecode(data, index, depth + 1)
            value, index = _bdecode(data, index, depth + 1)
            out[key] = value
        return out, index + 1
    if marker.isdigit():
        colon = data.index(b":", index)
        length = int(data[index:colon])
        start = colon + 1
        end = start + length
        return data[start:end], end
    raise ValueError("invalid bencode")


def _decode_tracker_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _torrent_file_trackers(torrent_path):
    try:
        with open(torrent_path, "rb") as fh:
            data = fh.read(2 * 1024 * 1024 + 1)
        decoded, _ = _bdecode(data)
    except Exception as exc:
        raise RemoteDownloadError("BT 種子檔格式無法解析") from exc
    if not isinstance(decoded, dict):
        raise RemoteDownloadError("BT 種子檔格式無效")
    trackers = []
    announce = decoded.get(b"announce")
    if announce:
        trackers.append(_decode_tracker_value(announce))
    announce_list = decoded.get(b"announce-list")
    if isinstance(announce_list, list):
        for tier in announce_list:
            if isinstance(tier, list):
                trackers.extend(_decode_tracker_value(item) for item in tier)
            else:
                trackers.append(_decode_tracker_value(tier))
    return [tracker for tracker in trackers if str(tracker or "").strip()]


def inspect_torrent_file_trackers(torrent_path):
    trackers = _torrent_file_trackers(torrent_path)
    blocked = []
    for tracker in trackers:
        try:
            _validate_tracker_url(tracker)
        except RemoteDownloadError as exc:
            blocked.append({"url": tracker, "reason": str(exc)})
    return {"trackers": trackers, "blocked": blocked}


def validate_torrent_file_trackers(torrent_path):
    report = inspect_torrent_file_trackers(torrent_path)
    return report


def validate_remote_url(raw_url):
    url = str(raw_url or "").strip()
    if not url:
        raise RemoteDownloadError("請輸入下載網址")
    if url.startswith("magnet:?"):
        validate_magnet_trackers(url)
        return {"kind": "magnet", "url": url}
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise RemoteDownloadError("只支援 http、https direct link 或 magnet link")
    _resolve_public_endpoint(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    if parsed.path.lower().endswith(".torrent"):
        return {"kind": "torrent_url", "url": url}
    return {"kind": "direct", "url": url}


def _filename_from_response(url, headers):
    disposition = headers.get("Content-Disposition") or ""
    _, params = urllib.request.parse_http_list(disposition), {}
    for item in urllib.request.parse_http_list(disposition):
        if "=" in item:
            key, value = item.split("=", 1)
            params[key.strip().lower()] = value.strip().strip('"')
    filename = params.get("filename*") or params.get("filename")
    if filename and "''" in filename:
        filename = urllib.parse.unquote(filename.split("''", 1)[1])
    if not filename:
        filename = Path(urllib.parse.urlparse(url).path).name
    return safe_public_filename(filename or "remote-download.bin")


def _emit_progress(progress_callback, **payload):
    if not progress_callback:
        return
    try:
        progress_callback(payload)
    except Exception:
        pass


def _check_remote_download_control(cancel_check):
    if not cancel_check:
        return
    cancel_check()


def _progress_speed_bytes_per_sec(current_bytes, previous_bytes, current_ts, previous_ts):
    try:
        current = int(current_bytes or 0)
        previous = int(previous_bytes or 0)
        delta_t = float(current_ts or 0) - float(previous_ts or 0)
    except Exception:
        return 0
    if current < previous or delta_t <= 0:
        return 0
    return int((current - previous) / delta_t)


def _positive_int(value, default):
    try:
        parsed = int(value)
    except Exception:
        return int(default)
    return parsed if parsed > 0 else int(default)


def _positive_float(value, default):
    try:
        parsed = float(value)
    except Exception:
        return float(default)
    return parsed if parsed > 0 else float(default)


def _env_positive_int(name, default):
    return _positive_int(os.environ.get(name), default)


def _env_positive_float(name, default):
    return _positive_float(os.environ.get(name), default)


def _bt_idle_timeout_seconds(timeout_seconds):
    default_timeout = _positive_int(timeout_seconds, 1800)
    return _env_positive_int("HACKME_BT_IDLE_TIMEOUT_SECONDS", default_timeout)


def _bt_absolute_timeout_seconds():
    raw = os.environ.get("HACKME_BT_MAX_RUNTIME_SECONDS")
    if raw is None:
        return BT_DEFAULT_MAX_RUNTIME_SECONDS
    try:
        parsed = int(raw)
    except Exception:
        return BT_DEFAULT_MAX_RUNTIME_SECONDS
    return max(0, parsed)


def _bt_stop_timeout_seconds(idle_timeout_seconds):
    raw = os.environ.get("HACKME_ARIA2_BT_STOP_TIMEOUT_SECONDS")
    if raw is not None:
        try:
            return max(0, int(raw))
        except Exception:
            pass
    return max(600, min(int(idle_timeout_seconds or 1800), 3600))


def _bt_progress_interval_seconds():
    return max(0.5, min(10.0, _env_positive_float("HACKME_BT_PROGRESS_INTERVAL_SECONDS", 2.0)))


def _safe_staging_component(value, fallback):
    text = safe_public_filename(str(value or "").strip())
    return text or str(fallback)


def _bt_staging_root():
    raw = str(os.environ.get("HACKME_BT_DOWNLOAD_STAGING_DIR", "") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _make_bt_tempdir(prefix, *, owner_user_id=None, task_id=None):
    root = _bt_staging_root()
    if root is None:
        return tempfile.mkdtemp(prefix=prefix)
    user_part = _safe_staging_component(f"user-{owner_user_id}", "user-unknown")
    task_part = _safe_staging_component(f"task-{task_id}", "task-unknown")
    parent = root / user_part / task_part
    parent.mkdir(parents=True, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix=prefix, dir=str(parent))
    try:
        os.chmod(tmpdir, 0o2770)
    except OSError:
        pass
    return tmpdir


def _directory_downloaded_bytes(path):
    total = 0
    for item in Path(path).rglob("*"):
        if not item.is_file() or item.name == "aria2.log":
            continue
        try:
            stat = item.stat()
            total += stat.st_size
        except OSError:
            pass
    return total


def _file_allocated_bytes(stat):
    blocks = int(getattr(stat, "st_blocks", 0) or 0)
    if blocks > 0:
        return blocks * 512
    return int(getattr(stat, "st_size", 0) or 0)


def _directory_download_progress(path):
    loaded = 0
    total = 0
    root = Path(path)
    for item in root.rglob("*"):
        if not item.is_file() or item.name == "aria2.log" or item.name.endswith(".aria2"):
            continue
        try:
            stat = item.stat()
        except OSError:
            continue
        logical_size = int(stat.st_size or 0)
        total += logical_size
        companion = item.with_name(item.name + ".aria2")
        if companion.exists():
            loaded += min(logical_size, _file_allocated_bytes(stat))
        else:
            loaded += logical_size
    return {"loaded_bytes": loaded, "total_bytes": total or None}


def _http_response_once(url, *, timeout_seconds=60):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise RemoteDownloadError("只支援 http 或 https direct link")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    family, socktype, proto, sockaddr = _resolve_public_endpoint(parsed.hostname, port)
    sock = socket.socket(family, socktype, proto)
    sock.settimeout(timeout_seconds)
    try:
        sock.connect(sockaddr)
        if parsed.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            sock.settimeout(timeout_seconds)
        path = urllib.parse.urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
        host_header = parsed.hostname or ""
        if parsed.port and parsed.port not in {80, 443}:
            host_header = f"{host_header}:{parsed.port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "User-Agent: hackme_web-remote-downloader/1.0\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii", "ignore")
        sock.sendall(request)
        response = http.client.HTTPResponse(sock)
        response.begin()
        return response, sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


def _open_http_response(url, *, timeout_seconds=60, redirects=0):
    response, sock = _http_response_once(url, timeout_seconds=timeout_seconds)
    if response.status in {301, 302, 303, 307, 308}:
        location = response.getheader("Location")
        response.close()
        sock.close()
        if redirects >= 3 or not location:
            raise RemoteDownloadError("遠端下載重新導向次數過多")
        next_url = urllib.parse.urljoin(url, location)
        validate_remote_url(next_url)
        return _open_http_response(next_url, timeout_seconds=timeout_seconds, redirects=redirects + 1)
    if response.status >= 400:
        response.close()
        sock.close()
        raise RemoteDownloadError(f"遠端伺服器回應 HTTP {response.status}")
    return response, sock


def download_direct_link(url, *, timeout_seconds=60, max_bytes=None, progress_callback=None, rate_limit_kb_per_sec=None, cancel_check=None):
    tmpdir = tempfile.mkdtemp(prefix="hackme_remote_")
    response = None
    sock = None
    try:
        _check_remote_download_control(cancel_check)
        response, sock = _open_http_response(url, timeout_seconds=timeout_seconds)
        _check_remote_download_control(cancel_check)
        filename = _filename_from_response(url, response.headers)
        mimetype = response.headers.get_content_type() or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        total_bytes = None
        try:
            length = response.headers.get("Content-Length")
            total_bytes = int(length) if length else None
        except Exception:
            total_bytes = None
        if max_bytes is not None and total_bytes is not None and total_bytes > int(max_bytes):
            raise RemoteDownloadError("遠端檔案超過容量限制")
        target = os.path.join(tmpdir, filename)
        total = 0
        last_progress_bytes = 0
        last_progress_ts = time.monotonic()
        _emit_progress(progress_callback, phase="downloading", filename=filename, loaded_bytes=0, total_bytes=total_bytes, speed_bytes_per_sec=0)
        started = time.monotonic()
        with open(target, "wb") as out:
            while True:
                _check_remote_download_control(cancel_check)
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                _check_remote_download_control(cancel_check)
                total += len(chunk)
                if max_bytes is not None and total > int(max_bytes):
                    raise RemoteDownloadError("遠端檔案超過容量限制")
                out.write(chunk)
                now_ts = time.monotonic()
                speed = _progress_speed_bytes_per_sec(total, last_progress_bytes, now_ts, last_progress_ts)
                _emit_progress(progress_callback, phase="downloading", filename=filename, loaded_bytes=total, total_bytes=total_bytes, speed_bytes_per_sec=speed)
                last_progress_bytes = total
                last_progress_ts = now_ts
                if rate_limit_kb_per_sec:
                    expected_elapsed = total / max(1, int(rate_limit_kb_per_sec) * 1024)
                    elapsed = time.monotonic() - started
                    if expected_elapsed > elapsed:
                        time.sleep(min(1.0, expected_elapsed - elapsed))
                        _check_remote_download_control(cancel_check)
        _check_remote_download_control(cancel_check)
        _emit_progress(progress_callback, phase="downloaded", filename=filename, loaded_bytes=total, total_bytes=total_bytes, speed_bytes_per_sec=0)
        return DownloadedFile(path=target, filename=filename, mimetype=mimetype, cleanup_dir=tmpdir)
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RemoteDownloadError(f"遠端下載失敗：{exc}") from exc
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    finally:
        if response:
            response.close()
        if sock:
            sock.close()


def _zip_download_dir(tmpdir, files):
    archive = os.path.join(tmpdir, "bt-download.zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_out:
        for path in files:
            zip_out.write(path, arcname=os.path.relpath(path, tmpdir))
    return archive


def _tail_lines(text, *, max_lines=8, max_chars=800):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])[-max_chars:]


def _read_tail(path, *, max_lines=12, max_chars=1200):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = "\n".join(deque(fh, maxlen=max(1, int(max_lines or 12))))
            return _tail_lines(text, max_lines=max_lines, max_chars=max_chars)
    except OSError:
        return ""


def _transmission_rpc_call(method, arguments=None, *, timeout_seconds=30, session_id=None):
    url = _transmission_rpc_url()
    if not url:
        raise RemoteDownloadError("Transmission RPC URL 未設定")
    payload = {"method": method, "arguments": arguments or {}}
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if session_id:
        headers["X-Transmission-Session-Id"] = session_id
    auth = _transmission_rpc_auth_header()
    if auth:
        headers["Authorization"] = auth
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            new_session_id = exc.headers.get("X-Transmission-Session-Id")
            if new_session_id and not session_id:
                return _transmission_rpc_call(method, arguments, timeout_seconds=timeout_seconds, session_id=new_session_id)
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise RemoteDownloadError(f"Transmission RPC 回應 HTTP {exc.code}{(': ' + detail) if detail else ''}") from exc
    except urllib.error.URLError as exc:
        raise RemoteDownloadError(f"Transmission RPC 無法連線：{exc.reason}") from exc
    try:
        parsed = json.loads(body)
    except Exception as exc:
        raise RemoteDownloadError("Transmission RPC 回應不是 JSON") from exc
    if parsed.get("result") != "success":
        raise RemoteDownloadError(f"Transmission RPC 失敗：{parsed.get('result') or 'unknown'}")
    return parsed.get("arguments") or {}


def _transmission_failure_message(detail):
    text = str(detail or "").strip()
    if not text:
        return "BT/magnet 下載失敗：Transmission 未提供錯誤細節"
    return f"BT/magnet 下載失敗：Transmission：{text}"


def _magnet_has_trackers(source):
    parsed = urllib.parse.urlparse(str(source or ""))
    if parsed.scheme.lower() != "magnet":
        return False
    params = urllib.parse.parse_qs(parsed.query)
    return any(str(item or "").strip() for item in params.get("tr", []))


def _supplement_transmission_magnet_trackers(torrent_id, source):
    if torrent_id is None or _magnet_has_trackers(source):
        return
    try:
        _transmission_rpc_call(
            "torrent-set",
            {"ids": [torrent_id], "trackerAdd": PUBLIC_BT_TRACKERS},
            timeout_seconds=10,
        )
        _transmission_rpc_call("torrent-reannounce", {"ids": [torrent_id]}, timeout_seconds=10)
    except Exception:
        # Tracker supplementation is best-effort; DHT/PEX may still succeed.
        return


def _torrent_files_from_transmission(tmpdir):
    files = []
    root = Path(tmpdir)
    for path in root.rglob("*"):
        if not path.is_file() or path.name.endswith(".part"):
            continue
        try:
            if path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        files.append(str(path))
    return files


def _transmission_completed_file_candidates(item, tmpdir, incomplete_dir=None):
    files_meta = item.get("files") or []
    roots = []
    for raw_root in (item.get("downloadDir"), tmpdir, incomplete_dir):
        if not raw_root:
            continue
        root = Path(str(raw_root)).expanduser()
        if root not in roots:
            roots.append(root)
    candidates = []
    seen = set()
    torrent_name = str(item.get("name") or "").strip()
    for meta in files_meta:
        rel_name = str((meta or {}).get("name") or "").strip()
        if not rel_name:
            continue
        length = int((meta or {}).get("length") or 0)
        completed = int((meta or {}).get("bytesCompleted") or 0)
        if length > 0 and completed < length:
            continue
        rel_path = Path(rel_name)
        rel_candidates = [rel_path]
        if torrent_name:
            rel_candidates.append(Path(torrent_name) / rel_path)
        if len(files_meta) == 1:
            rel_candidates.append(Path(rel_path.name))
        for root in roots:
            for rel in rel_candidates:
                path = (root / rel).resolve(strict=False)
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                if not path.is_file() or path.name.endswith(".part"):
                    continue
                try:
                    if path.stat().st_size <= 0:
                        continue
                except OSError:
                    continue
                candidates.append(str(path))
    return candidates


def _stage_transmission_completed_files(candidates, tmpdir):
    staged = []
    root = Path(tmpdir).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    for source in candidates:
        src = Path(source).resolve(strict=False)
        if not src.is_file():
            continue
        try:
            src.relative_to(root)
            staged.append(str(src))
            continue
        except ValueError:
            pass
        target = root / safe_public_filename(src.name)
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            index = 2
            while target.exists():
                target = root / f"{stem}-{index}{suffix}"
                index += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        staged.append(str(target))
    return staged


def _resolve_magnet_metadata_with_aria2(source, *, timeout_seconds=300, cancel_check=None, owner_user_id=None, task_id=None):
    if not str(source or "").startswith("magnet:?"):
        return None
    aria2c = shutil.which("aria2c")
    if not aria2c:
        return None
    tmpdir = _make_bt_tempdir("hackme_bt_metadata_", owner_user_id=owner_user_id, task_id=task_id)
    try:
        metadata_timeout = max(30, min(600, _positive_int(os.environ.get("HACKME_BT_METADATA_TIMEOUT_SECONDS"), min(int(timeout_seconds or 300), 300))))
        cmd = [
            aria2c,
            "--dir", tmpdir,
            "--bt-metadata-only=true",
            "--bt-save-metadata=true",
            f"--bt-stop-timeout={min(metadata_timeout, 300)}",
            "--bt-enable-lpd=false",
            "--enable-dht=true",
            "--enable-peer-exchange=true",
            f"--bt-tracker={','.join(PUBLIC_BT_TRACKERS)}",
            "--summary-interval=0",
            "--console-log-level=warn",
            source,
        ]
        _check_remote_download_control(cancel_check)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started = time.monotonic()
        while proc.poll() is None:
            _check_remote_download_control(cancel_check)
            if time.monotonic() - started > metadata_timeout:
                _terminate_child_process(proc)
                return None
            time.sleep(0.5)
        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            return None
        torrent_files = sorted(Path(tmpdir).glob("*.torrent"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not torrent_files:
            return None
        with open(torrent_files[0], "rb") as fh:
            return fh.read()
    except (RemoteDownloadCancelled, RemoteDownloadPaused):
        raise
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _download_bt_with_transmission(source, *, source_label="BT/magnet", source_is_torrent_file=False, resolve_magnet_metadata=False, timeout_seconds=300, max_bytes=None, progress_callback=None, rate_limit_kb_per_sec=None, cancel_check=None, owner_user_id=None, task_id=None):
    tmpdir = _make_bt_tempdir("hackme_bt_transmission_", owner_user_id=owner_user_id, task_id=task_id)
    torrent_id = None
    idle_timeout_seconds = _bt_idle_timeout_seconds(timeout_seconds)
    absolute_timeout_seconds = _bt_absolute_timeout_seconds()
    progress_interval_seconds = _bt_progress_interval_seconds()
    session = {}
    incomplete_dir = None
    try:
        _check_remote_download_control(cancel_check)
        try:
            session = _transmission_rpc_call("session-get", timeout_seconds=10)
        except Exception:
            session = {}
        if session.get("incomplete-dir-enabled") and session.get("incomplete-dir"):
            incomplete_dir = str(session.get("incomplete-dir"))
        add_args = {"download-dir": tmpdir, "paused": False}
        magnet_metainfo = None
        if resolve_magnet_metadata and not source_is_torrent_file and str(source or "").startswith("magnet:?"):
            _emit_progress(progress_callback, phase="metadata", filename=source_label, loaded_bytes=0, total_bytes=None, speed_bytes_per_sec=0)
            magnet_metainfo = _resolve_magnet_metadata_with_aria2(
                source,
                timeout_seconds=timeout_seconds,
                cancel_check=cancel_check,
                owner_user_id=owner_user_id,
                task_id=task_id,
            )
        if magnet_metainfo:
            add_args["metainfo"] = base64.b64encode(magnet_metainfo).decode("ascii")
        elif source_is_torrent_file:
            with open(source, "rb") as fh:
                add_args["metainfo"] = base64.b64encode(fh.read()).decode("ascii")
        else:
            add_args["filename"] = source
        added = _transmission_rpc_call("torrent-add", add_args, timeout_seconds=min(30, max(5, int(timeout_seconds or 30))))
        torrent = added.get("torrent-added") or added.get("torrent-duplicate") or {}
        torrent_id = torrent.get("id")
        if torrent_id is None:
            raise RemoteDownloadError("Transmission 未回傳 torrent id")
        if not source_is_torrent_file:
            _supplement_transmission_magnet_trackers(torrent_id, source)
        if rate_limit_kb_per_sec:
            _transmission_rpc_call(
                "torrent-set",
                {"ids": [torrent_id], "downloadLimited": True, "downloadLimit": int(rate_limit_kb_per_sec)},
                timeout_seconds=10,
            )
        started = time.monotonic()
        last_progress_bytes = 0
        last_progress_ts = started
        last_activity_bytes = 0
        last_activity_ts = started
        _emit_progress(progress_callback, phase="downloading", filename=source_label, loaded_bytes=0, total_bytes=None, speed_bytes_per_sec=0)
        while True:
            try:
                _check_remote_download_control(cancel_check)
            except (RemoteDownloadCancelled, RemoteDownloadPaused):
                _transmission_rpc_call("torrent-remove", {"ids": [torrent_id], "delete-local-data": True}, timeout_seconds=10)
                torrent_id = None
                raise
            now_ts = time.monotonic()
            fields = ["id", "name", "status", "percentDone", "totalSize", "downloadedEver", "rateDownload", "eta", "error", "errorString", "files", "downloadDir", "leftUntilDone", "isFinished"]
            info = _transmission_rpc_call("torrent-get", {"ids": [torrent_id], "fields": fields}, timeout_seconds=10)
            torrents = info.get("torrents") or []
            if not torrents:
                raise RemoteDownloadError("Transmission 任務不存在")
            item = torrents[0]
            name = safe_public_filename(item.get("name") or source_label) or source_label
            total_size = item.get("totalSize") or None
            downloaded = int(item.get("downloadedEver") or _directory_downloaded_bytes(tmpdir))
            if downloaded > last_activity_bytes:
                last_activity_bytes = downloaded
                last_activity_ts = now_ts
            if item.get("error"):
                raise RemoteDownloadError(_transmission_failure_message(item.get("errorString")))
            if max_bytes is not None and downloaded > int(max_bytes):
                _transmission_rpc_call("torrent-remove", {"ids": [torrent_id], "delete-local-data": True}, timeout_seconds=10)
                torrent_id = None
                raise RemoteDownloadError("BT 下載內容超過容量限制")
            if absolute_timeout_seconds and now_ts - started > absolute_timeout_seconds:
                _transmission_rpc_call("torrent-remove", {"ids": [torrent_id], "delete-local-data": True}, timeout_seconds=10)
                torrent_id = None
                raise RemoteDownloadError(f"BT 下載超過最長執行時間（{absolute_timeout_seconds} 秒），已停止。")
            if idle_timeout_seconds and now_ts - last_activity_ts > idle_timeout_seconds:
                _transmission_rpc_call("torrent-remove", {"ids": [torrent_id], "delete-local-data": True}, timeout_seconds=10)
                torrent_id = None
                raise RemoteDownloadError(f"BT 下載停滯逾時：已 {idle_timeout_seconds} 秒沒有下載進度。請確認做種/節點、tracker、DHT 與防火牆狀態。")
            speed = int(item.get("rateDownload") or _progress_speed_bytes_per_sec(downloaded, last_progress_bytes, now_ts, last_progress_ts))
            progress_percent = None
            try:
                progress_percent = max(0, min(100, round(float(item.get("percentDone") or 0) * 100, 1)))
            except Exception:
                progress_percent = None
            _emit_progress(
                progress_callback,
                phase="downloading",
                filename=name,
                loaded_bytes=downloaded,
                total_bytes=total_size,
                speed_bytes_per_sec=speed,
                progress_percent=progress_percent,
                eta_seconds=item.get("eta"),
                transmission_status=item.get("status"),
                transmission_torrent_id=torrent_id,
            )
            last_progress_bytes = downloaded
            last_progress_ts = now_ts
            completed_candidates = _transmission_completed_file_candidates(item, tmpdir, incomplete_dir)
            left_until_done = int(item.get("leftUntilDone") or 0)
            if float(item.get("percentDone") or 0) >= 1.0 and left_until_done <= 0 and (completed_candidates or _torrent_files_from_transmission(tmpdir)):
                break
            time.sleep(progress_interval_seconds)
        _check_remote_download_control(cancel_check)
        completed_candidates = _transmission_completed_file_candidates(item, tmpdir, incomplete_dir)
        _transmission_rpc_call("torrent-remove", {"ids": [torrent_id], "delete-local-data": False}, timeout_seconds=10)
        torrent_id = None
        if completed_candidates:
            _stage_transmission_completed_files(completed_candidates, tmpdir)
        files = _torrent_files_from_transmission(tmpdir)
        if not files:
            raise RemoteDownloadError("BT 下載沒有產生可保存的檔案")
        if max_bytes is not None:
            total_downloaded = sum(os.path.getsize(path) for path in files)
            if total_downloaded > int(max_bytes):
                raise RemoteDownloadError("BT 下載內容超過容量限制")
        if len(files) == 1:
            target = files[0]
            filename = safe_public_filename(Path(target).name)
            mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        else:
            target = _zip_download_dir(tmpdir, files)
            filename = "bt-download.zip"
            mimetype = "application/zip"
        try:
            total = os.path.getsize(target)
        except OSError:
            total = None
        _emit_progress(progress_callback, phase="downloaded", filename=filename, loaded_bytes=total, total_bytes=total, speed_bytes_per_sec=0)
        return DownloadedFile(path=target, filename=filename, mimetype=mimetype, cleanup_dir=tmpdir)
    except Exception:
        if torrent_id is not None:
            try:
                _transmission_rpc_call("torrent-remove", {"ids": [torrent_id], "delete-local-data": True}, timeout_seconds=10)
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def _download_bt_with_preferred_backend(source, *, source_label="BT/magnet", source_is_torrent_file=False, timeout_seconds=300, max_bytes=None, progress_callback=None, rate_limit_kb_per_sec=None, exclude_trackers=None, cancel_check=None, owner_user_id=None, task_id=None):
    backend = _bt_backend_preference()
    if backend in {"auto", "transmission"}:
        try:
            return _download_bt_with_transmission(
                source,
                source_label=source_label,
                source_is_torrent_file=source_is_torrent_file,
                resolve_magnet_metadata=backend == "transmission",
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
                progress_callback=progress_callback,
                rate_limit_kb_per_sec=rate_limit_kb_per_sec,
                cancel_check=cancel_check,
                owner_user_id=owner_user_id,
                task_id=task_id,
            )
        except (RemoteDownloadCancelled, RemoteDownloadPaused):
            raise
        except RemoteDownloadError as exc:
            message = str(exc)
            if backend == "transmission" or not shutil.which("aria2c"):
                raise
            if not (message.startswith("Transmission RPC") or "Transmission 未回傳 torrent id" in message):
                raise
    return _download_bt_with_aria2(
        source,
        source_label=source_label,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        progress_callback=progress_callback,
        rate_limit_kb_per_sec=rate_limit_kb_per_sec,
        exclude_trackers=exclude_trackers,
        cancel_check=cancel_check,
        owner_user_id=owner_user_id,
        task_id=task_id,
    )


def _aria2_failure_message(proc, log_path):
    log_tail = _read_tail(log_path)
    output_tail = _tail_lines((proc.stderr or "") + "\n" + (proc.stdout or ""))
    generic = "If there are any errors, then see the log file"
    combined = "\n".join([log_tail, output_tail])
    if "failed to bind" in combined or "Errors occurred while binding port" in combined:
        return "BT/magnet 下載失敗：aria2c 無法綁定 BT/DHT 連接埠。請確認 server 不是在受限沙盒中執行，並允許 aria2c 開啟 TCP/UDP BT 連接埠。"
    if "Stop downloading torrent due to --bt-stop-timeout option" in combined or "[METADATA]" in combined:
        return "BT/magnet 下載失敗：指定時間內抓不到 torrent metadata。常見原因是做種/節點太少、tracker 無回應、DHT 被網路或防火牆阻擋，或該 magnet 已失效。請換其他 magnet、補充 tracker，或稍後再試。"
    candidates = []
    for text in (log_tail, output_tail):
        filtered_lines = []
        for line in str(text or "").splitlines():
            if generic in line:
                continue
            if "NOTICE" in line and "error" not in line.lower() and "failure" not in line.lower():
                continue
            filtered_lines.append(line)
        filtered = "\n".join(filtered_lines).strip()
        if filtered:
            candidates.append(filtered)
    detail = candidates[0] if candidates else ""
    if not detail:
        return "BT/magnet 下載失敗：aria2c 未提供錯誤細節"
    return f"BT/magnet 下載失敗：{detail}"


def _terminate_child_process(proc, *, timeout=5):
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.communicate(timeout=timeout)
        return
    except Exception:
        pass
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    try:
        proc.communicate(timeout=timeout)
    except Exception:
        pass


def _download_bt_with_aria2(source, *, source_label="BT/magnet", timeout_seconds=300, max_bytes=None, progress_callback=None, rate_limit_kb_per_sec=None, exclude_trackers=None, cancel_check=None, owner_user_id=None, task_id=None):
    aria2c = shutil.which("aria2c")
    if not aria2c:
        raise RemoteDownloadError("BT 下載需要先安裝 aria2c")
    tmpdir = _make_bt_tempdir("hackme_bt_", owner_user_id=owner_user_id, task_id=task_id)
    log_path = os.path.join(tmpdir, "aria2.log")
    idle_timeout_seconds = _bt_idle_timeout_seconds(timeout_seconds)
    absolute_timeout_seconds = _bt_absolute_timeout_seconds()
    progress_interval_seconds = _bt_progress_interval_seconds()
    # Public trackers to supplement DHT for better magnet-link peer discovery.
    public_trackers = ",".join(PUBLIC_BT_TRACKERS)
    cmd = [
        aria2c,
        "--dir", tmpdir,
        "--log", log_path,
        "--log-level=notice",
        "--seed-time=0",
        f"--bt-stop-timeout={_bt_stop_timeout_seconds(idle_timeout_seconds)}",
        "--bt-enable-lpd=false",
        "--enable-dht=true",
        "--enable-peer-exchange=true",
        f"--bt-tracker={public_trackers}",
        "--max-tries=2",
        "--max-file-not-found=2",
        "--file-allocation=none",
        "--follow-torrent=mem",
        "--allow-overwrite=false",
        "--auto-file-renaming=true",
        "--summary-interval=0",
        "--console-log-level=warn",
        source,
    ]
    if rate_limit_kb_per_sec:
        cmd[1:1] = ["--max-download-limit", f"{int(rate_limit_kb_per_sec)}K"]
    safe_excludes = [str(item or "").strip() for item in (exclude_trackers or []) if str(item or "").strip()]
    if safe_excludes:
        cmd[1:1] = ["--bt-exclude-tracker", ",".join(safe_excludes)]
    try:
        _check_remote_download_control(cancel_check)
        _emit_progress(progress_callback, phase="downloading", filename=source_label, loaded_bytes=0, total_bytes=max_bytes, speed_bytes_per_sec=0)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started = time.monotonic()
        last_progress_bytes = 0
        last_progress_ts = started
        last_activity_bytes = 0
        last_activity_ts = started
        while proc.poll() is None:
            try:
                _check_remote_download_control(cancel_check)
            except (RemoteDownloadCancelled, RemoteDownloadPaused):
                _terminate_child_process(proc)
                raise
            now_ts = time.monotonic()
            progress = _directory_download_progress(tmpdir)
            total_downloaded = int(progress.get("loaded_bytes") or 0)
            total_bytes = progress.get("total_bytes")
            if total_downloaded > last_activity_bytes:
                last_activity_bytes = total_downloaded
                last_activity_ts = now_ts
            if absolute_timeout_seconds and now_ts - started > absolute_timeout_seconds:
                proc.kill()
                proc.communicate(timeout=5)
                raise RemoteDownloadError(f"BT 下載超過最長執行時間（{absolute_timeout_seconds} 秒），已停止。")
            if idle_timeout_seconds and now_ts - last_activity_ts > idle_timeout_seconds:
                proc.kill()
                proc.communicate(timeout=5)
                raise RemoteDownloadError(f"BT 下載停滯逾時：已 {idle_timeout_seconds} 秒沒有下載進度。請確認做種/節點、tracker、DHT 與防火牆狀態。")
            if max_bytes is not None:
                if total_downloaded > int(max_bytes) or (total_bytes is not None and int(total_bytes) > int(max_bytes)):
                    proc.kill()
                    proc.communicate(timeout=5)
                    raise RemoteDownloadError("BT 下載內容超過容量限制")
            speed = _progress_speed_bytes_per_sec(total_downloaded, last_progress_bytes, now_ts, last_progress_ts)
            _emit_progress(progress_callback, phase="downloading", filename=source_label, loaded_bytes=total_downloaded, total_bytes=total_bytes, speed_bytes_per_sec=speed)
            last_progress_bytes = total_downloaded
            last_progress_ts = now_ts
            time.sleep(progress_interval_seconds)
        _check_remote_download_control(cancel_check)
        stdout, stderr = proc.communicate()
        proc = subprocess.CompletedProcess(cmd, proc.returncode, stdout=stdout, stderr=stderr)
        if proc.returncode != 0:
            raise RemoteDownloadError(_aria2_failure_message(proc, log_path))
        files = [
            str(path)
            for path in Path(tmpdir).rglob("*")
            if path.is_file() and not path.name.endswith(".aria2") and path.name != "aria2.log"
        ]
        if not files:
            raise RemoteDownloadError("BT 下載沒有產生可保存的檔案")
        if max_bytes is not None:
            total_downloaded = sum(os.path.getsize(path) for path in files)
            if total_downloaded > int(max_bytes):
                raise RemoteDownloadError("BT 下載內容超過容量限制")
        if len(files) == 1:
            target = files[0]
            filename = safe_public_filename(Path(target).name)
            mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        else:
            target = _zip_download_dir(tmpdir, files)
            filename = "bt-download.zip"
            mimetype = "application/zip"
        try:
            total = os.path.getsize(target)
        except OSError:
            total = None
        _emit_progress(progress_callback, phase="downloaded", filename=filename, loaded_bytes=total, total_bytes=total, speed_bytes_per_sec=0)
        return DownloadedFile(path=target, filename=filename, mimetype=mimetype, cleanup_dir=tmpdir)
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RemoteDownloadError("BT 下載逾時") from exc
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def download_magnet_with_aria2(url, *, timeout_seconds=300, max_bytes=None, progress_callback=None, rate_limit_kb_per_sec=None, cancel_check=None, owner_user_id=None, task_id=None):
    tracker_report = validate_magnet_trackers(url)
    return _download_bt_with_preferred_backend(
        url,
        source_label="BT/magnet",
        source_is_torrent_file=False,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        progress_callback=progress_callback,
        rate_limit_kb_per_sec=rate_limit_kb_per_sec,
        exclude_trackers=[item["url"] for item in tracker_report.get("blocked", [])],
        cancel_check=cancel_check,
        owner_user_id=owner_user_id,
        task_id=task_id,
    )


def download_torrent_file_with_aria2(torrent_path, *, display_name="BT 檔案", timeout_seconds=300, max_bytes=None, progress_callback=None, rate_limit_kb_per_sec=None, cancel_check=None, owner_user_id=None, task_id=None):
    if not os.path.isfile(torrent_path):
        raise RemoteDownloadError("找不到 BT 種子檔")
    _check_remote_download_control(cancel_check)
    tracker_report = validate_torrent_file_trackers(torrent_path)
    return _download_bt_with_preferred_backend(
        torrent_path,
        source_label=display_name or "BT 檔案",
        source_is_torrent_file=True,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        progress_callback=progress_callback,
        rate_limit_kb_per_sec=rate_limit_kb_per_sec,
        exclude_trackers=[item["url"] for item in tracker_report.get("blocked", [])],
        cancel_check=cancel_check,
        owner_user_id=owner_user_id,
        task_id=task_id,
    )


def download_torrent_url_with_aria2(url, *, timeout_seconds=300, max_bytes=None, progress_callback=None, rate_limit_kb_per_sec=None, cancel_check=None, owner_user_id=None, task_id=None):
    parsed = validate_remote_url(url)
    if parsed["kind"] != "torrent_url":
        raise RemoteDownloadError("BT/torrent URL 必須指向 .torrent 種子檔")
    _check_remote_download_control(cancel_check)
    torrent_limit = 2 * 1024 * 1024
    torrent_file = download_direct_link(
        parsed["url"],
        timeout_seconds=min(int(timeout_seconds or 120), 120),
        max_bytes=torrent_limit,
        progress_callback=progress_callback,
        rate_limit_kb_per_sec=rate_limit_kb_per_sec,
        cancel_check=cancel_check,
    )
    try:
        return download_torrent_file_with_aria2(
            torrent_file.path,
            display_name=torrent_file.filename,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            progress_callback=progress_callback,
            rate_limit_kb_per_sec=rate_limit_kb_per_sec,
            cancel_check=cancel_check,
            owner_user_id=owner_user_id,
            task_id=task_id,
        )
    finally:
        if torrent_file.cleanup_dir:
            shutil.rmtree(torrent_file.cleanup_dir, ignore_errors=True)


def download_remote_url(url, *, timeout_seconds=120, max_bytes=None, progress_callback=None, rate_limit_kb_per_sec=None, treat_torrent_as_bt=True, cancel_check=None, owner_user_id=None, task_id=None):
    parsed = validate_remote_url(url)
    if parsed["kind"] == "magnet":
        return download_magnet_with_aria2(parsed["url"], timeout_seconds=timeout_seconds, max_bytes=max_bytes, progress_callback=progress_callback, rate_limit_kb_per_sec=rate_limit_kb_per_sec, cancel_check=cancel_check, owner_user_id=owner_user_id, task_id=task_id)
    if parsed["kind"] == "torrent_url" and treat_torrent_as_bt:
        return download_torrent_url_with_aria2(parsed["url"], timeout_seconds=timeout_seconds, max_bytes=max_bytes, progress_callback=progress_callback, rate_limit_kb_per_sec=rate_limit_kb_per_sec, cancel_check=cancel_check, owner_user_id=owner_user_id, task_id=task_id)
    return download_direct_link(parsed["url"], timeout_seconds=timeout_seconds, max_bytes=max_bytes, progress_callback=progress_callback, rate_limit_kb_per_sec=rate_limit_kb_per_sec, cancel_check=cancel_check)
