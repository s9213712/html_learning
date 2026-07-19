"""Durable, exact cleanup for per-run ComfyUI input media.

Template media is uploaded below ``ComfyUI/input/<run_id>/``.  A cleanup is
only successful when every recorded input reference is proven absent (or when
no upload was ever attempted).  The registry is mirrored to a small SQLite
database under the isolated runtime root so a new server process can reap
entries whose owning process died before reaching its terminal cleanup.
"""

from __future__ import annotations

import fcntl
import inspect
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from services.comfyui.files import normalize_file_ref, safe_local_image_path
from services.comfyui.template.safety import _safe_run_id
from services.server.runtime import default_runtime_root_path


COMFYUI_RUN_TTL_SECONDS = 24 * 60 * 60
_REGISTRY_SCHEMA_VERSION = 1


@dataclass
class _RunDirEntry:
    run_id: str
    created_at: float
    user_id: int
    purged: bool = False
    backend_url: str = ""
    input_refs: list[dict[str, str]] = field(default_factory=list)
    upload_attempted: bool = False
    owner_pid: int = 0
    owner_start_ticks: str = ""
    owner_boot_id: str = ""


_registry: dict[str, _RunDirEntry] = {}
_registry_lock = threading.RLock()


def _runtime_root() -> Path:
    configured = str(os.environ.get("HACKME_RUNTIME_DIR") or "").strip()
    return Path(configured).expanduser().resolve() if configured else default_runtime_root_path()


def _registry_db_path() -> Path:
    override = str(os.environ.get("HACKME_COMFYUI_RUN_REGISTRY_DB") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _runtime_root() / "state" / "comfyui_run_temp_registry.sqlite3"


def _open_registry_db() -> sqlite3.Connection:
    path = _registry_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    current_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0] or "").lower()
    if current_mode != "wal":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS comfyui_run_temp_registry (
            run_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            created_at REAL NOT NULL,
            user_id INTEGER NOT NULL,
            purged INTEGER NOT NULL DEFAULT 0,
            backend_url TEXT NOT NULL DEFAULT '',
            input_refs_json TEXT NOT NULL DEFAULT '[]',
            upload_attempted INTEGER NOT NULL DEFAULT 0,
            owner_pid INTEGER NOT NULL DEFAULT 0,
            owner_start_ticks TEXT NOT NULL DEFAULT '',
            owner_boot_id TEXT NOT NULL DEFAULT '',
            cleanup_receipt_json TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _process_start_ticks(pid: int) -> str:
    try:
        # comm may contain spaces and parentheses; split only after its final ')'.
        tail = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1]
        fields_after_comm = tail.strip().split()
        return fields_after_comm[19]  # proc stat field 22
    except (OSError, ValueError, IndexError):
        return ""


def _owner_identity(pid: int | None = None) -> tuple[int, str, str]:
    value = int(pid or os.getpid())
    return value, _process_start_ticks(value), _boot_id()


def _owner_is_alive(entry: _RunDirEntry) -> bool:
    if entry.owner_pid <= 0:
        return False
    current_boot_id = _boot_id()
    if not entry.owner_boot_id or not current_boot_id or entry.owner_boot_id != current_boot_id:
        return False
    if not entry.owner_start_ticks:
        return False
    current_start = _process_start_ticks(entry.owner_pid)
    return bool(current_start) and current_start == entry.owner_start_ticks


def _entry_from_row(row: sqlite3.Row) -> _RunDirEntry:
    try:
        refs = json.loads(row["input_refs_json"] or "[]")
    except (TypeError, ValueError):
        refs = []
    refs = [dict(item) for item in refs if isinstance(item, Mapping)]
    return _RunDirEntry(
        run_id=str(row["run_id"]),
        created_at=float(row["created_at"]),
        user_id=int(row["user_id"]),
        purged=bool(row["purged"]),
        backend_url=str(row["backend_url"] or ""),
        input_refs=refs,
        upload_attempted=bool(row["upload_attempted"]),
        owner_pid=int(row["owner_pid"] or 0),
        owner_start_ticks=str(row["owner_start_ticks"] or ""),
        owner_boot_id=str(row["owner_boot_id"] or ""),
    )


