import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import request

from services.points_chain.security_context import points_safe_mode_security_context
from services.server.backpressure import backpressure_status
from services.server.domain_databases import DOMAIN_DATABASES
from services.system.gpu_probe import find_nvidia_smi
from services.system.ci_status import playwright_ci_status


_LAST_CPU_SAMPLE = None
_LAST_PROCESS_CPU_SAMPLE = None
_RESOURCE_USAGE_CACHE = {"expires_at": 0.0, "value": None}
_RESOURCE_USAGE_CACHE_LOCK = threading.Lock()


def _env_float(name, default, *, minimum=0.5, maximum=60.0):
    try:
        value = float(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        value = float(default)
    return max(float(minimum), min(float(maximum), value))


_RESOURCE_USAGE_CACHE_TTL_SECONDS = _env_float(
    "HTML_LEARNING_RESOURCE_USAGE_CACHE_TTL_SECONDS",
    5.0,
    minimum=1.0,
    maximum=30.0,
)


def _safe_percent(value):
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed < 0:
        return None
    return round(max(0.0, min(100.0, parsed)), 1)


def _safe_process_percent(value):
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed < 0:
        return None
    cores = os.cpu_count() or 1
    return round(max(0.0, min(float(cores) * 100.0, parsed)), 1)


def _read_proc_cpu_times():
    try:
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            parts = fh.readline().strip().split()
    except OSError:
        return None
    if not parts or parts[0] != "cpu":
        return None
    try:
        values = [int(part) for part in parts[1:]]
    except ValueError:
        return None
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total, idle


def _cpu_usage_snapshot():
    global _LAST_CPU_SAMPLE
    cores = os.cpu_count() or 1
    load_avg = None
    try:
        load_avg = list(os.getloadavg())
    except OSError:
        load_avg = None
    sample = _read_proc_cpu_times()
    percent = None
    now = time.monotonic()
    if sample:
        if _LAST_CPU_SAMPLE:
            prev_total, prev_idle, _ = _LAST_CPU_SAMPLE
            total_delta = max(0, sample[0] - prev_total)
            idle_delta = max(0, sample[1] - prev_idle)
            if total_delta > 0:
                percent = ((total_delta - idle_delta) / total_delta) * 100
        _LAST_CPU_SAMPLE = (sample[0], sample[1], now)
    if percent is None and load_avg:
        percent = (float(load_avg[0]) / max(1, cores)) * 100
    return {
        "label": "CPU",
        "percent": _safe_percent(percent),
        "cores": cores,
        "load_avg": load_avg,
    }


def _ram_usage_snapshot():
    meminfo = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                parts = value.strip().split()
                if not parts:
                    continue
                meminfo[key] = int(parts[0]) * 1024
    except (OSError, ValueError):
        meminfo = {}
    total = int(meminfo.get("MemTotal") or 0)
    available = int(meminfo.get("MemAvailable") or 0)
    used = max(0, total - available) if total else 0
    percent = (used / total) * 100 if total else None
    return {
        "label": "RAM",
        "percent": _safe_percent(percent),
        "used_bytes": used,
        "available_bytes": available or None,
        "total_bytes": total or None,
    }


def _read_process_cpu_seconds():
    try:
        raw = Path("/proc/self/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        rest = raw.rsplit(")", 1)[1].strip().split()
        utime_ticks = int(rest[11])
        stime_ticks = int(rest[12])
        ticks_per_second = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK")) or 100
        return (utime_ticks + stime_ticks) / float(ticks_per_second)
    except Exception:
        return None


def _read_process_memory_status():
    status = {}
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                parts = value.strip().split()
                if not parts:
                    continue
                if key in {"VmRSS", "VmSize", "VmHWM"}:
                    try:
                        status[key] = int(parts[0]) * 1024
                    except ValueError:
                        pass
                elif key == "Threads":
                    try:
                        status[key] = int(parts[0])
                    except ValueError:
                        pass
    except OSError:
        pass
    return status


def _process_usage_snapshot():
    global _LAST_PROCESS_CPU_SAMPLE
    pid = os.getpid()
    now = time.monotonic()
    cpu_seconds = _read_process_cpu_seconds()
    cpu_percent = None
    if cpu_seconds is not None:
        previous = _LAST_PROCESS_CPU_SAMPLE
        if previous and int(previous.get("pid") or 0) == pid:
            elapsed = max(0.0, now - float(previous.get("monotonic") or now))
            cpu_delta = max(0.0, cpu_seconds - float(previous.get("cpu_seconds") or cpu_seconds))
            if elapsed > 0:
                cpu_percent = (cpu_delta / elapsed) * 100.0
        _LAST_PROCESS_CPU_SAMPLE = {
            "pid": pid,
            "monotonic": now,
            "cpu_seconds": cpu_seconds,
        }
    memory = _read_process_memory_status()
    return {
        "label": "Process",
        "pid": pid,
        "cpu_percent": _safe_process_percent(cpu_percent),
        "rss_bytes": int(memory.get("VmRSS") or 0) or None,
        "vms_bytes": int(memory.get("VmSize") or 0) or None,
        "peak_rss_bytes": int(memory.get("VmHWM") or 0) or None,
        "threads": int(memory.get("Threads") or 0) or None,
    }


def _process_socket_count(pid):
    try:
        fd_dir = Path(f"/proc/{int(pid)}/fd")
        return sum(1 for item in fd_dir.iterdir() if os.readlink(item).startswith("socket:"))
    except Exception:
        return None


def _environment_identity_payload():
    return {
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "pid": os.getpid(),
    }


def _classify_related_process(comm, args, *, base="", current_pid=0, parent_pid=0, pid=0, ppid=0):
    comm_lower = str(comm or "").lower()
    args_text = str(args or "")
    args_lower = args_text.lower()
    base_lower = str(base or "").lower()
    if comm_lower in {"ffmpeg", "ffprobe"}:
        if any(token in args_lower for token in ("hls", "playlist.m3u8", ".m3u8", ".m4s", "media_derivatives", "hackme_stream")):
            return "HLS decode / ffmpeg"
        return "ffmpeg"
    if comm_lower == "aria2c" or "aria2c" in args_lower:
        return "aria2 download"
    if "hls_prepare_worker.py" in args_lower:
        return "HLS prepare worker"
    if "remote_download_worker.py" in args_lower:
        return "remote download worker"
    if "background_engine" in args_lower or "trading/background" in args_lower:
        return "trading engine worker"
    if "gunicorn" in args_lower and "server:app" in args_lower:
        return "web worker / trading engine"
    if "server.py" in args_lower and (not base_lower or base_lower in args_lower):
        return "main server / trading engine"
    if int(pid or 0) == int(current_pid or 0):
        return "current web worker"
    if int(pid or 0) == int(parent_pid or 0):
        return "gunicorn master"
    if int(ppid or 0) in {int(current_pid or 0), int(parent_pid or 0)}:
        return comm or "child process"
    if base_lower and base_lower in args_lower and comm_lower in {"python", "python3", "node", "yt-dlp"}:
        return comm or "project process"
    if "hackme_web" in args_lower and comm_lower in {"python", "python3", "node", "yt-dlp"}:
        return comm or "project process"
    return ""


def _related_processes_snapshot(base_dir=None, limit=32):
    cmd = ["ps", "-eo", "pid=,ppid=,comm=,pcpu=,rss=,args="]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1.5, check=False)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    base = str(base_dir or "").strip()
    current_pid = os.getpid()
    parent_pid = os.getppid()
    related = []
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            cpu = float(parts[3])
            rss_bytes = int(float(parts[4])) * 1024
        except Exception:
            continue
        comm = parts[2]
        args = parts[5]
        role = _classify_related_process(
            comm,
            args,
            base=base,
            current_pid=current_pid,
            parent_pid=parent_pid,
            pid=pid,
            ppid=ppid,
        )
        if not role:
            continue
        command = str(args or "").strip()
        command_limit = 4000
        related.append({
            "pid": pid,
            "ppid": ppid,
            "name": role,
            "process_name": comm,
            "cpu_percent": _safe_process_percent(cpu),
            "rss_bytes": rss_bytes,
            "socket_count": _process_socket_count(pid),
            "network": "",
            "network_available": False,
            "args": command[:command_limit],
            "command": command[:command_limit],
            "command_truncated": len(command) > command_limit,
        })
    related.sort(key=lambda item: (-(item.get("cpu_percent") or 0), -int(item.get("rss_bytes") or 0), int(item.get("pid") or 0)))
    return related[: max(1, min(100, int(limit or 32)))]


