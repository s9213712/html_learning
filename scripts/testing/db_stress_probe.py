#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.job_center import (  # noqa: E402
    add_job_event,
    create_job,
    ensure_job_center_schema,
    job_progress_buffer_status,
    list_jobs,
    reset_job_progress_buffer_for_tests,
    update_job,
)
from services.core.progress_backend import reset_progress_backend_for_tests  # noqa: E402
from services.server.database import (  # noqa: E402
    ensure_audit_db_schema,
    get_audit_db,
    get_auth_db,
    get_control_db,
    get_db,
    get_readonly_audit_db,
    get_readonly_auth_db,
    get_readonly_control_db,
    get_readonly_db,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_lock_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text or "database is busy" in text


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.ops: dict[str, int] = {}
        self.latencies: dict[str, list[float]] = {}
        self.errors: list[dict[str, Any]] = []

    def record(self, name: str, elapsed_ms: float) -> None:
        with self.lock:
            self.ops[name] = self.ops.get(name, 0) + 1
            self.latencies.setdefault(name, []).append(float(elapsed_ms))

    def error(self, name: str, exc: BaseException) -> None:
        with self.lock:
            self.errors.append(
                {
                    "worker": name,
                    "type": exc.__class__.__name__,
                    "message": str(exc)[:500],
                    "lock_error": is_lock_error(exc),
                }
            )

    def time_call(self, name: str, fn) -> Any:
        started = time.perf_counter()
        try:
            value = fn()
        except Exception as exc:
            self.error(name, exc)
            return None
        self.record(name, (time.perf_counter() - started) * 1000.0)
        return value

    def summary(self) -> dict[str, Any]:
        latency_summary = {}
        for name, values in sorted(self.latencies.items()):
            values = sorted(values)
            if not values:
                continue
            latency_summary[name] = {
                "count": len(values),
                "p50_ms": round(float(median(values)), 3),
                "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 3),
                "p99_ms": round(values[min(len(values) - 1, int(len(values) * 0.99))], 3),
                "max_ms": round(values[-1], 3),
            }
        return {
            "ops": dict(sorted(self.ops.items())),
            "latencies": latency_summary,
            "errors": self.errors[:50],
            "error_count": len(self.errors),
            "lock_error_count": sum(1 for item in self.errors if item.get("lock_error")),
        }


def read_proc_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            parts = raw.strip().split()
            if parts:
                values[key] = int(parts[0])
    except Exception:
        pass
    return values


def read_proc_cpu_times() -> tuple[int, int] | None:
    try:
        first = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        if not first or first[0] != "cpu":
            return None
        nums = [int(value) for value in first[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        return sum(nums), idle
    except Exception:
        return None


def read_rss_kb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{int(pid)}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                return int(parts[1]) if len(parts) >= 2 else 0
    except Exception:
        return 0
    return 0


def read_proc_process_table() -> dict[int, dict[str, int]]:
    table: dict[int, dict[str, int]] = {}
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            pid = int(stat_path.parent.name)
            raw = stat_path.read_text(encoding="utf-8")
            fields = raw.rsplit(")", 1)[1].strip().split()
            table[pid] = {
                "ppid": int(fields[1]),
                "start_time": int(fields[19]),
            }
        except Exception:
            continue
    return table


def process_tree_pids(root_identities: dict[int, int], table: dict[int, dict[str, int]] | None = None) -> list[int]:
    table = table or read_proc_process_table()
    live_roots = {
        int(pid)
        for pid, start_time in root_identities.items()
        if pid in table and int(table[pid].get("start_time") or -1) == int(start_time)
    }
    children: dict[int, list[int]] = {}
    for pid, item in table.items():
        children.setdefault(int(item.get("ppid") or 0), []).append(int(pid))
    selected = set(live_roots)
    pending = list(live_roots)
    while pending:
        parent = pending.pop()
        for child in children.get(parent, []):
            if child in selected:
                continue
            selected.add(child)
            pending.append(child)
    return sorted(selected)


class ResourceMonitor:
    def __init__(self, *, runtime_root: Path, paths: dict[str, Path], interval: float, pids: list[int] | None = None):
        self.runtime_root = runtime_root
        self.paths = paths
        self.interval = max(0.1, float(interval or 1.0))
        self.pids = list(pids or [])
        process_table = read_proc_process_table()
        self._pid_identities = {
            int(pid): int(process_table[int(pid)]["start_time"])
            for pid in self.pids
            if int(pid) in process_table
        }
        self._monitored_pids_seen: set[int] = set()
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cpu = read_proc_cpu_times()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="db-stress-resource-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1.0, self.interval * 2))
        self.collect()
        return self.summary()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.collect()

    def _cpu_percent(self) -> float | None:
        current = read_proc_cpu_times()
        previous = self._last_cpu
        self._last_cpu = current
        if not current or not previous:
            return None
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0:
            return None
        return round(max(0.0, min(100.0, (1.0 - (idle_delta / total_delta)) * 100.0)), 2)

    def _db_snapshot(self, label: str, path: Path) -> dict[str, Any]:
        def safe_size(target: Path) -> int:
            try:
                return target.stat().st_size
            except FileNotFoundError:
                return 0

        payload: dict[str, Any] = {
            "path": str(path),
            "db_bytes": safe_size(path),
            "wal_bytes": safe_size(path.with_name(path.name + "-wal")),
            "shm_bytes": safe_size(path.with_name(path.name + "-shm")),
        }
        try:
            conn = get_readonly_db(str(path))
            try:
                payload["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
                payload["page_count"] = int(conn.execute("PRAGMA page_count").fetchone()[0])
                payload["freelist_count"] = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
                payload["page_size"] = int(conn.execute("PRAGMA page_size").fetchone()[0])
            finally:
                conn.close()
        except Exception as exc:
            payload["error"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
        return payload

    def collect(self) -> None:
        mem = read_proc_meminfo()
        disk = shutil.disk_usage(self.runtime_root)
        try:
            load = os.getloadavg()
        except Exception:
            load = (0.0, 0.0, 0.0)
        monitored_pids = process_tree_pids(self._pid_identities)
        self._monitored_pids_seen.update(monitored_pids)
        sample = {
            "ts": time.time(),
            "cpu_percent": self._cpu_percent(),
            "load1": round(float(load[0]), 3),
            "load5": round(float(load[1]), 3),
            "load15": round(float(load[2]), 3),
            "mem_total_mb": round(mem.get("MemTotal", 0) / 1024, 2),
            "mem_available_mb": round(mem.get("MemAvailable", 0) / 1024, 2),
            "swap_free_mb": round(mem.get("SwapFree", 0) / 1024, 2),
            "runtime_disk_used_mb": round(disk.used / 1024 / 1024, 2),
            "runtime_disk_free_mb": round(disk.free / 1024 / 1024, 2),
            "monitored_rss_mb": round(sum(read_rss_kb(pid) for pid in monitored_pids) / 1024, 2),
            "monitored_pids": monitored_pids,
            "monitored_pid_count": len(monitored_pids),
            "db": {label: self._db_snapshot(label, path) for label, path in self.paths.items()},
        }
        self.samples.append(sample)

    def summary(self) -> dict[str, Any]:
        samples = list(self.samples)
        cpu_values = [float(item["cpu_percent"]) for item in samples if item.get("cpu_percent") is not None]
        db_peak: dict[str, dict[str, Any]] = {}
        for label in self.paths:
            db_samples = [sample.get("db", {}).get(label, {}) for sample in samples]
            db_peak[label] = {
                "max_db_mb": round(max((float(item.get("db_bytes") or 0) for item in db_samples), default=0.0) / 1024 / 1024, 3),
                "max_wal_mb": round(max((float(item.get("wal_bytes") or 0) for item in db_samples), default=0.0) / 1024 / 1024, 3),
                "max_shm_mb": round(max((float(item.get("shm_bytes") or 0) for item in db_samples), default=0.0) / 1024 / 1024, 3),
                "max_page_count": max((int(item.get("page_count") or 0) for item in db_samples), default=0),
                "max_freelist_count": max((int(item.get("freelist_count") or 0) for item in db_samples), default=0),
                "last": db_samples[-1] if db_samples else {},
            }
        return {
            "sample_count": len(samples),
            "interval_seconds": self.interval,
            "cpu_percent_avg": round(sum(cpu_values) / len(cpu_values), 2) if cpu_values else None,
            "cpu_percent_max": max(cpu_values) if cpu_values else None,
            "load1_max": max((float(item.get("load1") or 0) for item in samples), default=0.0),
            "mem_available_min_mb": min((float(item.get("mem_available_mb") or 0) for item in samples), default=0.0),
            "monitored_rss_max_mb": max((float(item.get("monitored_rss_mb") or 0) for item in samples), default=0.0),
            "monitored_pid_count_max": max((int(item.get("monitored_pid_count") or 0) for item in samples), default=0),
            "monitored_pids_seen": sorted(self._monitored_pids_seen),
            "runtime_disk_free_min_mb": min((float(item.get("runtime_disk_free_mb") or 0) for item in samples), default=0.0),
            "db_peak": db_peak,
            "first_sample": samples[0] if samples else {},
            "last_sample": samples[-1] if samples else {},
        }


def configure_env(runtime_root: Path, *, backend: str, flush_interval: float, event_flush_interval: float) -> dict[str, Path]:
    runtime_root.mkdir(parents=True, exist_ok=True)
    database_dir = runtime_root / "database"
    cache_dir = runtime_root / "job_progress_cache"
    database_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HACKME_RUNTIME_DIR"] = str(runtime_root)
    os.environ["HACKME_JOB_PROGRESS_BACKEND"] = backend
    os.environ["HACKME_PROGRESS_CACHE_DIR"] = str(cache_dir)
    os.environ["HACKME_JOB_PROGRESS_FLUSH_INTERVAL_SECONDS"] = str(flush_interval)
    os.environ["HACKME_JOB_PROGRESS_EVENT_FLUSH_INTERVAL_SECONDS"] = str(event_flush_interval)
    os.environ.setdefault("HACKME_SQLITE_BUSY_TIMEOUT_MS", "30000")
    os.environ.setdefault("HACKME_SQLITE_LOCK_RETRY_ATTEMPTS", "5")
    os.environ.setdefault("HACKME_SQLITE_LOCK_RETRY_BASE_SLEEP", "0.03")
    reset_progress_backend_for_tests()
    reset_job_progress_buffer_for_tests()
    return {
        "main": database_dir / "database.db",
        "auth": database_dir / "auth.db",
        "audit": database_dir / "audit.db",
        "control": database_dir / "control.db",
    }


def init_schemas(paths: dict[str, Path], *, user_count: int = 150) -> None:
    user_count = max(1, int(user_count or 150))
    main = get_db(str(paths["main"]))
    try:
        main.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, role TEXT)")
        main.execute("INSERT OR IGNORE INTO users (id, username, role) VALUES (1, 'dbstress', 'user')")
        main.executemany(
            "INSERT OR IGNORE INTO users (id, username, role) VALUES (?, ?, ?)",
            [(idx, f"stress_user_{idx:04d}", "user") for idx in range(2, user_count + 1)],
        )
        main.execute(
            """
            CREATE TABLE IF NOT EXISTS db_stress_main (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker TEXT NOT NULL,
                seq INTEGER NOT NULL,
                payload TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        main.execute(
            """
            CREATE TABLE IF NOT EXISTS db_stress_user_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                activity_type TEXT NOT NULL,
                payload TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        main.execute(
            """
            CREATE TABLE IF NOT EXISTS db_stress_user_counters (
                user_id INTEGER PRIMARY KEY,
                write_count INTEGER NOT NULL DEFAULT 0,
                read_hint TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        main.execute("CREATE INDEX IF NOT EXISTS idx_db_stress_user_activity_user ON db_stress_user_activity(user_id)")
        main.execute("CREATE INDEX IF NOT EXISTS idx_db_stress_user_activity_updated ON db_stress_user_activity(updated_at)")
        main.execute("CREATE INDEX IF NOT EXISTS idx_db_stress_main_updated ON db_stress_main(updated_at)")
        ensure_job_center_schema(main)
        main.executemany(
            "INSERT OR IGNORE INTO db_stress_user_counters (user_id, write_count, read_hint, updated_at) VALUES (?, 0, '', ?)",
            [(idx, now_iso()) for idx in range(1, user_count + 1)],
        )
        main.commit()
    finally:
        main.close()

    auth = get_auth_db(str(paths["auth"]))
    try:
        auth.execute(
            """
            CREATE TABLE IF NOT EXISTS db_stress_auth (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker TEXT NOT NULL,
                seq INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        auth.execute(
            """
            CREATE TABLE IF NOT EXISTS db_stress_sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                last_seen TEXT NOT NULL,
                payload TEXT
            )
            """
        )
        auth.commit()
    finally:
        auth.close()

    audit = get_audit_db(str(paths["audit"]))
    try:
        ensure_audit_db_schema(audit)
        audit.execute(
            """
            CREATE TABLE IF NOT EXISTS db_stress_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker TEXT NOT NULL,
                seq INTEGER NOT NULL,
                ts TEXT NOT NULL,
                payload TEXT
            )
            """
        )
        audit.execute("CREATE INDEX IF NOT EXISTS idx_db_stress_audit_events_ts ON db_stress_audit_events(ts)")
        audit.commit()
    finally:
        audit.close()

    control = get_control_db(str(paths["control"]))
    try:
        control.execute(
            """
            CREATE TABLE IF NOT EXISTS db_stress_control (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker TEXT NOT NULL,
                seq INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        control.commit()
    finally:
        control.close()


def main_db_writer(paths: dict[str, Path], recorder: Recorder, deadline: float, worker_id: int) -> None:
    conn = get_db(str(paths["main"]))
    try:
        seq = 0
        while time.monotonic() < deadline:
            seq += 1
            def op():
                conn.execute(
                    "INSERT INTO db_stress_main (worker, seq, payload, updated_at) VALUES (?, ?, ?, ?)",
                    (f"main-{worker_id}", seq, json.dumps({"seq": seq}), now_iso()),
                )
                if seq % 3 == 0:
                    conn.execute(
                        "UPDATE db_stress_main SET payload=?, updated_at=? WHERE id=(SELECT MAX(id) FROM db_stress_main)",
                        (json.dumps({"updated": seq}), now_iso()),
                    )
                conn.commit()
            recorder.time_call("main_write", op)
    finally:
        conn.close()


def user_activity_writer(paths: dict[str, Path], recorder: Recorder, deadline: float, worker_id: int, *, user_count: int) -> None:
    conn = get_db(str(paths["main"]))
    try:
        seq = 0
        while time.monotonic() < deadline:
            seq += 1
            user_id = ((seq + worker_id * 17) % max(1, user_count)) + 1
            payload = json.dumps({"seq": seq, "worker": worker_id, "user_id": user_id}, ensure_ascii=False)
            def op():
                conn.execute(
                    "INSERT INTO db_stress_user_activity (user_id, activity_type, payload, updated_at) VALUES (?, ?, ?, ?)",
                    (user_id, "write", payload, now_iso()),
                )
                conn.execute(
                    """
                    INSERT INTO db_stress_user_counters (user_id, write_count, read_hint, updated_at)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        write_count=write_count+1,
                        read_hint=excluded.read_hint,
                        updated_at=excluded.updated_at
                    """,
                    (user_id, f"worker={worker_id},seq={seq}", now_iso()),
                )
                conn.commit()
            recorder.time_call("user_activity_write", op)
    finally:
        conn.close()


def user_profile_reader(paths: dict[str, Path], recorder: Recorder, deadline: float, worker_id: int, *, user_count: int) -> None:
    conn = get_readonly_db(str(paths["main"]))
    try:
        seq = 0
        while time.monotonic() < deadline:
            seq += 1
            user_id = ((seq * 13 + worker_id) % max(1, user_count)) + 1
            recorder.time_call(
                "user_profile_read",
                lambda: conn.execute(
                    """
                    SELECT u.id, u.username, c.write_count,
                           (SELECT COUNT(*) FROM db_stress_user_activity a WHERE a.user_id=u.id) AS activity_count
                    FROM users u
                    LEFT JOIN db_stress_user_counters c ON c.user_id=u.id
                    WHERE u.id=?
                    """,
                    (user_id,),
                ).fetchone(),
            )
            if seq % 20 == 0:
                recorder.time_call(
                    "user_leaderboard_read",
                    lambda: conn.execute(
                        """
                        SELECT u.id, u.username, c.write_count
                        FROM db_stress_user_counters c
                        JOIN users u ON u.id=c.user_id
                        ORDER BY c.write_count DESC, u.id ASC
                        LIMIT 20
                        """
                    ).fetchall(),
                )
    finally:
        conn.close()


def online_user_worker(
    paths: dict[str, Path],
    recorder: Recorder,
    deadline: float,
    user_id: int,
    *,
    write_every: int = 3,
    session_touch_every: int = 2,
) -> None:
    read_conn = get_readonly_db(str(paths["main"]))
    write_conn = get_db(str(paths["main"]))
    auth_conn = get_auth_db(str(paths["auth"]))
    try:
        seq = 0
        token = f"online-{user_id}"
        while time.monotonic() < deadline:
            seq += 1
            recorder.time_call(
                "online_profile_read",
                lambda: read_conn.execute(
                    """
                    SELECT u.id, u.username, c.write_count, c.read_hint
                    FROM users u
                    LEFT JOIN db_stress_user_counters c ON c.user_id=u.id
                    WHERE u.id=?
                    """,
                    (user_id,),
                ).fetchone(),
            )
            if seq % max(1, int(write_every)) == 0:
                payload = json.dumps({"user_id": user_id, "seq": seq}, ensure_ascii=False)

                def write_activity():
                    write_conn.execute(
                        "INSERT INTO db_stress_user_activity (user_id, activity_type, payload, updated_at) VALUES (?, ?, ?, ?)",
                        (user_id, "online_tick", payload, now_iso()),
                    )
                    write_conn.execute(
                        """
                        INSERT INTO db_stress_user_counters (user_id, write_count, read_hint, updated_at)
                        VALUES (?, 1, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            write_count=write_count+1,
                            read_hint=excluded.read_hint,
                            updated_at=excluded.updated_at
                        """,
                        (user_id, f"online seq={seq}", now_iso()),
                    )
                    write_conn.commit()

                recorder.time_call("online_activity_write", write_activity)
            if int(session_touch_every or 0) > 0 and seq % int(session_touch_every) == 0:
                def session_touch():
                    auth_conn.execute(
                        """
                        INSERT INTO db_stress_sessions (token, user_id, last_seen, payload)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(token) DO UPDATE SET
                            last_seen=excluded.last_seen,
                            payload=excluded.payload
                        """,
                        (token, user_id, now_iso(), json.dumps({"seq": seq}, ensure_ascii=False)),
                    )
                    auth_conn.commit()

                recorder.time_call("online_session_touch", session_touch)
            if seq % 12 == 0:
                recorder.time_call(
                    "online_leaderboard_read",
                    lambda: read_conn.execute(
                        """
                        SELECT user_id, write_count
                        FROM db_stress_user_counters
                        ORDER BY write_count DESC, user_id ASC
                        LIMIT 20
                        """
                    ).fetchall(),
                )
    finally:
        read_conn.close()
        write_conn.close()
        auth_conn.close()


def job_progress_writer(paths: dict[str, Path], recorder: Recorder, deadline: float, worker_id: int) -> None:
    conn = get_db(str(paths["main"]))
    try:
        job = create_job(
            conn,
            owner_user_id=1,
            created_by_user_id=1,
            job_type="db.stress.job_progress",
            title=f"DB stress job {worker_id}",
            source_module="db_stress",
            source_ref=f"thread:{worker_id}:{time.time_ns()}",
            status="running",
            progress_percent=0,
            stage="running",
            stage_detail="stress start",
            cancellable=True,
        )
        conn.commit()
        seq = 0
        while time.monotonic() < deadline:
            seq += 1
            percent = seq % 100
            def op():
                update_job(
                    conn,
                    job["job_uuid"],
                    status="running",
                    progress_percent=percent,
                    stage="stress",
                    stage_detail=f"progress {seq}",
                    metadata_json={"seq": seq, "worker": worker_id},
                    defer_progress=True,
                )
                add_job_event(
                    conn,
                    job["job_uuid"],
                    event_type="progress",
                    stage="stress",
                    message=f"progress {seq}",
                    progress_percent=percent,
                    payload={"seq": seq},
                    defer_progress=True,
                )
                conn.commit()
            recorder.time_call("job_progress", op)
            if seq % 25 == 0:
                recorder.time_call("job_list", lambda: list_jobs(conn, user_id=1, limit=20))
        update_job(
            conn,
            job["job_uuid"],
            status="succeeded",
            progress_percent=100,
            stage="done",
            stage_detail="stress done",
            finished_at=now_iso(),
        )
        add_job_event(conn, job["job_uuid"], event_type="completed", stage="done", message="stress done", progress_percent=100)
        conn.commit()
    finally:
        conn.close()


def auth_writer(paths: dict[str, Path], recorder: Recorder, deadline: float, worker_id: int) -> None:
    conn = get_auth_db(str(paths["auth"]))
    try:
        seq = 0
        while time.monotonic() < deadline:
            seq += 1
            recorder.time_call(
                "auth_write",
                lambda: (
                    conn.execute(
                        "INSERT INTO db_stress_auth (worker, seq, updated_at) VALUES (?, ?, ?)",
                        (f"auth-{worker_id}", seq, now_iso()),
                    ),
                    conn.commit(),
                ),
            )
    finally:
        conn.close()


def session_writer(paths: dict[str, Path], recorder: Recorder, deadline: float, worker_id: int, *, user_count: int) -> None:
    conn = get_auth_db(str(paths["auth"]))
    try:
        seq = 0
        while time.monotonic() < deadline:
            seq += 1
            user_id = ((seq + worker_id * 31) % max(1, user_count)) + 1
            token = f"stress-{worker_id}-{user_id}-{seq % 30}"
            payload = json.dumps({"worker": worker_id, "seq": seq, "user_id": user_id}, ensure_ascii=False)
            def op():
                conn.execute(
                    """
                    INSERT INTO db_stress_sessions (token, user_id, last_seen, payload)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(token) DO UPDATE SET
                        user_id=excluded.user_id,
                        last_seen=excluded.last_seen,
                        payload=excluded.payload
                    """,
                    (token, user_id, now_iso(), payload),
                )
                conn.commit()
            recorder.time_call("session_write", op)
    finally:
        conn.close()


def audit_writer(paths: dict[str, Path], recorder: Recorder, deadline: float, worker_id: int) -> None:
    conn = get_audit_db(str(paths["audit"]))
    try:
        seq = 0
        while time.monotonic() < deadline:
            seq += 1
            now = now_iso()
            payload = json.dumps(
                {
                    "action": "DB_STRESS",
                    "worker_id": worker_id,
                    "seq": seq,
                    "ip": "127.0.0.1",
                    "ua": "db-stress",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            recorder.time_call(
                "audit_write",
                lambda: (
                    conn.execute(
                        """
                        INSERT INTO db_stress_audit_events (worker, seq, ts, payload)
                        VALUES (?, ?, ?, ?)
                        """,
                        (f"audit-{worker_id}", seq, now, payload),
                    ),
                    conn.commit(),
                ),
            )
    finally:
        conn.close()


def control_writer(paths: dict[str, Path], recorder: Recorder, deadline: float, worker_id: int) -> None:
    conn = get_control_db(str(paths["control"]))
    try:
        seq = 0
        while time.monotonic() < deadline:
            seq += 1
            recorder.time_call(
                "control_write",
                lambda: (
                    conn.execute(
                        "INSERT INTO db_stress_control (worker, seq, updated_at) VALUES (?, ?, ?)",
                        (f"control-{worker_id}", seq, now_iso()),
                    ),
                    conn.commit(),
                ),
            )
    finally:
        conn.close()


def mixed_reader(paths: dict[str, Path], recorder: Recorder, deadline: float, worker_id: int) -> None:
    conns = {
        "main": get_readonly_db(str(paths["main"])),
        "auth": get_readonly_auth_db(str(paths["auth"])),
        "audit": get_readonly_audit_db(str(paths["audit"])),
        "control": get_readonly_control_db(str(paths["control"])),
    }
    try:
        while time.monotonic() < deadline:
            recorder.time_call("mixed_read", lambda: conns["main"].execute("SELECT COUNT(*) AS c FROM db_stress_main").fetchone()["c"])
            recorder.time_call("mixed_read", lambda: conns["auth"].execute("SELECT COUNT(*) AS c FROM db_stress_auth").fetchone()["c"])
            recorder.time_call("mixed_read", lambda: conns["audit"].execute("SELECT COUNT(*) AS c FROM db_stress_audit_events").fetchone()["c"])
            recorder.time_call("mixed_read", lambda: conns["control"].execute("SELECT COUNT(*) AS c FROM db_stress_control").fetchone()["c"])
            if worker_id % 2 == 0:
                recorder.time_call("job_list", lambda: list_jobs(conns["main"], user_id=1, limit=10))
    finally:
        for conn in conns.values():
            conn.close()


def lock_contention(paths: dict[str, Path], recorder: Recorder, deadline: float) -> None:
    conn = get_db(str(paths["main"]))
    try:
        seq = 0
        while time.monotonic() < deadline:
            seq += 1
            def op():
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO db_stress_main (worker, seq, payload, updated_at) VALUES (?, ?, ?, ?)",
                    ("lock-holder", seq, "held", now_iso()),
                )
                time.sleep(0.08)
                conn.commit()
            recorder.time_call("lock_contention", op)
            time.sleep(0.02)
    finally:
        conn.close()


def child_job_progress(args: argparse.Namespace) -> int:
    paths = configure_env(
        Path(args.runtime_root),
        backend=args.backend,
        flush_interval=args.flush_interval,
        event_flush_interval=args.event_flush_interval,
    )
    if not args.child_skip_init:
        init_schemas(paths, user_count=int(args.user_count))
    recorder = Recorder()
    deadline = time.monotonic() + float(args.duration)
    job_progress_writer(paths, recorder, deadline, int(args.child_id or 0))
    print(json.dumps({"ok": recorder.summary()["error_count"] == 0, "summary": recorder.summary()}, ensure_ascii=False))
    return 0 if recorder.summary()["error_count"] == 0 else 1


def final_counts(paths: dict[str, Path]) -> dict[str, int]:
    main = get_db(str(paths["main"]))
    auth = get_auth_db(str(paths["auth"]))
    audit = get_audit_db(str(paths["audit"]))
    control = get_control_db(str(paths["control"]))
    try:
        return {
            "main_rows": int(main.execute("SELECT COUNT(*) AS c FROM db_stress_main").fetchone()["c"]),
            "users": int(main.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]),
            "user_activity_rows": int(main.execute("SELECT COUNT(*) AS c FROM db_stress_user_activity").fetchone()["c"]),
            "jobs": int(main.execute("SELECT COUNT(*) AS c FROM job_center_jobs").fetchone()["c"]),
            "job_events": int(main.execute("SELECT COUNT(*) AS c FROM job_center_events").fetchone()["c"]),
            "auth_rows": int(auth.execute("SELECT COUNT(*) AS c FROM db_stress_auth").fetchone()["c"]),
            "session_rows": int(auth.execute("SELECT COUNT(*) AS c FROM db_stress_sessions").fetchone()["c"]),
            "audit_stress_rows": int(audit.execute("SELECT COUNT(*) AS c FROM db_stress_audit_events").fetchone()["c"]),
            "secure_audit_rows": int(audit.execute("SELECT COUNT(*) AS c FROM secure_audit").fetchone()["c"]),
            "control_rows": int(control.execute("SELECT COUNT(*) AS c FROM db_stress_control").fetchone()["c"]),
        }
    finally:
        main.close()
        auth.close()
        audit.close()
        control.close()


def run_stress(args: argparse.Namespace) -> int:
    runtime_root = Path(args.runtime_root or tempfile.mkdtemp(prefix="hackme-db-stress-b-"))
    paths = configure_env(
        runtime_root,
        backend=args.backend,
        flush_interval=args.flush_interval,
        event_flush_interval=args.event_flush_interval,
    )
    init_schemas(paths, user_count=int(args.user_count))
    recorder = Recorder()
    deadline = time.monotonic() + float(args.duration)

    children: list[subprocess.Popen] = []
    for idx in range(max(0, int(args.external_workers))):
        children.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child-job-progress",
                    "--runtime-root",
                    str(runtime_root),
                    "--duration",
                    str(max(1.0, float(args.duration) - 0.5)),
                    "--backend",
                    args.backend,
                    "--flush-interval",
                    str(args.flush_interval),
                    "--event-flush-interval",
                    str(args.event_flush_interval),
                    "--user-count",
                    str(args.user_count),
                    "--child-id",
                    str(1000 + idx),
                    "--child-skip-init",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    monitor = None
    if not args.no_monitor:
        monitor = ResourceMonitor(
            runtime_root=runtime_root,
            paths=paths,
            interval=float(args.monitor_interval),
            pids=[os.getpid(), *[child.pid for child in children if child.pid]],
        )
        monitor.start()

    tasks = []
    for idx in range(max(0, int(args.online_users))):
        user_id = (idx % max(1, int(args.user_count))) + 1
        tasks.append((
            online_user_worker,
            (paths, recorder, deadline, user_id),
            {
                "write_every": int(args.online_write_every),
                "session_touch_every": int(args.online_session_touch_every),
            },
        ))
    for idx in range(max(1, int(args.main_writers))):
        tasks.append((main_db_writer, (paths, recorder, deadline, idx), {}))
    for idx in range(max(0, int(args.user_writers))):
        tasks.append((user_activity_writer, (paths, recorder, deadline, idx), {"user_count": int(args.user_count)}))
    for idx in range(max(0, int(args.user_readers))):
        tasks.append((user_profile_reader, (paths, recorder, deadline, idx), {"user_count": int(args.user_count)}))
    for idx in range(max(1, int(args.job_writers))):
        tasks.append((job_progress_writer, (paths, recorder, deadline, idx), {}))
    for idx in range(max(1, int(args.auth_writers))):
        tasks.append((auth_writer, (paths, recorder, deadline, idx), {}))
    for idx in range(max(0, int(args.session_writers))):
        tasks.append((session_writer, (paths, recorder, deadline, idx), {"user_count": int(args.user_count)}))
    for idx in range(max(1, int(args.audit_writers))):
        tasks.append((audit_writer, (paths, recorder, deadline, idx), {}))
    for idx in range(max(1, int(args.control_writers))):
        tasks.append((control_writer, (paths, recorder, deadline, idx), {}))
    for idx in range(max(1, int(args.readers))):
        tasks.append((mixed_reader, (paths, recorder, deadline, idx), {}))
    if args.lock_contention:
        tasks.append((lock_contention, (paths, recorder, deadline), {}))

    start_event = threading.Event()
    deadline_ref = {"value": 0.0}
    futures = []
    max_workers = max(int(args.threads), len(tasks))

    def run_task(fn, fn_args, fn_kwargs):
        start_event.wait()
        args_list = list(fn_args)
        if len(args_list) >= 3:
            args_list[2] = deadline_ref["value"]
        return fn(*args_list, **fn_kwargs)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for fn, fn_args, fn_kwargs in tasks:
            futures.append(executor.submit(run_task, fn, fn_args, fn_kwargs))
        deadline_ref["value"] = time.monotonic() + float(args.duration)
        start_event.set()
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                recorder.error("future", exc)

    child_results = []
    for child in children:
        stdout, stderr = child.communicate(timeout=max(10.0, float(args.duration) + 5.0))
        child_results.append({"returncode": child.returncode, "stdout": stdout[-2000:], "stderr": stderr[-2000:]})
        if child.returncode != 0:
            recorder.error("external_worker", RuntimeError(stderr.strip() or stdout.strip() or f"exit={child.returncode}"))
    resource_summary = monitor.stop() if monitor else {"disabled": True}

    summary = recorder.summary()
    counts = final_counts(paths)
    ok = summary["error_count"] == 0 and summary["lock_error_count"] == 0 and all(child.get("returncode") == 0 for child in child_results)
    payload = {
        "ok": ok,
        "profile": "db_stress_b",
        "runtime_root": str(runtime_root),
        "backend": job_progress_buffer_status(),
        "duration_seconds": float(args.duration),
        "logical_online_users": int(args.online_users),
        "resource_monitor": resource_summary,
        "counts": counts,
        "summary": summary,
        "external_workers": child_results,
    }
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if ok else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run multi-angle SQLite/Job Center stress profile B.")
    parser.add_argument("--runtime-root", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--threads", type=int, default=12)
    parser.add_argument("--backend", default="file", choices=["memory", "file", "redis", "auto"])
    parser.add_argument("--flush-interval", type=float, default=1.5)
    parser.add_argument("--event-flush-interval", type=float, default=5.0)
    parser.add_argument("--monitor-interval", type=float, default=1.0)
    parser.add_argument("--no-monitor", action="store_true")
    parser.add_argument("--user-count", type=int, default=150)
    parser.add_argument("--online-users", type=int, default=0)
    parser.add_argument("--online-write-every", type=int, default=3)
    parser.add_argument("--online-session-touch-every", type=int, default=2)
    parser.add_argument("--main-writers", type=int, default=2)
    parser.add_argument("--user-writers", type=int, default=4)
    parser.add_argument("--user-readers", type=int, default=4)
    parser.add_argument("--job-writers", type=int, default=3)
    parser.add_argument("--auth-writers", type=int, default=2)
    parser.add_argument("--session-writers", type=int, default=4)
    parser.add_argument("--audit-writers", type=int, default=2)
    parser.add_argument("--control-writers", type=int, default=1)
    parser.add_argument("--readers", type=int, default=3)
    parser.add_argument("--external-workers", type=int, default=2)
    parser.add_argument("--lock-contention", action="store_true")
    parser.add_argument("--child-job-progress", action="store_true")
    parser.add_argument("--child-skip-init", action="store_true")
    parser.add_argument("--child-id", type=int, default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.child_job_progress:
        return child_job_progress(args)
    return run_stress(args)


if __name__ == "__main__":
    raise SystemExit(main())