def _persist_entry(entry: _RunDirEntry) -> None:
    with _open_registry_db() as conn:
        conn.execute(
            """
            INSERT INTO comfyui_run_temp_registry (
                run_id, schema_version, created_at, user_id, purged,
                backend_url, input_refs_json, upload_attempted, owner_pid,
                owner_start_ticks, owner_boot_id, cleanup_receipt_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
            ON CONFLICT(run_id) DO UPDATE SET
                user_id=excluded.user_id,
                purged=excluded.purged,
                backend_url=CASE WHEN excluded.backend_url != '' THEN excluded.backend_url ELSE comfyui_run_temp_registry.backend_url END,
                input_refs_json=excluded.input_refs_json,
                upload_attempted=excluded.upload_attempted,
                owner_pid=excluded.owner_pid,
                owner_start_ticks=excluded.owner_start_ticks,
                owner_boot_id=excluded.owner_boot_id,
                updated_at=excluded.updated_at
            """,
            (
                entry.run_id,
                _REGISTRY_SCHEMA_VERSION,
                float(entry.created_at),
                int(entry.user_id),
                int(bool(entry.purged)),
                entry.backend_url,
                json.dumps(entry.input_refs, ensure_ascii=False, sort_keys=True),
                int(bool(entry.upload_attempted)),
                int(entry.owner_pid),
                entry.owner_start_ticks,
                entry.owner_boot_id,
                time.time(),
            ),
        )
        conn.commit()


