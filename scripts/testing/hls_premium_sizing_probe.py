#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.testing.hls_worker_slot_probe import (  # noqa: E402
    close_proc_log,
    db_connect,
    init_db,
    load_job,
    seed_video,
    start_worker,
    wait_proc,
)
from scripts.testing.realtime_proxy_stress_probe import generate_multiaudio_fixture  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


def utc_ms() -> int:
    return int(time.time() * 1000)


def ps_snapshot(runtime_marker: str) -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,ppid,pcpu,pmem,rss,nlwp,stat,comm,args"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        pid, ppid, pcpu, pmem, rss, nlwp, stat, comm, args = parts
        if runtime_marker not in args:
            continue
        try:
            rows.append({
                "pid": int(pid),
                "ppid": int(ppid),
                "cpu_percent": float(pcpu),
                "mem_percent": float(pmem),
                "rss_kb": int(rss),
                "threads": int(nlwp),
                "stat": stat,
                "comm": comm,
                "args": args[:500],
            })
        except Exception:
            continue
    return rows


class ResourceSampler:
    def __init__(self, *, runtime_marker: str, interval: float) -> None:
        self.runtime_marker = str(runtime_marker)
        self.interval = max(0.1, float(interval or 0.25))
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hls-sizing-resource-sampler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.interval * 4))
        return summarize_resource_samples(self.samples)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.samples.append({
                "t_ms": utc_ms(),
                "processes": ps_snapshot(self.runtime_marker),
            })
            self._stop.wait(self.interval)


def summarize_resource_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    total_cpu_values: list[float] = []
    total_rss_values: list[int] = []
    max_per_process_rss_kb = 0
    max_threads = 0
    ffmpeg_process_peak = 0
    worker_process_peak = 0
    ffmpeg_sample_count = 0
    worker_sample_count = 0
    for sample in samples:
        processes = sample.get("processes") or []
        total_cpu = sum(float(proc.get("cpu_percent") or 0.0) for proc in processes)
        total_rss = sum(int(proc.get("rss_kb") or 0) for proc in processes)
        total_cpu_values.append(total_cpu)
        total_rss_values.append(total_rss)
        ffmpeg_count = 0
        worker_count = 0
        for proc in processes:
            max_per_process_rss_kb = max(max_per_process_rss_kb, int(proc.get("rss_kb") or 0))
            max_threads = max(max_threads, int(proc.get("threads") or 0))
            comm = str(proc.get("comm") or "")
            args = str(proc.get("args") or "")
            if comm == "ffmpeg":
                ffmpeg_count += 1
                ffmpeg_sample_count += 1
            if "hls_prepare_worker.py" in args:
                worker_count += 1
                worker_sample_count += 1
        ffmpeg_process_peak = max(ffmpeg_process_peak, ffmpeg_count)
        worker_process_peak = max(worker_process_peak, worker_count)
    avg_cpu = sum(total_cpu_values) / len(total_cpu_values) if total_cpu_values else 0.0
    return {
        "samples": len(samples),
        "total_cpu_peak_percent": round(max(total_cpu_values or [0.0]), 2),
        "total_cpu_avg_percent": round(avg_cpu, 2),
        "total_rss_peak_mb": round(max(total_rss_values or [0]) / 1024.0, 2),
        "max_per_process_rss_mb": round(max_per_process_rss_kb / 1024.0, 2),
        "max_threads_seen_per_process": max_threads,
        "ffmpeg_process_peak": ffmpeg_process_peak,
        "hls_worker_process_peak": worker_process_peak,
        "ffmpeg_process_sample_count": ffmpeg_sample_count,
        "hls_worker_process_sample_count": worker_sample_count,
    }


def directory_stats(path: Path) -> dict[str, Any]:
    total = 0
    files = 0
    suffix_counts: dict[str, int] = {}
    if path.exists():
        for item in path.rglob("*"):
            if not item.is_file():
                continue
            files += 1
            total += int(item.stat().st_size)
            suffix = item.suffix.lower() or "(none)"
            suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1
    return {
        "path": str(path),
        "exists": path.exists(),
        "files": files,
        "bytes": total,
        "mb": round(total / 1024 / 1024, 3),
        "suffix_counts": suffix_counts,
    }


