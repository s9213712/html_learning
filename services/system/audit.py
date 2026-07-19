import fcntl
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime

_STATE = {
    "get_db": None,
    "chain_seed": None,
    "integrity_key": None,
    "audit_log_path": None,
    "audit_anchor_path": None,
    "audit_anchor_latest_path": None,
    "audit_anchor_interval_seconds": 60,
}

_audit_lock = threading.Lock()
_audit_db_lock = threading.Lock()
_anchor_lock = threading.Lock()
_last_audit_anchor_at = 0.0
_AUDIT_VERIFY_STABLE_ATTEMPTS = 8
_AUDIT_VERIFY_RETRY_SECONDS = 0.04


def configure_audit_service(
    *,
    get_db,
    chain_seed,
    integrity_key,
    audit_log_path,
    audit_anchor_path,
    audit_anchor_latest_path,
    audit_anchor_interval_seconds,
):
    _STATE.update({
        "get_db": get_db,
        "chain_seed": chain_seed,
        "integrity_key": integrity_key,
        "audit_log_path": audit_log_path,
        "audit_anchor_path": audit_anchor_path,
        "audit_anchor_latest_path": audit_anchor_latest_path,
        "audit_anchor_interval_seconds": audit_anchor_interval_seconds,
    })


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@contextmanager
def _audit_mutation_guard():
    """Serialize audit DB/file mutations across threads and worker processes."""

    log_path = _STATE["audit_log_path"]
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    # Keep the lock on a dedicated inode.  ``audit.log`` is truncated by a
    # legitimate runtime reset, so locking the evidence file itself would let
    # a new opener lock a different inode during replacement/truncation flows.
    lock_path = log_path + ".mutation.lock"
    with _audit_lock:
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield


def _entry_hash(entry_json):
    return hashlib.sha256(entry_json.encode("utf-8")).hexdigest()


def _chain_hash(prev_hash, entry_hash):
    material = f"{prev_hash}:{entry_hash}".encode("utf-8")
    return hmac.new(_STATE["integrity_key"], material, "sha256").hexdigest()


def _legacy_chain_hash(prev_hash, entry_json):
    return hmac.new(_STATE["integrity_key"], (prev_hash + entry_json).encode(), "sha256").hexdigest()


