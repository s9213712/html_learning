import hashlib
import hmac
import json
import os
import random
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

import argon2
from flask import current_app, has_request_context, jsonify, make_response, request

from services.security.events import record_security_event


def _env_int(name, default, *, minimum=1, maximum=64):
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


def _env_float(name, default, *, minimum=0.1, maximum=120.0):
    try:
        value = float(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        value = float(default)
    return max(float(minimum), min(float(maximum), value))


SESSION_TTL = 3600 * 4
CSRF_TOKEN_TTL = SESSION_TTL
AUTHENTICATED_CSRF_TOKEN_KEEP = 8
SESSION_IDLE_TIMEOUT = 10 * 60
SESSION_LAST_SEEN_REFRESH_INTERVAL = _env_int(
    "HTML_LEARNING_SESSION_LAST_SEEN_REFRESH_INTERVAL",
    20,
    minimum=1,
    maximum=3600,
)
SESSION_LAST_SEEN_REFRESH_JITTER_SECONDS = _env_int(
    "HTML_LEARNING_SESSION_LAST_SEEN_REFRESH_JITTER_SECONDS",
    20,
    minimum=0,
    maximum=3600,
)
MIN_DELAY = 0.25
MAX_DELAY = 0.90
CSRF_PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_STATE = {
    "get_db": None,
    "get_auth_db": None,
    "get_readonly_auth_db": None,
    "get_user_by_username": None,
    "fernet": None,
    "get_client_ip": None,
    "session_ttl": SESSION_TTL,
    "csrf_token_ttl": CSRF_TOKEN_TTL,
    "session_idle_timeout": SESSION_IDLE_TIMEOUT,
    "session_last_seen_refresh_interval": SESSION_LAST_SEEN_REFRESH_INTERVAL,
    "session_last_seen_refresh_jitter_seconds": SESSION_LAST_SEEN_REFRESH_JITTER_SECONDS,
    "tester_token_user_lookup": None,
    "get_runtime_server_mode": None,
    "get_system_settings": None,
    "csrf_secret": None,
}

_ARGON2_TIME_COST = _env_int(
    "HTML_LEARNING_ARGON2_TIME_COST",
    3,
    minimum=1,
    maximum=10,
)
_ARGON2_MEMORY_COST = _env_int(
    "HTML_LEARNING_ARGON2_MEMORY_COST",
    65536,
    minimum=1024,
    maximum=1048576,
)
_ARGON2_PARALLELISM = _env_int(
    "HTML_LEARNING_ARGON2_PARALLELISM",
    min(4, os.cpu_count() or 1),
    minimum=1,
    maximum=16,
)
_hasher = argon2.PasswordHasher(
    time_cost=_ARGON2_TIME_COST,
    memory_cost=_ARGON2_MEMORY_COST,
    parallelism=_ARGON2_PARALLELISM,
    hash_len=32,
    salt_len=16,
)


_ARGON2_VERIFY_CONCURRENCY = _env_int(
    "HTML_LEARNING_ARGON2_VERIFY_CONCURRENCY",
    min(4, os.cpu_count() or 1),
    minimum=1,
    maximum=16,
)
_ARGON2_VERIFY_TIMEOUT_SECONDS = _env_float(
    "HTML_LEARNING_ARGON2_VERIFY_TIMEOUT_SECONDS",
    30.0,
    minimum=1.0,
    maximum=120.0,
)
_ARGON2_VERIFY_SEMAPHORE = threading.BoundedSemaphore(_ARGON2_VERIFY_CONCURRENCY)
_SESSION_COLUMNS_CACHE = {"columns": None}
_SESSION_COLUMNS_LOCK = threading.RLock()
_SESSION_TOUCH_CACHE: dict[int, float] = {}
_SESSION_TOUCH_LOCK = threading.RLock()


def _is_sqlite_locked_error(exc):
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def configure_auth_service(
    *,
    get_db,
    get_auth_db=None,
    get_readonly_auth_db=None,
    get_user_by_username,
    fernet,
    get_client_ip=None,
    session_ttl=SESSION_TTL,
    csrf_token_ttl=CSRF_TOKEN_TTL,
    session_idle_timeout=SESSION_IDLE_TIMEOUT,
    session_last_seen_refresh_interval=SESSION_LAST_SEEN_REFRESH_INTERVAL,
    session_last_seen_refresh_jitter_seconds=SESSION_LAST_SEEN_REFRESH_JITTER_SECONDS,
    tester_token_user_lookup=None,
    get_runtime_server_mode=None,
    get_system_settings=None,
    csrf_secret=None,
):
    _STATE.update({
        "get_db": get_db,
        "get_auth_db": get_auth_db or get_db,
        "get_readonly_auth_db": get_readonly_auth_db,
        "get_user_by_username": get_user_by_username,
        "fernet": fernet,
        "get_client_ip": get_client_ip,
        "session_ttl": session_ttl,
        "csrf_token_ttl": csrf_token_ttl,
        "session_idle_timeout": session_idle_timeout,
        "session_last_seen_refresh_interval": session_last_seen_refresh_interval,
        "session_last_seen_refresh_jitter_seconds": session_last_seen_refresh_jitter_seconds,
        "tester_token_user_lookup": tester_token_user_lookup,
        "get_runtime_server_mode": get_runtime_server_mode,
        "get_system_settings": get_system_settings,
        "csrf_secret": csrf_secret,
    })
    with _SESSION_COLUMNS_LOCK:
        _SESSION_COLUMNS_CACHE["columns"] = None
    with _SESSION_TOUCH_LOCK:
        _SESSION_TOUCH_CACHE.clear()


def _is_superweak_csrf_bypass():
    """SERVER_MODE_V2_PROFILE_MATRIX.md §Mode Behavior Matrix footnote 1.

    CSRF is on in every mode EXCEPT `superweak` (the deliberate weakest
    web mode used for red-team / fuzz / pentest, alongside disabled
    rate limit / login lock / account lock / password strength).

    Returns True only when the runtime mode is exactly 'superweak';
    every other mode (including unknown / unconfigured) keeps CSRF on.
    Reads mode every call — no cache — so a switch *out of* superweak
    re-arms CSRF immediately and a switch *into* superweak only
    bypasses for requests that actually start in that mode.
    """
    reader = _STATE.get("get_runtime_server_mode")
    if not callable(reader):
        return False
    try:
        return reader() == "superweak"
    except Exception:
        return False


def json_resp(data, status=200):
    started = time.perf_counter()
    r = make_response(jsonify(data), status)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    if has_request_context():
        try:
            previous = float(request.environ.get("hackme.json_serialize_ms") or 0.0)
            request.environ["hackme.json_serialize_ms"] = round(previous + elapsed_ms, 3)
        except Exception:
            pass
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Cache-Control"] = "no-store"
    return r


def _request_ip():
    if not has_request_context():
        return "-"
    get_client_ip = _STATE.get("get_client_ip")
    if callable(get_client_ip):
        try:
            return get_client_ip() or "-"
        except Exception:
            pass
    return request.remote_addr or "-"


def _request_user_agent():
    if not has_request_context():
        return ""
    try:
        return str(request.headers.get("User-Agent") or "")
    except Exception:
        return ""


def _read_bool_setting(conn, key, default=False):
    try:
        row = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
    except Exception:
        return default
    value = row["value"] if row and "value" in row.keys() else (row[0] if row else default)
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _main_db():
    return _STATE["get_db"]()


def _auth_db():
    getter = _STATE.get("get_auth_db") or _STATE.get("get_db")
    return getter()


def _auth_read_db():
    getter = _STATE.get("get_readonly_auth_db") or _STATE.get("get_auth_db") or _STATE.get("get_db")
    return getter()


def _auth_write(operation, *, attempts=5, delay_seconds=0.1):
    last_exc = None
    for attempt in range(max(1, int(attempts))):
        conn = _auth_db()
        try:
            result = operation(conn)
            conn.commit()
            return result
        except Exception as exc:
            last_exc = exc
            try:
                conn.rollback()
            except Exception:
                pass
            if _is_sqlite_locked_error(exc) and attempt + 1 < max(1, int(attempts)):
                time.sleep(delay_seconds * (attempt + 1))
                continue
            raise
        finally:
            conn.close()
    if last_exc is not None:
        raise last_exc
    return None


_PUBLIC_CSRF_PREFIX = "pcsrf1"


def _csrf_signing_secret():
    secret = _STATE.get("csrf_secret")
    if secret:
        return str(secret)
    if has_request_context():
        try:
            app_secret = current_app.config.get("SECRET_KEY")
            if app_secret:
                return str(app_secret)
        except Exception:
            pass
    return "hackme-public-csrf-test-secret"


def _public_csrf_payload(issued_at, expires_at, nonce):
    return f"{_PUBLIC_CSRF_PREFIX}|__public__|{issued_at}|{expires_at}|{nonce}"


def _public_csrf_signature(issued_at, expires_at, nonce):
    return hmac.new(
        _csrf_signing_secret().encode("utf-8"),
        _public_csrf_payload(issued_at, expires_at, nonce).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _make_public_csrf_token():
    issued_at = int(time.time())
    expires_at = issued_at + int(effective_csrf_token_ttl_seconds())
    nonce = secrets.token_urlsafe(18)
    sig = _public_csrf_signature(issued_at, expires_at, nonce)
    return f"{_PUBLIC_CSRF_PREFIX}.{issued_at}.{expires_at}.{nonce}.{sig}"


def _verify_public_csrf_token(token):
    if not isinstance(token, str) or not token.startswith(f"{_PUBLIC_CSRF_PREFIX}."):
        return False
    parts = token.split(".")
    if len(parts) != 5:
        return False
    _, issued_raw, expires_raw, nonce, sig = parts
    try:
        issued_at = int(issued_raw)
        expires_at = int(expires_raw)
    except Exception:
        return False
    now = int(time.time())
    if issued_at > now + 300 or expires_at <= now:
        return False
    if expires_at - issued_at > int(effective_csrf_token_ttl_seconds()) + 300:
        return False
    if not nonce or len(sig) != 64:
        return False
    expected = _public_csrf_signature(issued_at, expires_at, nonce)
    return hmac.compare_digest(sig, expected)


def _session_columns(auth_conn):
    with _SESSION_COLUMNS_LOCK:
        cached = _SESSION_COLUMNS_CACHE.get("columns")
        if cached is not None:
            return cached
    try:
        columns = frozenset(item["name"] for item in auth_conn.execute("PRAGMA table_info(sessions)").fetchall())
    except Exception:
        columns = frozenset()
    with _SESSION_COLUMNS_LOCK:
        _SESSION_COLUMNS_CACHE["columns"] = columns
    return columns


def _session_last_seen_refresh_threshold(session_id):
    base = max(1, int(_STATE.get("session_last_seen_refresh_interval") or SESSION_LAST_SEEN_REFRESH_INTERVAL))
    jitter = max(0, int(_STATE.get("session_last_seen_refresh_jitter_seconds") or 0))
    try:
        sid = int(session_id or 0)
    except Exception:
        sid = 0
    return base + (sid % (jitter + 1) if jitter else 0)


def _claim_session_touch(session_id, now_ts, threshold_seconds):
    try:
        sid = int(session_id)
    except Exception:
        return True
    with _SESSION_TOUCH_LOCK:
        previous = float(_SESSION_TOUCH_CACHE.get(sid, 0.0) or 0.0)
        if previous and (float(now_ts) - previous) < max(1, int(threshold_seconds)):
            return False
        _SESSION_TOUCH_CACHE[sid] = float(now_ts)
        if len(_SESSION_TOUCH_CACHE) > 10000:
            cutoff = float(now_ts) - max(60, int(threshold_seconds) * 4)
            for key, value in list(_SESSION_TOUCH_CACHE.items())[:2000]:
                if float(value or 0.0) < cutoff:
                    _SESSION_TOUCH_CACHE.pop(key, None)
        return True


def record_login_attempt(*, user_id, ip_address, user_agent, success, attempted_at):
    def _write(conn):
        conn.execute(
            "INSERT INTO login_attempts (user_id, ip_address, user_agent, success, attempted_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, ip_address, user_agent, 1 if success else 0, attempted_at),
        )
    _auth_write(_write)


def _record_csrf_failure(reason, username="-"):
    record_security_event(
        "csrf_fail",
        _request_ip(),
        target_user=username or "-",
        detail=f"path={request.path},reason={reason}",
    )


def csrf_invalid_response(reason="invalid", username="-"):
    _record_csrf_failure(reason, username)
    return json_resp({
        "ok": False,
        "error": "csrf_invalid",
        "message": "CSRF token expired or invalid",
    }), 403


def generate_csrf_dummy():
    tok = request.cookies.get("csrf_token", "")
    return tok


def verify_csrf_double_submit(body_token):
    if not isinstance(body_token, str):
        return False
    cookie_tok = request.cookies.get("csrf_token", "")
    if not isinstance(cookie_tok, str) or not cookie_tok or not body_token:
        return False
    # Constant-time compare to deny a timing oracle on the CSRF token.
    # `==` short-circuits on the first mismatching byte, leaking position
    # information. hmac.compare_digest takes the same time for any pair
    # of equal-length strings.  See issue #180.
    if not hmac.compare_digest(cookie_tok, body_token):
        return False
    tok_hash = hashlib.sha256(cookie_tok.encode()).hexdigest()
    conn = _auth_db()
    try:
        now = datetime.now().isoformat()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT username FROM csrf_tokens WHERE token_hash=? AND expires_at>?",
            (tok_hash, now)
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        conn.execute("DELETE FROM csrf_tokens WHERE token_hash=?", (tok_hash,))
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def hash_password(pw):
    return _hasher.hash(pw)


def verify_password(h, p):
    acquired = _ARGON2_VERIFY_SEMAPHORE.acquire(timeout=_ARGON2_VERIFY_TIMEOUT_SECONDS)
    if not acquired:
        return False
    try:
        return _hasher.verify(h, p)
    except argon2.exceptions.VerifyMismatchError:
        return False
    except argon2.exceptions.VerificationError:
        return False
    except argon2.exceptions.InvalidHash:
        return False
    except (argon2.exceptions.DecodeError, ValueError, TypeError):
        return False
    except Exception:
        return False
    finally:
        _ARGON2_VERIFY_SEMAPHORE.release()


def make_token(username):
    payload = json.dumps({
        "user": username,
        "exp": (datetime.now() + timedelta(seconds=effective_session_ttl_seconds())).isoformat(),
        "nonce": secrets.token_hex(8),
    }, ensure_ascii=False)
    return _STATE["fernet"].encrypt(payload.encode()).decode()


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _device_info_from_user_agent(ua):
    ua = ua or "-"
    lowered = ua.lower()
    browser = "Other"
    if "edg/" in lowered:
        browser = "Edge"
    elif "chrome/" in lowered and "chromium" not in lowered:
        browser = "Chrome"
    elif "firefox/" in lowered:
        browser = "Firefox"
    elif "safari/" in lowered and "chrome/" not in lowered:
        browser = "Safari"
    os_name = "Other"
    if "windows" in lowered:
        os_name = "Windows"
    elif "android" in lowered:
        os_name = "Android"
    elif "iphone" in lowered or "ipad" in lowered:
        os_name = "iOS"
    elif "mac os" in lowered or "macintosh" in lowered:
        os_name = "macOS"
    elif "linux" in lowered:
        os_name = "Linux"
    device = "Mobile" if any(token in lowered for token in ("mobile", "android", "iphone")) else "Desktop"
    return json.dumps({"browser": browser, "os": os_name, "device": device}, ensure_ascii=False)


def _current_security_epoch(conn):
    try:
        row = conn.execute("SELECT value FROM system_settings WHERE key='server_security_epoch'").fetchone()
        return int((row["value"] if row else 0) or 0)
    except Exception:
        return 0


def _positive_int(value, fallback, *, minimum=1):
    try:
        parsed = int(value)
    except Exception:
        parsed = int(fallback)
    return max(int(minimum), parsed)


def _runtime_setting_value(key, *, conn=None):
    if conn is not None:
        try:
            row = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
            if row is not None:
                return row["value"]
        except Exception:
            pass
    provider = _STATE.get("get_system_settings")
    if callable(provider):
        try:
            settings = provider() or {}
        except Exception:
            settings = {}
        if isinstance(settings, dict) and key in settings:
            return settings.get(key)
    return None


def effective_session_ttl_seconds(*, conn=None):
    fallback = _positive_int(_STATE.get("session_ttl"), SESSION_TTL, minimum=60)
    hours = _runtime_setting_value("session_ttl_hours", conn=conn)
    if hours is None:
        return fallback
    return _positive_int(hours, max(1, fallback // 3600), minimum=1) * 3600


def effective_csrf_token_ttl_seconds(*, conn=None):
    fallback = _positive_int(_STATE.get("csrf_token_ttl"), CSRF_TOKEN_TTL, minimum=60)
    return max(fallback, effective_session_ttl_seconds(conn=conn))


def effective_session_idle_timeout_seconds(*, conn=None):
    fallback = _positive_int(_STATE.get("session_idle_timeout"), SESSION_IDLE_TIMEOUT, minimum=30)
    minutes = _runtime_setting_value("session_idle_timeout_minutes", conn=conn)
    if minutes is None:
        return fallback
    try:
        minutes_value = int(minutes)
    except Exception:
        return fallback
    if minutes_value <= 0:
        return 0
    return _positive_int(minutes_value, max(1, fallback // 60), minimum=1) * 60


def _parse_datetime(value):
    try:
        return datetime.fromisoformat(str(value or ""))
    except Exception:
        return None


def timing_delay():
    time.sleep(MIN_DELAY + random.uniform(0, MAX_DELAY - MIN_DELAY))


def make_csrf_token():
    return _make_public_csrf_token()


def store_csrf_token(token, username):
    if username == "__public__" and _verify_public_csrf_token(token):
        return
    expires = (datetime.now() + timedelta(seconds=effective_csrf_token_ttl_seconds())).isoformat()
    now = datetime.now().isoformat()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    def _write(conn):
        conn.execute("DELETE FROM csrf_tokens WHERE expires_at<=?", (now,))
        conn.execute(
            "INSERT OR REPLACE INTO csrf_tokens (token_hash, username, expires_at) VALUES (?, ?, ?)",
            (token_hash, username, expires)
        )
    _auth_write(_write)


def verify_csrf_token(token, username):
    if not isinstance(token, str) or not token:
        return False
    if not isinstance(username, str) or not username:
        return False
    if username == "__public__" and _verify_public_csrf_token(token):
        return True
    conn = _auth_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM csrf_tokens WHERE token_hash=? AND username=? AND expires_at>?",
            (hashlib.sha256(token.encode()).hexdigest(), username, datetime.now().isoformat())
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def consume_csrf_token(token, username):
    if not isinstance(token, str) or not token:
        return False
    if not isinstance(username, str) or not username:
        return False
    if username == "__public__" and _verify_public_csrf_token(token):
        return True
    conn = _auth_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT 1 FROM csrf_tokens WHERE token_hash=? AND username=? AND expires_at>?",
            (hashlib.sha256(token.encode()).hexdigest(), username, datetime.now().isoformat())
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        conn.execute(
            "DELETE FROM csrf_tokens WHERE token_hash=? AND username=?",
            (hashlib.sha256(token.encode()).hexdigest(), username),
        )
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def delete_csrf_token(token):
    if not token:
        return
    h = hashlib.sha256(token.encode()).hexdigest()
    conn = _auth_db()
    conn.execute("DELETE FROM csrf_tokens WHERE token_hash=?", (h,))
    conn.commit()
    conn.close()


def delete_csrf_tokens_for_username(username):
    if not username:
        return
    conn = _auth_db()
    try:
        conn.execute("DELETE FROM csrf_tokens WHERE username=?", (username,))
        conn.commit()
    finally:
        conn.close()


def prune_authenticated_csrf_tokens(username, *, keep=AUTHENTICATED_CSRF_TOKEN_KEEP):
    if not username:
        return
    try:
        keep_count = max(1, int(keep or AUTHENTICATED_CSRF_TOKEN_KEEP))
    except Exception:
        keep_count = AUTHENTICATED_CSRF_TOKEN_KEEP
    conn = _auth_db()
    try:
        now = datetime.now().isoformat()
        conn.execute("DELETE FROM csrf_tokens WHERE expires_at<=?", (now,))
        conn.execute(
            """
            DELETE FROM csrf_tokens
            WHERE username=?
              AND token_hash NOT IN (
                SELECT token_hash FROM csrf_tokens
                WHERE username=?
                ORDER BY expires_at DESC, rowid DESC
                LIMIT ?
              )
            """,
            (username, username, keep_count),
        )
        conn.commit()
    finally:
        conn.close()


def _rotate_authenticated_csrf(response, csrf_tok, username):
    resp = make_response(response)
    if resp.status_code >= 400 or not csrf_tok or not username:
        return resp
    new_csrf = make_csrf_token()
    store_csrf_token(new_csrf, username)
    prune_authenticated_csrf_tokens(username)
    resp.set_cookie(
        "csrf_token",
        new_csrf,
        max_age=effective_csrf_token_ttl_seconds(),
        httponly=False,
        samesite=current_app.config.get("SESSION_COOKIE_SAMESITE", "Lax"),
        secure=bool(current_app.config.get("SESSION_COOKIE_SECURE", False)),
        path="/",
    )
    return resp


def require_csrf(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method not in CSRF_PROTECTED_METHODS:
            return f(*args, **kwargs)
        # SERVER_MODE_V2_PROFILE_MATRIX.md §Mode Behavior Matrix footnote 1:
        # CSRF on in every mode EXCEPT `superweak`.
        if _is_superweak_csrf_bypass():
            record_security_event(
                "csrf_skipped_superweak",
                _request_ip(),
                target_user="-",
                detail=f"path={request.path},decorator=require_csrf",
            )
            return f(*args, **kwargs)
        csrf_tok = get_request_csrf_token()
        body_username = None
        if request.is_json:
            body = request.get_json(silent=True) or {}
            if isinstance(body, dict):
                body_username = body.get("username", "")
                if isinstance(body_username, str):
                    body_username = body_username.strip()
                else:
                    body_username = ""

        tok = request.cookies.get("session_token")
        user = db_get_user_from_token(tok) if tok else None

        if request.path == "/api/login":
            csrf_owner = ""
            if user and verify_csrf_token(csrf_tok, user):
                csrf_owner = user
            elif verify_csrf_token(csrf_tok, "__public__"):
                csrf_owner = "__public__"
            elif body_username and verify_csrf_token(csrf_tok, body_username):
                csrf_owner = body_username
            if not csrf_owner:
                return csrf_invalid_response("invalid_login", body_username or user or "-")
            response = f(*args, **kwargs)
            if csrf_tok:
                consume_csrf_token(csrf_tok, csrf_owner)
            return response

        if user:
            # Authenticated: token stored under username
            if not verify_csrf_token(csrf_tok, user):
                return csrf_invalid_response("invalid_authenticated", user)
        else:
            # Unauthenticated: token stored under "__public__" OR under body_username
            if not csrf_tok:
                return csrf_invalid_response("missing_public", body_username or "-")
            csrf_owner = "__public__"
            if not verify_csrf_token(csrf_tok, csrf_owner):
                # Also accept token stored under body_username (e.g. login request)
                if not body_username or not verify_csrf_token(csrf_tok, body_username):
                    return csrf_invalid_response("invalid_public", body_username or "-")
                csrf_owner = body_username

        response = f(*args, **kwargs)
        if user:
            response = _rotate_authenticated_csrf(response, csrf_tok, user)
        elif csrf_tok:
            consume_csrf_token(csrf_tok, csrf_owner)
        return response
    return decorated


def require_csrf_safe(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        tok = request.cookies.get("session_token")
        user = db_get_user_from_token(tok) if tok else None
        if not user:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        if request.method not in CSRF_PROTECTED_METHODS:
            return f(*args, **kwargs)
        # CSRF policy: see comment in require_csrf above. superweak bypass
        # also applies here so that CSRF posture is uniform across both
        # decorators — otherwise red-team / pentest tooling would be blocked
        # on authenticated endpoints in superweak only.
        if _is_superweak_csrf_bypass():
            record_security_event(
                "csrf_skipped_superweak",
                _request_ip(),
                target_user=user,
                detail=f"path={request.path},decorator=require_csrf_safe",
            )
            return f(*args, **kwargs)
        csrf_tok = get_request_csrf_token()
        if not verify_csrf_token(csrf_tok, user):
            return csrf_invalid_response("invalid_safe", user)
        response = f(*args, **kwargs)
        response = _rotate_authenticated_csrf(response, csrf_tok, user)
        return response
    return decorated


def get_request_csrf_token():
    token = request.headers.get("X-CSRF-Token", "") or ""
    if not isinstance(token, str):
        token = ""
    if token:
        return token
    if request.form:
        req_token = request.form.get("csrf_token", "")
        if isinstance(req_token, str):
            return req_token
    if request.is_json:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            req_token = body.get("csrf_token", "")
            if isinstance(req_token, str):
                return req_token
    return ""


def _normalize_session_allowed_features(allowed_features):
    if not allowed_features:
        return []
    if isinstance(allowed_features, str):
        items = allowed_features.replace("\n", ",").split(",")
    else:
        try:
            items = list(allowed_features)
        except Exception:
            items = []
    normalized = []
    for item in items:
        key = str(item or "").strip()
        if key and key not in normalized:
            normalized.append(key)
    return normalized


def db_save_session(user_id, token, ip, ua, *, auth_scope="", allowed_features=None):
    now = datetime.now().isoformat()
    expires = (datetime.now() + timedelta(seconds=effective_session_ttl_seconds())).isoformat()
    # Sessions may live in a dedicated auth database, while the security epoch
    # is authoritative in the main control database.  Reading the epoch from
    # the auth connection silently returned zero after incident/maintenance
    # rotation, so a successful root login was revoked on its very next
    # request.  Capture the authoritative epoch before writing auth state.
    main_conn = _main_db()
    try:
        session_epoch = _current_security_epoch(main_conn)
    finally:
        main_conn.close()

    def _write(conn):
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        auth_scope_value = str(auth_scope or "").strip()
        allowed_features_json = json.dumps(_normalize_session_allowed_features(allowed_features), ensure_ascii=True, sort_keys=True)
        optional_cols = []
        optional_values = []
        if "auth_scope" in cols:
            optional_cols.append("auth_scope")
            optional_values.append(auth_scope_value)
        if "allowed_features_json" in cols:
            optional_cols.append("allowed_features_json")
            optional_values.append(allowed_features_json)
        if "device_info" in cols:
            cols_sql = ["user_id", "token_hash", "ip_address", "user_agent", "device_info", "expires_at", "last_seen"]
            values = [user_id, _hash_token(token), ip, ua, _device_info_from_user_agent(ua), expires, now]
            if "session_epoch" in cols:
                cols_sql.append("session_epoch")
                values.append(session_epoch)
        else:
            cols_sql = ["user_id", "token_hash", "ip_address", "user_agent", "expires_at", "last_seen"]
            values = [user_id, _hash_token(token), ip, ua, expires, now]
        if "created_at" in cols:
            cols_sql.append("created_at")
            values.append(now)
        cols_sql.extend(optional_cols)
        values.extend(optional_values)
        placeholders = ", ".join("?" for _ in values)
        conn.execute(
            f"INSERT INTO sessions ({', '.join(cols_sql)}) VALUES ({placeholders})",
            tuple(values),
        )
    _auth_write(_write)


def db_delete_session(token, *, notify_security_event=False, detail="single_session_logout"):
    conn = _auth_db()
    try:
        conn.execute(
            "UPDATE sessions SET is_revoked=1, revoked_at=? WHERE token_hash=?",
            (datetime.now().isoformat(), _hash_token(token))
        )
        conn.commit()
        if notify_security_event:
            record_security_event("session_revoked", _request_ip(), detail=detail)
    finally:
        conn.close()


def revoke_user_sessions(user_id, *, notify_security_event=True, detail="user_sessions_revoked"):
    conn = _auth_db()
    try:
        conn.execute(
            "UPDATE sessions SET is_revoked=1, revoked_at=? WHERE user_id=? AND is_revoked=0",
            (datetime.now().isoformat(), user_id)
        )
        conn.commit()
        if notify_security_event:
            record_security_event("session_revoked", "-", target_user=str(user_id), detail=detail)
    finally:
        conn.close()


def db_clean_expired_sessions():
    conn = _auth_db()
    try:
        conn.execute(
            "DELETE FROM sessions WHERE expires_at < ? OR is_revoked=1",
            (datetime.now().isoformat(),)
        )
        conn.commit()
    finally:
        conn.close()


def db_get_user_from_token(token):
    auth_conn = _auth_read_db()
    try:
        now = datetime.now()
        now_iso = now.isoformat()
        session_cols = _session_columns(auth_conn)
        session_epoch_expr = "s.session_epoch" if "session_epoch" in session_cols else "0"
        auth_scope_expr = "s.auth_scope" if "auth_scope" in session_cols else "''"
        allowed_features_expr = "s.allowed_features_json" if "allowed_features_json" in session_cols else "'[]'"
        created_at_expr = "s.created_at" if "created_at" in session_cols else "s.expires_at"
        expires_at_expr = "s.expires_at" if "expires_at" in session_cols else "''"
        row = auth_conn.execute(
            f"SELECT s.id, s.user_id, s.last_seen, s.ip_address, s.user_agent, "
            f"{created_at_expr} AS created_at, {expires_at_expr} AS expires_at, "
            f"COALESCE({session_epoch_expr}, 0) AS session_epoch, "
            f"COALESCE({auth_scope_expr}, '') AS auth_scope, "
            f"COALESCE({allowed_features_expr}, '[]') AS allowed_features_json "
            "FROM sessions s "
            "WHERE s.token_hash=? AND COALESCE(s.is_revoked, 0)=0",
            (_hash_token(token),)
        ).fetchone()
        if not row:
            return None
        main_conn = _main_db()
        try:
            user_row = main_conn.execute(
                "SELECT username, status FROM users WHERE id=?",
                (row["user_id"],),
            ).fetchone()
            if not user_row or str(user_row["status"] or "") != "active":
                return None
            username = user_row["username"]
            current_epoch = _current_security_epoch(main_conn)
            strict_ip_binding = _read_bool_setting(main_conn, "session_strict_ip_binding", default=False)
            session_ttl_seconds = effective_session_ttl_seconds(conn=main_conn)
            session_idle_timeout_seconds = effective_session_idle_timeout_seconds(conn=main_conn)
        finally:
            main_conn.close()
        created_at = _parse_datetime(row["created_at"]) or _parse_datetime(row["expires_at"]) or now
        effective_expires_at = created_at + timedelta(seconds=session_ttl_seconds)
        stored_expires_at = _parse_datetime(row["expires_at"])
        if now >= effective_expires_at:
            try:
                _auth_write(lambda conn: conn.execute(
                    "UPDATE sessions SET is_revoked=1, revoked_at=? WHERE id=?",
                    (now_iso, row["id"])
                ))
            except sqlite3.OperationalError:
                pass
            record_security_event("session_revoked", "-", target_user=username, detail="session_ttl_expired")
            return None
        if stored_expires_at is not None and stored_expires_at < effective_expires_at:
            try:
                _auth_write(lambda conn: conn.execute(
                    "UPDATE sessions SET expires_at=? WHERE id=?",
                    (effective_expires_at.isoformat(), row["id"])
                ))
            except sqlite3.OperationalError:
                pass
        if int(row["session_epoch"] or 0) < current_epoch:
            try:
                _auth_write(lambda conn: conn.execute(
                    "UPDATE sessions SET is_revoked=1, revoked_at=? WHERE id=?",
                    (now_iso, row["id"])
                ))
            except sqlite3.OperationalError:
                pass
            record_security_event("session_revoked", "-", target_user=username, detail="security_epoch_rotated")
            return None

        stored_ip = str(row["ip_address"] or "").strip()
        current_ip = _request_ip()
        if stored_ip and current_ip and stored_ip != current_ip:
            record_security_event(
                "session_ip_mismatch",
                current_ip,
                target_user=username,
                detail=f"stored={stored_ip}",
            )
            if strict_ip_binding:
                try:
                    _auth_write(lambda conn: conn.execute(
                        "UPDATE sessions SET is_revoked=1, revoked_at=? WHERE id=?",
                        (now_iso, row["id"])
                    ))
                except sqlite3.OperationalError:
                    pass
                record_security_event("session_revoked", current_ip, target_user=username, detail=f"strict_ip_binding stored={stored_ip}")
                return None

        stored_ua = str(row["user_agent"] or "").strip()
        current_ua = _request_user_agent().strip()
        if stored_ua and current_ua and stored_ua != current_ua:
            record_security_event(
                "session_user_agent_mismatch",
                current_ip or "-",
                target_user=username,
                detail=f"stored={stored_ua[:120]},current={current_ua[:120]}",
            )

        last_seen = row["last_seen"]
        idle_seconds = 0
        if last_seen:
            try:
                idle_seconds = (now - datetime.fromisoformat(last_seen)).total_seconds()
            except Exception:
                idle_seconds = 0
            if session_idle_timeout_seconds > 0 and idle_seconds > session_idle_timeout_seconds:
                try:
                    _auth_write(lambda conn: conn.execute(
                        "UPDATE sessions SET is_revoked=1, revoked_at=? WHERE id=?",
                        (now_iso, row["id"])
                    ))
                except sqlite3.OperationalError:
                    pass
                record_security_event("session_revoked", "-", target_user=username, detail="idle_timeout")
                return None

        refresh_threshold = _session_last_seen_refresh_threshold(row["id"])
        if idle_seconds >= refresh_threshold and _claim_session_touch(row["id"], now.timestamp(), refresh_threshold):
            try:
                _auth_write(lambda conn: conn.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now_iso, row["id"])))
            except sqlite3.OperationalError:
                pass
        if has_request_context():
            allowed_features = []
            try:
                parsed_features = json.loads(str(row["allowed_features_json"] or "[]"))
                if isinstance(parsed_features, list):
                    allowed_features = [str(item).strip() for item in parsed_features if str(item).strip()]
            except Exception:
                allowed_features = []
            request.environ["hackme_web.session_meta"] = {
                "auth_scope": str(row["auth_scope"] or "").strip(),
                "allowed_features": allowed_features,
            }
        return username
    finally:
        auth_conn.close()


def get_current_user_ctx():
    tok = request.cookies.get("session_token")
    username = db_get_user_from_token(tok) if tok else None
    if not username:
        lookup = _STATE.get("tester_token_user_lookup")
        if callable(lookup):
            try:
                username = lookup(request)
            except Exception:
                username = None
    if not username:
        return None
    user = _STATE["get_user_by_username"](username)
    if user and has_request_context():
        meta = request.environ.get("hackme_web.session_meta")
        if isinstance(meta, dict):
            try:
                user_payload = dict(user)
                user_payload["_auth_scope"] = str(meta.get("auth_scope") or "")
                user_payload["_allowed_features"] = list(meta.get("allowed_features") or [])
                user_payload["auth_scope"] = user_payload["_auth_scope"]
                user_payload["allowed_features"] = user_payload["_allowed_features"]
                return user_payload
            except Exception:
                return user
    return user