def _durable_entry(run_id: str) -> _RunDirEntry | None:
    try:
        with _open_registry_db() as conn:
            row = conn.execute(
                "SELECT * FROM comfyui_run_temp_registry WHERE run_id=?",
                (_safe_run_id(run_id),),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    return _entry_from_row(row) if row is not None else None


def register_run_dir(
    *,
    run_id: str,
    user_id: int,
    backend_url: str = "",
    clock: Callable[[], float] = time.time,
    owner_pid: int | None = None,
) -> None:
    """Register one isolated run directory in memory and durable storage."""
    safe = _safe_run_id(run_id)
    pid, start_ticks, boot_id = _owner_identity(owner_pid)
    with _registry_lock:
        entry = _registry.get(safe)
        if entry is None:
            entry = _RunDirEntry(
                run_id=safe,
                created_at=float(clock()),
                user_id=int(user_id),
                backend_url=str(backend_url or "").strip(),
                owner_pid=pid,
                owner_start_ticks=start_ticks,
                owner_boot_id=boot_id,
            )
            _registry[safe] = entry
        elif backend_url and not entry.backend_url:
            entry.backend_url = str(backend_url).strip()
        _persist_entry(entry)


def record_run_input_ref(
    *,
    run_id: str,
    user_id: int,
    input_ref: Mapping[str, Any],
    backend_url: str = "",
) -> dict[str, str]:
    """Record a planned or returned input ref before continuing the upload path."""
    safe = _safe_run_id(run_id)
    normalized = normalize_file_ref(
        dict(input_ref or {}),
        error_cls=ValueError,
        default_type="input",
        empty_label="ComfyUI input",
    )
    if normalized["type"] != "input" or normalized.get("subfolder") != safe:
        raise ValueError("ComfyUI 暫存媒體引用必須精確位於 input/<run_id>/")
    register_run_dir(run_id=safe, user_id=user_id, backend_url=backend_url)
    with _registry_lock:
        entry = _registry[safe]
        entry.upload_attempted = True
        if normalized not in entry.input_refs:
            entry.input_refs.append(normalized)
        _persist_entry(entry)
    return normalized


def reset_registry() -> None:
    """Test/admin helper: clear both the process cache and durable registry."""
    with _registry_lock:
        _registry.clear()
        try:
            with _open_registry_db() as conn:
                conn.execute("DELETE FROM comfyui_run_temp_registry")
                conn.commit()
        except (OSError, sqlite3.Error):
            pass


def registry_size() -> int:
    with _registry_lock:
        return len(_registry)


def list_active_run_dirs() -> list[_RunDirEntry]:
    with _registry_lock:
        return [entry for entry in _registry.values() if not entry.purged]


def _callback_kwargs(callback: Callable[..., Any], values: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return {"run_id": values["run_id"], "user_id": values["user_id"]}
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return values
    return {key: value for key, value in values.items() if key in signature.parameters}


def cleanup_run_temp_files(
    *,
    run_id: str,
    user_id: int,
    cleanup_callback: Callable[..., Any],
    audit: Callable[..., None] | None = None,
    audit_user: str | None = None,
    audit_ip: str = "",
    audit_ua: str = "",
    reason: str = "gate5_failure",
    return_receipt: bool = False,
) -> bool | dict[str, Any]:
    """Delete one run and emit a machine-readable absence receipt.

    Mapping callbacks must explicitly return ``absence_verified=true``.  The
    legacy boolean callback remains supported for existing callers/tests.
    """
    safe = _safe_run_id(run_id)
    with _registry_lock:
        entry = _registry.get(safe) or _durable_entry(safe)
    values = {
        "run_id": safe,
        "user_id": int(user_id),
        "backend_url": entry.backend_url if entry else "",
        "input_refs": list(entry.input_refs) if entry else [],
        "upload_attempted": bool(entry.upload_attempted) if entry else False,
    }
    callback_result: Any = False
    detail = ""
    try:
        callback_result = cleanup_callback(**_callback_kwargs(cleanup_callback, values))
        if isinstance(callback_result, Mapping):
            success = bool(callback_result.get("ok")) and bool(callback_result.get("absence_verified"))
            detail = str(callback_result.get("detail") or ("ok" if success else "absence_not_verified"))
        else:
            success = bool(callback_result)
            detail = "ok" if success else "callback_returned_false"
    except Exception as exc:  # cleanup must produce a receipt, never mask the root failure
        success = False
        detail = f"callback_raised: {type(exc).__name__}: {exc}"

    receipt = {
        "schema_version": 1,
        "run_id": safe,
        "user_id": int(user_id),
        "backend_url": values["backend_url"],
        "reason": str(reason or ""),
        "input_ref_count": len(values["input_refs"]),
        "upload_attempted": values["upload_attempted"],
        "ok": bool(success),
        "absence_verified": bool(success),
        "detail": detail,
        "completed_at": time.time(),
    }
    if isinstance(callback_result, Mapping):
        receipt["cleanup"] = dict(callback_result)

    # Persist both success and failure receipts.  A failed row remains active
    # so bounded startup retry (or a later restart) has exact evidence and can
    # retry it.  Receipt persistence itself is part of a successful cleanup.
    with _registry_lock:
        try:
            with _open_registry_db() as conn:
                cursor = conn.execute(
                    "UPDATE comfyui_run_temp_registry SET purged=?, cleanup_receipt_json=?, updated_at=? WHERE run_id=?",
                    (
                        int(bool(success)),
                        json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                        time.time(),
                        safe,
                    ),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("cleanup registry row missing")
                conn.commit()
            if entry is not None:
                entry.purged = bool(success)
        except Exception as exc:
            success = False
            receipt["ok"] = False
            receipt["absence_verified"] = False
            receipt["detail"] = f"registry_receipt_persist_failed:{type(exc).__name__}"
            if entry is not None:
                entry.purged = False

    if audit is not None:
        try:
            audit(
                "COMFYUI_TEMPLATE_RUN_INPUT_CLEANUP",
                audit_ip,
                user=audit_user or "-",
                success=bool(receipt["ok"]),
                ua=audit_ua,
                detail=json.dumps(receipt, ensure_ascii=False, sort_keys=True)[:1800],
            )
        except Exception:
            pass
    return receipt if return_receipt else bool(receipt["ok"])


def _listening_socket_inodes(proc_root: Path, port: int) -> set[str]:
    inodes: set[str] = set()
    for relative in ("net/tcp", "net/tcp6"):
        try:
            lines = (proc_root / relative).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":  # TCP_LISTEN
                continue
            try:
                local_port = int(fields[1].rsplit(":", 1)[1], 16)
            except (ValueError, IndexError):
                continue
            if local_port == port and fields[9].isdigit():
                inodes.add(fields[9])
    return inodes


def _local_backend_binding_proof(
    backend_url: str,
    local_base_dir: str | os.PathLike[str] | None,
    *,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    """Prove that a loopback backend listener belongs to the configured tree.

    A path existing locally is not proof that it backs a URL.  Bind the URL's
    TCP LISTEN inode to an owning PID via ``/proc/<pid>/fd`` and require that
    listener process's cwd to equal the configured ComfyUI project directory.
    """
    proof: dict[str, Any] = {
        "binding_verified": False,
        "backend_url": str(backend_url or ""),
        "backend_host": "",
        "backend_port": None,
        "project_dir": "",
        "listener_inodes": [],
        "listeners": [],
        "listener_pid": None,
        "listener_inode": "",
        "listener_cwd": "",
        "detail": "binding_unavailable",
    }
    if not local_base_dir:
        proof["detail"] = "project_dir_not_configured"
        return proof
    try:
        parsed = urlparse(str(backend_url or ""))
        host = (parsed.hostname or "").lower()
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
        raw_project_dir = Path(local_base_dir).expanduser()
        if raw_project_dir.is_symlink():
            proof["detail"] = "project_dir_symlink_rejected"
            return proof
        project_dir = raw_project_dir.resolve(strict=True)
    except (OSError, TypeError, ValueError):
        proof["detail"] = "binding_input_invalid"
        return proof
    proof.update({
        "backend_host": host,
        "backend_port": port,
        "project_dir": str(project_dir),
    })
    if parsed.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost", "::1"}:
        proof["detail"] = "backend_not_loopback_http"
        return proof
    inodes = _listening_socket_inodes(proc_root, port)
    proof["listener_inodes"] = sorted(inodes)
    if not inodes:
        proof["detail"] = "listener_inode_not_found"
        return proof

    listeners: list[dict[str, Any]] = []
    try:
        process_dirs = sorted(
            (item for item in proc_root.iterdir() if item.name.isdigit()),
            key=lambda item: int(item.name),
        )
    except OSError:
        proof["detail"] = "proc_process_scan_failed"
        return proof
    for process_dir in process_dirs:
        try:
            cwd = (process_dir / "cwd").resolve(strict=True)
            fd_entries = list((process_dir / "fd").iterdir())
        except OSError:
            continue
        for fd_entry in fd_entries:
            try:
                target = os.readlink(fd_entry)
            except OSError:
                continue
            if not target.startswith("socket:[") or not target.endswith("]"):
                continue
            inode = target[8:-1]
            if inode not in inodes:
                continue
            listener = {
                "pid": int(process_dir.name),
                "inode": inode,
                "cwd": str(cwd),
                "cwd_matches_project": cwd == project_dir,
            }
            listeners.append(listener)
            if cwd == project_dir and not proof["binding_verified"]:
                proof.update({
                    "binding_verified": True,
                    "listener_pid": listener["pid"],
                    "listener_inode": inode,
                    "listener_cwd": str(cwd),
                    "detail": "listener_owner_cwd_matches_project",
                })
    proof["listeners"] = listeners
    if not listeners:
        proof["detail"] = "listener_owner_not_visible"
    elif not proof["binding_verified"]:
        proof["detail"] = "listener_owner_cwd_mismatch"
    return proof


def _remote_ref_absent(client: Any, ref: Mapping[str, str]) -> tuple[bool, str]:
    query = urllib.parse.urlencode({
        "filename": ref["filename"],
        "subfolder": ref.get("subfolder") or "",
        "type": ref.get("type") or "output",
    })
    request = urllib.request.Request(client._url(f"/view?{query}"), headers={"Accept": "*/*"})
    timeout = max(1.0, min(float(getattr(client, "timeout", 10) or 10), 10.0))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
        return False, "still_present"
    except urllib.error.HTTPError as exc:
        return (True, "http_404") if exc.code == 404 else (False, f"verify_http_{exc.code}")
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"verify_unavailable:{type(exc).__name__}"


def local_backend_binding_proof(
    backend_url: str,
    local_base_dir: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """Public reusable wrapper for exact URL-listener-project binding proof."""
    return _local_backend_binding_proof(backend_url, local_base_dir)


def remote_ref_absent(client: Any, ref: Mapping[str, str]) -> tuple[bool, str]:
    """Public post-delete GET verifier; only HTTP 404 proves absence."""
    return _remote_ref_absent(client, ref)


def discard_comfyui_ref_exact(
    *,
    client: Any,
    file_ref: Mapping[str, Any],
    backend_url: str,
    local_base_dir: str | os.PathLike[str] | None,
    prompt_id: str | None = None,
) -> dict[str, Any]:
    """Discard one owned ComfyUI ref and prove post-delete absence."""
    ref = normalize_file_ref(dict(file_ref or {}), error_cls=ValueError)
    binding = local_backend_binding_proof(backend_url, local_base_dir)
    use_local = bool(binding.get("binding_verified"))
    result: dict[str, Any] = {
        "file_deleted": False,
        "file_missing": False,
        "file_delete_supported": False,
        "history_deleted": False,
        "absence_verified": False,
        "verification": "",
        "remote_preview_only": False,
        "local_binding": binding,
    }
    if use_local:
        raw_base = Path(local_base_dir).expanduser()
        if raw_base.is_symlink():
            result["verification"] = "unsafe_symlink_project_dir"
            return result
        base = raw_base.resolve(strict=True)
        type_root = base / ref["type"]
        if type_root.is_symlink():
            result["verification"] = "unsafe_symlink_type_root"
            return result
        raw_target = type_root
        for part in [part for part in (ref.get("subfolder") or "").split("/") if part]:
            raw_target = raw_target / part
            if raw_target.is_symlink():
                result["verification"] = "unsafe_symlink_directory"
                return result
        raw_target = raw_target / ref["filename"]
        if raw_target.is_symlink():
            result["verification"] = "unsafe_symlink_target"
            return result
        target = safe_local_image_path(ref, error_cls=ValueError, local_base_dir=base)
        deletion = client.discard_image(
            ref,
            prompt_id=prompt_id,
            local_base_dir=str(base),
            allow_api_delete=False,
        )
        result.update(dict(deletion or {}))
        result["absence_verified"] = bool(target is not None and not target.exists())
        result["verification"] = "local_lstat_absent" if result["absence_verified"] else "local_target_still_present"
    else:
        deletion = client.discard_image(
            ref,
            prompt_id=prompt_id,
            local_base_dir=None,
            allow_api_delete=True,
        )
        result.update(dict(deletion or {}))
        absent, verification = remote_ref_absent(client, ref)
        result["absence_verified"] = bool(absent)
        result["verification"] = verification
    if result["absence_verified"] and not (result.get("file_deleted") or result.get("file_missing")):
        # A post-delete 404/lstat is stronger than the transport's often
        # incomplete delete response; expose it as an exact missing receipt.
        result["file_missing"] = True
    return result


def purge_comfyui_run_input(
    *,
    run_id: str,
    user_id: int,
    backend_url: str,
    input_refs: list[Mapping[str, Any]],
    upload_attempted: bool,
    client: Any,
    local_base_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Delete only recorded ``input/<run_id>`` refs and verify absence."""
    safe = _safe_run_id(run_id)
    refs: list[dict[str, str]] = []
    for raw_ref in input_refs or []:
        try:
            ref = normalize_file_ref(dict(raw_ref), error_cls=ValueError, default_type="input")
        except ValueError as exc:
            return {"ok": False, "absence_verified": False, "detail": f"invalid_ref:{exc}"}
        if ref["type"] != "input" or ref.get("subfolder") != safe:
            return {"ok": False, "absence_verified": False, "detail": "ref_outside_run_scope"}
        if ref not in refs:
            refs.append(ref)

    if not refs:
        untouched = not bool(upload_attempted)
        return {
            "ok": untouched,
            "absence_verified": untouched,
            "detail": "no_upload_attempt" if untouched else "upload_attempt_without_exact_ref",
            "method": "execution_receipt",
            "refs": [],
        }

    binding_proof = _local_backend_binding_proof(backend_url, local_base_dir)
    use_local = bool(binding_proof.get("binding_verified"))
    raw_run_dir = None
    if use_local:
        raw_base = Path(local_base_dir).expanduser()
        if raw_base.is_symlink():
            return {
                "ok": False,
                "absence_verified": False,
                "detail": "unsafe_symlink_project_directory",
                "method": "local_filesystem",
                "local_binding": binding_proof,
                "refs": [],
            }
        input_root = raw_base.resolve() / "input"
        if input_root.is_symlink():
            return {
                "ok": False,
                "absence_verified": False,
                "detail": "unsafe_symlink_input_root",
                "method": "local_filesystem",
                "local_binding": binding_proof,
                "refs": [],
            }
        raw_run_dir = input_root / safe
        if raw_run_dir.is_symlink():
            return {
                "ok": False,
                "absence_verified": False,
                "detail": "unsafe_symlink_run_directory",
                "method": "local_filesystem",
                "local_binding": binding_proof,
                "refs": [],
            }
    results: list[dict[str, Any]] = []
    for ref in refs:
        item = {"ref": ref, "deleted": False, "absent": False, "verification": ""}
        try:
            if use_local:
                raw_target = raw_run_dir / ref["filename"]
                if raw_target.is_symlink():
                    raise ValueError("unsafe_symlink_input_target")
                target = safe_local_image_path(ref, error_cls=ValueError, local_base_dir=local_base_dir)
                if target is None:
                    raise ValueError("local_input_path_unavailable")
                if target.exists():
                    if not target.is_file() or target.is_symlink():
                        raise ValueError("unsafe_local_input_target")
                    target.unlink()
                    item["deleted"] = True
                item["absent"] = not target.exists()
                item["verification"] = "local_lstat"
            else:
                deletion = client.discard_image(
                    ref,
                    local_base_dir=None,
                    allow_api_delete=True,
                )
                item["deleted"] = bool((deletion or {}).get("file_deleted"))
                item["delete_result"] = deletion
                item["absent"], item["verification"] = _remote_ref_absent(client, ref)
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        results.append(item)

    directory_absent = True
    if use_local:
        run_dir = raw_run_dir.resolve()
        input_root = (Path(local_base_dir).expanduser().resolve() / "input").resolve()
        try:
            run_dir.relative_to(input_root)
            if run_dir.exists() and run_dir.is_dir() and not any(run_dir.iterdir()):
                run_dir.rmdir()
            directory_absent = not run_dir.exists()
        except (OSError, ValueError):
            directory_absent = False

    exact = all(bool(item.get("absent")) for item in results) and directory_absent
    return {
        "ok": exact,
        "absence_verified": exact,
        "detail": "all_recorded_inputs_absent" if exact else "input_absence_not_verified",
        "method": "local_filesystem" if use_local else "remote_delete_and_get",
        "binding_verified": bool(binding_proof.get("binding_verified")),
        "listener_pid": binding_proof.get("listener_pid"),
        "listener_inode": binding_proof.get("listener_inode") or "",
        "listener_cwd": binding_proof.get("listener_cwd") or "",
        "local_binding": binding_proof,
        "directory_absent": directory_absent if use_local else None,
        "refs": results,
    }


def sweep_orphaned_run_dirs(
    *,
    cleanup_callback: Callable[..., Any],
    audit: Callable[..., None] | None = None,
    ttl_seconds: float = COMFYUI_RUN_TTL_SECONDS,
    clock: Callable[[], float] = time.time,
    audit_user: str = "-",
) -> dict[str, Any]:
    now = float(clock())
    with _registry_lock:
        targets = [
            entry for entry in _registry.values()
            if not entry.purged and (now - entry.created_at) >= ttl_seconds
        ]
    reaped = 0
    failed = 0
    for entry in targets:
        receipt = cleanup_run_temp_files(
            run_id=entry.run_id,
            user_id=entry.user_id,
            cleanup_callback=cleanup_callback,
            audit=audit,
            audit_user=audit_user,
            reason="sweeper_24h",
            return_receipt=True,
        )
        if receipt["ok"]:
            reaped += 1
        else:
            failed += 1
    return {
        "ttl_seconds": ttl_seconds,
        "candidates": len(targets),
        "reaped": reaped,
        "failed": failed,
        "remaining": len([entry for entry in list_active_run_dirs() if not entry.purged]),
    }


def sweep_restart_orphaned_run_dirs(
    *,
    cleanup_callback: Callable[..., Any],
    audit: Callable[..., None] | None = None,
    owner_alive: Callable[[_RunDirEntry], bool] = _owner_is_alive,
    ttl_seconds: float | None = COMFYUI_RUN_TTL_SECONDS,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Reap durable dead-owner entries and optional 24h TTL expirations."""
    try:
        with _open_registry_db() as conn:
            rows = conn.execute(
                "SELECT * FROM comfyui_run_temp_registry WHERE purged=0 ORDER BY created_at"
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return {
            "candidates": 0,
            "reaped": 0,
            "failed": 1,
            "registry_read_failed": True,
            "error": f"{type(exc).__name__}: {exc}",
            "receipts": [],
        }
    now = float(clock())
    targets: list[tuple[_RunDirEntry, str]] = []
    dead_owner_candidates = 0
    ttl_candidates = 0
    for entry in map(_entry_from_row, rows):
        try:
            alive = bool(owner_alive(entry))
        except Exception:
            alive = False
        expired = ttl_seconds is not None and (now - entry.created_at) >= float(ttl_seconds)
        if not alive:
            dead_owner_candidates += 1
            targets.append((entry, "restart_orphan_sweeper"))
        elif expired:
            ttl_candidates += 1
            targets.append((entry, "durable_ttl_sweeper"))
    reaped = 0
    failed = 0
    receipts = []
    for entry, reason in targets:
        receipt = cleanup_run_temp_files(
            run_id=entry.run_id,
            user_id=entry.user_id,
            cleanup_callback=cleanup_callback,
            audit=audit,
            audit_user="system",
            reason=reason,
            return_receipt=True,
        )
        receipts.append(receipt)
        if receipt["ok"]:
            reaped += 1
        else:
            failed += 1
    return {
        "candidates": len(targets),
        "reaped": reaped,
        "failed": failed,
        "dead_owner_candidates": dead_owner_candidates,
        "ttl_candidates": ttl_candidates,
        "ttl_seconds": ttl_seconds,
        "receipts": receipts,
    }


def get_run_cleanup_receipt(run_id: str) -> dict[str, Any] | None:
    """Return the last durable cleanup attempt for operator/machine audit."""
    try:
        with _open_registry_db() as conn:
            row = conn.execute(
                "SELECT cleanup_receipt_json FROM comfyui_run_temp_registry WHERE run_id=?",
                (_safe_run_id(run_id),),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["cleanup_receipt_json"] or "{}")
        return dict(payload) if isinstance(payload, Mapping) else None
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def sweep_restart_orphaned_run_dirs_with_retry(
    *,
    cleanup_callback: Callable[..., Any],
    audit: Callable[..., None] | None = None,
    owner_alive: Callable[[_RunDirEntry], bool] = _owner_is_alive,
    ttl_seconds: float | None = COMFYUI_RUN_TTL_SECONDS,
    max_attempts: int = 4,
    initial_backoff_seconds: float = 2.0,
    max_backoff_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Boundedly retry startup cleanup when the backend is not ready yet."""
    attempts = max(1, min(int(max_attempts), 10))
    delay = max(0.0, float(initial_backoff_seconds))
    delay_cap = max(delay, float(max_backoff_seconds))
    attempt_summaries: list[dict[str, Any]] = []
    for attempt_index in range(1, attempts + 1):
        summary = sweep_restart_orphaned_run_dirs(
            cleanup_callback=cleanup_callback,
            audit=audit,
            owner_alive=owner_alive,
            ttl_seconds=ttl_seconds,
        )
        attempt_summaries.append({"attempt": attempt_index, **summary})
        if int(summary.get("failed") or 0) == 0:
            break
        if attempt_index < attempts and delay > 0:
            sleep(delay)
            delay = min(delay_cap, max(delay * 3.0, delay + 1.0))

    final_summary = attempt_summaries[-1]
    result = {
        "ok": int(final_summary.get("failed") or 0) == 0,
        "attempts": len(attempt_summaries),
        "max_attempts": attempts,
        "final": final_summary,
        "attempt_summaries": attempt_summaries,
    }
    if audit is not None:
        try:
            audit(
                "COMFYUI_TEMPLATE_ORPHAN_SWEEP_COMPLETE",
                "-",
                user="system",
                success=bool(result["ok"]),
                ua="startup-sweeper",
                detail=json.dumps(result, ensure_ascii=False, sort_keys=True)[:4000],
            )
        except Exception:
            pass
    return result


def run_cleanup_maintenance_daemon(
    *,
    cleanup_callback: Callable[..., Any],
    audit: Callable[..., None] | None = None,
    interval_seconds: float | None = None,
    max_cycles: int | None = None,
    lock_path: str | os.PathLike[str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Single-leader startup retry plus periodic durable TTL maintenance.

    ``flock`` prevents every gunicorn worker from running the reaper.  The
    leader holds the lock for its thread lifetime; if that worker exits, its
    replacement can acquire the lock during route registration.
    """
    if interval_seconds is None:
        try:
            configured_interval = float(
                os.environ.get("HACKME_COMFYUI_CLEANUP_INTERVAL_SECONDS", "900") or 900
            )
        except (TypeError, ValueError):
            configured_interval = 900.0
        interval = max(60.0, min(configured_interval, 3600.0))
    else:
        interval = max(0.0, float(interval_seconds))
    cycle_limit = None if max_cycles is None else max(1, int(max_cycles))
    path = Path(lock_path) if lock_path else (_runtime_root() / "state" / "comfyui_input_cleanup.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"leader": False, "cycles": 0, "reason": "maintenance_leader_exists"}
        cycles: list[dict[str, Any]] = []
        while cycle_limit is None or len(cycles) < cycle_limit:
            try:
                cycle = sweep_restart_orphaned_run_dirs_with_retry(
                    cleanup_callback=cleanup_callback,
                    audit=audit,
                    ttl_seconds=COMFYUI_RUN_TTL_SECONDS,
                )
            except Exception as exc:
                cycle = {
                    "ok": False,
                    "attempts": 0,
                    "error": f"maintenance_cycle_failed:{type(exc).__name__}:{exc}",
                }
            cycles.append(cycle)
            if cycle_limit is not None and len(cycles) >= cycle_limit:
                break
            sleep(interval)
        return {
            "leader": True,
            "cycles": len(cycles),
            "interval_seconds": interval,
            "cycle_results": cycles,
        }
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


__all__ = [
    "COMFYUI_RUN_TTL_SECONDS",
    "cleanup_run_temp_files",
    "discard_comfyui_ref_exact",
    "get_run_cleanup_receipt",
    "list_active_run_dirs",
    "local_backend_binding_proof",
    "purge_comfyui_run_input",
    "record_run_input_ref",
    "remote_ref_absent",
    "run_cleanup_maintenance_daemon",
    "register_run_dir",
    "registry_size",
    "reset_registry",
    "sweep_orphaned_run_dirs",
    "sweep_restart_orphaned_run_dirs",
    "sweep_restart_orphaned_run_dirs_with_retry",
]
