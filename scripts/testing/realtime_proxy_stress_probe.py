#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.media import streaming as media_streaming  # noqa: E402


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole_seconds = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        whole_seconds += 1
        millis -= 1000
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def generate_multiaudio_fixture(
    target: Path,
    *,
    ffmpeg_bin: str,
    duration: float,
    size: str = "320x180",
    rate: int = 24,
    video_bitrate: str = "",
) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    subtitle = target.with_suffix(".srt")
    subtitle_end = max(1.0, float(duration) - 0.2)
    subtitle.write_text(
        f"1\n00:00:00,200 --> {srt_timestamp(subtitle_end)}\nrealtime proxy stress subtitle\n",
        encoding="utf-8",
    )
    bitrate_mode = str(video_bitrate or "").strip().lower()
    source_filter = "testsrc2" if bitrate_mode else "testsrc"
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"{source_filter}=duration={float(duration):.3f}:size={size}:rate={int(rate)}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={float(duration):.3f}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=880:duration={float(duration):.3f}",
        "-i",
        str(subtitle),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map",
        "2:a:0",
        "-map",
        "3:s:0",
        "-metadata:s:a:0",
        "language=jpn",
        "-metadata:s:a:0",
        "title=Japanese",
        "-metadata:s:a:1",
        "language=eng",
        "-metadata:s:a:1",
        "title=English",
        "-metadata:s:s:0",
        "language=zh",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
    ]
    if bitrate_mode == "lossless":
        cmd.extend([
            "-crf",
            "0",
        ])
    elif video_bitrate:
        cmd.extend([
            "-b:v",
            str(video_bitrate),
            "-minrate",
            str(video_bitrate),
            "-maxrate",
            str(video_bitrate),
            "-bufsize",
            str(video_bitrate),
            "-x264-params",
            "nal-hrd=cbr:filler=1",
        ])
    cmd.extend([
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-c:s",
        "srt",
        str(target),
    ])
    started = time.monotonic()
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=max(30, int(duration) + 20))
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "duration_seconds": float(duration),
        "size": size,
        "rate": int(rate),
        "video_bitrate": str(video_bitrate or ""),
        "generate_ms": round((time.monotonic() - started) * 1000, 3),
    }


def compact_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
    metrics = dict(metrics or {})
    allowed = [
        "pid",
        "runtime_active_at_start",
        "runtime_local_active_at_start",
        "runtime_limit",
        "runtime_scope",
        "runtime_slot_index",
        "chunk_size",
        "bytes_sent",
        "chunks_sent",
        "first_chunk_latency_ms",
        "duration_ms",
        "rss_peak_bytes",
        "cpu_time_seconds",
        "resource_samples",
        "returncode",
        "closed_by_client",
        "timed_out",
        "terminated",
        "killed",
        "finished",
    ]
    return {key: metrics.get(key) for key in allowed if key in metrics}