def db_asset_summary(db_path: Path, file_ids: list[str]) -> dict[str, Any]:
    conn = db_connect(db_path)
    try:
        placeholders = ",".join("?" for _ in file_ids)
        assets = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT id, uploaded_file_id, status, source_size_bytes,
                       duration_seconds, master_manifest_path, error_message
                FROM media_stream_assets
                WHERE uploaded_file_id IN ({placeholders})
                ORDER BY uploaded_file_id
                """,
                tuple(file_ids),
            ).fetchall()
        ]
        variants = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT a.uploaded_file_id, v.name, v.media_kind, v.width, v.height,
                       v.bitrate, v.codec, COUNT(s.id) AS segments,
                       COALESCE(SUM(s.byte_size), 0) AS segment_bytes
                FROM media_stream_variants v
                JOIN media_stream_assets a ON a.id=v.asset_id
                LEFT JOIN media_stream_segments s ON s.variant_id=v.id
                WHERE a.uploaded_file_id IN ({placeholders})
                GROUP BY v.id
                ORDER BY a.uploaded_file_id, v.id
                """,
                tuple(file_ids),
            ).fetchall()
        ]
        subtitles = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT a.uploaded_file_id, st.name, st.language, st.codec, st.is_forced
                FROM media_stream_subtitles st
                JOIN media_stream_assets a ON a.id=st.asset_id
                WHERE a.uploaded_file_id IN ({placeholders})
                ORDER BY a.uploaded_file_id, st.id
                """,
                tuple(file_ids),
            ).fetchall()
        ]
        return {
            "assets": assets,
            "variants": variants,
            "subtitles": subtitles,
        }
    finally:
        conn.close()


def mem_available_mb() -> float:
    path = Path("/proc/meminfo")
    if not path.exists():
        return 0.0
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("MemAvailable:"):
                return float(line.split()[1]) / 1024.0
    except Exception:
        return 0.0
    return 0.0


def build_sizing_recommendation(
    *,
    args: argparse.Namespace,
    resource_summary: dict[str, Any],
    source_bytes: int,
    derivative_bytes: int,
) -> dict[str, Any]:
    cpu_count = max(1, int(os.cpu_count() or 1))
    active_slots = max(1, min(int(args.jobs), int(args.max_concurrent)))
    peak_cpu = max(0.0, float(resource_summary.get("total_cpu_peak_percent") or 0.0))
    peak_rss_mb = max(0.0, float(resource_summary.get("total_rss_peak_mb") or 0.0))
    per_slot_cpu_peak = peak_cpu / active_slots if peak_cpu > 0 else 0.0
    per_slot_rss_mb = peak_rss_mb / active_slots if peak_rss_mb > 0 else 0.0
    cpu_budget_percent = cpu_count * 100.0 * 0.65
    cpu_floor = max(per_slot_cpu_peak, 35.0)
    cpu_limited_slots = max(1, int(cpu_budget_percent // cpu_floor))
    available_mb = mem_available_mb()
    if available_mb > 0 and per_slot_rss_mb > 0:
        mem_limited_slots = max(1, int((available_mb * 0.35) // max(per_slot_rss_mb, 1.0)))
    else:
        mem_limited_slots = 4
    suggested = max(1, min(4, cpu_limited_slots, mem_limited_slots))
    derivative_multiplier = round(float(derivative_bytes) / float(source_bytes), 3) if source_bytes > 0 else 0.0
    return {
        "cpu_count": cpu_count,
        "active_slots_assumed": active_slots,
        "cpu_budget_percent": round(cpu_budget_percent, 2),
        "per_slot_cpu_peak_percent": round(per_slot_cpu_peak, 2),
        "per_slot_rss_mb": round(per_slot_rss_mb, 2),
        "mem_available_mb": round(available_mb, 2),
        "cpu_limited_slots": cpu_limited_slots,
        "mem_limited_slots": mem_limited_slots,
        "suggested_hls_max_concurrent": suggested,
        "source_bytes": int(source_bytes),
        "derivative_bytes": int(derivative_bytes),
        "derivative_to_source_multiplier": derivative_multiplier,
        "notes": [
            "This is a conservative host-level suggestion capped at 4 slots.",
            "Run again with a production-sized source before raising Premium concurrency.",
        ],
    }


def worker_slot_wait_ms(worker: dict[str, Any]) -> float:
    try:
        return float((((worker.get("job") or {}).get("result") or {}).get("worker_slot") or {}).get("wait_ms") or 0.0)
    except Exception:
        return 0.0


def summarize_worker_queue(workers: list[dict[str, Any]], *, max_concurrent: int) -> dict[str, Any]:
    waits = [worker_slot_wait_ms(worker) for worker in workers]
    queued = [wait for wait in waits if wait >= 1000.0]
    return {
        "workers": len(workers),
        "max_concurrent": int(max_concurrent),
        "max_wait_ms": round(max(waits or [0.0]), 3),
        "waited_workers": len(queued),
        "expected_queue": len(workers) > int(max_concurrent),
        "expected_queue_observed": bool(len(workers) <= int(max_concurrent) or queued),
        "wait_ms_by_file": {
            str(worker.get("file_id") or ""): worker_slot_wait_ms(worker)
            for worker in workers
        },
    }


def build_fixture(args: argparse.Namespace, *, fixture_dir: Path, ffmpeg_bin: str) -> dict[str, Any]:
    source_video = str(args.source_video or "").strip()
    if not source_video:
        fixture_path = fixture_dir / "premium-sizing-source.mkv"
        return generate_multiaudio_fixture(
            fixture_path,
            ffmpeg_bin=ffmpeg_bin,
            duration=float(args.duration),
            size=str(args.fixture_size),
            rate=int(args.fixture_rate),
            video_bitrate=str(args.video_bitrate or ""),
        )
    source_path = Path(source_video).expanduser()
    if not source_path.exists():
        raise FileNotFoundError(f"source video not found: {source_path}")
    trim_seconds = max(0.0, float(args.source_trim_seconds or 0.0))
    if trim_seconds <= 0:
        return {
            "path": str(source_path),
            "bytes": source_path.stat().st_size,
            "source": "source_video",
            "trim_seconds": 0.0,
        }
    fixture_path = fixture_dir / f"premium-sizing-source-{int(trim_seconds)}s.mkv"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-t",
        f"{trim_seconds:.3f}",
        "-i",
        str(source_path),
        "-map",
        "0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(fixture_path),
    ]
    started = time.monotonic()
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=max(60, int(trim_seconds) + 30))
    return {
        "path": str(fixture_path),
        "bytes": fixture_path.stat().st_size,
        "source": "source_video_trim",
        "source_path": str(source_path),
        "trim_seconds": trim_seconds,
        "generate_ms": round((time.monotonic() - started) * 1000, 3),
    }


def wait_all(workers: list[dict[str, Any]], timeout_seconds: float) -> None:
    for worker in workers:
        proc = worker.get("proc")
        if proc is None:
            continue
        try:
            rc = wait_proc(proc, timeout_seconds)
            worker["returncode"] = rc
        except subprocess.TimeoutExpired:
            proc.kill()
            worker["returncode"] = -9
            worker["timeout"] = True
        finally:
            worker["finished_perf"] = time.perf_counter()
            worker["wall_ms"] = round((float(worker["finished_perf"]) - float(worker["started_perf"])) * 1000, 3)


def write_reports(result: dict[str, Any], json_path: Path) -> tuple[Path, Path]:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path = json_path.with_suffix(".md")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# Premium HLS Sizing Probe",
        "",
        f"- OK: `{result.get('ok')}`",
        f"- Runtime root: `{result.get('runtime_root')}`",
        f"- Jobs: `{result.get('jobs')}`",
        f"- Max concurrent: `{result.get('max_concurrent')}`",
        f"- Quality heights: `{result.get('hls_quality_heights')}`",
        f"- HLS profile: `{result.get('hls_profile') or 'default'}`",
        f"- Original variant mode: `{result.get('hls_original_variant_mode') or 'default'}`",
        f"- HLS audio bitrate: `{result.get('hls_audio_bitrate') or 'default'}`",
        "",
        "## Checks",
        "",
    ]
    for key, value in (result.get("checks") or {}).items():
        lines.append(f"- {key}: `{value}`")
    resource_summary = result.get("resource_summary") or {}
    recommendation = result.get("sizing_recommendation") or {}
    queue_summary = result.get("queue_summary") or {}
    lines.extend([
        "",
        "## Resource Summary",
        "",
        f"- CPU peak percent: `{resource_summary.get('total_cpu_peak_percent')}`",
        f"- CPU avg percent: `{resource_summary.get('total_cpu_avg_percent')}`",
        f"- Total RSS peak MB: `{resource_summary.get('total_rss_peak_mb')}`",
        f"- ffmpeg process peak: `{resource_summary.get('ffmpeg_process_peak')}`",
        f"- HLS worker process peak: `{resource_summary.get('hls_worker_process_peak')}`",
        "",
        "## Recommendation",
        "",
        f"- Suggested `HACKME_MEDIA_HLS_MAX_CONCURRENT`: `{recommendation.get('suggested_hls_max_concurrent')}`",
        f"- Per-slot CPU peak percent: `{recommendation.get('per_slot_cpu_peak_percent')}`",
        f"- Per-slot RSS MB: `{recommendation.get('per_slot_rss_mb')}`",
        f"- Derivative/source multiplier: `{recommendation.get('derivative_to_source_multiplier')}`",
        "",
        "## Queue",
        "",
        f"- Expected queue: `{queue_summary.get('expected_queue')}`",
        f"- Expected queue observed: `{queue_summary.get('expected_queue_observed')}`",
        f"- Waited workers: `{queue_summary.get('waited_workers')}`",
        f"- Max wait ms: `{queue_summary.get('max_wait_ms')}`",
        "",
        "## Workers",
        "",
    ])
    for worker in result.get("workers") or []:
        job = worker.get("job") or {}
        result_json = job.get("result") or {}
        lines.append(
            "- "
            f"{worker.get('file_id')}: rc=`{worker.get('returncode')}`, "
            f"wall_ms=`{worker.get('wall_ms')}`, "
            f"job_status=`{job.get('status')}`, "
            f"slot_wait_ms=`{((result_json.get('worker_slot') or {}).get('wait_ms'))}`, "
            f"worker_metrics=`{result_json.get('worker_metrics')}`"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    args.jobs = max(1, min(16, int(args.jobs)))
    args.max_concurrent = max(1, min(16, int(args.max_concurrent)))
    runtime_root = Path(args.runtime_root).resolve()
    report_dir = runtime_root / "reports" / "qa"
    storage_root = runtime_root / "storage"
    db_path = runtime_root / "database" / "database.db"
    fixture_dir = report_dir / "fixtures"
    report_dir.mkdir(parents=True, exist_ok=True)
    storage_root.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    json_path = Path(args.json_out).resolve() if args.json_out else report_dir / "hls_premium_sizing_probe.json"

    ffmpeg_bin = shutil.which(args.ffmpeg_bin) or args.ffmpeg_bin
    ffprobe_bin = shutil.which(args.ffprobe_bin) or args.ffprobe_bin
    if not ffmpeg_bin or not ffprobe_bin:
        result = {
            "ok": False,
            "error": "ffmpeg_or_ffprobe_missing",
            "runtime_root": str(runtime_root),
        }
        write_reports(result, json_path)
        return result

    fixture = build_fixture(args, fixture_dir=fixture_dir, ffmpeg_bin=ffmpeg_bin)
    fixture_path = Path(str(fixture.get("path") or ""))
    file_ids: list[str] = []
    video_ids: dict[str, int] = {}
    conn = db_connect(db_path)
    try:
        for index in range(1, int(args.jobs) + 1):
            file_id = f"premium-sizing-{index}-{uuid.uuid4().hex[:8]}"
            video_id = seed_video(
                conn,
                storage_root,
                file_id=file_id,
                filename=f"premium-sizing-{index}.mkv",
                source_path=fixture_path,
            )
            file_ids.append(file_id)
            video_ids[file_id] = int(video_id)
        conn.commit()
    finally:
        conn.close()

    result: dict[str, Any] = {
        "ok": False,
        "created_at": utc_now(),
        "runtime_root": str(runtime_root),
        "db_path": str(db_path),
        "storage_root": str(storage_root),
        "jobs": int(args.jobs),
        "max_concurrent": int(args.max_concurrent),
        "hls_quality_heights": str(args.hls_quality_heights),
        "hls_original_variant_mode": str(args.hls_original_variant_mode or ""),
        "hls_profile": str(args.hls_profile or ""),
        "hls_audio_bitrate": str(args.hls_audio_bitrate or ""),
        "hold_seconds": float(args.hold_seconds),
        "fixture": fixture,
        "checks": {},
        "workers": [],
    }
    workers: list[dict[str, Any]] = []
    sampler = ResourceSampler(runtime_marker=str(runtime_root), interval=float(args.sample_interval))
    try:
        sampler.start()
        for index, file_id in enumerate(file_ids, start=1):
            log_path = report_dir / f"hls_premium_sizing_{index}.log"
            proc = start_worker(
                args,
                db_path=db_path,
                storage_root=storage_root,
                file_id=file_id,
                video_id=video_ids[file_id],
                title=f"Premium Sizing {index}",
                log_path=log_path,
            )
            workers.append({
                "file_id": file_id,
                "video_id": video_ids[file_id],
                "log": str(log_path),
                "pid": proc.pid,
                "proc": proc,
                "started_perf": time.perf_counter(),
            })
            if float(args.launch_gap) > 0:
                time.sleep(float(args.launch_gap))
        wait_all(workers, float(args.worker_timeout))
    except Exception as exc:
        result["error"] = exc.__class__.__name__
        result["message"] = str(exc)
    finally:
        resource_summary = sampler.stop()
        for worker in workers:
            proc = worker.get("proc")
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            close_proc_log(proc)
            worker.pop("proc", None)

    asset_db = db_asset_summary(db_path, file_ids)
    total_derivative_bytes = 0
    derivative_dirs: dict[str, dict[str, Any]] = {}
    for file_id in file_ids:
        stats = directory_stats(storage_root / "media_derivatives" / file_id)
        derivative_dirs[file_id] = stats
        total_derivative_bytes += int(stats.get("bytes") or 0)
    for worker in workers:
        file_id = str(worker.get("file_id") or "")
        worker["job"] = load_job(db_path, file_id)
        worker["derivative_dir"] = derivative_dirs.get(file_id, {})
    result["workers"] = workers
    result["resource_summary"] = resource_summary
    result["asset_db"] = asset_db
    result["derivative_dirs"] = derivative_dirs
    result["queue_summary"] = summarize_worker_queue(workers, max_concurrent=int(args.max_concurrent))
    result["sizing_recommendation"] = build_sizing_recommendation(
        args=args,
        resource_summary=resource_summary,
        source_bytes=int(fixture.get("bytes") or 0),
        derivative_bytes=total_derivative_bytes,
    )
    checks = {
        "all_workers_zero_rc": all(int(worker.get("returncode") or 0) == 0 for worker in workers),
        "all_jobs_succeeded": all(((worker.get("job") or {}).get("status") == "succeeded") for worker in workers),
        "derivatives_created": all(int((worker.get("derivative_dir") or {}).get("bytes") or 0) > 0 for worker in workers),
        "resource_samples_present": int(resource_summary.get("samples") or 0) > 0,
        "ffmpeg_observed": int(resource_summary.get("ffmpeg_process_sample_count") or 0) > 0,
        "worker_results_have_metrics": all(bool((((worker.get("job") or {}).get("result") or {}).get("worker_metrics") or {})) for worker in workers),
        "worker_results_have_slot": all(bool((((worker.get("job") or {}).get("result") or {}).get("worker_slot") or {})) for worker in workers),
        "expected_queue_observed": bool((result.get("queue_summary") or {}).get("expected_queue_observed")),
    }
    result["checks"] = checks
    result["ok"] = all(checks.values()) and not result.get("error")
    json_report, md_report = write_reports(result, json_path)
    result["json"] = str(json_report)
    result["md"] = str(md_report)
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Premium HLS worker sizing probe.")
    parser.add_argument("--runtime-root", default="/tmp/hackme_web_hls_premium_sizing_probe")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--fixture-size", default="640x360")
    parser.add_argument("--fixture-rate", type=int, default=24)
    parser.add_argument("--video-bitrate", default="")
    parser.add_argument("--source-video", default="")
    parser.add_argument("--source-trim-seconds", type=float, default=0.0)
    parser.add_argument("--hold-seconds", type=float, default=0.0)
    parser.add_argument("--launch-gap", type=float, default=0.0)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--worker-timeout", type=float, default=180.0)
    parser.add_argument("--hls-quality-heights", default="480")
    parser.add_argument("--hls-original-variant-mode", default="")
    parser.add_argument("--hls-profile", default="")
    parser.add_argument("--hls-audio-bitrate", default="")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--ffprobe-bin", default="ffprobe")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = run_probe(args)
    print(json.dumps({"ok": result.get("ok"), "json": result.get("json"), "md": result.get("md")}, ensure_ascii=False), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
