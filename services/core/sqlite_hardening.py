from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode

try:  # POSIX is the production runtime; keep a safe local-lock fallback elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts.
    fcntl = None


_WRITE_LOCKS: dict[str, threading.RLock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()
_WAL_READY_DB_PATHS: set[str] = set()
_WAL_READY_GUARD = threading.Lock()
_PROCESS_WRITE_LOCK_STATES: dict[str, dict[str, object]] = {}
_PROCESS_WRITE_LOCKS_GUARD = threading.Lock()


def _env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except Exception:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def sqlite_busy_timeout_ms() -> int:
    # Keep this aligned with the web connection timeout (15 seconds). A
    # shorter SQLite wait turns a recoverable writer queue into 503s.
    return _env_int("HACKME_SQLITE_BUSY_TIMEOUT_MS", 15000, minimum=1000, maximum=120000)


def sqlite_retry_attempts() -> int:
    return _env_int("HACKME_SQLITE_LOCK_RETRY_ATTEMPTS", 3, minimum=1, maximum=10)


def sqlite_retry_base_sleep_seconds() -> float:
    raw = os.environ.get("HACKME_SQLITE_LOCK_RETRY_BASE_SLEEP", "0.05")
    try:
        return max(0.01, min(1.0, float(str(raw).strip())))
    except Exception:
        return 0.05


def sqlite_cache_size_kb() -> int:
    return _env_int("HACKME_SQLITE_CACHE_SIZE_KB", 16384, minimum=1024, maximum=262144)


def sqlite_mmap_size_bytes() -> int:
    return _env_int("HACKME_SQLITE_MMAP_SIZE_BYTES", 64 * 1024 * 1024, minimum=0, maximum=1024 * 1024 * 1024)


def sqlite_journal_size_limit_bytes() -> int:
    return _env_int("HACKME_SQLITE_JOURNAL_SIZE_LIMIT_BYTES", 64 * 1024 * 1024, minimum=1024 * 1024, maximum=1024 * 1024 * 1024)


def sqlite_wal_autocheckpoint_pages() -> int:
    return _env_int("HACKME_SQLITE_WAL_AUTOCHECKPOINT_PAGES", 1000, minimum=100, maximum=100000)


def is_sqlite_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message or "database is busy" in message


def is_sqlite_retryable_operational_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    if is_sqlite_lock_error(exc):
        return True
    message = str(exc).lower()
    return "database schema has changed" in message


def _sql_starts_with(sql: str, prefixes: tuple[str, ...]) -> bool:
    head = str(sql or "").lstrip().lower()
    return any(head.startswith(prefix) for prefix in prefixes)


def _sql_may_write(sql: str) -> bool:
    return _sql_starts_with(
        sql,
        (
            "insert ",
            "update ",
            "delete ",
            "replace ",
            "create ",
            "drop ",
            "alter ",
            "reindex ",
            "vacuum",
            "attach ",
            "detach ",
            "begin",
            "pragma journal_mode",
            "pragma wal_checkpoint",
            "pragma optimize",
        ),
    )


def _db_lock_key(database: object) -> str:
    raw = str(database or "")
    if raw.startswith("file:") or raw == ":memory:":
        return raw
    try:
        return str(Path(raw).expanduser().resolve())
    except Exception:
        return raw


def _write_lock_for(database: object) -> threading.RLock:
    key = _db_lock_key(database)
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _WRITE_LOCKS[key] = lock
        return lock


def _process_write_lock_path(database: object) -> Path | None:
    """Return a sibling lock file for a file-backed SQLite database."""
    raw = str(database or "")
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        return None
    try:
        path = Path(raw).expanduser().resolve()
    except Exception:
        return None
    return path.with_name(f".{path.name}.hackme-write.lock")


def _acquire_process_write_lock(database: object) -> bool:
    """Serialize SQLite writers across sibling web-worker processes."""
    lock_path = _process_write_lock_path(database)
    if lock_path is None or fcntl is None:
        return False
    key = _db_lock_key(database)
    owner = threading.get_ident()
    with _PROCESS_WRITE_LOCKS_GUARD:
        state = _PROCESS_WRITE_LOCK_STATES.get(key)
        if state and state.get("owner") == owner:
            state["depth"] = int(state.get("depth") or 0) + 1
            return True
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise sqlite3.OperationalError(f"database is locked: cannot open writer lock ({exc})") from exc
    deadline = time.monotonic() + (sqlite_busy_timeout_ms() / 1000.0)
    sleep_seconds = min(0.05, sqlite_retry_base_sleep_seconds())
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise sqlite3.OperationalError("database is locked: writer queue timeout")
                time.sleep(sleep_seconds)
        with _PROCESS_WRITE_LOCKS_GUARD:
            _PROCESS_WRITE_LOCK_STATES[key] = {"owner": owner, "depth": 1, "handle": handle}
        return True
    except Exception:
        try:
            os.close(handle)
        except OSError:
            pass
        raise


def _release_process_write_lock(database: object) -> None:
    lock_path = _process_write_lock_path(database)
    if lock_path is None or fcntl is None:
        return
    key = _db_lock_key(database)
    owner = threading.get_ident()
    handle = None
    with _PROCESS_WRITE_LOCKS_GUARD:
        state = _PROCESS_WRITE_LOCK_STATES.get(key)
        if not state or state.get("owner") != owner:
            return
        remaining = int(state.get("depth") or 0) - 1
        if remaining > 0:
            state["depth"] = remaining
            return
        handle = state.get("handle")
        _PROCESS_WRITE_LOCK_STATES.pop(key, None)
    if isinstance(handle, int):
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            try:
                os.close(handle)
            except OSError:
                pass


def _can_use_wal(database: object, *, uri: bool = False) -> bool:
    raw = str(database or "")
    if not raw or raw == ":memory:":
        return False
    if uri and "mode=memory" in raw:
        return False
    return True


def _ensure_parent(database: object, *, uri: bool = False) -> None:
    if uri:
        return
    raw = str(database or "")
    if not raw or raw == ":memory:":
        return
    try:
        Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _readonly_uri(database: object) -> str:
    raw = str(database or "")
    if raw.startswith("file:"):
        separator = "&" if "?" in raw else "?"
        return f"{raw}{separator}{urlencode({'mode': 'ro', 'cache': 'shared'})}"
    path = Path(raw).expanduser().resolve()
    return f"file:{quote(str(path), safe='/')}?{urlencode({'mode': 'ro', 'cache': 'shared'})}"


class HardenedSQLiteConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        database = args[0] if args else ""
        super().__init__(*args, **kwargs)
        self._hackme_write_lock = _write_lock_for(database)
        self._hackme_write_lock_held = False
        self._hackme_process_write_lock_held = False
        self._hackme_database = database
        self._hackme_retry_attempts = sqlite_retry_attempts()
        self._hackme_retry_sleep = sqlite_retry_base_sleep_seconds()

    def _acquire_write_lock_for(self, sql: str) -> None:
        if _sql_may_write(sql) and not self._hackme_write_lock_held:
            self._hackme_write_lock.acquire()
            try:
                self._hackme_process_write_lock_held = _acquire_process_write_lock(self._hackme_database)
                self._hackme_write_lock_held = True
            except Exception:
                self._hackme_write_lock.release()
                raise

    def _release_write_lock(self) -> None:
        if self._hackme_write_lock_held:
            try:
                if self._hackme_process_write_lock_held:
                    _release_process_write_lock(self._hackme_database)
            finally:
                self._hackme_process_write_lock_held = False
                self._hackme_write_lock_held = False
                self._hackme_write_lock.release()

    def _with_locked_retry(self, operation):
        attempts = max(1, int(getattr(self, "_hackme_retry_attempts", 1) or 1))
        base_sleep = float(getattr(self, "_hackme_retry_sleep", 0.05) or 0.05)
        for attempt in range(attempts):
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not is_sqlite_retryable_operational_error(exc) or attempt >= attempts - 1:
                    raise
                time.sleep(base_sleep * (attempt + 1))

    def execute(self, sql, parameters=(), /):
        self._acquire_write_lock_for(sql)
        return self._with_locked_retry(lambda: sqlite3.Connection.execute(self, sql, parameters))

    def executemany(self, sql, parameters, /):
        self._acquire_write_lock_for(sql)
        return self._with_locked_retry(lambda: sqlite3.Connection.executemany(self, sql, parameters))

    def executescript(self, sql_script, /):
        self._acquire_write_lock_for(sql_script)
        return self._with_locked_retry(lambda: sqlite3.Connection.executescript(self, sql_script))

    def commit(self):
        try:
            return self._with_locked_retry(lambda: sqlite3.Connection.commit(self))
        except Exception:
            try:
                sqlite3.Connection.rollback(self)
            except Exception:
                pass
            raise
        finally:
            self._release_write_lock()

    def rollback(self):
        try:
            return sqlite3.Connection.rollback(self)
        finally:
            self._release_write_lock()

    def close(self):
        try:
            return sqlite3.Connection.close(self)
        finally:
            self._release_write_lock()


def configure_sqlite_connection(
    conn: sqlite3.Connection,
    database: object,
    *,
    row_factory: bool = True,
    foreign_keys: bool = True,
    wal: bool = True,
    uri: bool = False,
    busy_timeout_ms: int | None = None,
    query_only: bool = False,
) -> sqlite3.Connection:
    if row_factory:
        conn.row_factory = sqlite3.Row
    timeout_ms = sqlite_busy_timeout_ms() if busy_timeout_ms is None else max(1000, int(busy_timeout_ms))
    try:
        conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    except Exception:
        pass
    try:
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        pass
    if wal and _can_use_wal(database, uri=uri):
        key = _db_lock_key(database)
        if key not in _WAL_READY_DB_PATHS:
            with _WAL_READY_GUARD:
                if key not in _WAL_READY_DB_PATHS:
                    try:
                        conn.execute("PRAGMA journal_mode = WAL")
                        _WAL_READY_DB_PATHS.add(key)
                    except Exception:
                        pass
    try:
        conn.execute(f"PRAGMA wal_autocheckpoint = {sqlite_wal_autocheckpoint_pages()}")
    except Exception:
        pass
    try:
        conn.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        pass
    try:
        conn.execute("PRAGMA temp_store = MEMORY")
    except Exception:
        pass
    try:
        conn.execute(f"PRAGMA cache_size = {-sqlite_cache_size_kb()}")
    except Exception:
        pass
    mmap_size = sqlite_mmap_size_bytes()
    if mmap_size > 0:
        try:
            conn.execute(f"PRAGMA mmap_size = {mmap_size}")
        except Exception:
            pass
    try:
        conn.execute(f"PRAGMA journal_size_limit = {sqlite_journal_size_limit_bytes()}")
    except Exception:
        pass
    if query_only:
        try:
            conn.execute("PRAGMA query_only = ON")
        except Exception:
            pass
    if isinstance(conn, HardenedSQLiteConnection) and conn._hackme_write_lock_held:
        try:
            conn.commit()
        except Exception:
            pass
    return conn


def connect_sqlite(
    database: object,
    *,
    timeout: float | None = None,
    row_factory: bool = True,
    foreign_keys: bool = True,
    wal: bool = True,
    uri: bool = False,
    serialized_writes: bool = True,
    busy_timeout_ms: int | None = None,
    read_only: bool = False,
) -> sqlite3.Connection:
    if read_only:
        database = _readonly_uri(database)
        uri = True
        wal = False
        serialized_writes = False
    _ensure_parent(database, uri=uri)
    timeout_seconds = float(timeout if timeout is not None else sqlite_busy_timeout_ms() / 1000.0)
    factory = HardenedSQLiteConnection if serialized_writes else sqlite3.Connection
    conn = sqlite3.connect(str(database), timeout=timeout_seconds, uri=uri, factory=factory)
    return configure_sqlite_connection(
        conn,
        database,
        row_factory=row_factory,
        foreign_keys=foreign_keys,
        wal=wal,
        uri=uri,
        busy_timeout_ms=busy_timeout_ms,
        query_only=read_only,
    )


def connect_sqlite_readonly(
    database: object,
    *,
    timeout: float | None = None,
    row_factory: bool = True,
    foreign_keys: bool = True,
    busy_timeout_ms: int | None = None,
) -> sqlite3.Connection:
    return connect_sqlite(
        database,
        timeout=timeout,
        row_factory=row_factory,
        foreign_keys=foreign_keys,
        wal=False,
        uri=False,
        serialized_writes=False,
        busy_timeout_ms=busy_timeout_ms,
        read_only=True,
    )