def write_reports(result: dict[str, Any], *, json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    first = result.get("first_stream") or {}
    reopen = result.get("reopen_stream") or {}
    lines = [
        "# Realtime Proxy Stress Probe",
        "",
        f"- OK: `{result.get('ok')}`",
        f"- Runtime root: `{result.get('runtime_root')}`",
        f"- Fixture: `{(result.get('fixture') or {}).get('path')}`",
        f"- Max concurrent: `{result.get('max_concurrent')}`",
        "",
        "## Checks",
        "",
        f"- Busy limit: `{(result.get('checks') or {}).get('busy_limit')}`",
        f"- Disconnect release: `{(result.get('checks') or {}).get('disconnect_release')}`",
        f"- Reopen after disconnect: `{(result.get('checks') or {}).get('reopen_after_disconnect')}`",
        f"- Metrics present: `{(result.get('checks') or {}).get('metrics_present')}`",
        f"- Host-global slot scope: `{(result.get('checks') or {}).get('host_global_slot_scope')}`",
        "",
        "## First Stream",
        "",
        f"- First chunk bytes: `{first.get('first_chunk_bytes')}`",
        f"- Busy error: `{first.get('busy_error')}`",
        f"- Release ms: `{first.get('release_ms')}`",
        f"- Metrics: `{compact_metrics(first.get('metrics'))}`",
        "",
        "## Reopen Stream",
        "",
        f"- Output bytes: `{reopen.get('output_bytes')}`",
        f"- Selected audio: `{reopen.get('selected_audio')}`",
        f"- Metrics: `{compact_metrics(reopen.get('metrics'))}`",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    runtime_root = Path(args.runtime_root).resolve()
    report_dir = runtime_root / "reports" / "qa"
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = Path(args.json_out).resolve() if args.json_out else report_dir / "realtime_proxy_stress_probe.json"
    md_path = json_path.with_suffix(".md")

    ffmpeg_bin = shutil.which(args.ffmpeg_bin) if not Path(args.ffmpeg_bin).exists() else args.ffmpeg_bin
    ffprobe_bin = shutil.which(args.ffprobe_bin) if not Path(args.ffprobe_bin).exists() else args.ffprobe_bin
    if not ffmpeg_bin or not ffprobe_bin:
        result = {
            "ok": False,
            "error": "ffmpeg_or_ffprobe_missing",
            "ffmpeg_bin": ffmpeg_bin,
            "ffprobe_bin": ffprobe_bin,
            "runtime_root": str(runtime_root),
            "created_at": utc_now(),
        }
        write_reports(result, json_path=json_path, md_path=md_path)
        return result

    print(f"[phase] generating fixture under {runtime_root}", flush=True)
    fixture_path = report_dir / "realtime_proxy_stress_fixture.mkv"
    fixture = generate_multiaudio_fixture(fixture_path, ffmpeg_bin=ffmpeg_bin, duration=args.duration)

    previous_limit = os.environ.get("HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT")
    previous_timeout = os.environ.get("HACKME_MEDIA_REALTIME_PROXY_TIMEOUT_SECONDS")
    previous_scope = os.environ.get("HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE")
    previous_lock_dir = os.environ.get("HACKME_MEDIA_REALTIME_PROXY_LOCK_DIR")
    os.environ["HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT"] = str(int(args.max_concurrent))
    os.environ["HACKME_MEDIA_REALTIME_PROXY_TIMEOUT_SECONDS"] = str(int(args.timeout_seconds))
    os.environ["HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE"] = "global"
    os.environ["HACKME_MEDIA_REALTIME_PROXY_LOCK_DIR"] = str(runtime_root / "locks" / "realtime_proxy")
    media_streaming._REALTIME_PROXY_ACTIVE = 0
    media_streaming._REALTIME_PROXY_HELD_SLOTS = set()

    first_info: dict[str, Any] | None = None
    first_chunks = None
    result: dict[str, Any] = {
        "ok": False,
        "created_at": utc_now(),
        "runtime_root": str(runtime_root),
        "max_concurrent": int(args.max_concurrent),
        "fixture": fixture,
        "ffmpeg_bin": ffmpeg_bin,
        "ffprobe_bin": ffprobe_bin,
        "checks": {},
        "first_stream": {},
        "reopen_stream": {},
    }
    try:
        print("[phase] opening first realtime proxy stream", flush=True)
        first_info = media_streaming.open_realtime_proxy_stream(
            fixture_path,
            audio_track="audio_01_jpn",
            ffmpeg_bin=ffmpeg_bin,
            ffprobe_bin=ffprobe_bin,
            chunk_size=int(args.chunk_size),
        )
        first_chunks = first_info["chunks"]
        active_after_first = media_streaming.realtime_proxy_runtime_status()["active"]

        print("[phase] verifying busy response at concurrency limit", flush=True)
        busy_error = ""
        second_info = None
        try:
            second_info = media_streaming.open_realtime_proxy_stream(
                fixture_path,
                audio_track="audio_02_eng",
                ffmpeg_bin=ffmpeg_bin,
                ffprobe_bin=ffprobe_bin,
                chunk_size=int(args.chunk_size),
            )
        except RuntimeError as exc:
            busy_error = str(exc)
        finally:
            if second_info is not None:
                try:
                    second_info["chunks"].close()
                except Exception:
                    pass

        print("[phase] reading first chunk then simulating client disconnect", flush=True)
        first_chunk = next(first_chunks)
        release_started = time.monotonic()
        first_chunks.close()
        first_chunks = None
        release_ms = round((time.monotonic() - release_started) * 1000, 3)
        active_after_close = media_streaming.realtime_proxy_runtime_status()["active"]

        result["first_stream"] = {
            "active_after_first_open": active_after_first,
            "first_chunk_bytes": len(first_chunk),
            "busy_error": busy_error,
            "release_ms": release_ms,
            "active_after_close": active_after_close,
            "metrics": compact_metrics(first_info.get("metrics")),
        }

        print("[phase] reopening after disconnect and draining selected English track", flush=True)
        reopen = media_streaming.open_realtime_proxy_stream(
            fixture_path,
            audio_track="audio_02_eng",
            ffmpeg_bin=ffmpeg_bin,
            ffprobe_bin=ffprobe_bin,
            chunk_size=32 * 1024,
        )
        output_bytes = 0
        for chunk in reopen["chunks"]:
            output_bytes += len(chunk)
        result["reopen_stream"] = {
            "output_bytes": output_bytes,
            "selected_audio": (reopen.get("audio_track") or {}).get("name"),
            "active_after_reopen": media_streaming.realtime_proxy_runtime_status()["active"],
            "metrics": compact_metrics(reopen.get("metrics")),
        }

        checks = {
            "busy_limit": busy_error.startswith("realtime_proxy_busy:"),
            "disconnect_release": active_after_close == 0 and bool((first_info.get("metrics") or {}).get("closed_by_client")),
            "reopen_after_disconnect": output_bytes > 1024 and result["reopen_stream"]["selected_audio"] == "audio_02_eng",
            "metrics_present": (first_info.get("metrics") or {}).get("first_chunk_latency_ms") is not None
            and (reopen.get("metrics") or {}).get("first_chunk_latency_ms") is not None,
            "host_global_slot_scope": (first_info.get("metrics") or {}).get("runtime_scope") == "global"
            and (reopen.get("metrics") or {}).get("runtime_scope") == "global",
        }
        result["checks"] = checks
        result["ok"] = all(checks.values())
    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__
    finally:
        if first_chunks is not None:
            try:
                first_chunks.close()
            except Exception:
                pass
        media_streaming._REALTIME_PROXY_ACTIVE = 0
        media_streaming._REALTIME_PROXY_HELD_SLOTS = set()
        if previous_limit is None:
            os.environ.pop("HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT", None)
        else:
            os.environ["HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT"] = previous_limit
        if previous_timeout is None:
            os.environ.pop("HACKME_MEDIA_REALTIME_PROXY_TIMEOUT_SECONDS", None)
        else:
            os.environ["HACKME_MEDIA_REALTIME_PROXY_TIMEOUT_SECONDS"] = previous_timeout
        if previous_scope is None:
            os.environ.pop("HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE", None)
        else:
            os.environ["HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE"] = previous_scope
        if previous_lock_dir is None:
            os.environ.pop("HACKME_MEDIA_REALTIME_PROXY_LOCK_DIR", None)
        else:
            os.environ["HACKME_MEDIA_REALTIME_PROXY_LOCK_DIR"] = previous_lock_dir
        runtime_status = media_streaming.realtime_proxy_runtime_status()
        result["cleanup"] = {
            "active_slots_released": int(runtime_status.get("active") or 0) == 0,
            "held_slots_released": not bool(media_streaming._REALTIME_PROXY_HELD_SLOTS),
            "environment_restored": all((
                os.environ.get("HACKME_MEDIA_REALTIME_PROXY_MAX_CONCURRENT") == previous_limit,
                os.environ.get("HACKME_MEDIA_REALTIME_PROXY_TIMEOUT_SECONDS") == previous_timeout,
                os.environ.get("HACKME_MEDIA_REALTIME_PROXY_LIMIT_SCOPE") == previous_scope,
                os.environ.get("HACKME_MEDIA_REALTIME_PROXY_LOCK_DIR") == previous_lock_dir,
            )),
        }

    write_reports(result, json_path=json_path, md_path=md_path)
    result["json"] = str(json_path)
    result["md"] = str(md_path)
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stress Standard realtime proxy slot, disconnect, and metrics behavior.")
    parser.add_argument("--runtime-root", default="/tmp/hackme_web_realtime_proxy_stress")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--chunk-size", type=int, default=1024)
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--ffprobe-bin", default="ffprobe")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = run_probe(args)
    print(json.dumps({"ok": result.get("ok"), "json": result.get("json"), "md": result.get("md")}, ensure_ascii=False), flush=True)
    if result.get("ok"):
        print("[pass] realtime proxy stress probe", flush=True)
        return 0
    print("[fail] realtime proxy stress probe", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
