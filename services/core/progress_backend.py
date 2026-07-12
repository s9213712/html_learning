from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from services.server.runtime import default_runtime_root_path


DEFAULT_PROGRESS_TTL_SECONDS = 6 * 60 * 60


def _env_text(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(raw: bytes | str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        value = json.loads(raw)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _safe_cache_name(namespace: str, key: str) -> str:
    digest = hashlib.sha256(f"{namespace}:{key}".encode("utf-8")).hexdigest()
    return f"{digest}.json"


def _default_file_cache_dir() -> Path:
    runtime_dir = _env_text("HACKME_RUNTIME_DIR")
    if not runtime_dir:
        runtime_dir = str(default_runtime_root_path())
    return Path(runtime_dir) / "job_progress_cache"


class ProgressBackend:
    name = "base"
    available = True
    error = ""

    def put(self, namespace: str, key: str, payload: dict[str, Any], *, ttl_seconds: int | None = None) -> bool:
        raise NotImplementedError

    def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def delete(self, namespace: str, key: str) -> bool:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {"backend": self.name, "available": bool(self.available), "error": self.error}


class InMemoryProgressBackend(ProgressBackend):
    name = "memory"

    def __init__(self):
        self._items: dict[tuple[str, str], tuple[dict[str, Any], float | None]] = {}
        self._lock = threading.RLock()

    def put(self, namespace: str, key: str, payload: dict[str, Any], *, ttl_seconds: int | None = None) -> bool:
        expires_at = None
        if ttl_seconds is not None and int(ttl_seconds) > 0:
            expires_at = time.time() + int(ttl_seconds)
        with self._lock:
            self._items[(str(namespace), str(key))] = (dict(payload or {}), expires_at)
        return True

    def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        item_key = (str(namespace), str(key))
        with self._lock:
            item = self._items.get(item_key)
            if not item:
                return None
            payload, expires_at = item
            if expires_at is not None and expires_at <= time.time():
                self._items.pop(item_key, None)
                return None
            return dict(payload)

    def delete(self, namespace: str, key: str) -> bool:
        with self._lock:
            self._items.pop((str(namespace), str(key)), None)
        return True


class FileProgressBackend(ProgressBackend):
    name = "file"

    def __init__(self, cache_dir: str | os.PathLike[str] | None = None):
        self.cache_dir = Path(cache_dir or _env_text("HACKME_PROGRESS_CACHE_DIR") or _default_file_cache_dir())
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self.available = True
            self.error = ""
        except Exception as exc:
            self.available = False
            self.error = str(exc)

    def _path(self, namespace: str, key: str) -> Path:
        return self.cache_dir / _safe_cache_name(namespace, key)

    def put(self, namespace: str, key: str, payload: dict[str, Any], *, ttl_seconds: int | None = None) -> bool:
        if not self.available:
            return False
        expires_at = None
        if ttl_seconds is not None and int(ttl_seconds) > 0:
            expires_at = time.time() + int(ttl_seconds)
        body = {
            "namespace": str(namespace),
            "key": str(key),
            "payload": payload or {},
            "updated_at": time.time(),
            "expires_at": expires_at,
        }
        path = self._path(namespace, key)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(_json_dumps(body), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except Exception as exc:
            self.error = str(exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        if not self.available:
            return None
        path = self._path(namespace, key)
        try:
            body = _json_loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            self.error = str(exc)
            return None
        if not body:
            return None
        expires_at = body.get("expires_at")
        try:
            if expires_at is not None and float(expires_at) <= time.time():
                path.unlink(missing_ok=True)
                return None
        except Exception:
            pass
        if str(body.get("namespace") or "") != str(namespace) or str(body.get("key") or "") != str(key):
            return None
        payload = body.get("payload")
        return dict(payload) if isinstance(payload, dict) else None

    def delete(self, namespace: str, key: str) -> bool:
        try:
            self._path(namespace, key).unlink(missing_ok=True)
            return True
        except Exception as exc:
            self.error = str(exc)
            return False

    def status(self) -> dict[str, Any]:
        payload = super().status()
        payload["cache_dir"] = str(self.cache_dir)
        return payload


class RedisProgressBackend(ProgressBackend):
    name = "redis"

    def __init__(self, url: str | None = None, *, prefix: str | None = None):
        self.url = str(url or _env_text("HACKME_REDIS_URL") or _env_text("REDIS_URL") or "").strip()
        self.prefix = str(prefix or _env_text("HACKME_PROGRESS_REDIS_PREFIX", "hackme_web:progress")).strip()
        self._client = None
        if not self.url:
            self.available = False
            self.error = "redis url is not configured"
            return
        try:
            import redis  # type: ignore

            self._client = redis.Redis.from_url(self.url, socket_timeout=1.0, socket_connect_timeout=1.0)
            self.available = True
            self.error = ""
        except Exception as exc:
            self.available = False
            self.error = str(exc)

    def _redis_key(self, namespace: str, key: str) -> str:
        digest = hashlib.sha256(f"{namespace}:{key}".encode("utf-8")).hexdigest()
        return f"{self.prefix}:{digest}"

    def put(self, namespace: str, key: str, payload: dict[str, Any], *, ttl_seconds: int | None = None) -> bool:
        if not self.available or self._client is None:
            return False
        body = {"namespace": str(namespace), "key": str(key), "payload": payload or {}, "updated_at": time.time()}
        try:
            ttl = int(ttl_seconds or DEFAULT_PROGRESS_TTL_SECONDS)
            self._client.set(self._redis_key(namespace, key), _json_dumps(body), ex=max(1, ttl))
            return True
        except Exception as exc:
            self.available = False
            self.error = str(exc)
            return False

    def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        if not self.available or self._client is None:
            return None
        try:
            body = _json_loads(self._client.get(self._redis_key(namespace, key)))
        except Exception as exc:
            self.available = False
            self.error = str(exc)
            return None
        if not body:
            return None
        if str(body.get("namespace") or "") != str(namespace) or str(body.get("key") or "") != str(key):
            return None
        payload = body.get("payload")
        return dict(payload) if isinstance(payload, dict) else None

    def delete(self, namespace: str, key: str) -> bool:
        if not self.available or self._client is None:
            return False
        try:
            self._client.delete(self._redis_key(namespace, key))
            return True
        except Exception as exc:
            self.available = False
            self.error = str(exc)
            return False

    def status(self) -> dict[str, Any]:
        payload = super().status()
        payload["configured"] = bool(self.url)
        payload["prefix"] = self.prefix
        return payload


_BACKEND: ProgressBackend | None = None
_BACKEND_LOCK = threading.Lock()


def _make_backend() -> ProgressBackend:
    selector = (_env_text("HACKME_JOB_PROGRESS_BACKEND") or _env_text("HACKME_PROGRESS_BACKEND") or "memory").lower()
    if selector in {"redis", "rq", "redis-rq"}:
        redis_backend = RedisProgressBackend()
        if redis_backend.available:
            return redis_backend
        file_backend = FileProgressBackend()
        if file_backend.available:
            file_backend.error = f"redis unavailable; using file backend: {redis_backend.error}"
            return file_backend
        memory = InMemoryProgressBackend()
        memory.error = f"redis unavailable; using memory backend: {redis_backend.error}"
        return memory
    if selector == "file":
        file_backend = FileProgressBackend()
        return file_backend if file_backend.available else InMemoryProgressBackend()
    if selector == "auto":
        redis_url = _env_text("HACKME_REDIS_URL") or _env_text("REDIS_URL")
        if redis_url:
            redis_backend = RedisProgressBackend(redis_url)
            if redis_backend.available:
                return redis_backend
        file_backend = FileProgressBackend()
        return file_backend if file_backend.available else InMemoryProgressBackend()
    return InMemoryProgressBackend()


def get_progress_backend() -> ProgressBackend:
    global _BACKEND
    if _BACKEND is None:
        with _BACKEND_LOCK:
            if _BACKEND is None:
                _BACKEND = _make_backend()
    return _BACKEND


def progress_backend_status() -> dict[str, Any]:
    return get_progress_backend().status()


def reset_progress_backend_for_tests() -> None:
    global _BACKEND
    with _BACKEND_LOCK:
        _BACKEND = None