def _write_audit_anchor(audit_id, chain_hash, entry_hash, reason="interval"):
    payload = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "audit_id": int(audit_id),
        "entry_hash": entry_hash,
        "chain_hash": chain_hash,
        "reason": reason,
    }
    line = canonical_json(payload)
    anchor_path = _STATE["audit_anchor_path"]
    latest_path = _STATE["audit_anchor_latest_path"]
    anchor_dir = os.path.dirname(anchor_path)
    latest_dir = os.path.dirname(latest_path)
    if anchor_dir:
        os.makedirs(anchor_dir, exist_ok=True)
    if latest_dir:
        os.makedirs(latest_dir, exist_ok=True)
    with _anchor_lock:
        # ``_anchor_lock`` is process-local.  Serialize the append/latest pair
        # across server workers as well, and never let a delayed older writer
        # move the latest pointer backwards.
        with open(anchor_path, "a+", encoding="utf-8") as anchor_file:
            fcntl.flock(anchor_file.fileno(), fcntl.LOCK_EX)
            anchor_file.seek(0, os.SEEK_END)
            anchor_file.write(line + "\n")
            anchor_file.flush()

            current_audit_id = -1
            try:
                with open(latest_path, encoding="utf-8") as f:
                    current_audit_id = int(json.loads(f.read()).get("audit_id", -1))
            except FileNotFoundError:
                pass
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                # A new valid anchor replaces an unreadable latest pointer;
                # the append-only anchor history remains available.
                current_audit_id = -1

            if int(audit_id) >= current_audit_id:
                fd, tmp = tempfile.mkstemp(
                    prefix=os.path.basename(latest_path) + ".",
                    suffix=".tmp",
                    dir=latest_dir or ".",
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(line + "\n")
                    os.replace(tmp, latest_path)
                finally:
                    try:
                        os.unlink(tmp)
                    except FileNotFoundError:
                        pass


def _maybe_anchor_audit_head(audit_id, chain_hash, entry_hash, reason="interval"):
    global _last_audit_anchor_at
    now = time.time()
    if _last_audit_anchor_at and now - _last_audit_anchor_at < _STATE["audit_anchor_interval_seconds"]:
        return
    _write_audit_anchor(audit_id, chain_hash, entry_hash, reason)
    _last_audit_anchor_at = now


def reset_audit_chain_with_event(action, ip, user="-", success=True, ua="-", detail="-", write_event=True):
    """Clear the audit runtime chain and optionally write a fresh first event.

    Server reset keeps a pre-reset snapshot for recovery, but the live runtime
    audit chain should be able to start from a completely empty state after
    reset. Callers that need a genesis-equivalent event can keep write_event.
    """
    global _last_audit_anchor_at
    with _audit_mutation_guard():
        with _audit_db_lock:
            conn = _STATE["get_db"]()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM secure_audit")
                try:
                    conn.execute("DELETE FROM sqlite_sequence WHERE name='secure_audit'")
                except Exception:
                    pass
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        log_path = _STATE["audit_log_path"]
        log_dir = os.path.dirname(log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(log_path, "w", encoding="utf-8"):
            pass

        anchor_path = _STATE["audit_anchor_path"]
        latest_path = _STATE["audit_anchor_latest_path"]
        anchor_dir = os.path.dirname(anchor_path)
        if anchor_dir:
            os.makedirs(anchor_dir, exist_ok=True)
        with _anchor_lock:
            with open(anchor_path, "a+", encoding="utf-8") as anchor_file:
                fcntl.flock(anchor_file.fileno(), fcntl.LOCK_EX)
                anchor_file.seek(0)
                anchor_file.truncate()
                anchor_file.flush()
                try:
                    os.unlink(latest_path)
                except FileNotFoundError:
                    pass
        _last_audit_anchor_at = 0.0

        if write_event:
            _append_audit_under_mutation_lock(
                action,
                ip,
                user=user,
                success=success,
                ua=ua,
                detail=detail,
            )
            return {"ok": True, "reset": True, "event": action}
        return {"ok": True, "reset": True, "event": None}


def _read_latest_audit_anchor():
    latest_path = _STATE["audit_anchor_latest_path"]
    try:
        with open(latest_path, encoding="utf-8") as f:
            anchor = json.loads(f.read())
        audit_id = int(anchor.get("audit_id", 0))
        chain_hash = str(anchor.get("chain_hash") or "")
        if audit_id <= 0 or not chain_hash:
            raise ValueError("latest anchor is missing audit_id/chain_hash")
        return {
            "status": "ok",
            "audit_id": audit_id,
            "chain_hash": chain_hash,
            "entry_hash": str(anchor.get("entry_hash") or ""),
        }
    except FileNotFoundError:
        return {"status": "missing"}
    except Exception as exc:
        return {"status": "unreadable", "error": f"{type(exc).__name__}: {exc}"}


def _verify_latest_audit_anchor(rows_by_id, anchor):
    status = anchor.get("status")
    if status == "missing":
        return True, "no anchor yet"
    if status != "ok":
        return False, f"anchor unreadable: {anchor.get('error', 'unknown error')}"
    audit_id = anchor["audit_id"]
    row = rows_by_id.get(audit_id)
    if not row:
        return False, f"latest anchor points to missing audit id={audit_id}"
    if row["chain_hash"] != anchor["chain_hash"]:
        return False, f"latest anchor mismatch at audit id={audit_id}"
    return True, f"latest anchor OK at audit id={audit_id}"


def _append_audit_under_mutation_lock(action, ip, user="-", success=False, ua="-", detail="-"):
    """Append one DB/file/anchor record while the mutation guard is held."""

    ts = datetime.now().isoformat(timespec="milliseconds")
    entry = {
        "ts": ts,
        "action": action,
        "ip": ip,
        "user": user,
        "success": success,
        "ua": ua[:200],
        "detail": detail,
    }
    entry_json = canonical_json(entry)
    entry_hash = _entry_hash(entry_json)

    audit_id = None
    with _audit_db_lock:
        conn = _STATE["get_db"]()
        try:
            # The Python lock only serializes threads in this process.  The
            # production server has multiple worker processes, each with its
            # own lock, so reading the current head outside a SQLite write
            # transaction lets two workers derive entries from the same
            # ``prev_hash``.  Acquire SQLite's cross-connection writer lock
            # before reading the head and keep it through the insert/commit.
            conn.execute("BEGIN IMMEDIATE")
            prev_row = conn.execute(
                "SELECT chain_hash FROM secure_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = prev_row["chain_hash"] if prev_row else _STATE["chain_seed"]
            chain_hash = _chain_hash(prev_hash, entry_hash)
            try:
                cur = conn.execute(
                    "INSERT INTO secure_audit (ts, action, ip, user, success, ua, detail, prev_hash, entry_hash, chain_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ts, action, ip, user, 1 if success else 0, entry["ua"], detail, prev_hash, entry_hash, chain_hash)
                )
            except sqlite3.OperationalError as exc:
                # Retain compatibility with an old table that predates the
                # extended hash columns, but never reinterpret lock/I/O
                # failures as a legacy schema.
                error_text = str(exc).lower()
                if not any(
                    marker in error_text
                    for marker in (
                        "no column named prev_hash",
                        "no column named entry_hash",
                        "has no column named prev_hash",
                        "has no column named entry_hash",
                    )
                ):
                    raise
                chain_hash = _legacy_chain_hash(
                    prev_hash,
                    json.dumps(entry, ensure_ascii=False),
                )
                cur = conn.execute(
                    "INSERT INTO secure_audit (ts, action, ip, user, success, ua, detail, chain_hash) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (ts, action, ip, user, 1 if success else 0, entry["ua"], detail, chain_hash)
                )
            audit_id = cur.lastrowid
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    file_entry = dict(entry)
    file_entry["_audit_id"] = int(audit_id)
    file_entry["_prev_hash"] = prev_hash
    file_entry["_entry_hash"] = entry_hash
    file_entry["_chain_hash"] = chain_hash
    with open(_STATE["audit_log_path"], "a", encoding="utf-8") as f:
        f.write(canonical_json(file_entry) + "\n")
    if audit_id:
        _maybe_anchor_audit_head(audit_id, chain_hash, entry_hash)
    return audit_id


def audit(action, ip, user="-", success=False, ua="-", detail="-"):
    with _audit_mutation_guard():
        _append_audit_under_mutation_lock(
            action,
            ip,
            user=user,
            success=success,
            ua=ua,
            detail=detail,
        )


def _verify_audit_rows(rows, has_extended):
    prev_hash = _STATE["chain_seed"]
    for r in rows:
        base_entry = {
            "ts": r["ts"],
            "action": r["action"],
            "ip": r["ip"],
            "user": r["user"],
            "success": bool(r["success"]),
            "ua": r["ua"],
            "detail": r["detail"],
        }
        stored_entry_hash = r["entry_hash"] if has_extended else None
        stored_prev_hash = r["prev_hash"] if has_extended else None
        if stored_entry_hash:
            if stored_prev_hash != prev_hash:
                return False, r["id"], f"prev_hash mismatch at id={r['id']} (篡改或刪除偵測)"
            entry_json = canonical_json(base_entry)
            recomputed_entry_hash = _entry_hash(entry_json)
            if recomputed_entry_hash != stored_entry_hash:
                return False, r["id"], f"entry_hash mismatch at id={r['id']} (內容篡改偵測)"
            recomputed = _chain_hash(prev_hash, recomputed_entry_hash)
        else:
            legacy_json = json.dumps(base_entry, ensure_ascii=False)
            recomputed = _legacy_chain_hash(prev_hash, legacy_json)
        if recomputed != r["chain_hash"]:
            return False, r["id"], f"hash mismatch at id={r['id']} (篡改偵測)"
        prev_hash = r["chain_hash"]
    return True, None, None


def verify_audit_integrity(start_id=None, end_id=None):
    """Verify a stable DB/latest-anchor observation without blocking writers."""

    last_anchor_failure = (False, None, "audit snapshot did not stabilize")
    for attempt in range(_AUDIT_VERIFY_STABLE_ATTEMPTS):
        anchor_before = _read_latest_audit_anchor()
        conn = _STATE["get_db"]()
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(secure_audit)").fetchall()}
            has_extended = {"prev_hash", "entry_hash"}.issubset(cols)
            col_list = "id, ts, action, ip, user, success, ua, detail, chain_hash"
            if has_extended:
                col_list = "id, ts, action, ip, user, success, ua, detail, prev_hash, entry_hash, chain_hash"
            if start_id is None:
                rows = conn.execute(
                    f"SELECT {col_list} FROM secure_audit ORDER BY id ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {col_list} FROM secure_audit WHERE id>=? AND id<=? ORDER BY id ASC",
                    (start_id, end_id or start_id),
                ).fetchall()
        finally:
            conn.close()

        rows_ok, broken_at, row_details = _verify_audit_rows(rows, has_extended)
        if not rows_ok:
            return False, broken_at, row_details

        anchor_after = _read_latest_audit_anchor()
        if anchor_before != anchor_after:
            last_anchor_failure = (
                False,
                None,
                "latest anchor changed while the audit DB snapshot was read",
            )
        else:
            anchor_ok, anchor_details = _verify_latest_audit_anchor(
                {r["id"]: r for r in rows},
                anchor_after,
            )
            if anchor_ok:
                if not rows:
                    return True, None, "no entries; no latest anchor"
                return True, None, f"integrity OK ({len(rows)} entries verified); {anchor_details}"
            last_anchor_failure = (False, None, anchor_details)

        if attempt + 1 < _AUDIT_VERIFY_STABLE_ATTEMPTS:
            time.sleep(_AUDIT_VERIFY_RETRY_SECONDS)
    return last_anchor_failure


def _repair_audit_chain_under_mutation_lock(reason):
    with _audit_db_lock:
        conn = _STATE["get_db"]()
        try:
            # Resealing changes every downstream hash.  Hold SQLite's writer
            # lock before taking the source snapshot so another process
            # cannot append a tail derived from the pre-reseal head.
            conn.execute("BEGIN IMMEDIATE")
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(secure_audit)").fetchall()}
            has_extended = {"prev_hash", "entry_hash"}.issubset(cols)
            col_list = "id, ts, action, ip, user, success, ua, detail, chain_hash"
            if has_extended:
                col_list = "id, ts, action, ip, user, success, ua, detail, prev_hash, entry_hash, chain_hash"
            rows = conn.execute(f"SELECT {col_list} FROM secure_audit ORDER BY id ASC").fetchall()
            if not rows:
                conn.commit()
                return {"entries_resealed": 0, "head_id": None}

            prev_hash = _STATE["chain_seed"]
            head = None
            for r in rows:
                entry = {
                    "ts": r["ts"],
                    "action": r["action"],
                    "ip": r["ip"],
                    "user": r["user"],
                    "success": bool(r["success"]),
                    "ua": r["ua"],
                    "detail": r["detail"],
                }
                entry_hash = _entry_hash(canonical_json(entry))
                if has_extended:
                    chain_hash = _chain_hash(prev_hash, entry_hash)
                    conn.execute(
                        "UPDATE secure_audit SET prev_hash=?, entry_hash=?, chain_hash=? WHERE id=?",
                        (prev_hash, entry_hash, chain_hash, r["id"]),
                    )
                else:
                    chain_hash = _legacy_chain_hash(
                        prev_hash,
                        json.dumps(entry, ensure_ascii=False),
                    )
                    conn.execute(
                        "UPDATE secure_audit SET chain_hash=? WHERE id=?",
                        (chain_hash, r["id"]),
                    )
                prev_hash = chain_hash
                head = {"id": r["id"], "entry_hash": entry_hash, "chain_hash": chain_hash}
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    if head:
        _write_audit_anchor(head["id"], head["chain_hash"], head["entry_hash"], reason=reason)
    return {"entries_resealed": len(rows), "head_id": head["id"] if head else None}


def repair_audit_chain(reason="manual reseal"):
    """Recompute audit hash-chain metadata from the current stored entries."""

    with _audit_mutation_guard():
        return _repair_audit_chain_under_mutation_lock(reason)
