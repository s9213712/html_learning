"""Small app-level request backpressure gates.

This module is intentionally process-local. A bounded WSGI server still owns
the real execution capacity; these gates keep each worker process from spending
all request slots on normal/heavy feature traffic and preserve room for health
and status requests.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from flask import g, jsonify, request


HEALTH_FAST_LANE_PATHS = {
    "/livez",
    "/readyz",
    "/healthz",
    "/api/livez",
    "/api/readyz",
    "/api/healthz",
    "/api/version",
}

AUTH_FAST_LANE_PATHS = {
    "/api/csrf-token",
    "/api/login",
    "/api/me",
    "/api/site-config",
    "/api/captcha/challenge",
}

BOOTSTRAP_FAST_LANE_PATHS = {
    "/",
    "/index.html",
    "/favicon.ico",
    "/manifest.webmanifest",
    "/site.webmanifest",
    "/sw.js",
    "/service-worker.js",
    "/robots.txt",
}

STATIC_ASSET_SUFFIXES = (
    ".html",
    ".css",
    ".js",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
)

CSRF_EDGE_GUARD_PATHS = {
    "/api/csrf-token",
}

BACKPRESSURE_FAST_LANE_PREFIXES = (
    "/livez",
    "/readyz",
    "/healthz",
    "/api/livez",
    "/api/readyz",
    "/api/healthz",
    "/api/version",
    "/api/admin/health",
    "/api/root/status",
    "/api/root/backpressure",
    "/api/root/trading/background/status",
    "/api/trading/safety",
    "/api/comfyui/resources",
    "/api/games/multiplayer/invites/pending",
    "/api/notifications/unread-count",
    "/styles.css",
    "/experiments.css",
    "/i18n-language-switcher.css",
    "/js/",
    "/assets/",
)

ROOT_PRIORITY_PREFIXES = (
    "/api/root/",
    "/api/admin/",
)

HEAVY_EXACT_PATHS = {
    "/api/files/upload",
    "/api/cloud-drive/upload",
    "/api/videos/upload",
    "/api/videos/publish",
    "/api/comfyui/generate",
    "/api/root/comfyui/model-upload",
    "/api/root/comfyui/civitai/download",
    "/api/trading/workflow-editor/backtest",
    "/api/trading/bots/backtest",
}

HEAVY_PREFIXES = (
    "/api/cloud-drive/resumable-upload",
    "/api/cloud-drive/remote-download",
)

HEAVY_CONTAINS = (
    "/hls/",
    "/stream",
    "/ciphertext",
    "/download",
    "/e2ee-stream-v2/",
    "/ai-move",
)

AUTH_EDGE_GUARD_PATHS = {
    "/api/login",
    "/api/register",
    "/api/captcha/challenge",
    "/api/password-reset/request",
    "/api/password-reset/verify",
    "/api/password-reset/complete",
}

AUTH_EDGE_GUARD_PREFIXES = (
    "/api/password-reset",
    "/api/email-verification",
)

UPLOAD_EDGE_GUARD_PATHS = {
    "/api/files/upload",
    "/api/cloud-drive/upload",
    "/api/videos/upload",
    "/api/root/comfyui/model-upload",
}

UPLOAD_EDGE_GUARD_PREFIXES = (
    "/api/cloud-drive/remote-download",
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _setting_bool(settings: dict | None, key: str, default: bool) -> bool:
    if not settings or key not in settings:
        return default
    raw = settings.get(key)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _setting_int_or_none(settings: dict | None, key: str, minimum: int = 1, maximum: int = 4096) -> int | None:
    if not settings or key not in settings:
        return None
    raw = settings.get(key)
    try:
        parsed = int(raw)
    except Exception:
        return None
    if parsed <= 0:
        return None
    return max(minimum, min(maximum, parsed))


def _setting_mode(settings: dict | None) -> str:
    mode = str((settings or {}).get("server_backpressure_mode") or "auto").strip().lower()
    if mode in {"manual", "override", "fixed"}:
        return "manual"
    if mode in {"off", "disabled", "disable"}:
        return "off"
    return "auto"


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.environ.get(name, str(default))).strip())
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _edge_guard_enabled() -> bool:
    return _env_bool("HACKME_EDGE_BURST_GUARD_ENABLED", True)


def _edge_guard_window_seconds() -> int:
    return _env_int("HACKME_EDGE_BURST_WINDOW_SECONDS", 10, 1, 300)


def _edge_guard_limit(label: str) -> int:
    defaults = {
        "csrf": 600,
        "auth": 40,
        "management": 90,
        "upload": 24,
    }
    env_names = {
        "csrf": "HACKME_EDGE_CSRF_BURST_LIMIT",
        "auth": "HACKME_EDGE_AUTH_BURST_LIMIT",
        "management": "HACKME_EDGE_MANAGEMENT_BURST_LIMIT",
        "upload": "HACKME_EDGE_UPLOAD_BURST_LIMIT",
    }
    return _env_int(env_names.get(label, "HACKME_EDGE_BURST_LIMIT"), defaults.get(label, 60), 1, 5000)


def _env_int_or_none(name: str, minimum: int = 1, maximum: int = 4096) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value or value in {"auto", "dynamic", "default"}:
        return None
    try:
        parsed = int(value)
    except Exception:
        return None
    return max(minimum, min(maximum, parsed))


def _parse_gunicorn_threads_from_tokens(tokens) -> int | None:
    values = list(tokens or [])
    for idx, token in enumerate(values):
        text = str(token or "").strip()
        if text == "--threads" and idx + 1 < len(values):
            try:
                parsed = int(str(values[idx + 1]).strip())
            except Exception:
                continue
            return max(1, min(256, parsed))
        if text.startswith("--threads="):
            try:
                parsed = int(text.split("=", 1)[1].strip())
            except Exception:
                continue
            return max(1, min(256, parsed))
    return None


def _gunicorn_thread_capacity_from_process() -> int | None:
    parsed = _parse_gunicorn_threads_from_tokens(getattr(sys, "argv", []) or [])
    if parsed:
        return parsed
    try:
        raw = open("/proc/self/cmdline", "rb").read()
    except Exception:
        return None
    try:
        tokens = [part.decode("utf-8", "ignore") for part in raw.split(b"\0") if part]
    except Exception:
        return None
    return _parse_gunicorn_threads_from_tokens(tokens)


def _total_memory_mb() -> int:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages and page_size:
            return int((int(pages) * int(page_size)) / (1024 * 1024))
    except Exception:
        pass
    return 0


def _cap_to_gunicorn_threads(value: int, gunicorn_threads: int | None) -> int:
    value = max(1, int(value or 1))
    if gunicorn_threads:
        return max(1, min(value, int(gunicorn_threads)))
    return value


def _thread_capacity(settings: dict | None = None) -> int:
    gunicorn_threads = _gunicorn_thread_capacity_from_process()
    setting_value = _setting_int_or_none(settings, "server_backpressure_thread_capacity", minimum=4, maximum=256)
    if setting_value:
        return _cap_to_gunicorn_threads(setting_value, gunicorn_threads)
    for name in (
        "HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY",
        "HACKME_DEV_GUNICORN_THREADS",
        "GUNICORN_THREADS",
    ):
        value = _env_int_or_none(name, minimum=4, maximum=256)
        if value:
            return _cap_to_gunicorn_threads(value, gunicorn_threads)
    if gunicorn_threads:
        # Auto-detected WSGI threads are not the same as effective app
        # throughput. SQLite write serialization, Python scheduling, and
        # chain/accounting critical sections mean this app often saturates
        # before every configured thread is productive. Keep auto mode
        # conservative; root can lower this through settings/env, but never
        # above the live gthread count for the current worker process.
        return max(1, min(12, gunicorn_threads))
    cpu_count = max(1, int(os.cpu_count() or 1))
    mem_mb = _total_memory_mb()
    cpu_cap = max(4, min(8, cpu_count))
    if mem_mb > 0:
        mem_cap = max(4, min(8, mem_mb // 512))
        return max(4, min(cpu_cap, mem_cap))
    return max(4, cpu_cap)


def _auto_fast_lane_reserved(thread_capacity: int, settings: dict | None = None) -> int:
    setting_value = _setting_int_or_none(settings, "server_backpressure_fast_lane_reserved", minimum=1, maximum=64)
    if setting_value:
        return min(setting_value, max(1, thread_capacity - 2))
    configured = _env_int_or_none("HTML_LEARNING_BACKPRESSURE_FAST_LANE_RESERVED", minimum=1, maximum=64)
    if configured:
        return min(configured, max(1, thread_capacity - 2))
    if thread_capacity <= 3:
        return 1
    if thread_capacity <= 6:
        # Keep two gthread slots available for health/auth/root fast-lane work,
        # while allowing a measured 6-thread worker to admit four ordinary or
        # heavy requests.  Reserving four here reduced the feature gate to two
        # and prevented a four-slot external HLS pool from ever being filled.
        return min(2, max(1, thread_capacity - 2))
    if thread_capacity <= 8:
        return min(3, max(1, thread_capacity - 2))
    return min(max(2, thread_capacity // 3), max(1, thread_capacity - 2))


def _auto_heavy_limit(thread_capacity: int, cpu_count: int, mem_mb: int, settings: dict | None = None) -> tuple[int, str]:
    setting_value = _setting_int_or_none(settings, "server_backpressure_heavy_limit", minimum=1, maximum=256)
    if setting_value:
        return min(setting_value, max(1, thread_capacity - 1)), "settings"
    configured = _env_int_or_none("HTML_LEARNING_BACKPRESSURE_HEAVY_LIMIT", minimum=1, maximum=256)
    if configured:
        return min(configured, max(1, thread_capacity - 1)), "env"
    if thread_capacity <= 4 or (mem_mb > 0 and mem_mb < 2048):
        return 1, "auto"
    heavy_limit = max(2, thread_capacity // 2)
    if thread_capacity <= 6:
        # A 6-thread gthread worker is the default dev/capacity profile. The
        # previous cap of 4 rejected heavy uploads/backtests while the process
        # still had CPU and memory headroom; keep one thread back, not two.
        heavy_limit = max(heavy_limit, thread_capacity - 1)
    if mem_mb > 0 and mem_mb < 4096:
        heavy_limit = min(heavy_limit, 2)
    heavy_ceiling = max(1, thread_capacity - (1 if thread_capacity <= 6 else 2))
    return min(heavy_limit, heavy_ceiling), "auto"


def _root_priority_enabled(settings: dict | None = None) -> bool:
    default = _env_bool("HTML_LEARNING_BACKPRESSURE_ROOT_PRIORITY_ENABLED", True)
    return _setting_bool(settings, "server_backpressure_root_priority_enabled", default)


def _auto_root_limit(thread_capacity: int, settings: dict | None = None) -> tuple[int, str]:
    if not _root_priority_enabled(settings):
        return 0, "off"
    setting_value = _setting_int_or_none(settings, "server_backpressure_root_limit", minimum=1, maximum=64)
    if setting_value:
        return setting_value, "settings"
    configured = _env_int_or_none("HTML_LEARNING_BACKPRESSURE_ROOT_LIMIT", minimum=1, maximum=64)
    if configured:
        return configured, "env"
    return max(2, min(4, max(1, thread_capacity // 4))), "auto"


def _resolve_gate_limits(settings: dict | None = None) -> dict:
    cpu_count = max(1, int(os.cpu_count() or 1))
    mem_mb = _total_memory_mb()
    mode = _setting_mode(settings)
    manual_settings = settings if mode == "manual" else None
    thread_capacity = _thread_capacity(settings)
    reserved = _auto_fast_lane_reserved(thread_capacity, manual_settings)
    heavy_limit, heavy_source = _auto_heavy_limit(thread_capacity, cpu_count, mem_mb, manual_settings)
    root_limit, root_source = _auto_root_limit(thread_capacity, manual_settings if mode == "manual" else settings)
    feature_limit = max(1, thread_capacity - reserved)
    heavy_limit = min(heavy_limit, feature_limit)
    if mode == "manual":
        root_limit = min(root_limit, max(0, thread_capacity - reserved - heavy_limit))
    setting_normal = _setting_int_or_none(manual_settings, "server_backpressure_normal_limit", minimum=1, maximum=2048)
    env_normal = _env_int_or_none("HTML_LEARNING_BACKPRESSURE_NORMAL_LIMIT", minimum=1, maximum=2048)
    configured_normal = setting_normal or env_normal
    normal_source = "settings" if setting_normal else ("env" if env_normal else "auto")
    # App-level gates cannot reject requests until a WSGI thread starts running.
    # Keep normal traffic below the detected worker thread count even in auto
    # mode so health/static/auth fast-lane requests have threads left to drain
    # instead of timing out behind ordinary feature work.
    fast_lane_budget = reserved
    heavy_budget = heavy_limit if mode == "manual" else 0
    max_normal = max(1, thread_capacity - fast_lane_budget - heavy_budget)
    normal_limit = min(configured_normal, max_normal) if configured_normal else max_normal
    if normal_limit < 1:
        normal_limit = 1
    # Root and heavy priority are opportunistic in auto mode; only manual
    # budgets should reduce normal capacity.
    if normal_limit + heavy_budget + fast_lane_budget > thread_capacity:
        overflow = normal_limit + heavy_budget + fast_lane_budget - thread_capacity
        normal_limit = max(1, normal_limit - overflow)
    return {
        "normal": int(normal_limit),
        "heavy": int(heavy_limit),
        "root": int(root_limit),
        "feature": int(feature_limit),
        "fast_lane_reserved": int(reserved),
        "thread_capacity": int(thread_capacity),
        "cpu_count": int(cpu_count),
        "memory_total_mb": int(mem_mb),
        "mode": mode,
        "normal_source": normal_source,
        "heavy_source": heavy_source,
        "root_source": root_source,
        "feature_source": "auto",
        "root_priority_enabled": bool(root_limit > 0),
        "process_local": True,
    }


def _backpressure_settings_signature(settings: dict | None = None) -> tuple:
    settings = settings or {}
    keys = (
        "server_backpressure_enabled",
        "server_backpressure_mode",
        "server_backpressure_thread_capacity",
        "server_backpressure_normal_limit",
        "server_backpressure_heavy_limit",
        "server_backpressure_root_priority_enabled",
        "server_backpressure_root_limit",
        "server_backpressure_fast_lane_reserved",
        "server_backpressure_retry_after_seconds",
        "server_backpressure_refresh_seconds",
    )
    return tuple((key, settings.get(key)) for key in keys)


def is_health_fast_lane_path(path: str) -> bool:
    return (path or "") in HEALTH_FAST_LANE_PATHS


def is_static_asset_path(path: str) -> bool:
    normalized = str(path or "").lower()
    return bool(normalized.startswith("/") and not normalized.startswith("/api/") and normalized.endswith(STATIC_ASSET_SUFFIXES))


def is_backpressure_fast_lane_path(path: str) -> bool:
    path = path or ""
    if path in BOOTSTRAP_FAST_LANE_PATHS:
        return True
    if path in AUTH_FAST_LANE_PATHS:
        return True
    if is_static_asset_path(path):
        return True
    return any(path == prefix or path.startswith(prefix) for prefix in BACKPRESSURE_FAST_LANE_PREFIXES)


def is_root_priority_path(path: str) -> bool:
    path = path or ""
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in ROOT_PRIORITY_PREFIXES)


def is_heavy_request_path(path: str, method: str = "GET") -> bool:
    path = path or ""
    method = (method or "GET").upper()
    if path in HEAVY_EXACT_PATHS:
        return True
    if any(path == prefix or path.startswith(prefix) for prefix in HEAVY_PREFIXES):
        return True
    if path.startswith("/api/comfyui/workflows/") and path.endswith("/run"):
        return True
    if path.startswith("/api/comfyui/workflow-layouts/") and path.endswith("/run"):
        return True
    if path.startswith("/api/videos/") and any(marker in path for marker in HEAVY_CONTAINS):
        return True
    if path.startswith("/api/files/") and path.endswith("/download"):
        return True
    if path.startswith("/api/cloud-drive/files/") and path.endswith("/download"):
        return True
    if path.startswith("/api/games/") and method == "POST" and any(marker in path for marker in HEAVY_CONTAINS):
        return True
    return False


def classify_request_qos(path: str, method: str = "GET") -> str:
    path = path or ""
    method = (method or "GET").upper()
    if is_health_fast_lane_path(path):
        return "health"
    if path in BOOTSTRAP_FAST_LANE_PATHS:
        return "bootstrap"
    if is_static_asset_path(path) or path.startswith("/js/") or path.startswith("/assets/"):
        return "static"
    if path in AUTH_FAST_LANE_PATHS:
        return "auth"
    if path in AUTH_EDGE_GUARD_PATHS or any(path == prefix or path.startswith(prefix) for prefix in AUTH_EDGE_GUARD_PREFIXES):
        return "auth"
    if path.startswith("/api/root/") or path.startswith("/api/admin/"):
        return "management"
    if path.startswith("/api/points/explorer") or path == "/api/points/transactions":
        return "management"
    if is_heavy_request_path(path, method):
        return "heavy"
    if path.startswith("/api/"):
        return "api_read" if method in {"GET", "HEAD"} else "api_write"
    return "page"


def edge_guard_label_for_path(path: str, method: str = "GET") -> str | None:
    path = path or ""
    method = (method or "GET").upper()
    if is_health_fast_lane_path(path):
        return None
    if path in CSRF_EDGE_GUARD_PATHS:
        return "csrf"
    if path in AUTH_EDGE_GUARD_PATHS or any(path == prefix or path.startswith(prefix) for prefix in AUTH_EDGE_GUARD_PREFIXES):
        return "auth"
    if path in UPLOAD_EDGE_GUARD_PATHS or any(path == prefix or path.startswith(prefix) for prefix in UPLOAD_EDGE_GUARD_PREFIXES):
        return "upload"
    if path.startswith("/api/root/") or path.startswith("/api/admin/"):
        return "management"
    return None


class EdgeBurstGuard:
    def __init__(self, window_seconds: int = 10):
        self.window_seconds = max(1, min(300, int(window_seconds or 10)))
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], list[float]] = {}
        self._accepted: dict[str, int] = {}
        self._rejected: dict[str, int] = {}

    def allow(self, label: str, client_key: str, limit: int) -> tuple[bool, int]:
        label = str(label or "default")
        client_key = str(client_key or "-")
        limit = max(1, int(limit or 1))
        now = time.monotonic()
        cutoff = now - self.window_seconds
        key = (label, client_key)
        with self._lock:
            hits = [ts for ts in self._hits.get(key, []) if ts >= cutoff]
            if len(hits) >= limit:
                self._hits[key] = hits
                self._rejected[label] = self._rejected.get(label, 0) + 1
                oldest = min(hits) if hits else now
                retry_after = max(1, int(round(self.window_seconds - (now - oldest))))
                return False, retry_after
            hits.append(now)
            self._hits[key] = hits
            self._accepted[label] = self._accepted.get(label, 0) + 1
            if len(self._hits) > 4096:
                for old_key, old_hits in list(self._hits.items()):
                    fresh = [ts for ts in old_hits if ts >= cutoff]
                    if fresh:
                        self._hits[old_key] = fresh
                    else:
                        self._hits.pop(old_key, None)
            return True, 0

    def snapshot(self) -> dict:
        cutoff = time.monotonic() - self.window_seconds
        with self._lock:
            active_keys = 0
            active_hits: dict[str, int] = {}
            for (label, _client), hits in list(self._hits.items()):
                fresh = [ts for ts in hits if ts >= cutoff]
                if fresh:
                    self._hits[(label, _client)] = fresh
                    active_keys += 1
                    active_hits[label] = active_hits.get(label, 0) + len(fresh)
                else:
                    self._hits.pop((label, _client), None)
            labels = sorted(set(active_hits) | set(self._accepted) | set(self._rejected))
            per_label = {
                label: {
                    "active_hits": active_hits.get(label, 0),
                    "accepted": self._accepted.get(label, 0),
                    "rejected": self._rejected.get(label, 0),
                }
                for label in labels
            }
        return {
            "enabled": _edge_guard_enabled(),
            "window_seconds": self.window_seconds,
            "process_local": True,
            "active_keys": active_keys,
            "labels": per_label,
        }


@dataclass
class _GateLease:
    gate: "RequestGate"
    label: str

    def release(self) -> None:
        self.gate.release()


@dataclass
class _CompositeLease:
    label: str
    leases: tuple[_GateLease, ...]

    def release(self) -> None:
        for lease in reversed(self.leases):
            lease.release()


class RequestGate:
    def __init__(self, label: str, limit: int):
        self.label = str(label)
        self.limit = max(0, int(limit))
        self._semaphore = threading.BoundedSemaphore(self.limit)
        self._lock = threading.Lock()
        self._active = 0
        self.rejected = 0

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def acquire(self, *, count_reject: bool = True) -> _GateLease | None:
        if self.limit <= 0:
            if count_reject:
                with self._lock:
                    self.rejected += 1
            return None
        if not self._semaphore.acquire(blocking=False):
            if count_reject:
                with self._lock:
                    self.rejected += 1
            return None
        with self._lock:
            self._active += 1
        return _GateLease(self, self.label)

    def release(self) -> None:
        with self._lock:
            if self._active <= 0:
                return
            self._active -= 1
        try:
            self._semaphore.release()
        except ValueError:
            pass

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "label": self.label,
                "limit": self.limit,
                "active": self._active,
                "rejected": self.rejected,
            }


class RequestTrafficWindow:
    def __init__(self, window_seconds: int = 120):
        self.window_seconds = max(30, min(600, int(window_seconds or 120)))
        self._lock = threading.Lock()
        self._buckets: dict[int, dict] = {}
        self._started_at = int(time.time())
        self._cumulative_upload_bytes = 0
        self._cumulative_download_bytes = 0
        self._cumulative_requests = 0

    def record(
        self,
        label: str,
        status_code: int = 0,
        rejected: bool = False,
        *,
        upload_bytes: int = 0,
        download_bytes: int = 0,
    ) -> None:
        now = int(time.time())
        label = label if label in {"normal", "heavy", "root", "feature", "fast_lane", "edge_guard", "off"} else "other"
        status_code = int(status_code or 0)
        upload_bytes = max(0, int(upload_bytes or 0))
        download_bytes = max(0, int(download_bytes or 0))
        with self._lock:
            bucket = self._buckets.setdefault(now, {
                "ts": now,
                "total": 0,
                "accepted": 0,
                "rejected": 0,
                "normal": 0,
                "heavy": 0,
                "root": 0,
                "feature": 0,
                "fast_lane": 0,
                "edge_guard": 0,
                "off": 0,
                "other": 0,
                "hard_5xx": 0,
                "upload_bytes": 0,
                "download_bytes": 0,
            })
            bucket["total"] += 1
            bucket[label] += 1
            bucket["upload_bytes"] += upload_bytes
            bucket["download_bytes"] += download_bytes
            self._cumulative_requests += 1
            self._cumulative_upload_bytes += upload_bytes
            self._cumulative_download_bytes += download_bytes
            if rejected:
                bucket["rejected"] += 1
            else:
                bucket["accepted"] += 1
            if status_code >= 500 and not (status_code == 503 and rejected):
                bucket["hard_5xx"] += 1
            cutoff = now - self.window_seconds - 2
            for old_ts in list(self._buckets):
                if old_ts < cutoff:
                    self._buckets.pop(old_ts, None)

    def snapshot(self) -> dict:
        now = int(time.time())
        start = now - self.window_seconds + 1
        totals = {
            "total": 0,
            "accepted": 0,
            "rejected": 0,
            "normal": 0,
            "heavy": 0,
            "root": 0,
            "feature": 0,
            "fast_lane": 0,
            "edge_guard": 0,
            "off": 0,
            "other": 0,
            "hard_5xx": 0,
            "upload_bytes": 0,
            "download_bytes": 0,
        }
        points = []
        recent_upload = 0
        recent_download = 0
        recent_window_seconds = min(10, self.window_seconds)
        recent_start = now - recent_window_seconds + 1
        with self._lock:
            for ts in range(start, now + 1):
                source = self._buckets.get(ts) or {}
                point = {
                    "ts": ts,
                    "label": time.strftime("%H:%M:%S", time.localtime(ts)),
                    **{key: int(source.get(key) or 0) for key in totals},
                }
                for key in totals:
                    totals[key] += point[key]
                points.append(point)
                if ts >= recent_start:
                    recent_upload += int(point.get("upload_bytes") or 0)
                    recent_download += int(point.get("download_bytes") or 0)
            cumulative = {
                "started_at": self._started_at,
                "requests": self._cumulative_requests,
                "upload_bytes": self._cumulative_upload_bytes,
                "download_bytes": self._cumulative_download_bytes,
            }
        return {
            "window_seconds": self.window_seconds,
            "process_local": True,
            "points": points,
            "totals": totals,
            "recent_window_seconds": recent_window_seconds,
            "upload_bytes_per_sec": int(recent_upload / max(1, recent_window_seconds)),
            "download_bytes_per_sec": int(recent_download / max(1, recent_window_seconds)),
            "cumulative": cumulative,
        }


def _safe_request_content_length() -> int:
    try:
        return max(0, int(request.content_length or 0))
    except Exception:
        return 0


def _safe_response_content_length(response) -> int:
    try:
        header_value = response.headers.get("Content-Length")
        if header_value is not None:
            return max(0, int(header_value))
    except Exception:
        pass
    try:
        calculated = response.calculate_content_length()
        return max(0, int(calculated or 0))
    except Exception:
        return 0


def _backpressure_anomaly_log_path(app):
    configured = app.config.get("HACKME_BACKPRESSURE_ANOMALY_LOG_PATH")
    if configured:
        return str(configured)
    runtime = os.environ.get("HACKME_RUNTIME_DIR")
    if not runtime:
        return ""
    return str(Path(runtime) / "logs" / "backpressure_anomalies.jsonl")


def _record_backpressure_anomaly(app, payload: dict) -> None:
    now = time.time()
    event = {
        "ts": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "pid": os.getpid(),
        "event": payload.get("event") or "backpressure_anomaly",
        "method": request.method,
        "path": request.path,
        "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr or "-"),
        "user_agent": (request.headers.get("User-Agent") or "")[:240],
        **payload,
    }
    signature = "|".join(str(event.get(key) or "") for key in ("event", "path", "gate", "status_code"))
    cache = app.config.setdefault("HACKME_BACKPRESSURE_ANOMALY_THROTTLE", {})
    if now - float(cache.get(signature) or 0) < 30:
        return
    cache[signature] = now
    recent = app.config.setdefault("HACKME_BACKPRESSURE_RECENT_ANOMALIES", [])
    recent.append(event)
    del recent[:-50]

    log_path = _backpressure_anomaly_log_path(app)
    if log_path:
        try:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        except Exception:
            pass

    callback = app.config.get("HACKME_BACKPRESSURE_AUDIT_CALLBACK")
    if callable(callback):
        try:
            callback(dict(event))
        except Exception:
            pass


def _build_backpressure_state(settings: dict | None = None, previous_state: dict | None = None) -> dict:
    mode = _setting_mode(settings)
    enabled = _env_bool("HTML_LEARNING_BACKPRESSURE_ENABLED", True)
    # An explicit process-level switch is authoritative.  Capacity probes and
    # emergency operators must be able to turn the gate off even when the
    # persisted settings row still contains its default ``true`` value.
    if "HTML_LEARNING_BACKPRESSURE_ENABLED" not in os.environ:
        enabled = _setting_bool(settings, "server_backpressure_enabled", enabled)
    if mode == "off":
        enabled = False
    retry_after_default = _env_int("HTML_LEARNING_BACKPRESSURE_RETRY_AFTER_SECONDS", 2, 1, 60)
    retry_after = _setting_int_or_none(settings, "server_backpressure_retry_after_seconds", minimum=1, maximum=60)
    if not retry_after:
        retry_after = retry_after_default
    refresh_default = _env_int("HTML_LEARNING_BACKPRESSURE_REFRESH_SECONDS", 2, 1, 60)
    refresh_seconds = _setting_int_or_none(settings, "server_backpressure_refresh_seconds", minimum=1, maximum=60)
    if not refresh_seconds:
        refresh_seconds = refresh_default
    limits = _resolve_gate_limits(settings if mode in {"auto", "manual", "off"} else None)
    now = time.monotonic()
    previous_traffic = (previous_state or {}).get("traffic")
    traffic = previous_traffic if hasattr(previous_traffic, "record") else RequestTrafficWindow()
    previous_edge_guard = (previous_state or {}).get("edge_guard")
    edge_guard = previous_edge_guard if hasattr(previous_edge_guard, "allow") else EdgeBurstGuard(_edge_guard_window_seconds())
    if getattr(edge_guard, "window_seconds", None) != _edge_guard_window_seconds():
        edge_guard = EdgeBurstGuard(_edge_guard_window_seconds())
    return {
        "enabled": enabled,
        "retry_after": retry_after,
        "refresh_seconds": refresh_seconds,
        "last_refresh_at": now,
        "settings_signature": _backpressure_settings_signature(settings),
        "normal": RequestGate("normal", int(limits["normal"])),
        "heavy": RequestGate("heavy", int(limits["heavy"])),
        "root": RequestGate("root", int(limits["root"])),
        "feature": RequestGate("feature", int(limits["feature"])),
        "traffic": traffic,
        "edge_guard": edge_guard,
        "limits": limits,
    }


def _busy_response(label: str, retry_after: int):
    response = jsonify(
        {
            "ok": False,
            "error": "server_busy",
            "msg": f"目前是流量高峰，伺服器正在保護服務品質。請稍候 {retry_after} 秒後再試。",
            "message": f"目前是流量高峰，伺服器正在保護服務品質。請稍候 {retry_after} 秒後再試。",
            "user_message": f"目前是流量高峰，伺服器正在保護服務品質。請稍候 {retry_after} 秒後再試。",
            "gate": label,
            "retry_after_seconds": retry_after,
        }
    )
    response.status_code = 503
    response.headers["Retry-After"] = str(retry_after)
    response.headers["X-Hackme-Backpressure"] = label
    response.headers["X-Hackme-Backpressure-Rejected"] = "1"
    return response


def _client_burst_key() -> str:
    remote = request.remote_addr or "-"
    ua = (request.headers.get("User-Agent") or "")[:80]
    return f"{remote}|{ua}"


def _edge_rate_limited_response(label: str, retry_after: int):
    response = jsonify(
        {
            "ok": False,
            "error": "edge_rate_limited",
            "msg": f"請求過於頻繁，伺服器正在保護登入、管理與上傳入口。請稍候 {retry_after} 秒後再試。",
            "message": f"請求過於頻繁，伺服器正在保護登入、管理與上傳入口。請稍候 {retry_after} 秒後再試。",
            "user_message": f"請求過於頻繁，伺服器正在保護登入、管理與上傳入口。請稍候 {retry_after} 秒後再試。",
            "gate": label,
            "retry_after_seconds": retry_after,
        }
    )
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    response.headers["X-Hackme-Edge-Guard"] = label
    response.headers["X-Hackme-Backpressure"] = "edge_guard"
    return response


def apply_backpressure_settings(app, settings: dict | None = None) -> dict:
    state = _build_backpressure_state(settings, previous_state=app.config.get("HACKME_BACKPRESSURE"))
    app.config["HACKME_BACKPRESSURE"] = state
    return state


def _maybe_refresh_backpressure_state(app, *, force: bool = False) -> dict:
    state = app.config.get("HACKME_BACKPRESSURE") or {}
    provider = app.config.get("HACKME_BACKPRESSURE_SETTINGS_PROVIDER")
    if not callable(provider):
        return state
    now = time.monotonic()
    refresh_seconds = int(state.get("refresh_seconds") or 2)
    if not force and now - float(state.get("last_refresh_at") or 0.0) < refresh_seconds:
        return state
    lock = app.config.get("HACKME_BACKPRESSURE_REFRESH_LOCK")
    if lock is None:
        lock = threading.Lock()
        app.config["HACKME_BACKPRESSURE_REFRESH_LOCK"] = lock
    if not lock.acquire(blocking=False):
        return app.config.get("HACKME_BACKPRESSURE") or state
    try:
        state = app.config.get("HACKME_BACKPRESSURE") or state
        if not force and now - float(state.get("last_refresh_at") or 0.0) < int(state.get("refresh_seconds") or 2):
            return state
        try:
            settings = provider()
        except Exception:
            state["last_refresh_at"] = now
            app.config["HACKME_BACKPRESSURE"] = state
            return state
        signature = _backpressure_settings_signature(settings)
        if signature != state.get("settings_signature"):
            state = _build_backpressure_state(settings, previous_state=state)
        else:
            state["last_refresh_at"] = now
        app.config["HACKME_BACKPRESSURE"] = state
        return state
    finally:
        lock.release()


def install_backpressure(app, settings_provider=None, root_priority_detector=None, anomaly_audit_callback=None, anomaly_log_path=None) -> dict:
    settings = None
    if callable(settings_provider):
        try:
            settings = settings_provider()
        except Exception:
            settings = None
    app.config["HACKME_BACKPRESSURE_SETTINGS_PROVIDER"] = settings_provider
    app.config["HACKME_BACKPRESSURE_ROOT_PRIORITY_DETECTOR"] = root_priority_detector
    app.config["HACKME_BACKPRESSURE_AUDIT_CALLBACK"] = anomaly_audit_callback
    if anomaly_log_path:
        app.config["HACKME_BACKPRESSURE_ANOMALY_LOG_PATH"] = anomaly_log_path
    app.config["HACKME_BACKPRESSURE_REFRESH_LOCK"] = threading.Lock()
    app.config["HACKME_BACKPRESSURE"] = _build_backpressure_state(settings)

    @app.before_request
    def _backpressure_before_request():
        if request.environ.get("hackme.internal_dispatch") == "ai_agent_write_tool":
            return None
        g._backpressure_lease = None
        g._backpressure_release_registered = False
        g._backpressure_traffic_label = None
        g._backpressure_rejected = False
        g._hackme_qos_class = classify_request_qos(request.path, request.method)
        if request.method == "OPTIONS":
            return None
        state = dict(_maybe_refresh_backpressure_state(app) or app.config.get("HACKME_BACKPRESSURE") or {})
        enabled = bool(state.get("enabled"))
        if not enabled:
            g._backpressure_traffic_label = "off"
            return None
        path = request.path or ""
        edge_label = edge_guard_label_for_path(path, request.method)
        edge_guard = state.get("edge_guard")
        if edge_label and _edge_guard_enabled() and hasattr(edge_guard, "allow"):
            allowed, retry_after = edge_guard.allow(
                edge_label,
                _client_burst_key(),
                _edge_guard_limit(edge_label),
            )
            if not allowed:
                g._backpressure_traffic_label = "edge_guard"
                g._backpressure_rejected = True
                _record_backpressure_anomaly(app, {
                    "event": "edge_rate_limited",
                    "gate": edge_label,
                    "status_code": 429,
                    "retry_after": retry_after,
                    "qos_class": getattr(g, "_hackme_qos_class", ""),
                })
                return _edge_rate_limited_response(edge_label, retry_after)
        if is_backpressure_fast_lane_path(path):
            g._backpressure_traffic_label = "fast_lane"
            return None
        limits = state.get("limits") or {}
        if bool(limits.get("root_priority_enabled")) and is_root_priority_path(path):
            root_gate = state.get("root")
            detector = app.config.get("HACKME_BACKPRESSURE_ROOT_PRIORITY_DETECTOR")
            is_root_priority = False
            try:
                is_root_priority = callable(detector) and bool(detector())
            except Exception:
                is_root_priority = False
            if is_root_priority:
                root_lease = root_gate.acquire(count_reject=False) if hasattr(root_gate, "acquire") else None
                if root_lease is None:
                    g._backpressure_traffic_label = "root"
                    g._backpressure_rejected = True
                    _record_backpressure_anomaly(app, {
                        "event": "server_busy_rejected",
                        "gate": "root",
                        "status_code": 503,
                        "retry_after": int(state.get("retry_after") or 2),
                        "qos_class": getattr(g, "_hackme_qos_class", ""),
                    })
                    return _busy_response("root", int(state.get("retry_after") or 2))
                try:
                    g._backpressure_lease = root_lease
                    g._backpressure_traffic_label = root_lease.label
                    return None
                except Exception:
                    root_lease.release()
        gate = None
        if gate is None:
            gate = state.get("heavy") if is_heavy_request_path(path, request.method) else state.get("normal")
        if not hasattr(gate, "acquire"):
            return None
        feature_gate = state.get("feature")
        feature_lease = None
        if hasattr(feature_gate, "acquire"):
            feature_lease = feature_gate.acquire()
            if feature_lease is None:
                g._backpressure_traffic_label = feature_gate.label
                g._backpressure_rejected = True
                _record_backpressure_anomaly(app, {
                    "event": "server_busy_rejected",
                    "gate": feature_gate.label,
                    "status_code": 503,
                    "retry_after": int(state.get("retry_after") or 2),
                })
                return _busy_response(feature_gate.label, int(state.get("retry_after") or 2))
        lease = gate.acquire()
        if lease is None:
            if feature_lease is not None:
                feature_lease.release()
            g._backpressure_traffic_label = gate.label
            g._backpressure_rejected = True
            _record_backpressure_anomaly(app, {
                "event": "server_busy_rejected",
                "gate": gate.label,
                "status_code": 503,
                "retry_after": int(state.get("retry_after") or 2),
            })
            return _busy_response(gate.label, int(state.get("retry_after") or 2))
        if feature_lease is not None:
            g._backpressure_lease = _CompositeLease(lease.label, (feature_lease, lease))
        else:
            g._backpressure_lease = lease
        g._backpressure_traffic_label = lease.label
        return None

    @app.after_request
    def _backpressure_after_request(response):
        state = app.config.get("HACKME_BACKPRESSURE") or {}
        traffic = state.get("traffic")
        label = getattr(g, "_backpressure_traffic_label", None)
        qos_class = getattr(g, "_hackme_qos_class", None)
        if qos_class:
            response.headers.setdefault("X-Hackme-QoS-Class", qos_class)
        if label:
            response.headers.setdefault("X-Hackme-Backpressure", label)
        if label is None and response.status_code == 503:
            label = response.headers.get("X-Hackme-Backpressure") or None
        if label and hasattr(traffic, "record"):
            traffic.record(
                label,
                response.status_code,
                bool(getattr(g, "_backpressure_rejected", False)),
                upload_bytes=_safe_request_content_length(),
                download_bytes=_safe_response_content_length(response),
            )
        if response.status_code >= 500 and not bool(getattr(g, "_backpressure_rejected", False)):
            _record_backpressure_anomaly(app, {
                "event": "hard_5xx_response",
                "gate": label or "unknown",
                "status_code": int(response.status_code or 0),
            })
        lease = getattr(g, "_backpressure_lease", None)
        if lease is None:
            return response
        g._backpressure_lease = None
        lease.release()
        response.headers.setdefault("X-Hackme-Backpressure", lease.label)
        return response

    @app.teardown_request
    def _backpressure_teardown_request(_exc):
        if getattr(g, "_backpressure_release_registered", False):
            return None
        lease = getattr(g, "_backpressure_lease", None)
        if lease is not None:
            g._backpressure_lease = None
            lease.release()
        return None

    return app.config["HACKME_BACKPRESSURE"]


def backpressure_status(app) -> dict:
    state = dict(_maybe_refresh_backpressure_state(app) or app.config.get("HACKME_BACKPRESSURE") or {})
    normal = state.get("normal")
    heavy = state.get("heavy")
    root = state.get("root")
    feature = state.get("feature")
    traffic = state.get("traffic")
    edge_guard = state.get("edge_guard")
    limits = dict(state.get("limits") or {})
    return {
        "pid": os.getpid(),
        "process_local": True,
        "enabled": bool(state.get("enabled")),
        "retry_after": state.get("retry_after"),
        "refresh_seconds": state.get("refresh_seconds"),
        "mode": limits.get("mode") or "auto",
        "thread_capacity": limits.get("thread_capacity"),
        "fast_lane_reserved": limits.get("fast_lane_reserved"),
        "cpu_count": limits.get("cpu_count"),
        "memory_total_mb": limits.get("memory_total_mb"),
        "limit_sources": {
            "normal": limits.get("normal_source") or "unknown",
            "heavy": limits.get("heavy_source") or "unknown",
            "root": limits.get("root_source") or "unknown",
            "feature": limits.get("feature_source") or "unknown",
        },
        "normal": normal.snapshot() if hasattr(normal, "snapshot") else {},
        "heavy": heavy.snapshot() if hasattr(heavy, "snapshot") else {},
        "root": root.snapshot() if hasattr(root, "snapshot") else {},
        "feature": feature.snapshot() if hasattr(feature, "snapshot") else {},
        "traffic": traffic.snapshot() if hasattr(traffic, "snapshot") else {},
        "edge_guard": edge_guard.snapshot() if hasattr(edge_guard, "snapshot") else {},
        "anomaly_audit": {
            "log_path": _backpressure_anomaly_log_path(app),
            "recent": list(app.config.get("HACKME_BACKPRESSURE_RECENT_ANOMALIES") or [])[-20:],
        },
    }