def _gpu_usage_snapshot():
    nvidia_smi = find_nvidia_smi()
    empty = {
        "gpu": {"label": "GPU", "available": False, "percent": None, "gpus": [], "error": ""},
        "vram": {"label": "VRAM", "available": False, "percent": None, "used_bytes": None, "total_bytes": None},
    }
    if not nvidia_smi:
        empty["gpu"]["error"] = "nvidia-smi unavailable"
        return empty
    cmd = [
        nvidia_smi,
        "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1.5, check=False)
    except Exception as exc:
        empty["gpu"]["error"] = exc.__class__.__name__
        return empty
    if proc.returncode != 0:
        empty["gpu"]["error"] = (proc.stderr or proc.stdout or "nvidia-smi failed").strip()[:200]
        return empty
    gpus = []
    total_used = 0
    total_vram = 0
    util_values = []
    for line in (proc.stdout or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            util = float(parts[2])
            used = int(float(parts[3])) * 1024 * 1024
            total = int(float(parts[4])) * 1024 * 1024
        except ValueError:
            continue
        util_values.append(util)
        total_used += max(0, used)
        total_vram += max(0, total)
        gpus.append({
            "index": parts[0],
            "name": parts[1],
            "percent": _safe_percent(util),
            "vram_used_bytes": used,
            "vram_total_bytes": total,
        })
    gpu_percent = sum(util_values) / len(util_values) if util_values else None
    vram_percent = (total_used / total_vram) * 100 if total_vram else None
    return {
        "gpu": {"label": "GPU", "available": bool(gpus), "percent": _safe_percent(gpu_percent), "gpus": gpus, "error": ""},
        "vram": {"label": "VRAM", "available": bool(gpus), "percent": _safe_percent(vram_percent), "used_bytes": total_used or None, "total_bytes": total_vram or None},
    }


def _resource_board_refresh_seconds(settings=None):
    try:
        value = int((settings or {}).get("system_resource_board_refresh_seconds", 5))
    except Exception:
        value = 5
    return max(1, min(300, value))


def system_resource_usage_snapshot(ttl_seconds=None, *, base_dir=None):
    try:
        requested_ttl = float(ttl_seconds) if ttl_seconds is not None else _RESOURCE_USAGE_CACHE_TTL_SECONDS
    except Exception:
        requested_ttl = _RESOURCE_USAGE_CACHE_TTL_SECONDS
    requested_ttl = max(1.0, min(300.0, requested_ttl))
    now_mono = time.monotonic()
    cached = _RESOURCE_USAGE_CACHE.get("value")
    cached_ttl = float(_RESOURCE_USAGE_CACHE.get("ttl_seconds") or 0)
    if cached and abs(cached_ttl - requested_ttl) < 0.001 and now_mono < float(_RESOURCE_USAGE_CACHE.get("expires_at") or 0):
        return cached
    with _RESOURCE_USAGE_CACHE_LOCK:
        now_mono = time.monotonic()
        cached = _RESOURCE_USAGE_CACHE.get("value")
        cached_ttl = float(_RESOURCE_USAGE_CACHE.get("ttl_seconds") or 0)
        if cached and abs(cached_ttl - requested_ttl) < 0.001 and now_mono < float(_RESOURCE_USAGE_CACHE.get("expires_at") or 0):
            return cached
        gpu = _gpu_usage_snapshot()
        snapshot = {
            "sampled_at": datetime.now().replace(microsecond=0).isoformat(),
            "cpu": _cpu_usage_snapshot(),
            "ram": _ram_usage_snapshot(),
            "process": _process_usage_snapshot(),
            "processes": _related_processes_snapshot(base_dir=base_dir),
            "gpu": gpu["gpu"],
            "vram": gpu["vram"],
        }
        _RESOURCE_USAGE_CACHE["value"] = snapshot
        _RESOURCE_USAGE_CACHE["ttl_seconds"] = requested_ttl
        _RESOURCE_USAGE_CACHE["expires_at"] = now_mono + requested_ttl
        return snapshot


def _safe_path_size(path: Path) -> int:
    try:
        if path.is_file():
            return max(0, int(path.stat().st_size))
    except OSError:
        pass
    return 0


def _sqlite_file_size_detail(path: Path) -> dict:
    db_bytes = _safe_path_size(path)
    wal_bytes = _safe_path_size(Path(str(path) + "-wal"))
    shm_bytes = _safe_path_size(Path(str(path) + "-shm"))
    return {
        "database_bytes": db_bytes,
        "wal_bytes": wal_bytes,
        "shm_bytes": shm_bytes,
        "total_bytes": db_bytes + wal_bytes + shm_bytes,
        "exists": db_bytes > 0,
    }


def _database_usage_snapshot(*, db_path, db_dir, additional_db_paths, base_dir, public_relative_path, integrity_check=None, audit_hash_check=None):
    candidates = {"main": db_path}
    for label, path in (additional_db_paths or {}).items():
        if path:
            candidates[str(label)] = path
    root = Path(db_dir or Path(db_path).parent)
    for label, info in DOMAIN_DATABASES.items():
        filename = str(info.get("filename") or "").strip()
        if filename:
            candidates.setdefault(label, root / filename)
    try:
        for path in root.glob("*.db"):
            label = path.stem.replace("-", "_")
            candidates.setdefault(label, path)
    except OSError:
        pass

    seen = set()
    files = []
    for label, raw_path in candidates.items():
        path = Path(raw_path)
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        detail = _sqlite_file_size_detail(path)
        if not detail["exists"] and detail["wal_bytes"] <= 0 and detail["shm_bytes"] <= 0:
            continue
        detail.update({
            "label": label,
            "path": public_relative_path(str(path), base_dir),
        })
        files.append(detail)

    total_bytes = sum(int(item.get("total_bytes") or 0) for item in files)
    main_total = 0
    main_path = str(Path(db_path))
    for item in files:
        if item.get("label") == "main" or item.get("path") == public_relative_path(main_path, base_dir):
            main_total = int(item.get("total_bytes") or 0)
            break
    return {
        "db_dir": public_relative_path(str(root), base_dir),
        "total_bytes": total_bytes,
        "main_database_total_bytes": main_total,
        "file_count": len(files),
        "sidecar_bytes": sum(int(item.get("wal_bytes") or 0) + int(item.get("shm_bytes") or 0) for item in files),
        "integrity_check": integrity_check or {},
        "audit_hash_check": audit_hash_check or {},
        "files": files,
    }


def _safe_integrity_summary(summary_fn, fallback):
    try:
        result = summary_fn() if callable(summary_fn) else None
        if isinstance(result, dict):
            return result
    except Exception as exc:
        fallback = dict(fallback or {})
        fallback["error"] = exc.__class__.__name__
        fallback["details"] = str(exc)[:200]
    return dict(fallback or {})


def _transfer_usage_snapshot(app) -> dict:
    try:
        status = backpressure_status(app)
    except Exception:
        status = {}
    traffic = status.get("traffic") or {}
    totals = traffic.get("totals") or {}
    cumulative = traffic.get("cumulative") or {}
    return {
        "pid": status.get("pid") or os.getpid(),
        "process_local": bool(status.get("process_local", True)),
        "window_seconds": int(traffic.get("window_seconds") or 0),
        "recent_window_seconds": int(traffic.get("recent_window_seconds") or 0),
        "upload_bytes_per_second": int(traffic.get("upload_bytes_per_sec") or 0),
        "download_bytes_per_second": int(traffic.get("download_bytes_per_sec") or 0),
        "window_upload_bytes": int(totals.get("upload_bytes") or 0),
        "window_download_bytes": int(totals.get("download_bytes") or 0),
        "cumulative_upload_bytes": int(cumulative.get("upload_bytes") or 0),
        "cumulative_download_bytes": int(cumulative.get("download_bytes") or 0),
        "cumulative_requests": int(cumulative.get("requests") or 0),
        "started_at": cumulative.get("started_at"),
    }


def register_system_admin_security_routes(app, ctx):
    BASE_DIR = ctx["BASE_DIR"]
    CHAT_DIR = ctx["CHAT_DIR"]
    DB_DIR = ctx.get("DB_DIR") or os.path.dirname(ctx["DB_PATH"])
    DB_PATH = ctx["DB_PATH"]
    ADDITIONAL_DB_PATHS = ctx.get("ADDITIONAL_DB_PATHS") or {}
    LOG_DIR = ctx["LOG_DIR"]
    ANCHOR_DIR = ctx["ANCHOR_DIR"]
    SERVER_LOG_PATH = ctx["SERVER_LOG_PATH"]
    STORAGE_DIR = ctx["STORAGE_DIR"]
    CURRENT_SCHEMA_VERSION = ctx["CURRENT_SCHEMA_VERSION"]
    CONFIRM_APPROVE = ctx["CONFIRM_APPROVE"]
    SECURITY_TEST_JOBS = ctx["SECURITY_TEST_JOBS"]
    SECURITY_TEST_JOBS_LOCK = ctx["SECURITY_TEST_JOBS_LOCK"]
    SECURITY_SETTING_KEYS = ctx["SECURITY_SETTING_KEYS"]
    SECURITY_THRESHOLD_KEYS = ctx["SECURITY_THRESHOLD_KEYS"]
    SERVER_UPDATE_WARNING = ctx["SERVER_UPDATE_WARNING"]
    GIT_REPO_DIR = ctx["GIT_REPO_DIR"]
    points_service = ctx.get("points_service")

    audit = ctx["audit"]
    get_client_ip = ctx["get_client_ip"]
    get_current_user_ctx = ctx["get_current_user_ctx"]
    get_db = ctx["get_db"]
    get_feature_settings = ctx["get_feature_settings"]
    get_server_output = ctx["get_server_output"]
    get_system_settings = ctx["get_system_settings"]
    get_ua = ctx["get_ua"]
    is_audit_chain_enabled = ctx["is_audit_chain_enabled"]
    json_resp = ctx["json_resp"]
    save_settings = ctx["save_settings"]
    server_mode_service = ctx["server_mode_service"]
    snapshot_service = ctx["snapshot_service"]
    integrity_guard = ctx["integrity_guard"]
    verify_audit_integrity = ctx["verify_audit_integrity"]

    require_csrf = ctx["require_csrf"]
    require_csrf_safe = ctx["require_csrf_safe"]
    require_root_actor = ctx["require_root_actor"]
    require_super_admin_actor = ctx["require_super_admin_actor"]

    dir_stats = ctx["dir_stats"]
    safe_count = ctx["safe_count"]
    health_counts = ctx["health_counts"]
    db_schema_summary = ctx["db_schema_summary"]
    db_integrity_summary = ctx["db_integrity_summary"]
    readiness_summary = ctx["readiness_summary"]
    anomaly_summary = ctx["anomaly_summary"]
    audit_integrity_summary = ctx["audit_integrity_summary"]
    security_center_payload = ctx["security_center_payload"]
    security_profile_payload = ctx["security_profile_payload"]
    public_relative_path = ctx["public_relative_path"]
    current_git_state = ctx["current_git_state"]
    git_short_text = ctx["git_short_text"]
    git_update_preview = ctx["git_update_preview"]
    rebuild_integrity_baseline_after_update = ctx["rebuild_integrity_baseline_after_update"]
    prepare_server_update_recovery_points = ctx["prepare_server_update_recovery_points"]
    run_git_command = ctx["run_git_command"]
    read_update_summary = ctx["read_update_summary"]
    schedule_server_restart = ctx["schedule_server_restart"]
    audit_settings_changed = ctx["audit_settings_changed"]
    notify_root = ctx["notify_root"]
    safe_security_test_int = ctx["safe_security_test_int"]
    security_test_job_payload = ctx["security_test_job_payload"]
    security_test_report_root = ctx["security_test_report_root"]
    start_security_test_job = ctx["start_security_test_job"]
    tail_text_lines = ctx["tail_text_lines"]
    validate_git_branch_name = ctx["validate_git_branch_name"]
    repair_audit_chain = ctx["repair_audit_chain"]
    repair_violation_chains = ctx["repair_violation_chains"]
    audit_storage_capacity = ctx["audit_storage_capacity"]

    def _peek_management_snapshot_summary(snapshot_key):
        conn = get_db()
        try:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='management_plane_snapshots'"
            ).fetchone()
            if not table:
                return {"ok": False, "missing": True, "summary": {}, "msg": "management-plane snapshot table missing"}
            row = conn.execute(
                """
                SELECT snapshot_key, summary_json, source_job_uuid, generated_at, updated_at, error
                FROM management_plane_snapshots
                WHERE snapshot_key=?
                """,
                (str(snapshot_key),),
            ).fetchone()
            if not row:
                return {"ok": False, "missing": True, "summary": {}, "msg": "management-plane snapshot missing"}
            try:
                summary = json.loads(row["summary_json"] or "{}")
            except Exception:
                summary = {}
            if not isinstance(summary, dict):
                summary = {}
            return {
                "ok": True,
                "missing": False,
                "snapshot_key": row["snapshot_key"],
                "summary": summary,
                "generated_at": row["generated_at"],
                "updated_at": row["updated_at"],
                "source_job_uuid": row["source_job_uuid"],
                "error": row["error"],
            }
        finally:
            conn.close()

    def _points_finality_health_snapshot():
        if not points_service or not hasattr(points_service, "transfer_finality_observability_snapshot"):
            return {
                "ok": True,
                "status": "unavailable",
                "snapshot_type": "points_transfer_finality_observability",
                "bounded": True,
                "disabled": True,
                "msg": "points service unavailable",
            }
        safe_mode = {}
        safe_mode_error = ""
        if hasattr(points_service, "safe_mode_status"):
            try:
                maybe_safe_mode = points_service.safe_mode_status()
                safe_mode = maybe_safe_mode if isinstance(maybe_safe_mode, dict) else {}
            except Exception as exc:
                safe_mode_error = f"{exc.__class__.__name__}: {str(exc)[:160]}"
        try:
            payload = points_service.transfer_finality_observability_snapshot(recent_limit=200)
        except Exception as exc:
            payload = {
                "ok": False,
                "status": "warning",
                "snapshot_type": "points_transfer_finality_observability",
                "bounded": True,
                "error": exc.__class__.__name__,
                "msg": str(exc)[:200],
            }
        if not isinstance(payload, dict):
            payload = {
                "ok": False,
                "status": "warning",
                "snapshot_type": "points_transfer_finality_observability",
                "bounded": True,
                "error": "invalid_snapshot",
                "msg": "points finality snapshot did not return an object",
            }
        security_context = points_safe_mode_security_context(
            safe_mode,
            source="admin_health.points_finality",
            source_ip=get_client_ip() or "0.0.0.0",
            user_agent=get_ua() or "",
            blocked_action="points ledger writes",
        )
        payload["safe_mode"] = safe_mode
        payload["safe_mode_security"] = security_context
        if safe_mode_error:
            payload["safe_mode_error"] = safe_mode_error
        if safe_mode.get("safe_mode"):
            payload["ok"] = False
            payload["status"] = "critical"
            payload["safe_mode_active"] = True
            payload["attack_method"] = security_context.get("attack_method")
            payload["msg"] = (
                "PointsChain safe mode active; ledger writes are paused until "
                "branch/governance recovery resolves the incident."
            )
        try:
            latest = _peek_management_snapshot_summary("points_finality_sweep")
            payload["latest_sweep_snapshot"] = {
                "ok": bool(latest.get("ok")),
                "missing": bool(latest.get("missing")),
                "generated_at": latest.get("generated_at"),
                "updated_at": latest.get("updated_at"),
                "source_job_uuid": latest.get("source_job_uuid"),
                "summary": latest.get("summary") or {},
                "error": latest.get("error") or "",
            }
        except Exception as exc:
            payload["latest_sweep_snapshot"] = {
                "ok": False,
                "missing": True,
                "error": f"{exc.__class__.__name__}: {str(exc)[:160]}",
                "summary": {},
            }
        return payload

    @app.route("/api/admin/health", methods=["GET"])
    @require_csrf_safe
    def admin_health():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        if ctx["role_rank"](actor_role) < ctx["role_rank"]("super_admin"):
            return json_resp({"ok":False,"msg":"只有最高管理者可查看伺服器健康度"}), 403

        settings = get_system_settings()
        audit_state = audit_integrity_summary()
        audit_enabled = bool(audit_state.get("enabled"))
        audit_ok = audit_state.get("ok")

        counts, count_errors = health_counts()
        counts["pending_reports"] = counts.get("pending_chat_reports", 0)

        db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        chat_stats = dir_stats(CHAT_DIR, ".jsonl")
        log_stats = dir_stats(LOG_DIR)
        anchor_stats = dir_stats(ANCHOR_DIR)
        storage_stats = dir_stats(STORAGE_DIR)
        capacity_conn = get_db()
        try:
            storage_capacity = audit_storage_capacity(capacity_conn, STORAGE_DIR, include_users=False)
            storage_catalog_files, _storage_catalog_error = safe_count(capacity_conn, "storage_files", optional=True)
        finally:
            capacity_conn.close()
        readiness = readiness_summary(db=db_schema_summary(), audit_state=audit_state)
        anomaly = anomaly_summary(audit_state=audit_state)
        points_finality = _points_finality_health_snapshot()
        database_usage = _database_usage_snapshot(
            db_path=DB_PATH,
            db_dir=DB_DIR,
            additional_db_paths=ADDITIONAL_DB_PATHS,
            base_dir=BASE_DIR,
            public_relative_path=public_relative_path,
            integrity_check={"ok": None, "details": "skipped in fast health endpoint; use /api/admin/health/db-integrity"},
            audit_hash_check={"ok": None, "details": "skipped in fast health endpoint; use /api/admin/environment"},
        )
        status = "critical" if ((audit_enabled and audit_ok is False) or settings.get("maintenance_mode", False) or readiness["status"] == "critical") else "ok"
        if storage_capacity["status"] == "critical":
            status = "critical"
        if points_finality.get("status") == "critical":
            status = "critical"
        if status == "ok" and (readiness["status"] == "degraded" or anomaly["status"] in {"warning", "critical"} or count_errors):
            status = "degraded"
        if status == "ok" and storage_capacity["status"] == "warning":
            status = "degraded"
        if status == "ok" and points_finality.get("status") in {"warning", "degraded"}:
            status = "degraded"
        return json_resp({
            "ok": True,
            "status": status,
            "maintenance_mode": settings.get("maintenance_mode", False),
            "audit_integrity": {
                "enabled": audit_enabled,
                "ok": audit_ok,
                "broken_at": audit_state.get("broken_at"),
                "details": audit_state.get("details"),
                "operator_action_required": audit_ok is False,
                "auto_lockdown_applied": False,
            },
            "counts": counts,
            "count_errors": count_errors,
            "storage": {
                "database_bytes": db_size,
                "chat_files": chat_stats["files"],
                "chat_bytes": chat_stats["bytes"],
                "chat_dir": public_relative_path(CHAT_DIR, BASE_DIR),
                "log_files": log_stats["files"],
                "log_bytes": log_stats["bytes"],
                "anchor_files": anchor_stats["files"],
                "anchor_bytes": anchor_stats["bytes"],
                "storage_files": storage_catalog_files or storage_stats["files"],
                "storage_bytes": int(storage_capacity.get("cloud_used_bytes") or storage_stats["bytes"] or 0),
                "storage_dir": public_relative_path(storage_stats["path"], BASE_DIR),
                "capacity_audit": storage_capacity,
            },
            "database_usage": database_usage,
            "points_finality": points_finality,
            "readiness": readiness,
            "anomaly": anomaly,
        })

    @app.route("/api/admin/environment", methods=["GET"])
    @require_csrf_safe
    def admin_environment():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        if actor["username"] != "root":
            return json_resp({"ok":False,"msg":"只有 root 可查看系統環境"}), 403

        settings = get_system_settings()
        refresh_seconds = _resource_board_refresh_seconds(settings)
        db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        database_usage = _database_usage_snapshot(
            db_path=DB_PATH,
            db_dir=DB_DIR,
            additional_db_paths=ADDITIONAL_DB_PATHS,
            base_dir=BASE_DIR,
            public_relative_path=public_relative_path,
            integrity_check=_safe_integrity_summary(db_integrity_summary, {"ok": None, "details": "database integrity unavailable"}),
            audit_hash_check=_safe_integrity_summary(audit_integrity_summary, {"enabled": None, "ok": None, "details": "audit hash unavailable"}),
        )
        transfer_usage = _transfer_usage_snapshot(app)
        log_files = [name for name in os.listdir(LOG_DIR) if os.path.isfile(os.path.join(LOG_DIR, name))] if os.path.isdir(LOG_DIR) else []
        chat_files = [name for name in os.listdir(CHAT_DIR) if os.path.isfile(os.path.join(CHAT_DIR, name))] if os.path.isdir(CHAT_DIR) else []
        anchor_files = [name for name in os.listdir(ANCHOR_DIR) if os.path.isfile(os.path.join(ANCHOR_DIR, name))] if os.path.isdir(ANCHOR_DIR) else []
        return json_resp({
            "ok": True,
            "environment": {
                **_environment_identity_payload(),
                "base_dir": ".",
                "database_path": public_relative_path(DB_PATH, BASE_DIR),
                "database_dir": public_relative_path(DB_DIR, BASE_DIR),
                "log_dir": public_relative_path(LOG_DIR, BASE_DIR),
                "chat_dir": public_relative_path(CHAT_DIR, BASE_DIR),
                "anchor_dir": public_relative_path(ANCHOR_DIR, BASE_DIR),
                "database_bytes": db_size,
                "log_files": len(log_files),
                "chat_files": len(chat_files),
                "anchor_files": len(anchor_files),
            },
            "resource_refresh_seconds": refresh_seconds,
            "resource_usage": system_resource_usage_snapshot(ttl_seconds=refresh_seconds, base_dir=BASE_DIR),
            "database_usage": database_usage,
            "transfer_usage": transfer_usage,
        })

    @app.route("/api/admin/environment/resources", methods=["GET"])
    @require_csrf_safe
    def admin_environment_resources():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        if actor["username"] != "root":
            return json_resp({"ok":False,"msg":"只有 root 可查看系統資源"}), 403

        settings = get_system_settings()
        refresh_seconds = _resource_board_refresh_seconds(settings)
        return json_resp({
            "ok": True,
            "environment": _environment_identity_payload(),
            "resource_refresh_seconds": refresh_seconds,
            "resource_usage": system_resource_usage_snapshot(ttl_seconds=refresh_seconds, base_dir=BASE_DIR),
            "database_usage": _database_usage_snapshot(
                db_path=DB_PATH,
                db_dir=DB_DIR,
                additional_db_paths=ADDITIONAL_DB_PATHS,
                base_dir=BASE_DIR,
                public_relative_path=public_relative_path,
                integrity_check=_safe_integrity_summary(db_integrity_summary, {"ok": None, "details": "database integrity unavailable"}),
                audit_hash_check=_safe_integrity_summary(audit_integrity_summary, {"enabled": None, "ok": None, "details": "audit hash unavailable"}),
            ),
            "transfer_usage": _transfer_usage_snapshot(app),
        })

    @app.route("/api/admin/health/readiness", methods=["GET"])
    @require_csrf_safe
    def admin_health_readiness():
        _, error = require_super_admin_actor()
        if error:
            return error
        summary = readiness_summary()
        return json_resp({"ok": True, "readiness": summary})

    @app.route("/api/admin/health/anomaly", methods=["GET"])
    @require_csrf_safe
    def admin_health_anomaly():
        _, error = require_super_admin_actor()
        if error:
            return error
        return json_resp({"ok": True, "anomaly": anomaly_summary()})

    @app.route("/api/admin/health/audit-chain", methods=["GET"])
    @require_csrf_safe
    def admin_health_audit_chain():
        _, error = require_super_admin_actor()
        if error:
            return error
        return json_resp({"ok": True, "audit_integrity": audit_integrity_summary()})

    @app.route("/api/admin/health/db-integrity", methods=["GET"])
    @require_csrf_safe
    def admin_health_db_integrity():
        _, error = require_super_admin_actor()
        if error:
            return error
        return json_resp({"ok": True, "database": db_integrity_summary()})

    @app.route("/api/admin/health/playwright-ci", methods=["GET"])
    @require_csrf_safe
    def admin_health_playwright_ci():
        _, error = require_super_admin_actor()
        if error:
            return error
        workflow = str(request.args.get("workflow") or "playwright-qa.yml").strip() or "playwright-qa.yml"
        result = playwright_ci_status(repo_dir=GIT_REPO_DIR, workflow_file=workflow)
        return json_resp({"ok": True, "playwright_ci": result})

    @app.route("/api/admin/security-center", methods=["GET"])
    @require_csrf_safe
    def admin_security_center():
        _, error = require_root_actor()
        if error:
            return error
        return json_resp({"ok": True, "security_center": security_center_payload()})

    @app.route("/api/admin/server-output", methods=["GET"])
    @require_csrf_safe
    def admin_server_output():
        _, error = require_root_actor()
        if error:
            return error
        limit = request.args.get("limit", 200)
        try:
            limit_int = max(1, int(limit))
        except (TypeError, ValueError):
            limit_int = 200
        result = get_server_output(limit=limit_int) or {"lines": [], "max_lines": 0}
        # When the in-process runtime buffer is empty (e.g. immediately after
        # gunicorn boot, or after a runtime reset) fall back to tailing the log
        # files used by the current runner so the operator still sees activity.
        lines = result.get("lines") or []
        if not lines:
            log_candidates = [
                ("gunicorn_error.log", os.path.join(LOG_DIR, "gunicorn_error.log")),
                ("server_direct.out", os.path.join(LOG_DIR, "server_direct.out")),
                ("server.log", SERVER_LOG_PATH),
            ]
            for source_name, log_path in log_candidates:
                if not os.path.isfile(log_path):
                    continue
                try:
                    tail = tail_text_lines(log_path, limit_int)
                    if not tail:
                        continue
                    parsed_lines = []
                    for raw in tail:
                        text = raw.rstrip("\n")
                        stream = "info"
                        m = re.search(r"\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]", text)
                        if m:
                            stream = m.group(1).lower()
                        elif " ERROR " in text or " Traceback" in text or "Traceback (most recent call last)" in text:
                            stream = "error"
                        elif " WARNING " in text or " WARNING]" in text:
                            stream = "warning"
                        parsed_lines.append({"stream": stream, "line": text})
                    result = {
                        "lines": parsed_lines,
                        "max_lines": result.get("max_lines") or 0,
                        "source": source_name,
                    }
                    break
                except OSError:
                    continue
        return json_resp({"ok": True, "server_output": result})

    @app.route("/api/root/security-tests", methods=["GET"])
    @require_csrf_safe
    def root_security_tests():
        _, error = require_root_actor()
        if error:
            return error
        with SECURITY_TEST_JOBS_LOCK:
            jobs = [security_test_job_payload(job) for job in SECURITY_TEST_JOBS.values()]
        jobs.sort(key=lambda item: item.get("started_at") or "", reverse=True)
        return json_resp({"ok": True, "jobs": jobs[:20], "report_root": os.path.relpath(security_test_report_root(), BASE_DIR)})

    @app.route("/api/root/security-tests/<job_id>", methods=["GET"])
    @require_csrf_safe
    def root_security_test_detail(job_id):
        _, error = require_root_actor()
        if error:
            return error
        with SECURITY_TEST_JOBS_LOCK:
            job = SECURITY_TEST_JOBS.get(job_id)
            payload = security_test_job_payload(job) if job else None
        if not payload:
            return json_resp({"ok": False, "msg": "找不到測試任務"}), 404
        return json_resp({"ok": True, "job": payload})

    @app.route("/api/root/security-tests/pentest", methods=["POST"])
    @require_csrf
    def root_security_test_pentest():
        actor, error = require_root_actor()
        if error:
            return error
        try:
            data = request.get_json(force=True) if request.is_json else {}
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        target = str(data.get("target") or request.host_url or "").strip().rstrip("/")
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-_\[\]:]+(?::\d+)?(?:/.*)?", target):
            return json_resp({"ok": False, "msg": "target 必須是 http(s) URL"}), 400
        tool_timeout = safe_security_test_int(data.get("tool_timeout_seconds"), 180, 1, 3600)
        if tool_timeout is None:
            return json_resp({"ok": False, "msg": "tool_timeout_seconds 必須介於 1-3600"}), 400
        report_root = security_test_report_root()
        command = [
            os.path.join(BASE_DIR, "scripts", "security", "pentest", "run_pentest.sh"),
            "--target", target,
            "--out", report_root,
            "--tool-timeout", str(tool_timeout),
        ]
        only = str(data.get("only") or "").strip()
        skip = str(data.get("skip") or "").strip()
        # Default to a quick-scan set so the root operator can fire the
        # endpoint with just `{target}` and still get a useful smoke run
        # instead of accidentally launching the full pentest matrix.
        if not only:
            only = "curl-baseline,functional-permissions,session-security,header-security"
        command.extend(["--only", only])
        if skip:
            command.extend(["--skip", skip])
        if bool(data.get("i_own_this_target")):
            command.append("--i-own-this-target")
        env = {}
        for key in ("ROOT_PASSWORD", "MANAGER_PASSWORD", "TEST_PASSWORD"):
            value = str(data.get(key.lower()) or "").strip()
            if not value:
                value = str(os.environ.get(f"HTML_LEARNING_{key}") or "").strip()
            if value:
                env[key] = value
        # Defaults match the seed users created by test_for_develop.sh /
        # the bootstrap routine so a fresh dev site can run the privilege
        # scan without forcing every operator to pass usernames in JSON.
        username_defaults = {
            "root_username": "root",
            "manager_username": "admin",
            "user_username": "test",
        }
        username_env_keys = {
            "root_username": "PENTEST_ROOT_USERNAME",
            "manager_username": "PENTEST_MANAGER_USERNAME",
            "user_username": "PENTEST_USER_USERNAME",
        }
        for payload_key, env_key in username_env_keys.items():
            value = str(data.get(payload_key) or "").strip()
            if not value:
                value = username_defaults[payload_key]
            env[env_key] = value
        job = start_security_test_job(
            "pentest",
            command,
            command_label=["scripts/security/pentest/run_pentest.sh", "--target", target],
            report_root=report_root,
            report_prefix="20",
            actor=actor,
            env=env,
        )
        return json_resp({"ok": True, "msg": "滲透測試已啟動", "job": security_test_job_payload(job)}, 202)

    @app.route("/api/root/security-tests/functional", methods=["POST"])
    @require_csrf
    def root_security_test_functional():
        actor, error = require_root_actor()
        if error:
            return error
        try:
            data = request.get_json(force=True) if request.is_json else {}
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        port = safe_security_test_int(data.get("port"), 50741, 1, 65535)
        if port is None:
            return json_resp({"ok": False, "msg": "port 必須介於 1-65535"}), 400
        report_root = security_test_report_root()
        command = [
            os.path.join(BASE_DIR, "scripts", "security", "pentest", "run_functional_smoke.sh"),
            "--port", str(port),
            "--out", report_root,
        ]
        if bool(data.get("keep_runtime")):
            command.append("--keep-runtime")
        env = {}
        for key in ("ROOT_PASSWORD", "ROOT_CHANGED_PASSWORD", "MANAGER_PASSWORD", "TEST_PASSWORD"):
            value = str(data.get(key.lower()) or "").strip()
            if not value:
                value = str(os.environ.get(f"HTML_LEARNING_{key}") or "").strip()
            if value:
                env[key] = value
        job = start_security_test_job(
            "functional",
            command,
            command_label=["scripts/security/pentest/run_functional_smoke.sh", "--port", str(port)],
            report_root=report_root,
            report_prefix="functional_",
            actor=actor,
            env=env,
        )
        return json_resp({"ok": True, "msg": "全功能測試已啟動", "job": security_test_job_payload(job)}, 202)

    @app.route("/api/root/security-tests/privilege", methods=["POST"])
    @require_csrf
    def root_security_test_privilege():
        actor, error = require_root_actor()
        if error:
            return error
        try:
            data = request.get_json(force=True) if request.is_json else {}
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        target = str(data.get("target") or request.host_url or "").strip().rstrip("/")
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-_\[\]:]+(?::\d+)?(?:/.*)?", target):
            return json_resp({"ok": False, "msg": "target 必須是 http(s) URL"}), 400
        report_root = security_test_report_root()
        artifact_prefix = f"privilege_{uuid.uuid4().hex[:10]}"
        out_json = os.path.join(report_root, f"{artifact_prefix}.json")
        out_md = os.path.join(report_root, f"{artifact_prefix}.md")
        command = [
            sys.executable,
            os.path.join(BASE_DIR, "scripts", "security", "pentest", "functional_permission_pentest.py"),
            "--base-url", target,
            "--out-json", out_json,
            "--out-md", out_md,
        ]
        if bool(data.get("destructive")):
            command.append("--destructive")
        env = {}
        for key in ("ROOT_PASSWORD", "MANAGER_PASSWORD", "TEST_PASSWORD"):
            value = str(data.get(key.lower()) or "").strip()
            if not value:
                value = str(os.environ.get(f"HTML_LEARNING_{key}") or "").strip()
            if value:
                env[key] = value
        # Defaults match the seed users created by test_for_develop.sh /
        # the bootstrap routine so a fresh dev site can run the privilege
        # scan without forcing every operator to pass usernames in JSON.
        username_defaults = {
            "root_username": "root",
            "manager_username": "admin",
            "user_username": "test",
        }
        username_env_keys = {
            "root_username": "PENTEST_ROOT_USERNAME",
            "manager_username": "PENTEST_MANAGER_USERNAME",
            "user_username": "PENTEST_USER_USERNAME",
        }
        for payload_key, env_key in username_env_keys.items():
            value = str(data.get(payload_key) or "").strip()
            if not value:
                value = username_defaults[payload_key]
            env[env_key] = value
        job = start_security_test_job(
            "privilege",
            command,
            command_label=[
                "python3",
                "scripts/security/pentest/functional_permission_pentest.py",
                "--base-url",
                target,
            ] + (["--destructive"] if bool(data.get("destructive")) else []),
            report_root=report_root,
            report_prefix=artifact_prefix,
            actor=actor,
            env=env,
        )
        return json_resp({"ok": True, "msg": "越權測試已啟動", "job": security_test_job_payload(job)}, 202)

    @app.route("/api/root/security-tests/stress", methods=["POST"])
    @require_csrf
    def root_security_test_stress():
        actor, error = require_root_actor()
        if error:
            return error
        try:
            data = request.get_json(force=True) if request.is_json else {}
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        target = str(data.get("target") or request.host_url or "").strip().rstrip("/")
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-_\[\]:]+(?::\d+)?(?:/.*)?", target):
            return json_resp({"ok": False, "msg": "target 必須是 http(s) URL"}), 400
        total_requests = safe_security_test_int(data.get("requests"), 200, 1, 5000)
        duration_seconds = safe_security_test_int(data.get("duration_seconds"), 30, 1, 600)
        max_requests = safe_security_test_int(data.get("max_requests"), 5000, 1, 20000)
        concurrency = safe_security_test_int(data.get("concurrency"), 20, 1, 100)
        burst_size = safe_security_test_int(data.get("burst_size"), 1, 1, 500)
        burst_interval_ms = safe_security_test_int(data.get("burst_interval_ms"), 0, 0, 60000)
        timeout_seconds = safe_security_test_int(data.get("timeout_seconds"), 8, 1, 120)
        mode = str(data.get("mode") or "count").strip().lower()
        if total_requests is None:
            return json_resp({"ok": False, "msg": "requests 必須介於 1-5000"}), 400
        if duration_seconds is None:
            return json_resp({"ok": False, "msg": "duration_seconds 必須介於 1-600"}), 400
        if max_requests is None:
            return json_resp({"ok": False, "msg": "max_requests 必須介於 1-20000"}), 400
        if concurrency is None:
            return json_resp({"ok": False, "msg": "concurrency 必須介於 1-100"}), 400
        if burst_size is None:
            return json_resp({"ok": False, "msg": "burst_size 必須介於 1-500"}), 400
        if burst_interval_ms is None:
            return json_resp({"ok": False, "msg": "burst_interval_ms 必須介於 0-60000"}), 400
        if timeout_seconds is None:
            return json_resp({"ok": False, "msg": "timeout_seconds 必須介於 1-120"}), 400
        if mode not in {"count", "duration"}:
            return json_resp({"ok": False, "msg": "mode 必須是 count 或 duration"}), 400
        paths = str(data.get("paths") or "").strip()
        report_root = security_test_report_root()
        command = [
            sys.executable,
            os.path.join(BASE_DIR, "scripts", "security", "pentest", "stress_test.py"),
            "--target", target,
            "--mode", mode,
            "--concurrency", str(concurrency),
            "--timeout", str(timeout_seconds),
            "--burst-size", str(burst_size),
            "--burst-interval-ms", str(burst_interval_ms),
            "--out", report_root,
        ]
        if mode == "duration":
            command.extend(["--duration-seconds", str(duration_seconds), "--max-requests", str(max_requests)])
        else:
            command.extend(["--requests", str(total_requests)])
        if paths:
            command.extend(["--paths", paths])
        job = start_security_test_job(
            "stress",
            command,
            command_label=[
                "python3",
                "scripts/security/pentest/stress_test.py",
                "--target",
                target,
                "--mode",
                mode,
                "--concurrency",
                str(concurrency),
            ],
            report_root=report_root,
            report_prefix="stress_",
            actor=actor,
        )
        return json_resp({"ok": True, "msg": "壓力測試已啟動", "job": security_test_job_payload(job)}, 202)

    @app.route("/api/admin/security-center/thresholds", methods=["PUT"])
    @require_csrf
    def admin_security_center_thresholds():
        actor, error = require_root_actor()
        if error:
            return error
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        updates = {}
        for key in SECURITY_THRESHOLD_KEYS:
            if key not in data:
                continue
            try:
                value = int(data.get(key))
            except Exception:
                return json_resp({"ok": False, "msg": f"{key} 必須是整數"}), 400
            if value < 0 or value > 100000:
                return json_resp({"ok": False, "msg": f"{key} 必須介於 0-100000"}), 400
            updates[key] = value
        if not updates:
            return json_resp({"ok": False, "msg": "沒有可寫入的閾值"}), 400
        before_settings = get_system_settings()
        saved = save_settings(updates)
        audit_settings_changed("SECURITY_THRESHOLDS_CHANGED", actor, before_settings, saved, scope="security_thresholds")
        return json_resp({"ok": True, "msg": "安全閾值已更新", "thresholds": {key: get_system_settings().get(key) for key in SECURITY_THRESHOLD_KEYS}})

    @app.route("/api/admin/security-center/controls", methods=["PUT"])
    @require_csrf
    def admin_security_center_controls():
        actor, error = require_root_actor()
        if error:
            return error
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        updates = {key: data[key] for key in SECURITY_SETTING_KEYS if key in data}
        if not updates:
            return json_resp({"ok": False, "msg": "沒有可寫入的安全機制開關"}), 400
        before_settings = get_system_settings()
        saved = save_settings(updates)
        audit_settings_changed("SECURITY_CONTROLS_CHANGED", actor, before_settings, saved, scope="security_controls")
        return json_resp({"ok": True, "msg": "安全機制設定已更新", "settings": {key: get_system_settings().get(key) for key in SECURITY_SETTING_KEYS}})

    @app.route("/api/root/server-update/status", methods=["GET"])
    @require_csrf_safe
    def root_server_update_status():
        actor, error = require_root_actor()
        if error:
            return error
        fetch = str(request.args.get("fetch") or "").lower() in {"1", "true", "yes"}
        state = current_git_state(fetch=fetch)
        return json_resp({"ok": bool(state.get("ok")), "update": state, "warning": SERVER_UPDATE_WARNING}), (200 if state.get("ok") else 500)

    @app.route("/api/root/server-update/preview", methods=["POST"])
    @require_csrf
    def root_server_update_preview():
        actor, error = require_root_actor()
        if error:
            return error
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        branch = validate_git_branch_name((data or {}).get("branch"))
        if not branch:
            return json_resp({"ok": False, "msg": "請選擇合法的更新分支"}), 400
        preview = git_update_preview(branch, fetch=True)
        audit(
            "SERVER_UPDATE_PREVIEW",
            get_client_ip(),
            user=actor["username"],
            success=bool(preview.get("ok")),
            ua=get_ua(),
            detail=json.dumps({"branch": branch, "ok": bool(preview.get("ok")), "msg": preview.get("msg", "")}, ensure_ascii=False, sort_keys=True),
        )
        return json_resp({"ok": bool(preview.get("ok")), "preview": preview, "msg": preview.get("msg", "")}), (200 if preview.get("ok") else 400)

    @app.route("/api/root/server-update/apply", methods=["POST"])
    @require_csrf
    def root_server_update_apply():
        actor, error = require_root_actor()
        if error:
            return error
        audit(
            "SERVER_UPDATE_APPLY_DISABLED",
            get_client_ip(),
            user=actor["username"],
            success=False,
            ua=get_ua(),
            detail=json.dumps({"reason": "online_server_update_disabled"}, ensure_ascii=False, sort_keys=True),
        )
        return json_resp({
            "ok": False,
            "msg": "線上套用 GitHub 更新已停用；請在部署流程更新程式碼、完成檢查後再重啟。",
        }), 410

    @app.route("/api/admin/security-center/profiles", methods=["GET", "POST"])
    @require_csrf_safe
    def admin_security_profiles():
        if not server_mode_service:
            return json_resp({"ok": False, "msg": "Server Mode 服務目前無法使用"}), 503
        if request.method == "GET":
            _, error = require_super_admin_actor()
            if error:
                return error
            return json_resp({"ok": True, "profiles": server_mode_service.list_profiles()})
        actor, error = require_root_actor()
        if error:
            return error
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        profile_payload, err = security_profile_payload(data)
        if err:
            return json_resp({"ok": False, "msg": err}), 400
        result = server_mode_service.save_profile(
            name=data.get("name"),
            label=data.get("label"),
            description=data.get("description") or "",
            settings=profile_payload["settings"],
            thresholds=profile_payload["thresholds"],
            actor=actor,
        )
        if result.get("ok"):
            audit("SECURITY_PROFILE_SAVED", get_client_ip(), user=actor["username"], success=True, detail=f"profile={result['profile']['name']}")
        return json_resp(result), (200 if result.get("ok") else 400)

    @app.route("/api/root/integrity/status", methods=["GET"])
    @require_csrf_safe
    def root_integrity_status():
        actor, error = require_root_actor()
        if error:
            return error
        if not integrity_guard:
            return json_resp({"ok":False,"msg": "Integrity Guard 服務目前無法使用"}), 503
        return json_resp({"ok":True,"integrity":integrity_guard.status()})

    @app.route("/api/root/integrity/rescan", methods=["POST"])
    @require_csrf
    def root_integrity_rescan():
        actor, error = require_root_actor()
        if error:
            return error
        if not integrity_guard:
            return json_resp({"ok":False,"msg": "Integrity Guard 服務目前無法使用"}), 503
        result = integrity_guard.scan(actor=actor["username"], create_initial_manifest=True)
        audit("INTEGRITY_RESCAN", get_client_ip(), user=actor["username"], success=bool(result.get("ok")), detail=f"status={result.get('status') or result.get('last_scan', {}).get('status')}")
        return json_resp({"ok":bool(result.get("ok", True)),"integrity":result}), (200 if result.get("ok", True) else 500)

    @app.route("/api/root/integrity/findings", methods=["GET"])
    @require_csrf_safe
    def root_integrity_findings():
        actor, error = require_root_actor()
        if error:
            return error
        if not integrity_guard:
            return json_resp({"ok":False,"msg": "Integrity Guard 服務目前無法使用"}), 503
        status = request.args.get("status") or None
        return json_resp({"ok":True,"findings":integrity_guard.list_findings(status=status)})

    @app.route("/api/root/integrity/findings/<int:finding_id>", methods=["GET"])
    @require_csrf_safe
    def root_integrity_finding(finding_id):
        actor, error = require_root_actor()
        if error:
            return error
        if not integrity_guard:
            return json_resp({"ok":False,"msg": "Integrity Guard 服務目前無法使用"}), 503
        finding = integrity_guard.get_finding(finding_id)
        if not finding:
            return json_resp({"ok":False,"msg":"找不到 integrity finding"}), 404
        return json_resp({"ok":True,"finding":finding})

    def handle_integrity_review(finding_id, action):
        actor, error = require_root_actor()
        if error:
            return error
        if not integrity_guard:
            return json_resp({"ok":False,"msg": "Integrity Guard 服務目前無法使用"}), 503
        try:
            data = request.get_json(force=True) if request.is_json else {}
        except Exception:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400
        result = integrity_guard.review_finding(
            finding_id,
            action=action,
            actor=actor,
            note=data.get("note") or "",
            confirm=data.get("confirm") or "",
        )
        return json_resp(result), (200 if result.get("ok") else 400)

    @app.route("/api/root/integrity/findings/bulk-review", methods=["POST"])
    @require_csrf
    def root_integrity_bulk_review():
        actor, error = require_root_actor()
        if error:
            return error
        if not integrity_guard:
            return json_resp({"ok":False,"msg": "Integrity Guard 服務目前無法使用"}), 503
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400
        action = str(data.get("action") or "").strip().lower()
        if action not in {"approve", "reject", "ignore"}:
            return json_resp({"ok":False,"msg": "不支援的 integrity 操作"}), 400
        raw_ids = data.get("finding_ids") or data.get("ids") or []
        if not isinstance(raw_ids, list) or not raw_ids:
            return json_resp({"ok":False,"msg":"finding_ids 不可為空"}), 400
        try:
            finding_ids = [int(item) for item in raw_ids]
        except Exception:
            return json_resp({"ok":False,"msg":"finding_ids 格式錯誤"}), 400
        confirm = str(data.get("confirm") or "")
        if action == "approve" and confirm != CONFIRM_APPROVE:
            return json_resp({"ok":False,"msg": "確認字串不正確"}), 400
        note = str(data.get("note") or "")[:1000]
        results = []
        ok_count = 0
        for finding_id in finding_ids:
            result = integrity_guard.review_finding(
                finding_id,
                action=action,
                actor=actor,
                note=note,
                confirm=confirm,
            )
            result["finding_id"] = finding_id
            results.append(result)
            if result.get("ok"):
                ok_count += 1
        audit(
            f"INTEGRITY_FINDING_BULK_{action.upper()}",
            get_client_ip(),
            user=actor["username"],
            success=ok_count == len(finding_ids),
            ua=get_ua(),
            detail=f"ids={finding_ids}, ok={ok_count}/{len(finding_ids)}, note={note}",
        )
        return json_resp({"ok": ok_count == len(finding_ids), "action": action, "reviewed": ok_count, "total": len(finding_ids), "results": results}), (200 if ok_count == len(finding_ids) else 400)

    @app.route("/api/root/integrity/findings/<int:finding_id>/approve", methods=["POST"])
    @require_csrf
    def root_integrity_approve(finding_id):
        return handle_integrity_review(finding_id, "approve")

    @app.route("/api/root/integrity/findings/<int:finding_id>/reject", methods=["POST"])
    @require_csrf
    def root_integrity_reject(finding_id):
        return handle_integrity_review(finding_id, "reject")

    @app.route("/api/root/integrity/findings/<int:finding_id>/ignore", methods=["POST"])
    @require_csrf
    def root_integrity_ignore(finding_id):
        return handle_integrity_review(finding_id, "ignore")

    @app.route("/api/root/integrity/report", methods=["GET"])
    @require_csrf_safe
    def root_integrity_report():
        actor, error = require_root_actor()
        if error:
            return error
        if not integrity_guard:
            return json_resp({"ok":False,"msg": "Integrity Guard 服務目前無法使用"}), 503
        return json_resp({"ok":True,"report":integrity_guard.export_report(),"approve_confirm":CONFIRM_APPROVE})

    @app.route("/api/admin/integrity/repair", methods=["POST"])
    @require_csrf
    def admin_repair_integrity_chains():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        if actor["username"] != "root":
            return json_resp({"ok":False,"msg":"只有 root 可處理鏈異常"}), 403

        audit_before = verify_audit_integrity() if is_audit_chain_enabled() else (None, None, "audit chain disabled")
        audit_result = repair_audit_chain(reason=f"manual_repair_by={actor['username']}")
        violation_result = repair_violation_chains()
        before_settings = get_system_settings()
        saved = save_settings({"maintenance_mode": False})
        audit_settings_changed("SETTINGS_CHANGED", actor, before_settings, saved, scope="integrity_repair")
        audit_after = verify_audit_integrity() if is_audit_chain_enabled() else (None, None, "audit chain disabled")

        audit(
            "INTEGRITY_CHAINS_RESEALED",
            get_client_ip(),
            user=actor["username"],
            success=True,
            detail=(
                f"audit_before={audit_before[2]}; audit_resealed={audit_result['entries_resealed']}; "
                f"violations_resealed={violation_result['entries_resealed']}; maintenance_mode=False"
            ),
        )
        return json_resp({
            "ok": True,
            "msg": "鏈異常已重新封鏈，維護模式已關閉",
            "audit": {
                "before": {"ok": audit_before[0], "broken_at": audit_before[1], "details": audit_before[2]},
                "after": {"ok": audit_after[0], "broken_at": audit_after[1], "details": audit_after[2]},
                **audit_result,
            },
            "violations": violation_result,
            "maintenance_mode": False,
        })
